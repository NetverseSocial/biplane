# 009 — A review outcome moves the ticket; the board follows the review

**Status: SUPERSEDED and NON-NORMATIVE** · **Superseded by:** *Who may move a
ticket*, `docs/scope-a-architecture.md` (John's ruling, 2026-08-14) ·
**Originally decided by:** John, 2026-08-12 · **Never implemented as written.**

> ## ⚠️ NOTHING IN THIS DOCUMENT IS IN FORCE
>
> Retained for the reasoning, which is still worth reading, and for the record of
> what was decided when. **Do not implement anything below.** Four things it
> requires were overturned:
>
> 1. **The automatic Review → Code & TDD write on a changes-requested review.**
>    Removed outright rather than deferred: under the ruling the bridge is a tool
>    with no authority of its own, and a changes-requested review is neither a
>    merge nor an approval, so it can never satisfy the conditions for a write.
>    A failed review is exactly the case where the bridge **asks**, and the
>    participant holding the role moves the ticket in whichever direction the
>    work actually went. **How the ask reaches them, exactly** (corrected
>    2026-08-16 — this warning is the current supersession summary, so a
>    mechanism named here must be one that exists): as a **comment on the pull
>    request**, and only where `FORGEJO_BRIDGE_WRITE_TOKEN` is configured. It is
>    **not** a notification — that half is cut from this release. On a deployment
>    without the token, which includes Pi today, the refusal is recorded durably
>    on the delivery result and **nobody is told at all**.
> 2. **"Completion does not require approval."** It does now: two or more
>    approving reviews from the participants named in the ticket's Reviewer(s)
>    field, with the pull request's author excluded from that count.
> 3. **The forward-only rule and its bounded exception.** There is no
>    forward-only rule on the board at all. Roles move tickets in any direction;
>    a board that cannot go backwards does not reflect the work.
> 4. **The interim procedure below, and it is the most important of the four to
>    retire.** It instructed reviewers to move tickets by hand until the path
>    landed. The path never landed as written, and a hand-move is precisely the
>    board action that used to withdraw the only protection the bridge had — a
>    stale instruction telling people to perform the action that re-armed the
>    hazard.
>
> The live rule is in *Who may move a ticket* in the Scope A architecture, which
> names its authorities: the forge owns what happened to the code, the board owns
> who is responsible for the work.

> ## ⚠️ HISTORICAL — what existed when this was written
>
> **Nothing new is implemented.** The bridge today handles two events — a push
> carrying a directive, and a merged pull request. It ignores every other event,
> including the review events the forge already delivers.
>
> **Until the rejection path lands, reviewers move the ticket by hand.** That is
> not a workaround to be tolerated quietly; it is the interim procedure, with a
> named prerequisite and a named fallback, written here so it is not forgotten
> in either direction.

## The problem

The rework loop — a review rejects, the ticket returns to the author, the author
fixes it and sends it back — has been carried in loop prompts and chat. Those do
not survive a context compaction. The procedure is real, everyone agrees with
it, and it is written down nowhere, so it is reconstructed from memory each time
and drifts each time.

An agent remembering to move a ticket is not a mechanism. The review outcome is
already recorded, authoritatively, by the forge. The board should follow it.

## The decision

| Trigger | Ticket moves to | Status |
|---|---|---|
| Push carrying any directive | Review | **live today** |
| Merged pull request carrying a completion directive | Done | **live today** |
| **Official changes-requested review, at the current head** | **Code & TDD** | **new** |

Only the third row is new. The first two describe what the bridge already does,
stated here so this record cannot contradict the running system.

**Completion does not require approval.** The live bridge completes a
directive-bearing merged pull request; it does not separately require an
approval event, and this decision does not introduce one. An earlier draft of
this table said "approved and merged", which implied a condition that does not
exist. (Rowan, RC 3381.)

### The readiness signal is the directive-bearing push — there is only one

An earlier draft said the author moves the ticket back to Review by hand,
"because the bridge must not guess from intermediate pushes." That contradicted
the first row of its own table: the live bridge already advances any
directive-bearing push to Review, so a fixing commit carrying `Refs BIP-N`
returns the ticket automatically. The document specified two owners for one
transition. (Morrow RC 3380 blocker 1, Rowan RC 3381 item 1 — converged
independently.)

**Resolved in favour of the live behaviour: a push carrying a directive is the
author's statement that the work is ready to look at.** It is explicit, it is
authored, it already works, and it needs no new code. The manual claim is
withdrawn.

The cost, stated rather than hidden: an author who references the ticket from a
work-in-progress commit returns it to Review before they meant to. The control
is the directive itself — **omit the reference on intermediate commits, include
it when the rework is ready.** That is a convention, not a mechanism, and it is
the price of not building a second readiness channel.

### Targets are matched by column name, not by position

Five of this board's columns share the `started` group — In Progress, Design,
Code & TDD, Review, Integration Test — so "the first started state" would land
on In Progress, which is wrong. Rework resolves to the column **named** Code &
TDD, the same way the existing transitions resolve Review and Done by name.

Backlog is a different group and is never a target. Nothing the bridge does can
push work back into Backlog.

### What counts as a rejection

The event header proves the **outcome type** and nothing else. It does not prove
the review counts, does not bind the review's identity, and does not prove the
reviewed commit is still current. An earlier draft claimed the implementation
"does not need to interpret the review object at all." That was wrong, and it
was wrong in the one place where being wrong matters most — the only transition
that can move work backward. (Morrow RC 3380 blocker 2, Rowan RC 3381 item 2.)

A rejection moves the ticket only when **all** of these hold, re-read under the
same lock as the transition:

1. the delivery is authenticated by the existing signature check;
2. the review is an **official, counting** changes-requested review — not a
   comment, not an approval, not a drive-by from someone without review
   authority on that repository;
3. the forge's current pull object names the **same stable repository id and
   display path** as the authenticated delivery's scoped repository — the
   stable id grants board authority, while a second mutable path must never
   select which repository supplies the review and directives;
4. the pull request is **still open and unmerged**. A delayed rejection from a
   closed or merged pull is historical evidence, not actionable authority;
5. the reviewed commit **still equals the pull request's current head at
   processing time**, read from the forge rather than from the payload;
6. the transition is exactly **Review → Code & TDD**; any other current column,
   including Done, Deploy and Cancelled, is inert;
7. the move is idempotent under a durable key derived from the **immutable
   review identity**, not from the delivery id.

Conditions 5 and 7 exist because the delivery inbox retries. Event-time
freshness is not enough: a delayed or superseded rejection can arrive after the
author has already pushed a fix and the ticket has returned to Review, and
without the current-head check it would regress work that was never rejected.
Forgejo replays mint a new delivery UUID, so delivery id cannot carry
idempotency — the review's own identity can.

### The forward-only rule gets one bounded exception, not a flag

The bridge refuses to drag a ticket backward. That rule is why replayed and
out-of-order deliveries are safe. This decision opens exactly one exception,
and it is written as a specific edge:

> An official changes-requested review on the current head of an open, unmerged,
> stably scoped pull request moves a ticket from **Review** to **Code & TDD**.
> Nothing else moves a ticket backward.

**It must not be implemented as a reusable `allow_backward` parameter.** A
general capability is a standing invitation to add a second backward case
without a second decision; a named edge is not. If a second backward transition
is ever wanted, that is a design change and belongs in its own record.

## Why the new transition is small

**No new subscription is required.** The repository's webhook is **already
subscribed** to `pull_request_review_approved` and `pull_request_review_rejected`
— read off the live hook configuration, not assumed. The bridge drops them
because it accepts only pushes and merges.

The body is the same pull-request payload the merge path already parses, with an
added `review` object; the pull request and its number sit where the merge
transition already reads them. What the implementation adds beyond that is the
authority and freshness re-read described above, which is a forge API call, not
a new store.

## Interim procedure — RETIRED 2026-08-14, NOT in force

1. A reviewer who requests changes **moves the ticket to Code & TDD in the same
   action**. Not later — a review that leaves the ticket in Review is invisible
   to everyone not reading that pull request.
2. The **author** pushes their fix with the ticket referenced when the rework is
   ready, which returns it to Review. Unchanged by this decision, and unchanged
   afterwards.
3. Reviewers pull continuously from the Review column rather than waiting to be
   assigned, and do not wait on each other.

**Prerequisite: board-write provisioning.** Step 1 assigns a board write to
every reviewer, and not every reviewer has one — Morrow's identity preflight
passes while the project endpoint returns 403, so the procedure as first written
was not executable by him. (Morrow RC 3380 blocker 3.)

**Fallback, until every reviewer has board write:** a reviewer who cannot
perform the move says so in the review and names it; **Aria performs the move
and reads it back in the same workflow step.** A procedure that documents an
impossible write as completed is worse than no procedure, because it reads as
done.

## Consequences

- The board becomes readable as the actual state of the work rather than as a
  record of who remembered to update it.
- A reviewer's rejection has an effect even if no human reads the pull request.
- The bridge gains its first backward transition. If a second one is ever
  proposed, that is the moment to re-examine whether forward-only is still the
  right default — not to add a second exception beside this one.

## What this decision does not do

- It does not move a ticket on **approval**. Approval alone changes nothing;
  the merge is what completes.
- It does not add an approval requirement to completion.
- It does not touch Backlog, Todo, Design, or Integration Test.
- It does not add a state machine, a durable review-state table, or a second
  record of who reviewed what. The forge holds the review; the board holds the
  column. One job, one owner.
