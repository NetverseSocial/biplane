"""BIP-18 source inventory: unsafe handlers that WRITE and then return >=400.

Rowan and Morrow both named this as required before the boundary is relied on
across all token handlers. The new rule is that an unsafe request returning an
error keeps no partial writes. Any handler that deliberately commits something
and then returns 4xx is an incompatibility to surface, not a reason to soften
the base.

This is a static scan, so it OVER-reports by design: it cannot tell whether a
write actually precedes the error return on a real execution path. Every hit
needs a human read. Under-reporting would be the dangerous direction.
"""

import ast
import os
from pathlib import Path

# Override with BIPLANE_VIEWS when not running inside the API container.
VIEWS = Path(os.environ.get("BIPLANE_VIEWS", "/code/plane/api/views"))
# DRF ViewSets route unsafe HTTP methods to create/update/partial_update/
# destroy without those names appearing (Morrow 10162: StickyViewSet and
# WorkspaceInvitationsViewset are reached exactly that way). Omitting them
# made the "every unsafe handler" claim underived.
UNSAFE = {"post", "put", "patch", "delete", "create", "update", "partial_update", "destroy"}

WRITE_CALLS = {"save", "create", "bulk_create", "update", "delete", "bulk_update", "get_or_create", "update_or_create"}


def write_calls(node):
    """Names of persistence calls anywhere inside this function."""
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in WRITE_CALLS:
                found.append((sub.lineno, sub.func.attr))
    return found


def error_returns(node):
    """(line, status) for every Response(...) returned with status >= 400."""
    out = []
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "Response"):
            continue
        for kw in sub.keywords:
            if kw.arg != "status":
                continue
            label = ast.unparse(kw.value)
            # status.HTTP_409_CONFLICT -> 409 ; a bare int -> itself
            digits = "".join(c for c in label.split("HTTP_")[-1][:3] if c.isdigit())
            if digits and int(digits) >= 400:
                out.append((sub.lineno, label))
            elif isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int) and kw.value.value >= 400:
                out.append((sub.lineno, str(kw.value.value)))
    return out


rows = []
for path in sorted(VIEWS.rglob("*.py")):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in UNSAFE:
            continue
        writes = write_calls(node)
        errors = error_returns(node)
        if not writes or not errors:
            continue
        # Only interesting when a write appears BEFORE an error return by line.
        first_write = min(w[0] for w in writes)
        later = [e for e in errors if e[0] > first_write]
        if later:
            rows.append((path.name, node.name, node.lineno, first_write, writes, later))

print(f"{'file':22} {'handler':8} {'def@':>6}  first write   error returns after it")
print("-" * 100)
for name, handler, lineno, fw, writes, later in rows:
    kinds = sorted({k for _, k in writes})
    codes = ", ".join(f"{ln}:{lbl.split('.')[-1]}" for ln, lbl in later[:4])
    print(f"{name:22} {handler:8} {lineno:>6}  {fw:>5} ({','.join(kinds)[:22]:22}) {codes}")

print()
print(f"{len(rows)} handlers need a human read.")
print("Static scan: over-reports by design. A write and an error return in the")
print("same function does not prove the write precedes the error on any real path.")

# ---------------------------------------------------------------------------
# The census (Morrow RC 3196): decision 008 and the tools README cite a
# 67-handler total. A record must not claim a number its committed tools do
# not derive, so the scanner EMITS it — per handler name, and summed — and
# the coverage tests pin the published figure to this output. A new handler
# changes this line, the pin, and the record together, on purpose.
# ---------------------------------------------------------------------------
census = {}
for path in sorted(VIEWS.rglob("*.py")):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in UNSAFE:
            census[node.name] = census.get(node.name, 0) + 1
print()
print(f"CENSUS: {sum(census.values())} unsafe handlers across {len(list(VIEWS.rglob('*.py')))} files")
for name in sorted(census):
    print(f"  {name}: {census[name]}")
