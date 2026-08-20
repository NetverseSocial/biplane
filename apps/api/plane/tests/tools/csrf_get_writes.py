"""BIP-27: do any GET/HEAD handlers write, or redirect into a write?

Both reviewers made this the gate before the CSRF severity is final.
`SESSION_COOKIE_SAMESITE = 'Lax'` blocks a cross-site POST, but Lax **still
sends the cookie on a cross-site top-level GET**. So a state-changing GET is
the one shape where the disabled CSRF check is directly exploitable
cross-site — and the one that would make the downgrade wrong.

Scans the three surfaces that inherit `BaseSessionAuthentication`:
`plane/app/views`, `plane/license/api/views`, `plane/space/views`.

Over-reports on purpose. A write call anywhere inside a `get()` is reported
even if it sits on a branch that never runs for a plain read; under-reporting
is the direction that gets someone hurt. Every hit needs a human read.
"""

import ast
import os
from pathlib import Path

SURFACES = [
    "plane/app/views",
    "plane/license/api/views",
    "plane/space/views",
]
ROOT = Path(os.environ.get("BIPLANE_API_ROOT", "/code"))

# Morrow RC 3135: `get`/`head` alone omits 45 handlers. DRF ViewSets route safe
# requests to `list` and `retrieve` — GET /things/ and GET /things/{id}/ — and a
# router maps them without either name ever appearing. Missing those made the
# earlier "103 is a census" claim FALSE; the real figure is 148.
SAFE_METHODS = {"get", "head", "list", "retrieve"}
# Morrow (PR #24 second pass): five more handlers are reached ONLY through
# URLconf name-mapping — as_view({"get": "subscription_status"}) — a routing
# surface this scanner deliberately does not parse (URL-map discovery was
# ruled out; this list is the agreed alternative). Their names match neither
# get/head nor the ViewSet pair, so without this list they were silently
# absent AND invisible to blind_spots(), which walks view files only — the
# scanner printed "blind spots: 0" while omitting them. Enumerated BY HAND
# from the URLconfs. Their bodies are scanned like any other safe handler,
# but their DISCOVERY is manual: a new as_view() name added tomorrow will
# not appear here until a human adds it, which is why every run reports this
# list as a standing blind spot and why the headline count is a floor.
URLMAP_SAFE_HANDLERS = {
    "list_detail",
    "subscription_status",
    "summary",
    "retrieve_user_settings",
    "retrieve_instance_admin",
}
WRITES = {"save", "create", "bulk_create", "update", "delete", "bulk_update", "get_or_create", "update_or_create"}
# A redirect out of a GET can land on a state-changing route; both reviewers
# asked for "redirects into writes" specifically.
REDIRECTS = {"HttpResponseRedirect", "redirect", "HttpResponsePermanentRedirect"}


def scan(fn):
    writes, redirects = [], []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Attribute) and sub.func.attr in WRITES:
                writes.append((sub.lineno, sub.func.attr))
            if isinstance(sub.func, ast.Name) and sub.func.id in REDIRECTS:
                redirects.append((sub.lineno, sub.func.id))
    return writes, redirects


def blind_spots(tree, path):
    """Safe-method routing this scanner CANNOT see.

    The scan matches `get`/`head` by FUNCTION NAME. Anything that routes a safe
    request to a differently-named callable is invisible to it — not reported,
    simply absent. That makes the headline count a FLOOR, not a census, and it
    is the assumption to attack first when reviewing this tool.

    Rather than leave that as a caveat in prose, the shapes that could hide a
    safe-method handler are listed explicitly, so a reader gets a bounded set to
    check by hand instead of an unbounded doubt.
    """
    found = []
    for node in ast.walk(tree):
        # @action(methods=["get"]) — DRF router-dispatched, arbitrary name
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                src = ast.unparse(dec)
                low = src.lower()
                if ("get" in low or "head" in low) and any(
                    k in low for k in ("action(", "api_view(", "require_http_methods", "method_decorator")
                ):
                    if node.name not in SAFE_METHODS:
                        found.append((node.lineno, f"decorated {node.name}(): {src[:60]}"))
        # a dispatch() override can route a safe request anywhere
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "dispatch":
            found.append((node.lineno, "dispatch() override — routing not statically visible"))
        # http_method_names re-declares which methods a view answers
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "http_method_names":
                    found.append((node.lineno, f"http_method_names = {ast.unparse(node.value)[:50]}"))
    return found


hits = 0
scanned = 0
unscannable = []
missing = [s for s in SURFACES if not (ROOT / s).exists()]
if missing:
    # Morrow RC 3135: this used to print a warning per surface, then a clean
    # "0 handlers, no state-changing safe-method handler found" and exit 0.
    # A scan that examined NOTHING reported the reassuring answer.
    #
    # That is the exact failure this tool was rewritten to avoid one commit
    # earlier -- silence that reads as safety -- and I built it into the tool
    # itself. Every surface must be present, or the run is not a result.
    print("FATAL: cannot scan, so this run proves nothing.")
    for s in missing:
        print(f"  missing surface: {ROOT / s}")
    print(f"\nSet BIPLANE_API_ROOT to the apps/api directory (currently {ROOT}).")
    raise SystemExit(2)

urlmap_seen = set()
for surface in SURFACES:
    base = ROOT / surface
    for path in sorted(base.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for lineno, why in blind_spots(tree, path):
            unscannable.append((path.relative_to(ROOT), lineno, why))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            urlmapped = fn.name in URLMAP_SAFE_HANDLERS
            if fn.name not in SAFE_METHODS and not urlmapped:
                continue
            if urlmapped:
                urlmap_seen.add(fn.name)
            scanned += 1
            writes, redirects = scan(fn)
            if not writes and not redirects:
                continue
            hits += 1
            rel = path.relative_to(ROOT)
            tag = "  (URL-mapped, hand-enumerated)" if urlmapped else ""
            print(f"\n{rel}:{fn.lineno}  def {fn.name}(){tag}")
            for ln, kind in writes:
                print(f"    WRITE     {ln}: .{kind}()")
            for ln, kind in redirects:
                print(f"    REDIRECT  {ln}: {kind}()")

print(f"\n{scanned} safe-method handlers scanned across {len(SURFACES)} surfaces")
print(f"({len(urlmap_seen)}/{len(URLMAP_SAFE_HANDLERS)} of them URL-mapped, discovered by hand, not by this tool).")
print(f"{hits} contain a write or a redirect and need a human read.")
if hits == 0:
    print("No state-changing safe-method handler found: the Lax downgrade holds on this axis.")

# The hand-maintained list must not rot silently: a listed name that no longer
# exists means the URLconfs moved under it and the enumeration is stale.
stale = URLMAP_SAFE_HANDLERS - urlmap_seen
if stale:
    print(f"\nFATAL: hand-enumerated handler(s) not found in any surface: {sorted(stale)}.")
    print("The URLconf enumeration is STALE; re-derive it before trusting this run.")
    raise SystemExit(2)

print(f"\n--- BLIND SPOTS: {len(unscannable)} in-file places a safe-method handler could hide ---")
for rel, lineno, why in unscannable:
    print(f"  {rel}:{lineno}  {why}")
if unscannable:
    print()
print("These are NOT findings. They are routing shapes this scanner cannot follow,")
print("and one whole surface it does not parse at all: URLconf as_view() name-mapping,")
print(f"covered only by the hand-maintained {len(URLMAP_SAFE_HANDLERS)}-name list in this tool's source. So the count above is a")
print("FLOOR, never a census. Each blind spot needs a human read -- which is exactly")
print("the assumption to attack when reviewing this.")
