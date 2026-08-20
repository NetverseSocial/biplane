# 010 — Semantic event identity, and the alias lifecycle

**Status:** accepted · **Implemented at:** 0087e9b (holder/alias lifecycle shipped and load-bearing) · **Accepted by:** Morrow, 2026-08-14 · **Decided by:** Aria, from 7of9's implementation and
Morrow's review findings · **Date:** 2026-08-12

> ## ⚠️ Why this record exists
>
> The semantic-key work reached **six review rounds**, each surfacing a new class
> of defect rather than closing the previous set. That is the signature of an
> unclear contract, not of careless work — every finding was real, and competent
> reviewers kept finding them because there was nothing settled to converge on.
>
> **One class recurs across all of them: migration↔runtime parity on identity and
> aliasing.** RC 3335 asked which row holds authority. RC 3343 asked what the key
> boundary accepts. RC 3348 asked how a non-holder is marked. Those are the same
> contract, re-derived by each reviewer, found unwritten in a different place each
> time. This document writes it down once so it is implemented once.
>
> 7of9's in-flight branch is the **reference implementation**, not a review
> request. It is code-complete against these rules; once this record is ratified
> she reconciles against it and that is the single review.

## The problem

A forge can deliver the same real-world event more than once, and a poller can
observe an event a webhook already delivered. Provider delivery IDs do not
identify the event — a Forgejo replay mints a new UUID — so deduplicating on
them executes the same event twice. Deduplicating on *content* instead requires
a key derived from what is actually immutable about the event.

## The decision

### 1. The identity tuple, per event class

| Event class | Tuple |
|---|---|
| push | `("push", provider_instance, stable_repo_id, ref, before, after)` |
| merged pull request | `("merged_pr", provider_instance, stable_repo_id, pr_number, merge_sha)` |
| review outcome (ADR 009) | `("review", provider_instance, stable_repo_id, pr_number, review_id)` |

**The verb prefix is part of the tuple, not a note in §2** (Vex, RC 3427 — a
blocker, and the sharpest finding this record has had). §2 lists three reasons
injectivity holds and says all three must; the prefix appeared only there, as a
*reason*, never in §1, which is where an implementer takes the tuple from.

It is load-bearing for a pair that would otherwise be one slot apart:

```
merged_pr  (provider_instance, stable_repo_id, pr_number, merge_sha)
review     (provider_instance, stable_repo_id, pr_number, review_id)
```

Same arity, same first three slots, differing only in slot 4 — and §2's type
argument governs **a slot**, not two roles sharing one slot across classes. The
constructor renders a separator-joined plaintext string in which `str(99)` and
`"99"` are the same bytes. Nothing collides today only because a merge SHA is
40 hex characters, which is an accident of the data rather than a property of
the key. The record required three things and normatively specified two — §2's
own complaint about enumerations, turned back on §1.

`stable_repo_id` is `repository.id` on GitHub and Forgejo, `project.id` on
GitLab. **Display paths are never identity** — they survive rename and reuse.

**The namespace is the PROVIDER INSTANCE, not the provider family** (Morrow, RC
3411). An earlier revision keyed on `provider` — "forgejo", "github" — which is
a product name rather than an authority: two Forgejo instances both numbering
repository 42 would collide, and the collision would be invisible.

