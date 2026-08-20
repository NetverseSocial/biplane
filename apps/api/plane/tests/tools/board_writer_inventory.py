"""BIP-37 §M8.3 census: code that writes work-item STATE around the board service.

The service (`plane.board.service`) is meant to be THE door for state
transitions — one transaction carrying mutation, outcome and audit. A writer
that reaches `Issue.state` without it produces a mutation with no ledger row,
which is the defect §M8 exists to remove. This scan is the executable half of
that claim: prose saying "everything converged" is not checkable; a census is.

VERBS ARE REPORTED SEPARATELY, because v1's door is the TRANSITION only:

    transition  an existing work item's state is reassigned
                (`issue.state = x`, `.update(state=...)`,
                 `.bulk_update([... "state" ...])`)
    create      a work item is created carrying an initial state
                (`Issue.objects.create(..., state_id=...)`)

A create is not a transition and v1 does not route it. Reporting them under one
label would have made the convergence target look larger than it is and hidden
which sites the door can actually accept today.

THIS SCAN OVER-REPORTS BY DESIGN, exactly as `audit_inventory.py` does: it
matches on attribute and keyword NAMES, and cannot prove the object involved is
an `Issue` on any real execution path. Every hit needs a human read. Under-
reporting is the dangerous direction.

WHAT IT CANNOT SEE — declared, because a scanner whose silence is ambiguous is
worse than none (the lesson `test_audit_scanner_coverage.py` already records):

  * serializer-driven writes — `serializer.save()` where the serializer sets
    state from validated_data. The state never appears at the call site.
  * `**kwargs` expansion — `Issue.objects.create(**payload)`.
  * `setattr(issue, "state", ...)` and any string-keyed attribute write.
  * raw SQL and `RawQuerySet`.
  * signals, and writes performed inside third-party or upstream Plane code
    that this repository does not own.
  * `import plane.board.service` (plain `Import`, not `ImportFrom`) is not
    walked by the convergence matcher, so a genuine call through that idiom
    reads as UNCONVERGED. Fail-safe direction, and declared rather than fixed
    (Vex 3863).
  * the convergence matcher trusts the import binding, so rebinding the name
    after import, or shadowing it locally, can produce a false positive.
    DELIBERATELY not fixed: scope tracking is real machinery against an
    implausible case, and this repository keeps correctly deciding against
    that build.

Those five are the standing blind spots. A site reached only through one of
them will not appear here, and the inventory records that risk rather than
implying the census is total.

Usage:  board_writer_inventory.py            (scans the API package)
        BIPLANE_SCAN_ROOT=<dir> board_writer_inventory.py
Exit code is always 0 — this is a census, not a gate. The gate is the test
that consumes it.
"""

import ast
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("BIPLANE_SCAN_ROOT", "/code/plane"))

# The service itself, its tests, and generated/vendored trees are not writers
# "around" the door — they are the door, or they are not shipped behaviour.
SKIP_PARTS = {"tests", "migrations", "__pycache__"}
# Only the TOP-LEVEL board package is the door itself. Matching any path
# component named `board` would silently exempt an unrelated future
# `<app>/board/` from the census (Vex, census review) — true of nothing today,
# and exactly the kind of exemption nobody would notice acquiring.
SKIP_ROOTS = ("board",)

STATE_ATTRS = {"state", "state_id"}
# Constructors of these carry WORK-ITEM state. Anything else taking a `state=`
# kwarg is reported under `unrelated` rather than removed — see `_hits_in`.
# A new work-item model added here-but-not-there is reported as unrelated, not
# hidden: the census still shows it, which is the recoverable failure.
WORK_ITEM_MODELS = {"Issue", "Draft", "DraftIssue", "IssueVersion"}
BULK_UPDATE = "bulk_update"
CREATE_CALLS = {"create", "bulk_create", "get_or_create", "update_or_create"}


def _skipped(path: Path) -> bool:
    if path.parts and path.parts[0] in SKIP_ROOTS:
        return True
    return any(part in SKIP_PARTS for part in path.parts)


