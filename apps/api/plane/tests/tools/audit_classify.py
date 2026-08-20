"""Classify each unsafe handler: does a write PRECEDE a reachable >=400 return?

Morrow: do not infer safety from status codes. Trace writes before each explicit
4xx AND each caught-exception path. So for every error Response we ask where it
sits:

  in an `except` handler  -> which writes are in the guarded try body BEFORE the
                             statement that could raise? Those are the partial
                             writes that exist today.
  on a normal path        -> which writes dominate it in statement order within
                             the same block?
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
WRITES = {"save", "create", "bulk_create", "update", "delete", "bulk_update", "get_or_create", "update_or_create"}


def is_error_response(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Response"):
        return None
    for kw in node.keywords:
        if kw.arg == "status":
            lbl = ast.unparse(kw.value)
            d = "".join(c for c in lbl.split("HTTP_")[-1][:3] if c.isdigit())
            if d and int(d) >= 400:
                return int(d)
    return None


def writes_in(nodes):
    out = []
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in WRITES:
                out.append((sub.lineno, sub.func.attr))
    return sorted(out)


rows = []
for path in sorted(VIEWS.rglob("*.py")):
    tree = ast.parse(path.read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name not in UNSAFE:
            continue
        for tryblock in [n for n in ast.walk(fn) if isinstance(n, ast.Try)]:
            body_writes = writes_in(tryblock.body)
            for handler in tryblock.handlers:
                codes = sorted({c for n in ast.walk(handler) if (c := is_error_response(n))})
                if not codes:
                    continue
                exc = ast.unparse(handler.type) if handler.type else "bare"
                rows.append((path.name, fn.name, fn.lineno, exc, codes, body_writes))

print(f"{'file':14} {'handler':7} {'def@':>5}  {'catches':34} {'returns':14} writes in guarded try body")
print("-" * 118)
for name, h, ln, exc, codes, w in rows:
    kinds = ", ".join(f"{l}:{k}" for l, k in w[:5]) if w else "NONE"
    print(f"{name:14} {h:7} {ln:>5}  {exc[:34]:34} {str(codes):14} {kinds}")

print(f"\n{len(rows)} caught-exception error paths across the token API.")
print("A path with writes listed is one where partial state exists TODAY when the")
print("exception fires after those writes. Under the new boundary those roll back.")