M2's scope guard already keys authority by `(provider instance, stable
repository id)`. **This key binds to that existing authority rather than
inventing a parallel namespace** — invariant 8, which this record's author
specified in the design doc and then failed to apply while writing a new
identity one document later.

**Where the instance id comes from, which is the whole of the rule** (7of9,
reading her reference against this record and finding it diverges here):

- it is a **configuration-supplied instance id, threaded from the endpoint** —
  M3's "selection precedes parsing", and invariant 7: **the input never names
  its own namespace**;
- it is **not** `forge.name`. A product family — "forgejo", "github" — is what
  the reference implements today, and it is exactly the collision above;
- it is **never read from the payload**, which is the sender's word for who the
  sender is.

**Instance ids have a lifecycle, and a configured value is not one** (Morrow,
RC 3423 — his cross-instance requirement, which the previous revision satisfied
only halfway by saying where the value comes from):

- **an enabled instance id is UNIQUE and STABLE once delivery rows exist for
  it.** Renaming one mints a new semantic identity for events already recorded
  under the old name; reusing one across two endpoints collides genuinely
  distinct events into a single key. Both are silent.
- **changing an instance id therefore requires an explicit namespace
  migration**, not a config edit. That is the whole difference between a
  configured value and an identity.

**The migration assigns the configured instance id to every historical row.**
Only one instance has ever existed, so there is no ambiguity to resolve and no
drift to reconcile — and because no instance-scoped hashes existed before, the
backfilled hashes are instance-scoped from the start rather than migrated into
it later.

This is currently **latent rather than live** — one Forgejo instance means
nothing collides today. It is written here because the day a second instance
appears, the collision is silent, and nothing in the running system would
report it.

### 1a. Incomplete and malformed are different, and must not be conflated

An earlier revision said any component "missing, empty, or of the wrong type"
yields no key. That merged two cases with opposite handling (Morrow, RC 3411):

**The discriminator is absent-versus-corrupt, not which field it is** (7of9,
mapping her reference onto this record — the principle her `_str_field` and
`_int_field` already implement):

> A component that is **absent or unprovable** — `None`, empty — is
> **INCOMPLETE**. A component that is **present but corrupt** — wrong type, or
> containing the reserved separator — is **MALFORMED**.

Stating it as a property rather than as two lists is what stops the next
component from being sorted into the wrong bucket by whoever adds it.

**`provider_instance` is exempt from this rule, and the exemption is the point**
(Morrow, RC 3432). It is **server configuration**, not a payload field — so an
empty or missing one is a **deployment defect**, not a forge that omitted
something.

> **An absent or empty configured `provider_instance` fails at CONFIG LOAD,
> before any parsing.** It never reaches the incomplete path and never yields a
> silently-unkeyed row.

Treating it as incomplete would degrade a misconfigured deployment into one that
quietly stops keying anything: every event unkeyed, nothing coalescing, nothing
reporting, and an operator looking at a working system. §1a governs what a
**sender** failed to supply; this governs what **we** failed to configure, and
the two must not share a failure mode.

**Incomplete ⇒ unkeyed, and the event still processes.** A forge that
legitimately does not supply a component produces a well-formed event we cannot
name semantically. The row stays **unkeyed**, falls back to delivery-id dedup,
and processes normally. This is not an error and is not reported as one.

**Malformed ⇒ 400, and nothing is stored.** A component present but of the wrong
type, or containing the reserved `0x1f` separator, is a **malformed delivery**:
the typed boundary rejects it with 400 and zero writes, exactly as the existing
shape validation does. It is never silently downgraded to unkeyed.

The distinction is load-bearing in both directions. Treating malformed as
merely-unkeyed admits a signed-but-wrong payload to the store as an ordinary
unnamed event; treating incomplete as malformed rejects legitimate traffic from
any forge that omits a field we happen to want.

Neither case ever pads, defaults, or coerces a component into a key.

### 2. Injectivity is a property, not a type list

Distinct real events cannot collide, for three reasons that must all hold:

- a **verb prefix** separates event classes, so a push can never key-collide with
  a merge or a review;
- the **`0x1f` separator cannot appear in any component**, so component
  boundaries are unambiguous and `("a", "b|c")` cannot render as `("a|b", "c")`;
- **each field's type is fixed by its role**, exhaustively (Rowan, RC 3419 — an
  earlier revision listed the roles loosely and omitted one outright):

  | Component | Type |
  |---|---|
  | `provider_instance` | **string**, the configured instance id (§1) — never a product family, never from the payload |
  | `stable_repo_id` | strict positive integer |
  | `pr_number` | strict positive integer |
  | `review_id` | **strict positive integer** |
  | `ref`, `before`, `after`, `merge_sha` | strings |

  So `"42"` and `42` can never both be produced for one slot.

Stating it this way matters: earlier revisions enumerated accepted types, and an
enumeration is satisfiable without being injective. The property is the
requirement; the types are one of three ways it is met.

### 3. Delivery IDs are provenance. Every observation is stored.

**Every observation that passes the typed boundary — webhook or poll — is
durably inserted as its own row, before any cursor moves.** Two deliveries
observing one event leave two rows.

**"Every observation" means every ACCEPTED one** (Vex, RC 3427). A malformed
delivery is rejected at the boundary with 400 and **zero writes** per §1a, and
never becomes an observation at all. Unqualified, this heading contradicted
§1a and acceptance test 4b two sections apart.

**Coalescing applies to EXECUTION, not to storage.** A design that rolls back the
second insertion produces one outcome and destroys audit provenance, which the
M3 contract requires. One event executes once; every observation of it remains
queryable.

### 4. The alias lifecycle

- The **holder** owns the unique semantic hash and is the row that executes.
- A **non-holder** carries `result.coalesced_to = <holder's delivery_id>`,
  retains its plaintext key, and has a NULL hash. **It never executes.**