def _hits_in(tree, rel):
    """Every state-write site in one module, as (line, verb, evidence)."""
    out = []
    for node in ast.walk(tree):
        # issue.state = x  /  issue.state_id = x
        if isinstance(node, ast.Assign):
            # Descend into tuple/list targets: `issue.state, issue.name = s, n`
            # binds state just as plainly as a single target, and the first
            # version only looked at top-level targets (Vex, census review).
            def _targets(t):
                if isinstance(t, (ast.Tuple, ast.List)):
                    for element in t.elts:
                        yield from _targets(element)
                else:
                    yield t

            for target in node.targets:
                for leaf in _targets(target):
                    if isinstance(leaf, ast.Attribute) and leaf.attr in STATE_ATTRS:
                        out.append((node.lineno, "transition", f"{ast.unparse(leaf)} = ..."))
        # queryset calls carrying state
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            touched = kwargs & STATE_ATTRS
            if name == "update" and touched:
                out.append((node.lineno, "transition", f".update({', '.join(sorted(touched))}=...)"))
            elif name in CREATE_CALLS and touched:
                out.append((node.lineno, "create", f".{name}({', '.join(sorted(touched))}=...)"))
            elif name == BULK_UPDATE:
                # Django's `fields` is POSITIONAL-OR-KEYWORD. The first version
                # iterated node.args only and asserted "the field list is
                # positional" as fact, so `bulk_update(objs, fields=['state'])`
                # was invisible — and the positive control used the positional
                # form, so it confirmed the handled shape rather than the
                # contract (Vex, census review). Both forms now count.
                candidates = list(node.args) + [kw.value for kw in node.keywords if kw.arg == "fields"]
                for arg in candidates:
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        fields = {e.value for e in arg.elts if isinstance(e, ast.Constant)}
                        if fields & STATE_ATTRS:
                            out.append((node.lineno, "transition", ".bulk_update([... 'state' ...])"))
        # WRITABLE SERIALIZER FIELD BOUND TO STATE (Rowan 3860). The declared
        # blind spot is `serializer.save()`, where the state never appears at
        # the call site — but the FIELD DECLARATION does appear, and it is the
        # thing that makes the route a state writer at all. Rowan executed the
        # live app route: it accepted `state_id`, changed the stored state, and
        # left zero operation rows, while every census test passed.
        #
        # A declared blind spot is a bound on the claim, not a licence to leave
        # a KNOWN instance of it unscanned. This does not make `save()` visible
        # in general; it makes THIS shape visible, which is the one with a
        # demonstrated bypass behind it.
        if isinstance(node, ast.Assign):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.value.__class__ is ast.Name and getattr(value.func.value, "id", "") == "serializers":
                    kwargs = {kw.arg: kw for kw in value.keywords if kw.arg}
                    source = kwargs.get("source")
                    src_val = source.value if source is not None else None
                    bound_to_state = (
                        isinstance(src_val, ast.Constant) and src_val.value in STATE_ATTRS
                    )
                    read_only = kwargs.get("read_only")
                    is_read_only = (
                        read_only is not None
                        and isinstance(read_only.value, ast.Constant)
                        and read_only.value.value is True
                    )
                    if bound_to_state and not is_read_only:
                        out.append(
                            (node.lineno, "transition", "writable serializer field source='state'")
                        )

        # DIRECT MODEL CONSTRUCTOR: `Issue(..., state_id=x)` — no attribute
        # call, so the branch above cannot see it. Found while running this
        # scanner against the real tree: workspace_seed_task builds rows this
        # way and was silently absent from the census. Under-reporting is the
        # direction that makes a scan worse than useless, so a capitalised
        # callable carrying a state kwarg counts, and the over-report on any
        # unrelated class named like a model is accepted.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id[:1].isupper():
                touched = {kw.arg for kw in node.keywords if kw.arg} & STATE_ATTRS
                if touched:
                    # `state` is not a word this codebase reserves: the OAuth
                    # providers take a CSRF `state=`, which has nothing to do
                    # with a work item. Those are CLASSIFIED, never dropped —
                    # a silently discarded hit is how a real writer disappears
                    # behind a filter someone wrote for readability.
                    verb = "create" if node.func.id in WORK_ITEM_MODELS else "unrelated"
                    out.append(
                        (node.lineno, verb, f"{node.func.id}({', '.join(sorted(touched))}=...)")
                    )
    return out


