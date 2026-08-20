# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""AST gate: no unsafe token-API handler may dispatch async work directly.

BIP-18, Morrow's option-(a) ruling. The mutation boundary wraps every unsafe
request in one transaction, so an immediate Celery dispatch inside a handler
runs BEFORE commit — the worker races a row that may not exist yet, and a
rollback leaves a task for a mutation that never happened. Every dispatch
must go through `dispatch_after_commit` (post-commit, robust, eager args).

This is the executable form of the 43-site sweep: it fails NAMING each bare
`.delay(...)`, `.apply_async(...)` or `send_task(...)` inside any unsafe or
DRF-action handler under plane/api/views, so the invariant cannot rot when
the next handler is written.
"""

import ast
from pathlib import Path

VIEWS = Path(__file__).resolve().parents[3] / "api" / "views"

UNSAFE_HANDLERS = {
    "post", "put", "patch", "delete",
    "create", "update", "partial_update", "destroy",
}
BANNED_ATTRS = {"delay", "apply_async"}


def _offenders():
    found = []
    for path in sorted(VIEWS.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_action = any("action(" in ast.unparse(d) for d in fn.decorator_list)
            if fn.name not in UNSAFE_HANDLERS and not is_action:
                continue
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if isinstance(f, ast.Attribute) and f.attr in BANNED_ATTRS:
                    found.append(f"{path.name}:{sub.lineno} {ast.unparse(f)}(...)")
                if (isinstance(f, ast.Name) and f.id == "send_task") or (
                    isinstance(f, ast.Attribute) and f.attr == "send_task"
                ):
                    found.append(f"{path.name}:{sub.lineno} {ast.unparse(f)}(...)")
    return found


def test_views_directory_exists_so_silence_cannot_mean_wrong_path():
    # A gate scanning nothing reports the reassuring answer (the csrf scanner
    # lesson): pin the scan root before trusting the empty offender list.
    assert VIEWS.is_dir(), f"scan root missing: {VIEWS}"
    assert list(VIEWS.rglob("*.py")), "scan root contains no python files"


def test_no_unsafe_handler_dispatches_before_commit():
    offenders = _offenders()
    assert offenders == [], (
        "Bare async dispatch inside an unsafe/action handler — use "
        "dispatch_after_commit (post-commit, robust, eager args):\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# TASK-AWARE ROUTING (Morrow's BIP-18 ruling). Two doors, and each task family
# must use the right one:
#
#   issue_activity  -> enqueue_audit  (DURABLE: a row in the mutation's
#                      transaction, drained by the outbox worker)
#   everything else -> dispatch_after_commit  (post-commit, best-effort;
#                      webhook and functional tasks are deliberately NOT made
#                      durable by BIP-18 — see decision 008)
#
# Without this, a future audit call site could quietly go back to the lossy
# path, or a webhook could be pushed into the outbox and delayed by a minute.
# ---------------------------------------------------------------------------

AUDIT_TASKS = {"issue_activity"}
NON_AUDIT_DISPATCH = {
    "model_activity",
    "webhook_activity",
    "get_asset_object_metadata",
    "crawl_work_item_link_title",
}


def _routing_offenders():
    wrong = []
    for path in sorted(VIEWS.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            fname = call.func.id
            if fname == "dispatch_after_commit" and call.args:
                first = call.args[0]
                if isinstance(first, ast.Name) and first.id in AUDIT_TASKS:
                    wrong.append(
                        f"{path.name}:{call.lineno} audit task {first.id} must use enqueue_audit"
                    )
            if fname == "enqueue_audit" and call.args:
                first = call.args[0]
                name = first.value if isinstance(first, ast.Constant) else ast.unparse(first)
                if name not in AUDIT_TASKS:
                    wrong.append(
                        f"{path.name}:{call.lineno} enqueue_audit({name!r}) is not an allowlisted audit task"
                    )
    return wrong


def test_audit_tasks_go_through_the_outbox_and_others_do_not():
    offenders = _routing_offenders()
    assert offenders == [], (
        "Task routed through the wrong door (BIP-18):\n  " + "\n  ".join(offenders)
    )


def test_the_audit_task_family_is_actually_present():
    # Silence must not read as compliance: if nothing calls enqueue_audit at
    # all, the routing assertion above passes vacuously.
    calls = 0
    for path in sorted(VIEWS.rglob("*.py")):
        for call in ast.walk(ast.parse(path.read_text())):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "enqueue_audit":
                calls += 1
    assert calls == AUDIT_CALL_COUNT, f"expected the {AUDIT_CALL_COUNT} converted audit sites, found {calls}"


# ---------------------------------------------------------------------------
# THE APPROVED NON-AUDIT PARTITION (Morrow's BIP-18 ruling, RC 3226 bar 4).
#
# Routing to the right door is not enough on its own: it says each call uses a
# permitted mechanism, not that the set of best-effort calls is still the set
# that was actually reviewed and approved. Without a census, a new webhook or
# functional dispatch can be added indefinitely and every routing assertion
# still passes — the boundary would grow silently after the decision.
#
# These counts are AST-derived at this head, not transcribed. A grep census
# undercounted model_activity by 2 because those calls span lines, which is
# the same class of blind spot as the flat-glob scanners: a tool that cannot
# see a shape reports its absence as compliance.
# ---------------------------------------------------------------------------

NON_AUDIT_PARTITION = {
    "model_activity": 12,
    "get_asset_object_metadata": 5,
    "crawl_work_item_link_title": 2,
    "webhook_activity": 1,
}
AUDIT_CALL_COUNT = 23


def _dispatch_census():
    census = {}
    for path in sorted(VIEWS.rglob("*.py")):
        for call in ast.walk(ast.parse(path.read_text())):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id != "dispatch_after_commit" or not call.args:
                continue
            first = call.args[0]
            name = first.id if isinstance(first, ast.Name) else ast.unparse(first)
            census[name] = census.get(name, 0) + 1
    return census


def test_the_non_audit_partition_is_exactly_the_approved_shape():
    census = _dispatch_census()

    assert census == NON_AUDIT_PARTITION, (
        "The best-effort dispatch set no longer matches the partition approved "
        "in decision 008. Adding to it is a boundary change and needs a ruling, "
        "not a passing routing check.\n"
        f"  approved: {NON_AUDIT_PARTITION}\n"
        f"  found:    {census}"
    )


def test_the_partition_totals_are_pinned():
    # Stated separately so a failure says which invariant moved: the shape, or
    # the totals the decision record quotes.
    assert sum(_dispatch_census().values()) == 20
    assert sum(NON_AUDIT_PARTITION.values()) == 20


def test_every_partition_member_is_routable():
    # A member with a count of zero would pin nothing while looking enforced.
    assert set(NON_AUDIT_PARTITION) == NON_AUDIT_DISPATCH, (
        "the partition and the routing allowlist have drifted apart"
    )
    assert all(v > 0 for v in NON_AUDIT_PARTITION.values())
