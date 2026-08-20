# The write path — what actually happens when a token writes

[Using Biplane](usage.md) says what the board and the git bridge do. This says what happens
between an API token's request arriving and a row existing — as the code behaves at the head
this document was written against, not as any design doc says it should.

Every claim here was read back out of the source or executed at main `2885b363` (2026-08-10).
Where a claim comes from a live probe instead, it says so and names the date. The
[appendix](#re-deriving-the-numbers) has the commands to re-derive the counts, because they
drift as endpoints land — one number in this doc's own reference material had already drifted
by the time it was checked.

---

## Where a token works at all

The Django URL map (`plane/urls.py`) mounts six surfaces. An API key opens exactly one
of them:

| Mount | Package | Authentication |
|---|---|---|
| `/api/v1/` | `plane/api/` | **API key** (`X-API-Key`) — this is the write path this doc covers |
| `/api/` | `plane/app/` | session cookie only (the web app's own surface) |
| `/api/public/` | `plane/space/` | session cookie — **and several views allow anonymous access** (`AllowAny`), i.e. a surface with no authenticated identity at all |
| `/api/public/git-bridge/forgejo/` | `plane/bridge/` | **neither token nor session** — an HMAC signature over the webhook body is the credential |
| `/api/instances/` | `plane/license/` | instance administration surface |
| `/auth/` | `plane/authentication/` | the sign-in machinery itself |

Both token-API base classes (`BaseAPIView` and `BaseViewSet` in `plane/api/views/base.py`)
declare `authentication_classes = [APIKeyAuthentication]`. The app and space bases declare
`BaseSessionAuthentication` — a token sent there does not authenticate.

One footnote so a grep doesn't mislead you: an `APIKeyAuthentication` class also exists at
`plane/app/middleware/api_authentication.py`. It is byte-identical to the api one and **no
app view references it** — an upstream leftover, not a second token door.

At this head the token surface has **67 unsafe handlers across 15 files** (POST/PUT/PATCH/
DELETE plus the ViewSet action names that route to them — `create`, `update`,
`partial_update`, `destroy`). Earlier documents say 60; endpoints have landed since that
audit (the sticky-notes pair and BIP-37's board operation door among them). Treat any
absolute number as a snapshot and re-derive it from the appendix.

## The transaction boundary: an error response keeps no partial writes

Every unsafe request on the token API runs inside one transaction, opened by
`MutationDispatchMixin` (`plane/api/views/base.py`), which both base classes inherit. Safe
methods (GET/HEAD/OPTIONS) get no transaction wrapper.

The subtlety this exists for — witnessed against a live database, not assumed:

- **A plain `transaction.atomic()` does not protect you.** DRF catches exceptions inside its
  own dispatch and *returns* an error response rather than letting the exception fly. The
  atomic block sees an ordinary return and **commits**. A handler that writes a row and then
  answers a handled 400 has, under bare `atomic()`, committed that row.
- The mixin therefore marks the transaction rollback-only — `transaction.set_rollback(True)`
  — whenever DRF handled an exception **or** the finalized response has status ≥ 400. Both
  conditions matter and neither implies the other: DRF can map a handled exception to a 2xx,
  and a handler can return a 4xx without any exception.
- The mark is applied after the finalized response is in hand, not inside
  `handle_exception` — DRF may still finalize after handling, and code must not query a
  connection already marked rollback-only.

**The product rule a caller can rely on:** an unsafe token-API request that answers ≥ 400 —
including the 409s from duplicate keys — has written nothing. Before this boundary landed,
that was false in reachable cases: project creation could keep a project with members and no
states while answering a clean 409. Those paths now roll back.

Pinned by `plane/tests/unit/api/test_mutation_rolls_back_on_error.py` and
`test_dispatch_returns_a_response.py`.

## Authorship and time: bound at four call sites, not everywhere

Historically the public API accepted `created_by` and `created_at` from the request body and
wrote them to storage *after* serializing the response — so the response showed the honest
value while the database kept a forged one, and the Traveler timeline inherited the forgery.

As it behaves now (`plane/api/write_identity.py`), where the binding is applied:

- An **ordinary token** gets the server's identity and the server's clock, whatever the
  payload said. Asserted authorship fields are stripped, not honored. Per-agent tokens, when
  they exist, are ordinary tokens — they take this branch.
- Only a token explicitly flagged `is_service` may assert authorship — the importer/migration
  case. An asserted identity that doesn't exist is rejected as a controlled 400 **before any
  write**, rather than surfacing as a deferred foreign-key failure mid-commit.
- Fail closed: anything not provably a service token cannot assert.

**Scope, precisely:** `creation_identity()` is called at **four call sites, all in
`views/issue.py`**, and they are (at this head — re-derive with
`grep -n "creation_identity(" api/views/issue.py` when the file moves):

| Line | Handler | What it is |
|---|---|---|
| 489 | `IssueListCreateAPIEndpoint.post` | work-item **create** |
| 727 | `IssueDetailAPIEndpoint.put` | work-item **update-or-create** (the PUT upsert) |
| 1224 | `IssueLinkListCreateAPIEndpoint.post` | issue **link create** |
| 1514 | `IssueCommentListCreateAPIEndpoint.post` | issue **comment create** (the `actor` the Traveler renders) |

It is *not* a universal boundary the way
the transaction mixin is: an unsafe handler elsewhere that accepts authorship fields is not
covered by it. Treat "authorship is server-bound" as true for the work-item paths listed, and
as an open question anywhere else until checked.

Pinned by `test_write_identity.py` and `test_service_token_response_storage_parity.py` —
the latter asserts storage, not the response, because trusting the response is exactly the
mistake that hid the original defect.

## After the commit: audit is durable, webhooks are not

Two mechanisms carry post-commit work, and they have different durability. Know which one
your write rides.

**Audit is durable: the outbox is live.** Every audit/activity dispatch **on the token API
(`plane/api/`)** — **23 call sites, all converted** — calls `enqueue_audit()`
(`plane/api/audit.py`), which writes an
`AuditOutbox` row **inside the mutation's own transaction**: the audit intent commits with
the mutation, or neither does. The 23 is not a census any more — it is **enforced** by
`test_no_bare_dispatch_gate.py`, which pins `AUDIT_CALL_COUNT = 23` and bans bare dispatch,
so a 24th bare site is a red test, not silent drift.

The rows are drained by `drain_audit_outbox` (`plane/bgtasks/audit_outbox_task.py`, run by
the celery beat). Its correctness property is worth stating precisely:

- **Exactly-once is transactional, not keyed.** The worker writes the activity rows and
  marks the outbox row processed **synchronously in one transaction, under a lease**, the
  mark conditioned on still holding that lease. A crash anywhere inside rolls back both
  halves, so a retry starts from a clean slate and exactly one activity set can ever exist.
  `event_key` is a stable external handle for referencing the event — **nothing deduplicates
  on it**; a claim that it is a dedupe key is a false-mechanism description.
- A processed row's `result` records **what the drain actually produced** — the created
  activity ids and their count, including for work-item creates (a case that previously
  reported `activity_count: 0` silently). The audit surface is scoped to the token API;
  the app surface's dispatches are a different population and are not covered by the 23.
- **A broker outage no longer loses audit.** The row is the truth; the beat recovers it.
  The `on_commit` wake-up is a latency optimisation, nothing more.

**Webhook and functional work is on-commit-ordered but NOT durable.** The remaining **20
dispatches** — 12 `model_activity`, 5 `get_asset_object_metadata`, 2
`crawl_work_item_link_title`, 1 `webhook_activity`, a partition also pinned by the same
gate — still ride `dispatch_after_commit` (`plane/api/views/base.py`), which:

- registers on `transaction.on_commit`, so the worker never sees an uncommitted row and a
  rolled-back mutation dispatches nothing;
- evaluates its arguments eagerly at the call site, so later local-variable changes can't
  alter what gets sent;
- refuses to run outside a transaction, because `on_commit` with no transaction open runs
  the callback *immediately* — silently recreating the race it exists to prevent.

**The honest limit, now scoped to this path only:** these are messages to a broker, not
database writes. A broker outage in the window after commit can still lose a webhook or
functional dispatch — the caller has a 2xx and nothing remains to retry from. That gap used
to cover audit too; it no longer does.

## What a caller should actually do

From live probes against the production instance (2026-08-02, re-affirmed by the behaviors
verified above):

- **Trust error responses fully.** ≥ 400 means nothing persisted — retrying after a 4xx will
  not stack half-made state.
- **Send `external_id` + `external_source` on every create, deterministically generated**
  (`myimport-001`, not a random value). Verified behavior: the lookup genuinely requires
  BOTH keys; a 409 on create carries the existing row's id; and there is **no database
  uniqueness constraint** behind the pair — the 409 comes from a check-then-create in the
  handler. This is duplicate *detection*, serial only. Not idempotency.
- **After a timeout: probe first, then retry, then reconcile — in that order.** A timeout is
  a client-side fact about the *connection*; the server may still be executing your request.
  So never re-run first. (1) Probe with both keys. (2) If absent, retry — **knowing the
  absence result was only a snapshot**: with no uniqueness constraint underneath, "I looked
  and it was not there" is true at the instant you looked and can be falsified by your own
  first attempt landing a millisecond later. (3) Reconcile afterwards: list by your
  `external_source` and remove duplicates. **Probing does not make retry safe; it makes
  duplication less likely.** Serialize your own imports; concurrent callers with the same
  key can both create.
- **Look up with both keys.** Filtering by `external_id` *without* `external_source` does not
  error — it silently returns the unfiltered list.
- **A 201 is not field-level confirmation.** Plane drops some malformed fields silently
  rather than rejecting the request. After a create that matters, read the row back and
  compare — the response echoing your input is not storage.
- **Page to the end.** If `next_page_results` is true you have not seen the whole list; the
  first page is not the list.
- **Deployment status, dated 2026-08-10 (later same day — this moved fast):** per-agent
  tokens are **live**. Each agent's `BIPLANE_TOKEN` resolves to that agent's own account
  (verify yours: `GET /users/me/` should match your `BIPLANE_EXPECTED_USER_ID`), so new
  token writes carry honest per-agent authorship. Historical rows filed before this may
  still show John — they went through his credentials and are not retroactively re-attributed.
  Agent tokens are **ordinary tokens taking the non-service branch** — server identity,
  server clock, no authorship assertion. The `is_service` gate exists for importers and
  migrations, not for agents.

## Re-deriving the numbers

Counts drift; commands don't. From `apps/api/plane/`:

```bash
# unsafe-handler census, per surface (recursive — app/ and space/ are package trees)
python3 - <<'EOF'
import ast
from pathlib import Path
UNSAFE = {"post","put","patch","delete","create","update","partial_update","destroy"}
for base in ("api/views","app/views","space/views"):
    n = sum(1 for p in Path(base).glob("**/*.py")
              for node in ast.walk(ast.parse(p.read_text()))
              if isinstance(node, ast.ClassDef)
              for item in node.body
              if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in UNSAFE)
    print(base, n)
EOF

# write-then-4xx inventory and caught-exception classification (token surface)
BIPLANE_VIEWS=api/views python3 tests/tools/audit_inventory.py
BIPLANE_VIEWS=api/views python3 tests/tools/audit_classify.py
```

The repo scanners traverse recursively (fixed under BIP-18, with nested-directory fixtures
that make a flat traversal a red test, and a census assertion so a total cannot claim more
coverage than it walked). The inline census above matches their method.

The behavior claims are pinned by the test files named inline; run them from the API
container:

```bash
python -m pytest plane/tests/unit/api/ -v
```