- The reconciler resolves a non-holder against the holder's **current** state,
  not a snapshot taken when the alias was created, and finalizes it to the
  holder's result once the holder completes.

**The state table — what each actor does, per state** (Morrow, RC 3411:
"identical shape" only has meaning if the shape has operational consequences
that every actor agrees on):

| Row state | `post()` | retry / processor | reconciler | migration produces it |
|---|---|---|---|---|
| **unkeyed** (no semantic key) | store, dedup on delivery id | executes | ignores | yes — components unprovable |
| **holder, unfinished** | stores, owns the hash | executes | leaves alone | yes — no processed row in group |
| **holder, processed** | returns its result as duplicate | no-op | leaves alone | yes — the processed row wins |
| **alias, holder unfinished** | stored, never executes | **never executes** | leaves it **pending**; does not finalize | yes — non-holder in a group with no processed row |
| **alias, holder processed** | returns the holder's result | **never executes** | finalizes to `processed` with the holder's **current** outcome | yes — non-holder in a group that has one |
| **alias, holder missing** | n/a | never executes | **pending / retryable, never terminal** | must not occur; if it does, stays retryable |

**The stored shape, field by field, because "identical shape" is only
enforceable if the fields are named** (Morrow, RC 3423 — the previous table said
what each actor *did* without saying what was *written*, so `post()` and the
migration could still disagree while both matching the prose):

| Alias state | `status` | `semantic_key_hash` | plaintext key | `result` |
|---|---|---|---|---|
| holder unfinished | `pending` | **NULL** | **retained** | contains `coalesced_to` → holder; **never independently claimable** |
| holder processed | `processed` | **NULL** | **retained** | **retains `coalesced_to`** *and* carries the holder's current outcome |
| holder missing | `pending` | **NULL** | **retained** | `coalesced_to` retained; **no fabricated terminal outcome** |

**`post()`, the retry/processor path, the reconciler and the migration must each
produce and recognise exactly these fields.** That is the parity rule made
checkable: an actor that writes a row matching this table is interoperable by
construction, and one that omits `coalesced_to` has written an unkeyed row that
executes, not an alias.

Two rules fall out rather than needing separate statement: an alias is defined
by `coalesced_to` being **set**, not by its hash being NULL — a NULL hash alone
is an unkeyed row, which executes — and no actor may finalize an alias from a
snapshot, because the holder's state can advance after the alias is created.

**The load-bearing rule, and the one every round rediscovered:**

> **`post()` and the migration backfill MUST write the IDENTICAL alias shape.**

Both sides cite this rule. A non-holder created by the migration and a
non-holder created at runtime must be indistinguishable to `_is_alias()` and to
the reconciler. Every defect in this class came from one side writing a shape the
other did not recognise — a migration-created row with a NULL hash but no
`coalesced_to` is not an alias, so the reconciler claims it and the event
executes twice.

### 5. What the migration must produce

Within each hash group:

