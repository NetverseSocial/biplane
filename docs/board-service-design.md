# M8 Board service (BIP-37) — design

**Status:** DESIGN, first cut 2026-08-15 · **Author:** Sia ·
**Spec:** `docs/scope-a-architecture.md` §M8 — the five-point contract is normative;
this page says how it lands in this codebase, and in what order. If this page and
§M8 disagree, §M8 wins and this page gets fixed.

## What it is, in one paragraph

One server-side **operation service** that every writer calls and one **outcome
ledger** that every caller can query, fronted by a small HTTP surface on the
existing token API — plus a **thin adapter** agents install instead of the
per-container helper scripts they carry today. The service is a *service layer
inside the board*, not a second server: REST, UI, import, MCP (M6) and the git
bridge (M2) all converge on it (§M8.3), and it consumes the write boundaries
that already exist (M7 identity binding, the mutation-transaction mixin) rather
than rivaling them (invariant 8).

## Endpoints (the thin-adapter surface)

Mounted under the existing token API (`/api/v1/`, `X-API-Key`,
`APIKeyAuthentication` — the one surface where a token works; see
`docs/write-path.md`). Four endpoints, deliberately few:

| Endpoint | Contract point it exists for |
|---|---|
| `POST /api/v1/board/ops/` | §M8.1+2 — THE mutation door. Body is an **operation envelope**: `op_key` (caller-durable, persisted by the caller BEFORE the call), immutable expected principal id, source, verb, workspace/project scope, payload. The expected id must equal the server-bound token principal. The key is bound to a canonical digest of that exact envelope. Replay returns the **stored outcome** with `replayed: true`; reusing the key for different work is a conflict. |
| `GET /api/v1/board/ops/<op_key>/` | §M8.1 — outcome-by-key. The query a caller makes after an unknown transport result, BEFORE any retry. 404 means "never committed", which is safe to act on precisely because the outcome row commits in the same transaction as the mutation. |
| `GET /api/v1/board/work-items/<workspace>/<project>/` | §M8.5 — the full project set by default. An explicit caller `limit` yields `truncated: true` and a continuation cursor when rows remain. Workspace and project are path-bound so permission checks never infer scope from an ambiguous identifier. |
| `GET /api/v1/board/work-items/<workspace>/<project>/<sequence>/` | §M8.5 — the **readback** target: read the row you think you wrote, not the response that said you wrote it. |

`GET`s carry no transaction; the `POST` runs inside the one commit described
next. Anything not listed here is not part of v1 — no destructive verbs
(security invariant 3: the deny is server-side; the v1 envelope simply has no
delete verb to scope).

## The one transaction (§M8.2)

`board.service.execute(envelope, principal)` does, in ONE `transaction.atomic()`:

1. **Op-key claim** — insert the `BoardOperation` row (unique on
   `(principal, op_key)`); an existing row short-circuits to its stored outcome
   under lock, before any domain read.
2. **Re-read + revalidate under lock** (§M8.4) — lock the project membership,
   target work-item and target state, then re-check their scope and the
   principal's current role *inside* the transaction. Rules checked outside
   the lock are advisory, exactly the defect §M8 names.
3. **Domain mutation**, through the existing model layer.
4. **Outcome + audit rows**, same commit. An outcome-less mutation and a
   post-commit audit write are contract violations, not implementation choices —
   the ledger row IS the mutation's receipt, or neither exists.

## Identity binding (per M7 — consumed, not rebuilt)

The token API binds `request.user` before this service runs. The operation
boundary accepts that server-bound principal directly and has no actor field,
so asserted authorship cannot enter the mutation envelope. The adapter also
carries its immutable expected principal id; the server compares it to
`request.user` before claiming the operation. That is a mismatch check, not an
identity override. What M8 adds is the **principal in the operation scope**:
the op key is scoped by the immutable principal, so one agent's replay can never
collide with — or read — another's outcome. `source` is recorded and
request-bound provenance, not a permission.

## Transition authority (§M8.4)

The later *Who may move a ticket* ruling in Scope A supersedes the forward-only
route model M8 originally referenced: authorized roles move work in either
direction. There is therefore no route table to export and no second policy
implementation to build. The service enforces the facts that still authorize
the write at the commit boundary — current server-bound principal, active
project membership with a write role, exact project/work-item/state scope — and
locks those rows before mutation. A caller does not choose or bypass those
checks.

## What the thin adapter replaces, and the migration path

**Today (the per-container helper model):** every agent container carries its
own board tooling — hand-kept curl recipes against raw REST, per-agent skill
files, memory notes with endpoint shapes — each drifting independently, each
re-deriving pagination, retry and identity handling, none with durable
operation identity (a lost response today means "retry and hope", which is how
double-writes happen).

