# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Pin the audit scanners' handler coverage (Morrow 10162, blocking 2).

The scanners once accepted only post/put/patch/delete function names, so the
DRF ViewSet actions (create/update/partial_update/destroy) — reached through
unsafe HTTP methods with neither name appearing — were silently outside the
census while the decision record claimed every unsafe handler. These fixtures
make that omission a red test instead of a recurring blind spot: each writes a
view module whose ONLY unsafe handler uses an action name, runs the real
scanner against it, and requires the hit to be reported.
"""

import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[3] / "tests" / "tools"

CREATE_FIXTURE = '''
from rest_framework.response import Response

class StickyLikeViewSet:
    def create(self, request):
        Thing.objects.create(name="x")
        return Response({"error": "no"}, status=400)
'''

PARTIAL_UPDATE_FIXTURE = '''
from rest_framework.response import Response

class StickyLikeViewSet:
    def partial_update(self, request, pk):
        thing.save()
        return Response({"error": "no"}, status=409)
'''


def _run_scanner(tool, views_dir):
    return subprocess.run(
        [sys.executable, str(TOOLS / tool)],
        env={"BIPLANE_VIEWS": str(views_dir), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout


@pytest.mark.parametrize(
    "fixture,action",
    [(CREATE_FIXTURE, "create"), (PARTIAL_UPDATE_FIXTURE, "partial_update")],
)
def test_inventory_reports_viewset_action_handlers(tmp_path, fixture, action):
    (tmp_path / "fixture_views.py").write_text(fixture)
    out = _run_scanner("audit_inventory.py", tmp_path)
    assert action in out, f"inventory no longer sees {action}() handlers:\n{out}"


def test_inventory_still_reports_plain_post_handlers(tmp_path):
    # Guard the guard: extending the set must not have narrowed it.
    (tmp_path / "fixture_views.py").write_text(
        "from rest_framework.response import Response\n"
        "class V:\n"
        "    def post(self, request):\n"
        '        Thing.objects.create(name="x")\n'
        '        return Response({"error": "no"}, status=400)\n'
    )
    out = _run_scanner("audit_inventory.py", tmp_path)
    assert "post" in out


def test_classify_traces_viewset_action_exception_paths(tmp_path):
    (tmp_path / "fixture_views.py").write_text(
        "from rest_framework.response import Response\n"
        "class V:\n"
        "    def partial_update(self, request, pk):\n"
        "        try:\n"
        "            thing.save()\n"
        "            other.save()\n"
        "        except IntegrityError:\n"
        '            return Response({"error": "no"}, status=409)\n'
        "        return Response({}, status=200)\n"
    )
    out = _run_scanner("audit_classify.py", tmp_path)
    assert "partial_update" in out, f"classify no longer sees action handlers:\n{out}"


def test_the_scanner_emits_the_census_the_record_cites():
    # Morrow RC 3196: decision 008 and the tools README publish a 67-handler
    # census attributed to these scanners. This pins that the committed tool
    # DERIVES that number from the real views directory — if a handler is
    # added or removed, this test, the scanner output, and the record must
    # move together.
    views = Path(__file__).resolve().parents[3] / "api" / "views"
    out = subprocess.run(
        [sys.executable, str(TOOLS / "audit_inventory.py")],
        env={"BIPLANE_VIEWS": str(views), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    assert "CENSUS: 67 unsafe handlers" in out, f"census line missing or drifted:\n{out[-400:]}"


NESTED_FIXTURE = '''
from rest_framework.response import Response

class NestedViewSet:
    def create(self, request):
        Thing.objects.create(name="nested")
        return Response({"error": "no"}, status=400)
'''


NESTED_GUARDED_FIXTURE = '''
from rest_framework.response import Response

class NestedGuardedViewSet:
    def create(self, request):
        try:
            Thing.objects.create(name="nested")
            return Response({"ok": True}, status=201)
        except ValueError:
            return Response({"error": "no"}, status=400)
'''


@pytest.mark.parametrize(
    "tool,fixture",
    [
        # Each tool needs a fixture matching what it actually looks for.
        # inventory is a handler census; classify reports caught-exception
        # error paths, so a handler with no try body is correctly invisible
        # to it and would prove nothing about traversal. (Caught on the farm:
        # my first version asserted classify saw a handler it had no reason
        # to report, which would have been a test failing for the wrong
        # reason rather than a real guard.)
        ("audit_inventory.py", NESTED_FIXTURE),
        ("audit_classify.py", NESTED_GUARDED_FIXTURE),
    ],
)
def test_scanner_traverses_subdirectories(tmp_path, tool, fixture):
    """Rowan/Sia, 2026-08-10: both scanners globbed "*.py" NON-recursively.

    That is correct on the flat api/views default, which is why the 67-handler
    token census stands. But the advertised BIPLANE_VIEWS override pointed at a
    PACKAGE tree reported a fraction while looking complete: app/views gave 3
    unsafe handlers flat against 158 recursively — a 155-handler blind spot,
    reported as a clean result.

    A scanner that answers "nothing there" when it means "I cannot see there"
    is worse than no scanner, because the silence is indistinguishable from a
    pass. This fixture puts the only handler one directory down, so a flat
    traversal returns the reassuring answer and fails.
    """
    package = tmp_path / "nested_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "nested_views.py").write_text(fixture)

    out = _run_scanner(tool, tmp_path)

    assert "create" in out, (
        f"{tool} did not see a handler one directory down — traversal is flat again:\n{out}"
    )


def test_inventory_census_counts_nested_files(tmp_path):
    """The census line must count what it actually walked."""
    (tmp_path / "flat_views.py").write_text(CREATE_FIXTURE)
    package = tmp_path / "deep" / "deeper"
    package.mkdir(parents=True)
    (package / "nested_views.py").write_text(NESTED_FIXTURE)

    out = _run_scanner("audit_inventory.py", tmp_path)

    census = [line for line in out.splitlines() if line.startswith("CENSUS:")]
    assert census, f"no CENSUS line in output:\n{out}"
    assert "across 2 files" in census[0], (
        f"census undercounts nested files, so its total cannot be trusted: {census[0]}"
    )