- **one holder** — the **processed** row (earliest processed; if none is
  processed, the earliest row) — owning the unique hash;
- **every non-holder** — plaintext key retained, hash NULL, `coalesced_to`
  pointing at the holder.

Precedence is *processed before unfinished, then chronology*. Choosing the
earliest row unconditionally can hand authority to a pending row while a later
processed row holds the real outcome, after which duplicates resolve to an
unfinished holder and the authoritative result is lost.

The migration **freezes its own copy of the key rules inline and never imports
runtime code**, so a later runtime change cannot retroactively alter what a
historical row hashed to. The parity rule above is what keeps the two copies
honest; a test must pin runtime-equals-migration in the violating direction,
including historical rows whose components were object-valued and must therefore
remain unkeyed.

**Uniqueness is partial:** `UNIQUE(hash) WHERE hash IS NOT NULL`. Non-holders and
unkeyable rows both carry NULL and must not collide with each other.

### 6. Delivery-id binding is resolved BEFORE semantic coalescing

A submitted delivery id is bound to the content it was first stored with. That
binding is checked **first**, before any semantic match is consulted.

> Store processed `D1/E1` and processed `D2/E2`, then submit delivery id `D1`
> carrying the correctly signed body for `E2`. A semantic match finds `D2` and
> would return duplicate 200. The standing contract requires **409**, because
> `D1` is bound to `E1`.

**The 409 holds in the concurrent-insert race too**, not only on the settled
path. In Django this is where implementations go wrong: an `IntegrityError`
caught **without a savepoint** poisons the enclosing `transaction.atomic()`
block, so the recovery path that produces the 409 must itself be transactionally
sound rather than merely present.

**The savepoint must be EXPLICIT, and the recovery path must never use a bare
`create()`** (7of9, reading the reference implementation against this record).
The reference is transactionally sound today only because Django's
`get_or_create` wraps its INSERT in an *internal* savepoint, and the endpoint
happens to run outside any outer atomic block. Both are implementation details:
refactoring that call to a bare `.create()`, or enabling `ATOMIC_REQUESTS`
later, silently reintroduces the poisoned-transaction bug this section warns
about, and nothing would catch it.

Wrap the binding recovery in an explicit `with transaction.atomic():`. The rule
is stated here so it outlives whichever ORM call happens to occupy that line.

**A missing holder stays retryable.** An alias whose holder cannot be resolved
is a transient state, never a terminal one — it must not finalize to an outcome
it has not observed.

### 7. Runtime and migration constructors are byte-for-byte equivalent

Stronger than "the same rules": the frozen migration constructor and the runtime
constructor must produce **identical bytes** for identical inputs, and a test
pins that in the **violating** direction — historical rows whose components were
object-valued must remain unkeyed under both.

The migration is **self-contained** (it imports no runtime code), **runs before
its own constraint is applied**, and **preserves every delivery observation**.
Applying the partial unique index before the backfill has assigned holders would
fail against exactly the duplicate data the backfill exists to resolve.

**Components never escape or substitute an empty string or the reserved `0x1f`
separator — and the two land in different places, per §1a.** (Rowan, RC 3419
found this paragraph saying both "yield no key at all", which contradicted §1a
one section above and would have made a separator-bearing payload an ordinary
unnamed event. Vex, RC 3427 then found the *lead sentence* still saying
components "reject the empty string" two lines above a bullet saying empty
processes — **the same contradiction surviving in a third location, in the
paragraph that fixed it.**)

- **empty / absent component ⇒ incomplete ⇒ unkeyed row that still processes;**
- **component containing the reserved separator ⇒ malformed ⇒ 400, zero
  writes.**

A separator in a component is not a component we cannot name; it is a component
that would forge a boundary, and it is rejected at the door.

### 8. These are acceptance tests, not prose

The following must exist as executable tests. A reviewer should be able to point
at each one:

1. delivery-id collision with different content returns **409**, on the settled
   path **and** under concurrent insert;
