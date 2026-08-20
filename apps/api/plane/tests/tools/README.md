# Static audit scanners

Tools used to justify findings and decisions, kept in the repo rather than
someone's `/tmp` so a result can be **re-derived and disputed** rather than
taken on trust — re-running a scanner from a fresh checkout and matching the
published counts is the standard to hold them to.

All scanners here over-report by design. Under-reporting is the direction
that gets someone hurt; every hit still needs a human read.

## BIP-18 — the write/rollback audit behind decision 008

Two static passes used to justify decision
[008](../../../../../docs/decisions/008-unsafe-request-transaction-boundary.md) —
"an unsafe token-API request that returns an error keeps no partial writes".

```bash
# inside the API container
python plane/tests/tools/audit_inventory.py
python plane/tests/tools/audit_classify.py

# or anywhere, pointing at a checkout
BIPLANE_VIEWS=apps/api/plane/api/views python audit_inventory.py
```

Both cover the DRF ViewSet action names (`create`, `update`, `partial_update`,
`destroy`) as well as `post/put/patch/delete`; omitting the action names once
silently dropped the Sticky and WorkspaceInvitations handlers, so that
coverage is pinned by fixture tests (`test_audit_scanner_coverage.py`).

**`audit_inventory.py`** — unsafe handlers containing both a write and a later
error return. Over-reports by design: line order does not prove reachability.

**`audit_classify.py`** — the question that actually matters. For each
caught-exception error path, which writes sit in the guarded `try` body ahead
of it. Those are the partial writes that exist today.

Neither proves reachability on a real execution path. The decision record says
which hand reads were done.

## BIP-27 — the CSRF GET/HEAD write scanner

```bash
# inside the API container
python plane/tests/tools/csrf_get_writes.py

# or against a plain checkout
BIPLANE_API_ROOT=apps/api python plane/tests/tools/csrf_get_writes.py
```

**`csrf_get_writes.py`** — do any GET/HEAD handlers write, or redirect into a
write? `SESSION_COOKIE_SAMESITE=Lax` blocks a cross-site POST but still sends
the cookie on a cross-site top-level GET, so a state-changing GET is the one
shape where a disabled CSRF check is directly exploitable cross-site.

It also prints its own **blind spots** — `dispatch()` overrides,
`http_method_names` re-declarations, decorators naming a safe method on a
differently-named function, and one whole surface it does not parse: URLconf
`as_view()` name-mapping (`as_view({"get": "subscription_status"})`), covered
only by a hand-enumerated list inside the tool, whose staleness is a FATAL.
Those are not findings; they are the places the scanner cannot see, so a
reader gets a bounded set to check by hand instead of an unbounded doubt. The
headline count is always a floor, never a census.
