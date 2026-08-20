# 008 — An unsafe token-API request that returns an error keeps no partial writes

**Status:** accepted, **partially implemented** · **Decided by:** Morrow and Rowan
(BIP-18) · **Date:** 2026-08-03

> ## ⚠️ What exists at this commit, and what does not
>
> Sia's review of PR 23 caught an earlier version of this record describing a
> live system that was not there; Morrow's 10162 then caught the correction
> itself going stale once this branch was stacked on the boundary slice. This
> box describes the ACTUAL stack at this commit (PR 23 on top of PR 22).
>
> **Present at this commit (from the boundary slice, PR 22):**
> - `MutationDispatchMixin` — the transaction boundary: one
>   `transaction.atomic` per unsafe token-API request, `set_rollback` on a
>   handled exception or a ≥400 response, with real-connection regressions.
> - identity bound at the write boundary (`creation_identity`), before the
>   serializer writes.
>
> **Present from this PR:** `AuditOutbox`, its migration (0127), and
> `enqueue_audit()`.
>
> **Also present now (the audit slice):** `drain_audit_outbox` — claims a due
> row under a lease and writes the activity AND the processed mark in ONE
> transaction — the 23 `issue_activity` call sites converted to
> `enqueue_audit`, an on-commit wake so latency is unchanged, and a
> task-aware gate.
>
> **DELIBERATELY OUT OF SCOPE — not made durable by this decision:**
> - **outbound webhooks** (`model_activity`, `webhook_activity`). They stay on
>   `dispatch_after_commit`: post-commit, best-effort, still lossy on a broker
>   outage. Routing them through a per-minute worker would add up to a minute
>   of webhook latency for no audit benefit.
> - **functional tasks** (`get_asset_object_metadata`,
>   `crawl_work_item_link_title`). Not audit; same best-effort path.
>
> **Consequence, stated plainly:** AUDIT is durable — an audit intent commits
> with its mutation and survives a broker outage, and a crash mid-delivery
> cannot duplicate it. Webhook and functional delivery are NOT durable and
> this decision does not claim they are.

## Context

The public API (`plane/api/`) accepted writes with no transaction anywhere:
`transaction.atomic` and `on_commit` appeared **zero** times across its 66
unsafe handlers. Audit was dispatched with `issue_activity.delay(...)`, a
message to a broker rather than a database write.

That produced three reachable failures:

1. the audit task could run **before** the mutation committed, and read stale or
   absent data;
2. a mutation that **rolled back** still emitted an audit record for a change
   that never happened;
3. a **broker outage after commit** lost the audit silently — the mutation
   committed, the caller got a 2xx, and nothing remained to retry from.

A first attempt converted call sites individually. That was abandoned: on its
own `transaction.on_commit` runs the callback immediately, so a per-site
deferral is only real if that site also opens its own transaction — which in
`project.py` would have been a behavioural change smuggled into a mechanical
sweep.

## Decision

**One transaction boundary, in the shared token-API base.** Not per-call-site
atomic blocks, not global `ATOMIC_REQUESTS`.

- `MutationDispatchMixin`, used by both `BaseAPIView` and `BaseViewSet`, wraps
  only `POST`/`PUT`/`PATCH`/`DELETE`.
- DRF catches exceptions and **returns** an error response, so a plain atomic
  block sees an ordinary return and commits. The mixin therefore calls
  `transaction.set_rollback(True)` when DRF handled an exception, or when the
  finalized response is status ≥ 400.
- The mark is made **after** the finalized response is in hand and before the
  block exits — not inside `handle_exception`, because DRF may still finalize
  and code must not query a connection already marked rollback-only.

**Audit truth is a row, not a message.** `AuditOutbox` is written inside the
same transaction as the mutation; `on_commit` is demoted to waking the worker
sooner. `on_commit` fixes failures 1 and 2 above but cannot fix 3, because the
deferred thing is still an external side effect.

Liveness comes from the worker itself, not a separate scanner. An earlier
revision of this record said "a scanner provides liveness when the wake-up is
lost"; no such process exists. `drain_audit_outbox` runs on an every-minute
beat and picks up both pending rows and rows whose lease has expired, so a
lost wake-up or a crashed processor costs latency, not the audit.