2. webhook and poll observations of one immutable event converge to **one
   execution**, while **both** transport delivery identities remain queryable —
   **one named test asserting all three clauses**, not three tests a reviewer
   has to assemble (7of9);
3. a migration-created alias, driven through the **real processor**, resolves to
   the holder's result and never re-executes;
4. runtime and frozen-migration constructors agree, including on inputs that
   must produce no key;
4a. **incomplete ⇒ unkeyed AND PROCESSED** — a well-formed event missing a
   component is stored with no semantic key, deduplicates on delivery id, and
   **executes normally**. Asserting the row is unkeyed is not enough; the test
   must assert it processed (Rowan, RC 3419). The reference's nearest test is
   `test_missing_and_empty_components_raise_and_yield_no_key`, whose **name
   asserts the behaviour §1a overturned** and which covers representation
   rather than handling — it must be renamed and extended to the processed
   assertion, or a second test added. §8's own standard is that a reviewer can
   point at each direction, and a name that says "raise and yield no key"
   points at the wrong one (Vex, RC 3427);
4b. **malformed ⇒ 400 AND ZERO WRITES** — a wrong-typed component, and
   separately a component containing the reserved separator, each return 400
   with **no row stored**. Asserting the status code is not enough; the test
   must assert nothing was written. Reference:
   `test_separator_in_component_is_clean_400_not_500` and
   `test_signed_ref_object_is_400_not_a_key`;
4c. **the earlier-pending / later-processed migration group, driven through the
   REAL reconciler** — a group whose earliest row is pending and whose later row
   is processed must, after migration, resolve to the processed outcome and
   never re-execute the pending one (Morrow, RC 3411 and 3423);
4d. **two instance ids do not collide, and one does not drift** — the same
   `stable_repo_id` and otherwise identical event under two configured instance
   ids produces **distinct bytes and distinct hashes**; the same instance
   produces the **same key after restart or config reload** (Morrow, RC 3423);
4e. **a missing or empty configured `provider_instance` REFUSES AT CONFIG LOAD,
   with zero delivery writes** — the executable half of §1a's exemption (Rowan,
   RC 3442). Assert both directions: the refusal happens **before parsing**, and
   **no delivery row exists** afterwards. A test that only asserts an error is
   raised would pass against an implementation that raises *after* storing, or
   one that degrades to unkeyed — which is the failure this exemption exists to
   prevent, and the one that looks healthy from the outside;
5. the partial unique constraint, and the savepoint behaviour under concurrent
   insert **exercised inside an outer `transaction.atomic()`** — asserting the
   outer transaction **survives** (still queryable) *and* the response is still
   409.

**Test 5's wording is deliberate and was wrong in the first revision.** As
originally stated it could not fail: the endpoint runs outside any atomic block,
so there was no enclosing transaction to poison, and a green result would have
proved nothing (7of9). That is exactly the measuring-device failure this record
exists to prevent, appearing in the record's own acceptance criteria — which is
worth leaving visible rather than quietly correcting.

Prose in this document is not evidence that any of them holds.

## Consequences

- `review_key` and its alias lifecycle are consumed by the review path, which
  identifies a review observation exactly as this contract specifies.

  > **SUPERSEDED CONSEQUENCE (BIP-67, 2026-08-16).** This bullet used to say
  > BIP-50 — ADR 009's review-outcome TRANSITION — was unblocked the moment this
  > contract ratified. **That transition was removed outright**, not deferred: a
  > changes-requested review is neither a merge nor an approval, so no fact can
  > authorise it, and every board/state write is refused. The IDENTITY contract
  > below is unaffected and still governs review observations; what is gone is
  > the write it was going to unblock. Recorded here because this record is
  > accepted and had no local warning around the stale consequence.
- Reviewers stop re-deriving this per round. A finding that contradicts this
  record is a finding against **the record**, and amends it.

## What this decision does not do

- It does not add a store. The identity lives in the delivery row it describes.
- It does not make delivery IDs identity, or make semantic keys provenance.
- It does not define poller cursor semantics beyond requiring that an
  observation is durable before a cursor advances.