def _meta_declared_state_fields(tree):
    """ModelSerializer classes whose Meta exposes state WITHOUT marking it read-only.

    The declared-field branch above catches `serializers.X(source="state")` —
    the form Rowan's executed bypass happens to take. DRF's commoner idiom has
    no call to match at all: a ModelSerializer whose `Meta.fields` lists
    `state`/`state_id` generates a writable field straight from the model
    (Vex, review 3863). Scanning one form of a two-form shape is how "one
    outstanding" becomes a number backed by half a scan.

    Over-reports on purpose: `fields = "__all__"` and any read_only_fields this
    cannot resolve statically are reported, because under-reporting is the
    direction that makes the census worse than useless.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for meta in node.body:
            if not (isinstance(meta, ast.ClassDef) and meta.name == "Meta"):
                continue
            declared, read_only, ro_is_fields, model = None, set(), False, None
            for stmt in meta.body:
                if not (isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)):
                    continue
                name, value = stmt.targets[0].id, stmt.value
                if name == "fields":
                    if isinstance(value, ast.Constant) and value.value == "__all__":
                        declared = {"__all__"}
                    elif isinstance(value, (ast.List, ast.Tuple)):
                        declared = {e.value for e in value.elts if isinstance(e, ast.Constant)}
                elif name == "model":
                    model = value.id if isinstance(value, ast.Name) else None
                elif name == "read_only_fields":
                    if isinstance(value, ast.Name) and value.id == "fields":
                        ro_is_fields = True   # read_only_fields = fields
                    elif isinstance(value, (ast.List, ast.Tuple)):
                        read_only = {e.value for e in value.elts if isinstance(e, ast.Constant)}
            # GATED ON THE MODEL. Without this, `fields = "__all__"` matched
            # every serializer in the codebase — 114 hits, ~100 of them for
            # webhooks, workspaces and user profiles that have no issue state
            # at all. That is not safe over-reporting; it is noise that would
            # make the census unreadable, which is the same failure as the
            # bucket labelled "ignore me". A serializer only exposes work-item
            # state if its Meta model is a work item.
            if declared is None or model not in WORK_ITEM_MODELS:
                continue
            exposed = declared & (STATE_ATTRS | {"__all__"})
            if exposed and not ro_is_fields and not (exposed & read_only):
                out.append(
                    (meta.lineno, "transition", f"{node.name}.Meta exposes {sorted(exposed)} writable")
                )
    return out


def scan(root: Path):
    rows = []
    files = 0
    for path in sorted(root.rglob("*.py")):
        if _skipped(path.relative_to(root)):
            continue
        files += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(root))
        for line, verb, evidence in _hits_in(tree, rel) + _meta_declared_state_fields(tree):
            rows.append({"file": rel, "line": line, "verb": verb, "evidence": evidence})
    return rows, files


def main():
    rows, files = scan(ROOT)
    if os.environ.get("BIPLANE_SCAN_JSON"):
        # ONLY the JSON. The human report contains bracketed evidence like
        # `.bulk_update([... 'state' ...])`, so any "find the first [" parse on
        # mixed output picks up the wrong bracket — and only when a bulk_update
        # happens to exist, which is the kind of intermittent that survives
        # review. Machine mode is machine-only.
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for row in rows:
        print(f"{row['verb']:11} {row['file']}:{row['line']}  {row['evidence']}")
    transitions = sum(1 for r in rows if r["verb"] == "transition")
    creates = sum(1 for r in rows if r["verb"] == "create")
    unrelated = sum(1 for r in rows if r["verb"] == "unrelated")
    print(
        f"\nCENSUS: {transitions + creates} work-item state-write sites "
        f"({transitions} transition, {creates} create) across {files} files"
    )
    print(
        f"ALSO SEEN: {unrelated} constructor `state=` sites whose class is not in "
        f"WORK_ITEM_MODELS — mostly OAuth CSRF, but READ THEM: a new work-item "
        f"model missing from that set lands here too, and this bucket must not "
        f"be a place things go to be ignored (Vex, census review)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