**After:** one **thin adapter** — a small client speaking the operation
envelope (CLI first; M6's MCP server becomes a second front on the same door) —
distributed to containers as a versioned artifact. It owns exactly four
behaviors: persist the op key before calling, replay/query-by-key on unknown
results, exhaust cursors, and read back after writes. It contains **no policy**
— policy lives server-side (§M8.3, security invariant 3).

Migration, in order, each step shippable alone:

1. **Ledger + reads land**: `BoardOperation` model,
   outcome-by-key read, work-item reads with honest truncation. Nothing writes
   the ledger yet; helpers keep working untouched.
2. **The mutation door and adapter land** (`POST /ops/`) for ONE verb (state
   transition). The adapter persists the exact request before sending, queries
   the outcome before any retry, exhausts read cursors and performs scoped
   readback without carrying policy.
3. **Callers converge**: import calls `board.service.execute` in-process; M6
   fronts the HTTP door. The **negative direct-route inventory test** is landed
   (`tests/tools/board_writer_inventory.py` + `board_writers.json`,
   `test_board_writer_convergence.py`) — an executable census of every
   work-item state write, with each site carrying a status and a reason.

   > **THE BRIDGE (M2) IS NO LONGER A CONVERGENCE TARGET.** This step used to
   > name it first. BIP-67 deleted every board and state write the bridge had,
   > so it has nothing to route through the service — verified, not assumed:
   > there is no `issue.save()` and no state assignment anywhere under
   > `plane/bridge/`. A plan that still listed it would have sent someone
   > looking for a writer that no longer exists.

   **The v1 door governs the TRANSITION verb only**, so the census reports
   transition and create separately and the inventory converges the three real
   transition writers. Birth-state creates, fixtures and mirrors are excluded
   *with their reason recorded in the data*, because an exclusion nobody can
   see is indistinguishable from an oversight. The convergence assertion is a
   strict `xfail` while writers remain — naming the outstanding ones rather than
   failing the suite permanently, and turning RED when the last one lands so the
   marker cannot rot in place.

   It briefly went green when the three then-known writers converged. Rowan
   then executed the app issue route and demonstrated a live writer the census
   could not see, so the count is honest again at **one outstanding**
   (`app/serializers/issue.py`). A guard that goes green because its instrument
   is blind is worse than one that admits it is counting.
4. **Per-container helpers are deleted**, not deprecated: the adapter is the
   only client shape agents carry.

## Shipped vs owed (kept current per push)

- **Shipped on this branch:** the `BoardOperation` ledger and migration;
  outcome-by-key and complete work-item reads; and the `POST /ops/` transition
  door. Its canonical request binding, operation claim, locked membership and
  scope checks, domain mutation, store readback, durable outcome and audit
  intent are one transaction. The policy-free adapter persists requests before
  transport, implements query-before-retry/resume, exhausts cursors and offers
  scoped detail readback.
- **Also shipped:** the negative direct-route inventory — a census of every
  work-item state write (`tests/tools/board_writer_inventory.py`), the target
  set as data with a reason on every entry (`board_writers.json`), and the test
  that makes them agree (`test_board_writer_convergence.py`). Intake acceptance
  converges on the door on both surfaces.

- **The authority model for machine actions — RULED (John, 2026-08-16):**
  **attribution follows the decision.** An automated transition is attributed to
  the entity whose completed act triggered it — for auto-close, the last entity
  that finished work on the issue before it went stale, because the close is a
  *consequence* of their act and of no one else's. An auto-deterministic
  function earns no credit; it reacted to a decision someone made. The
  operation records `source=automation.auto_close`, so a ledger row can never
  be read as that person's manual click.

  This is honest where the previous attribution was not: auto-close used
  `project.created_by_id`, a person who in general never touched the issue.

  **Edge, per the same ruling:** if the triggering decider is no longer an
  active member with a write role when the automation fires, the transition is
  **refused and recorded** rather than attributed to anyone else. Their
  authority is what the close rested on and does not survive its loss — the
  BIP-67 precedent, that absent a verified actor you write nothing.

  Auto-close is converged on this basis.

  > **CLAIM CORRECTED (Rowan 3860, executed).** This said all three transition
  > writers route through the door and the guard is green. **It was true only
  > of what the census could see.** Rowan drove the live app issue route on the
  > farm: it accepts `state_id`, changes the stored state, and leaves zero
  > operation rows — a KNOWN instance of the declared `serializer.save()` blind
  > spot, live while the prose claimed completeness. The census now detects the
  > shape that makes it a writer (a writable serializer field bound to `state`),
  > the site is recorded as an outstanding target, and the convergence guard is
  > a countdown again. **A declared blind spot bounds a claim; it does not
  > licence leaving a known instance of it unscanned.**

- **Owed after that:** per-container helper deletion — fleet-side, not in this
  repository, and gated on the adapter being distributed first.