**Exactly-once is transactional, not keyed.** The audit write and the
processed mark happen in one transaction under the lease, so a crash rolls
back both and a retry starts clean. A lease alone would not be enough: a
worker that wrote the activity and then died before marking the row would
leave valid work to be repeated, and the lease was validly held both times.
`event_key` is a stable external handle, not a dedupe key — nothing keys
deduplication on it. `result` records the activity ids a processed row
produced, written in the same statement as the processed mark.

`enqueue_audit()` raises when called outside a transaction. That is a
programming error — a call site outside the base boundary — and failing loudly
is deliberate: a quiet degrade would silently restore the behaviour this
decision removes.

## The handler audit that made the policy safe

Rolling back on every 4xx is a policy change, so before adopting it every
unsafe handler was classified: two AST passes plus a hand read of each hit.
The scanners live in the repo (below) so the result is re-derived, not
attested — an earlier revision of this record claimed an independent reviewer
re-run for which no durable artifact exists; that claim is withdrawn and the
evidence is the committed tools plus their pinned fixtures (Morrow 10162).

The unsafe set covers the DRF ViewSet action names too — `create`, `update`,
`partial_update`, `destroy` — which routers reach through unsafe HTTP methods
without those names appearing (StickyViewSet, WorkspaceInvitationsViewset).
The first revision scanned only `post/put/patch/delete` and called 60 a full
census; the derived total is **67**. The six action handlers were hand-read:
Sticky `create`/`partial_update` are the standard success-only shape (the 400
is the `else` of `is_valid()`, no write on that path) and the invite/sticky
remainder return their errors before any write.

| bucket | count | notes |
|---|---|---|
| intentional partial commit | **0** | no escape hatch is needed |
| success-only mutation | the rest | the 400 is the `else` of `is_valid()`; no write occurred |
| partial state today, accidental | **2** | `project.py` `post` and `patch` |

Fourteen caught-exception error paths were traced for writes preceding them —
ten shown below, plus four `InvalidAssertedIdentity` catches added by the
boundary slice itself, each with **no** writes in the guarded body (the
refusal happens before any write, by design):

| file | handler | catches | returns | writes ahead | verdict |
|---|---|---|---|---|---|
| project.py | post | IntegrityError / Workspace.DoesNotExist / ValidationError | 409 / 404 / 409 | save, create, create, bulk_create | **partial state today** |
| project.py | patch | IntegrityError / DoesNotExist / ValidationError | 409 / 404 / 409 | save, create | **partial state today** |
| state.py | post | IntegrityError | 409 | save is itself the raiser | none |
| issue.py | post (label) | IntegrityError | 409 | save is itself the raiser | none |
| asset.py | patch | FileAsset.DoesNotExist | 404 | raised by the `.get()` before the save | unreachable after write |
| issue.py | put | Issue.DoesNotExist | 400 | raised by `.get()`; the save is in the success branch | unreachable after write |

**Project creation is the case worth remembering.** It writes the project, a
member row, optionally a lead, then bulk-creates default states — four writes,
one `try`. If `bulk_create` raises `IntegrityError`, the caller receives a clean
409 and the database keeps **a project with members and no states**. Nobody
chose that; it is what the absence of a boundary produces. For these two the
decision is a **correctness repair**, not a policy break to be excepted.

Scope was verified: `plane.api`'s `BaseAPIView` and `BaseViewSet` are used by
the token API only — the app and space surfaces have their own base
implementations — so the 67-handler boundary is the right one.

## Limits

Static analysis plus a hand read. It cannot prove reachability on every runtime
path, and the six distinct code shapes were read rather than all 67 handlers
line by line. The scanners live at `apps/api/plane/tests/tools/` so the result
can be re-derived and disputed rather than taken on trust.

## Consequences

- An endpoint that intentionally commits partial state and returns 4xx is now
  an incompatibility to surface, not a reason to soften the base. None exist
  today.
- The temporary per-file budget guard on bare audit dispatches is scaffolding.
  Once the outbox lands it must be replaced by a zero-bare-dispatch invariant,
  and the per-site transaction guidance removed — it contradicts this decision.
