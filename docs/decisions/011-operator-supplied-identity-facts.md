# 011 — Operator-supplied identity and trust facts the system cannot self-verify

**Status:** accepted, partially implemented · **Decided by:** Aria, from 7of9's
identity-and-seeding lane and the review findings on PR #82 and BIP-42 ·
**Date:** 2026-08-14

> **Decision vs implementation.** The rule and its three instances are *decided*.
> Implementation is open for Instance 1 (cursor-seed derivation) and Instance 3
> (the forge↔board mapping); Instance 2's mechanism landed as the BIP-42 first hop
> (PR #89). The write-authority spec cites this record as its load-bearing identity
> contract, so it is accepted here, not left as a proposal.

> ## Why this record exists
>
> Some facts the running system depends on for correctness are supplied from
> outside the data it observes and cannot be verified from what it does see. Two
> instances (the poll cursor `repo_stable_id` seed — PR #82 review 3700 — and the
> BIP-42 first-hop deploy seed) failed the same way for the same reason, and a
> third (the forge↔board reviewer join) is the same class. This writes the
> contract once. It extends ADR 010 §1, whose sibling component `provider_instance`
> already has this contract, and fills the half §404 scopes out.

## The problem

Some facts the running system depends on for correctness are **supplied from
outside the data it observes** — by configuration, or by an operator — and are
**structurally unverifiable from what the system does see**. The system can
enforce *internal consistency* around such a fact. Internal consistency is not
verification of the fact. When the supplied value is **well-formed but wrong**,
the failure is silent: no exception, no log, a correctness violation with no
signal anywhere.

This is one class with (at least) two instances in Biplane today, and they fail
the same way for the same reason. Writing the contract once is the point — the
alternative is each subsystem re-deriving it, which is the spaghetti this halt
exists to stop.

## The rule

> **An identity or trust fact supplied by configuration or an operator, that the
> system cannot verify from the data it observes, MUST be cross-checked against
> an authoritative signal the supplier does not control — at the moment it
> enters the system — or its acceptance MUST fail closed.**

"Fail closed" means the fact is refused and nothing is created, never that it
falls back to the unverified value. The authoritative signal must be one the
supplier cannot forge into agreement — otherwise the check confirms only that
the operator was consistent with themselves, which is exactly the trap.

## Instance 1 — the poll cursor's `repo_stable_id` (the bridge seed)

ADR 010 §1 makes `stable_repo_id` part of every event's identity tuple and says
it is `repository.id` on GitHub/Forgejo. **Where the poll cursor's
`repo_stable_id` comes from is unspecified** — deliberately: ADR 010 §404 scopes
poller cursor semantics out. So the seed lives in no contract.

What the poller enforces (PR #82, `poller.poll_repo_page`): every observation in
a page must satisfy `is_identity_int(obs.repo_id)` **and** `obs.repo_id ==
cursor.repo_stable_id`, or the whole page is refused. This is a real guard — but
read what it compares. `observations_from(events, repo_full_name, repo_id, …)` is
called with `repo_id = cursor.repo_stable_id`, so every observation carries the
cursor's own value by construction. **The guard compares the seed to itself.** It
protects against a mixed or foreign page handed to the wrong cursor; it can never
detect a cursor seeded with the *wrong* numeric id.

The failure when the seed is wrong-but-well-formed:

- The webhook path reads the **real** `repository.id` from the delivered payload
  (`forge.repo_stable_id`) and builds its semantic key from that.
- The poll path builds its key from `cursor.repo_stable_id`.
- If those two integers differ, the two transports compute **different** semantic
  keys for the same real event. The unique-hash constraint never collapses them.
  You get **two holders for one event**, both execute, and the alias lifecycle —
  whose entire job is to make the second observation a non-holder — never
  engages, because there is no shared key to make it an alias of.
- Nothing reports this. No exception, no log. The board is silently wrong.

This is the same silent-and-invisible property ADR 010 §1 already calls out for a
second Forgejo instance colliding on `provider_instance` — one latent seam, live
the day the seed is wrong.

### The asymmetry that is the argument

`provider_instance` and `repo_stable_id` are **siblings in the same identity
tuple**, one slot apart. ADR 010 §1 gives `provider_instance` a full contract:
config-supplied and threaded from the endpoint, **never** read from the payload;
unique and stable once delivery rows exist; changing it requires an explicit
**namespace migration**, not a config edit; an absent or empty one fails at
**config load**, because it is a deployment defect, not a forge that omitted a
field. `repo_stable_id` gets **one line** — "it is `repository.id`; display paths
are never identity."

The ADR is not inventing rigor. It is asking **why one identity component in the
tuple got the seeding contract and its partner, which fails identically, did
not.** That is a much harder thing for a reviewer to wave off than a new demand.

### What the contract requires for Instance 1

- A poll cursor's `repo_stable_id` **MUST be derived from the provider's
  authoritative API at seed time** — `GET <provider>/repos/<owner>/<name>` →
  `.id` — never hand-entered and never read from a payload (the payload is the
  sender's word for who the sender is; ADR 010 §1, invariant 7).
- If the authoritative id cannot be fetched, **seeding fails closed**: no cursor
  is created. It does not fall back to an operator-typed value.
- `repo_stable_id` is **stable once delivery rows exist**, like `provider_instance`
  — but the resemblance stops there, and the difference must be stated rather than
  left as an unstated "probably not" that a future editor reads as "yes" (Aria,
  convergence 2026-08-14). **`provider_instance`'s namespace-migration machinery
  does NOT extend to `repo_stable_id`, because the two fail differently.** A
  provider instance can be legitimately *renamed while remaining the same
  instance* — that is what the migration machinery carries rows across. A
  repository id cannot be renamed; ADR 010 §1 is explicit that display paths
  survive rename and reuse *precisely because the id does not change*. So the only
  way `repo_stable_id` changes for a repository we are already polling is that it
  is **not the same repository** — deleted and recreated (a new identity, hence a
  new cursor), or the seed was wrong all along (the defect this ADR exists to
  prevent). Neither is a migration. **Migrating a wrong seed forward is exactly
  what must not be possible**, so there is no namespace-migration path for a
  `repo_stable_id`: a changed id is a new cursor or a refusal, never a carry-over.
- The existing `is_identity_int` / page-consistency guard **stays**, but is
  re-documented for what it is: **page integrity, not seed verification.** Naming
  it correctly is what stops the next reader from mistaking it for the check that
  is actually absent.
- **Reconciliation (periodic re-fetch of `.id`) is explicitly out of this ADR's
  scope** (Aria, convergence 2026-08-14). It can DETECT a mismatch but cannot
  repair one: by the time it fires, two holders already exist and the wrong one
  may have executed — so it would add a *second answer* to "what is the true id"
  with no action attached to the disagreement, which is the very shape that
  produced this defect. Seed time is the only moment the supplier is present and
  the only moment the fact can be refused. **Fail-closed at seed time is the
  contract; reconciliation is monitoring, and monitoring belongs to whoever owns
  the running system, not to this ADR.**

### Downstream seam — the seeded id is consumed by the directive and permission layers

The mis-seed does not stop at double-execution. Morrow's directive lane carries
`provider_instance` and `stable_repo_id` in the event envelope as **the immutable
coordinates the permission owner re-reads authority from**, and Sable's
write-boundary lane makes that authority re-read the gate on a board write. For a
**poll-sourced** event those coordinates *are* the cursor's seed. So a mis-seeded
`repo_stable_id` is not only Instance 1's silent double-holder — it is **carried
up as a directive coordinate, and a wrong repository's authority is re-read
against it.** ADR 011's seed-time fail-closed is therefore a **precondition for
the soundness of those layers**, not merely for the dedup constraint.

The exposure is specifically the **poll path**: the webhook path reads the real
`repository.id` from the delivered payload, so its envelope coordinate is
authoritative by construction; only the cursor seed can be wrong-but-well-formed.
Named here so the dependency is not an unstated assumption spanning three lanes —
which is the exact shape this re-evaluation exists to remove. (Morrow/Sable lanes
own the directive and permission contracts; this ADR owns only that the coordinate
they consume was correctly seeded or the cursor never existed.)

**Severity, verified at the fetch layer (2026-08-14): a bad poll seed is a
cross-project WRITE exposure, not only a double-execution.** The two paths differ
in whether the repository identity is *corroborated*. The webhook path is:
`forge.repo_stable_id` reads the delivered payload, and the scoped pull
independently re-reads the forge's own `base.repo.id`, **refusing on mismatch**
(`forgejo_bridge.py` — "pull request authority does not match the scoped
repository"). The poll path is not: `poll_github_task` calls
`observations_from(events, cursor.repo_full_name, cursor.repo_stable_id)`, that
value is stamped onto every observation, and the code **never reads the per-event
repo id the events feed itself carries**. So the stamped `repository.id` *is* the
cursor seed, and the page-consistency guard compares it to that same seed —
corroboration by nothing.

> **Provenance note — the sharpest lesson in this section.** This BIP-38 claim was
> first published from **two unverified halves presenting as corroboration**: one
> lane asserted the poll `repo_id` was seed-derived without tracing it to the fetch
> layer, the other adopted that assertion and added the severity, and from each
> side it read as confirmed — neither could see that no one had opened
> `poll_github_task`. It was traced afterward and it held, but it held by luck, not
> by method; and its mirror sat right beside it — a *true* provenance claim
> retracted as unverified, and a *false* "there is no second source" kept because
> it wore the costume of a structural observation about one function rather than an
> absence claim about the whole path. The narrow instance is what this ADR keeps:
> every seed claim in it now rests on a **named trace** (`poll_github_task.py:141`
> for provenance; `event.get("repo")` occurring zero times for the absent read),
> not on concurrence. The **general rule it implies — agreement between two agents
> is not evidence unless at least one of them traced it** — is a fleet failure mode
> a single reviewer does not have; it is stated as **Corollary 1b — agreement is
> not corroboration**, beside the tree-authority rule in the convening lane, not
> restated here (Sable's sentence, this exchange; Aria
> re-traced both legs herself before placing it — the only defensible way to write
> up a rule about adopting reports).

Because the seed is the *only* thing labelling a genuinely-fetched event with a
repository, it is the **sole guarantor of scope authority for every write the poll
path produces**, not merely the dedup key: a wrong-but-well-formed seed labels
repo X's real events as repo Y, and scope resolution then moves a ticket in
project Y on the authority of activity in repo X. That is the **BIP-38 class** —
cross-project movement — re-opened by a different door. So `seed-time-fail-closed`
is doing more than its name says: on the poll path it is the scope-authority
guard, and Sable's write-boundary scope guarantee is **conditional on it**
(declared in both lanes).

**Seed-time derivation is the requirement; an ingest-time cross-check is defense
in depth, not a substitute** (Morrow RC 3739). The contract is seed-time
provider-API derivation with a fail-closed refusal (above): seed time is the only
moment the supplier is present and the fact can be refused *before a cursor
exists*. The events feed additionally delivers a forge-authoritative per-event
repo id the code currently ignores, and cross-checking the seed against it at
ingest — the poll analog of the webhook's `base.repo.id` guard — is worthwhile
hardening. But it does **not** satisfy this ADR on its own and is not an
alternative to seed-time derivation: it fires only *after* the supplied fact has
entered the system, a quiet repository may never produce the signal, and by then
the cursor already exists. It layers on top of the seed-time contract; it never
replaces it.

## Instance 2 — the operator seed for a release apply (the deploy seed)

The same class in the deploy path — and the boundary of *what the operator
supplies* is the whole seam with the deployment & update lane (Rowan), so it must
be drawn precisely. **The operator supplies a SEED, not a release:** an exact
canonical **tag** and exactly **one backend image, by registry digest**. The
operator does **not** supply the commit, the level, or the other three service
digests. Those, with the tag, are the **forge-resolved release identity**,
produced by the canonical metadata resolver from the forge release record —
never operator input. (Requiring the operator to supply all four digests would
put a second release manifest in operator hands; requiring none would mean
executing unselected target code to decide which target code is authoritative.
One backend digest is the minimum seed that breaks the cycle — Rowan's lane.)

**The seed is exactly this ADR's class:** the backend digest is a trust fact an
unmanaged deployment cannot derive for itself, so it is supplied from outside and
cannot be checked against what the running system already knows. The rule
applies — cross-check it against authoritative signals the supplier does not
control, or fail closed.

**The cross-check is a chain of three distinct authorities, and no link may be
described as an image verifying its own digest** (Rowan, seam finding on 3f237fb):

1. the operator **seed digest** selects immutable bytes under the accepted
   **registry trust model** — pull-by-digest is content-addressing, so the
   registry guarantees you received exactly those bytes; it does **not** tell you
   they are the right release's backend;
2. the **forge release record** binds that backend digest to the exact four-image
   set and the commit — and the resolver's returned backend digest MUST equal the
   operator's seed digest;
3. the **baked `BIPLANE_VERSION` / `build`** in the selected backend bytes bind
   those bytes to the tag and the commit.

**Why the first hop always runs the resolver from the seed bytes** (Rowan's lane;
this corrects an earlier framing of mine that was wrong and self-defeating). It is
**not** because a running resolver would be "a signal the supplier controls": the
resolver *process* is not the authoritative signal — **the forge record it
retrieves is** — and the resolver the first hop *does* run is itself inside
operator-selected seed bytes, so that reasoning would disqualify the chosen path
more directly than the one it was meant to reject. The real reason is single-owner
and availability: an unmanaged running resolver may be **absent, stale, or a
second executable owner** of transport and schema semantics. The seed resolver is
accepted only as an **isolated implementation that fetches the independent forge
record**, with its output cross-bound to the selected digest and the baked fields
(links 2 and 3 above). The narrow ADR 011 point that survives is the one that was
true all along: **authority is the independent forge record and the registry,
never the process that fetches them** — which is why the chain binds to records
and bytes, not to any resolver's say-so.

**What the existing worked example does and does not prove.** `apply-update.sh`'s
`inspect_release_image` checks baked `BIPLANE_VERSION == tag` and
`build == commit` — that is **link 3 only**. It does **not** independently verify
the four resolved digests, and it **cannot** attest the digest of the very bytes
its baked fields live in: a field inside an image cannot say which digest selected
it, and inspecting the backend does not inspect the other three service images.
So the seed's trust is split across three authorities — the **registry** (link
1), the **forge record** (link 2), the **producer's baked fields** (link 3) — and
collapsing them into "one image verifies the release" is precisely the conflation
this ADR retires. In the accepted Scope A trust model the forge and registry are
the release authority; if that model changes, M5's signing re-entry condition
applies (Rowan's lane).

**The asymmetry with Instance 1 still holds:** the deploy seed has this
cross-check chain; the cursor seed has none. Instance 2 is where the pattern
already lives; Instance 1 is the case missing it. Where a supplied fact has **no**
authoritative signal to bind it, acceptance fails closed rather than trusting it.

**Seam to Rowan (deployment & update path lane):** the *operator seed* is an
instance of this class and states its cross-check obligation here; the *path* —
the managed/legacy two-state predicate, running the resolver from the seed bytes
in a least-authority ephemeral container, the whole first-hop mechanism — is
Rowan's lane (BIP-42), and his draft points back to this ADR as the owner of the
seed-fact class. This ADR says what the seed must satisfy; it does not specify the
path and does not require an operator-supplied manifest.

## Instance 3 — the forge↔board identity correspondence (the reviewer join)

The bot-moves-a-ticket rule (John, 2026-08-14) counts "the ticket's Reviewers
approved" from two authorities that nothing joins: the **forge** owns who approved
a pull request; the **board** owns who a ticket's Reviewers are. Today the bridge
has no join.

> **FACTUAL CORRECTION, 2026-08-15 (BIP-67 — Morrow's cold read; Sable).** The
> decision below is UNCHANGED. Two statements of fact in this paragraph are not:
>
> - It said the synthetic `_bridge_actor` was *the whole of the bridge's identity
>   handling*. That is false and understates what exists. The bridge hydrates and
>   stores the **forge** end of the join strictly: a merged pull request's
>   `author` and `merged_by` are recorded as a provider **`id` plus `login`**
>   (`github_events._actor_of`, `poller._actor_payload`), where an absent field
>   RAISES rather than being recorded as a measured absence, a malformed or
>   id-less actor RAISES, and an explicit null is stored as a measured "nobody".
>   The id is the identity and the login is display, precisely so a renamed or
>   reused login cannot carry someone else's standing.
> - What is missing is therefore narrower and sharper than "no identity
>   handling": it is the **correspondence** between a verified forge actor and a
>   board participant. Both endpoints exist; nothing joins them.
>
> This correction matters to this ADR's own argument: the supplier-control
> concern below is about the JOIN, and the forge end is already
> provider-attested rather than self-asserted.

The join needs a third fact — **which
forge account corresponds to which board participant** — which is identity work,
hence this lane's, and it is the same class as the two seeds above: a claimed
correspondence is a supplied fact, and a **self-asserted** one is
supplier-controlled — it would let a participant claim a forge account they do not
control and inherit its reviewer standing, i.e. become a reviewer they are not.

- **Home:** a field on the board **participant** (the forge login/account this
  participant is), owned by participant-administration. The bridge resolves
  against it; it never owns or mints identity. **`_bridge_actor` no longer exists**
  (corrected 2026-08-15): it was a messenger authorship label for board rows the
  bridge wrote, and it was **removed together with those writes** under BIP-67 —
  with no board write, there is no bot-written line needing an authorship label.
  It was never part of the join and its removal takes nothing the join needs; if
  a future slice restores board writes, the merging participant's *verified*
  identity is what must author them, not a synthetic stand-in.
- **Trust / writer:** set by the participant-administration authority — the same
  authority that grants the reviewer role — **not** participant self-service. A
  field whose value the claimant controls cannot carry cross-authority weight, and
  this field lets a forge approval carry a board reviewer's standing; the admin
  setter is the authoritative signal the claimant does not control. A field, not a
  protocol — and for agents it is set at provisioning beside the account and the
  role, so it is provisioning, not policing: no request-and-grant cycle per
  mapping.
- **Fail-closed:** a forge approval whose account has no participant mapping, or an
  ambiguous one, is **not** counted toward the reviewer check. A correspondence is
  never guessed. *(The "and nudges" half is a FUTURE mechanism, not a current one
  — see the application note at the end of this section. The decision here is
  only that an unmapped approval does not count.)*
- **Join:** forge-account → participant (via the field) → does that participant
  hold the reviewer role for this ticket. Each authority stays sovereign; the field
  is only the join key.

The write-boundary lane (Sable) owns the reviewer-authority *check* that consumes
this and cites it; this ADR owns only that the correspondence is admin-established
or the approval does not count.

## Scope: the rule is cited by its applications, not enlarged by them

John ruled on ticket movement on 2026-08-14, without having read this draft, and
reached this ADR's rule from the other end: **a directive in a PR body
(`Closes BIP-12`) is a fact supplied by the author, who controls it, so it
authorises nothing — at most it hints which ticket. What can move a ticket is the
merge event plus the review record, which the bot fetches from the forge itself;
where those do not determine the outcome the bot moves nothing and nudges the
responsible role.** That is exactly *an unverifiable supplied fact cross-checked
against an authoritative signal the supplier does not control, or fail closed* —
with the nudge as the fail-closed, and the reminder, in his words, the valuable
half. Two ends traced independently before either knew of the other, so the
agreement is corroboration, not the Corollary 1b trap.

> **APPLICATION NOTE — what of this runs today (added 2026-08-16, BIP-67; the
> DECISION above is unchanged).** The paragraph above states John's ruling in his
> framing, and as a statement of the *rule* it stands. As a description of the
> shipped bridge it is now false in three specific ways, and this record is
> accepted, so the facts are corrected here rather than left to mislead:
>
> - **"the bot moves nothing"** — true, and now unconditionally so. **No event
>   moves a ticket at all**, whether or not the facts determine an outcome. Every
>   board and state write is deleted rather than gated, so the fail-closed branch
>   is the only branch.
> - **"the review record, which the bot fetches from the forge itself"** — the
>   bridge no longer fetches it. That authority re-read was **deleted with the
>   write it guarded**; review data now comes from the **signed event body**,
>   which is sound precisely because nothing is authorised by it — selection needs
>   no forge permission.
> - **"and nudges the responsible role"** — there is **no nudge**. The
>   notification half is cut from this release. What exists is a **comment on the
>   pull request**, only where `FORGEJO_BRIDGE_WRITE_TOKEN` is set; on Pi today it
>   is unset, so the refusal is durable and **nobody is told**. A refusal arising
>   from a push reaches no person in any configuration.
>
> None of this touches the identity-join decision, which is what this ADR owns
> and which is unaffected: an unmapped or ambiguous forge account still must not
> count toward a reviewer check, whenever that check comes to exist.

It is nonetheless **not** a third instance of this ADR, and the boundary is the
point. This is a bounded decision about **identity and configuration seeds** — a
repo id, a release identity. Ticket movement is a **board write**, and what makes
a board write legitimate is the write-boundary lane's decision (Sable). That lane
**cites this rule** — the directive is the supplied fact, the forge-verified merge
and review record is the authoritative signal, and handing the work back to a
person is fail-closed (named "the nudge" in the ruling's own framing; see the
application note above for what that is today, which is a token-conditional pull
request comment and not a nudge) — exactly as the deploy lane cites it for
release identity. Folding the application in would
make 011 a general theory of trust rather than a decision, and would co-own a
decision that belongs to another lane. One owner for the rule; its applications
own themselves and point back — the same single-owner discipline as ADR 009.

## Consequences

- Cursor provisioning gains a mandatory authoritative-fetch step and a fail-closed
  refusal. This is new required behavior, not a refactor.
- The page-consistency guard is retained and re-documented; no code is removed on
  the strength of this ADR (the guard defends a different failure — a foreign page).
- The reference implementation of the rule is the first hop's **complete
  three-authority cross-check** — registry content-addressing (link 1) + the forge
  release record (link 2) + the baked identity (link 3) — landed in the BIP-42
  first hop. `inspect_release_image` is **one component** of it (link 3, the
  baked-identity binding), and cannot prove the registry or forge-record links on
  its own. New operator-supplied trust facts are measured against the full chain,
  not against any single link (Morrow RC 3739).

## What this decision does not do

- It does **not** define poller cursor lifecycle beyond seeding — ADR 010 §404
  stays; this ADR fills only the seed, which §404 left open.
- It does **not** specify the deploy/apply/bootstrap path (Rowan's lane, BIP-42).
- It does **not** cover what makes a *board write* legitimate — directive grammar,
  target-state and ticket-property axes (Sable's and Morrow's lanes, BIP-67). A
  wrong identity seed is a distinct axis from an illegitimate directive: this one
  is "the system trusted a fact it could not check," not "the system acted on
  text it should have ignored."
- It does **not** own the **tree-authority rule** (which tree is authority for a
  claim about the running system). That is Aria's lane; my `biplane_installed_build`
  finding is its worked example (the field's *absence* dates the box), referenced
  there, not restated here.

## Convergence record

Both questions this draft opened are resolved into the contract above; the
reasoning is kept because Aria asked that the *why* live in the ADR, not be
decided quietly.

1. **Seed-time-only, not reconciliation** (Aria, 2026-08-14). Reconciliation
   detects but cannot repair, and adds a second answer with no attached action —
   folded into Instance 1's requirements.
2. **The namespace-migration machinery does not extend to `repo_stable_id`**
   (Aria, 2026-08-14). Instance and repo id fail differently; a changed repo id is
   a new identity or a defect, never a migration — folded into Instance 1's
   lifecycle bullet with the reason stated.

Still genuinely open — for Sable or anyone who sees it differently, in which case
it becomes a stated fork rather than an answer: nothing at present. Raise here.
