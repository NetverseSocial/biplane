# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-37 §M8.3: nothing writes work-item state around the board service.

The design record says every writer converges on `plane.board.service`. That
sentence is not checkable, so this is the executable half: a census of every
state-write site, and a committed inventory (`board_writers.json`) giving each
one a status and a REASON. The two must agree.

Three guarantees, and they fail for different reasons on purpose:

  1. **No unrecorded writer.** Every site the census finds is in the inventory,
     keyed on `(file, line)` — not on the file set, which missed a new write
     added to an already-listed file (Vex, census review).
  2. **No stale inventory.** Every recorded site still exists in the census.
     With 1 this makes the two sets exactly equal, so a converged entry must
     record no lines at all rather than keeping ones that point at code which
     no longer writes.
  3. **Convergence itself** — every `converge` entry actually routes through
     the service. A strict `xfail` countdown while writers remain, turning red
     when the last one lands so the marker cannot rot in place. It went green
     briefly, then Rowan demonstrated a writer the census could not see; the
     count is honest again at one outstanding.

Plus positive controls: fixtures proving the census SEES each write shape. A
census that silently sees nothing would satisfy guarantee 1 perfectly.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

# parents[3] is the `plane` PACKAGE, not the api/ dir above it — a wrong root
# here makes the census EMPTY, which passes guarantee 1 vacuously and fails
# guarantee 2 for every entry. Caught by running these tests, not reading them.
PLANE = Path(__file__).resolve().parents[3]
TOOLS = PLANE / "tests" / "tools"
SCANNER = TOOLS / "board_writer_inventory.py"
INVENTORY = TOOLS / "board_writers.json"

# A site is converged when it calls the door. Match the FUNCTION, not an import
# idiom: the first version looked for "board.service", which `from plane.board
# import service as board_service` does not contain — so converged files kept
# counting as unconverged. Caught the moment real convergence landed.
SERVICE_CALL = "execute_transition"


def _census(root=None, want_json=True):
    env = {
        "BIPLANE_SCAN_ROOT": str(root or PLANE),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if want_json:
        env["BIPLANE_SCAN_JSON"] = "1"
    out = subprocess.run(
        [sys.executable, str(SCANNER)], env=env, capture_output=True, text=True, timeout=180
    ).stdout
    if not want_json:
        return out
    # The scanner emits ONLY JSON in machine mode, so this parses the whole
    # stream. An earlier version hunted for the first "[", which collided with
    # the bracketed evidence in `.bulk_update([...])` — caught by running.
    return json.loads(out)


def _inventory():
    return json.loads(INVENTORY.read_text())


def _work_item_rows(rows):
    """Census rows for the work-item verbs — `unrelated` is classified, not governed."""
    return [r for r in rows if r["verb"] in ("transition", "create")]


def _key(row):
    return row["file"]


def _census_sites():
    return {(r["file"], r["line"]) for r in _work_item_rows(_census())}


def _recorded_sites():
    return {(e["file"], line) for e in _inventory()["entries"] for line in e["lines"]}


def test_every_state_writer_is_recorded_in_the_inventory():
    """Guarantee 1: a new bypass cannot arrive quietly — PER SITE.

    Keyed on `(file, line)`, not on the file set. The inventory has always been
    site-granular (`db/models/issue.py` carries two entries with different
    reasons) while every guarantee compared file sets, so a FOURTH write added
    to an already-listed file stayed green: "a new bypass cannot arrive
    quietly" held only for files that write no state today, which is the easier
    half (Vex, census review).
    """
    unrecorded = sorted(_census_sites() - _recorded_sites())
    assert not unrecorded, (
        "these SITES write work-item state and are not recorded in "
        "board_writers.json — record each with a status and a reason:\n  "
        + "\n  ".join(f"{f}:{ln}" for f, ln in unrecorded)
    )


def _calls_the_door(source: str) -> bool:
    """A call that RESOLVES to `plane.board.service.execute_transition`.

    Two rounds of tightening, and the second is the one that matters:

    1. Substring matching was a false POSITIVE — a `# TODO: route through
       execute_transition` counted as converged (Vex).
    2. Matching the terminal callee NAME was still a false positive, one level
       down: a locally-defined `execute_transition`, or
       `unrelated_object.execute_transition()`, both certified convergence
       (Rowan, executed). Syntax is not identity. This resolves the BINDING —
       the module must actually import the door, and the call must go through
       that import.

    Guarantee 3 asserts the milestone, so every false positive here declares
    work done that is not.
    """
    tree = ast.parse(source)
    module_aliases = set()   # `from plane.board import service as X` -> X.execute_transition()
    direct_names = set()     # `from plane.board.service import execute_transition [as Y]` -> Y()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            if node.module == "plane.board" and alias.name == "service":
                module_aliases.add(alias.asname or alias.name)
            elif node.module == "plane.board.service" and alias.name == SERVICE_CALL:
                direct_names.add(alias.asname or alias.name)
            elif node.module == "plane" and alias.name == "board":
                module_aliases.add(f"{alias.asname or alias.name}.service")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == SERVICE_CALL:
            # The receiver must be an imported binding of the service module,
            # not any object that happens to expose the name.
            if isinstance(func.value, ast.Name) and func.value.id in module_aliases:
                return True
            if isinstance(func.value, ast.Attribute):
                if ast.unparse(func.value) in module_aliases:
                    return True
        elif isinstance(func, ast.Name) and func.id in direct_names:
            return True
    return False


def _converged(entry):
    return _calls_the_door((PLANE / entry["file"]).read_text())


def test_the_inventory_has_no_entries_for_code_that_no_longer_writes():
    """Guarantee 2: the record cannot rot into claims about code that has moved.

    With G1 this makes recorded sites and census sites EXACTLY equal, which
    removes the exemption the previous version needed. That exemption let a
    converged entry keep `lines` pointing at code that no longer writes state,
    and nothing would ever notice — both intake serializers were already
    carrying stale line numbers when Vex found it. A converged entry now simply
    has no sites to record, and that is enforced rather than trusted.
    """
    stale = sorted(_recorded_sites() - _census_sites())
    assert not stale, (
        "board_writers.json records these SITES as state writers, but the census "
        "no longer finds a write there. If the site converged, clear its `lines` "
        "(a converged entry writes nothing, so it points at nothing); otherwise "
        "the entry is stale or the scanner regressed:\n  "
        + "\n  ".join(f"{f}:{ln}" for f, ln in stale)
    )


def test_every_entry_states_a_reason():
    """An exclusion without a reason is an invisible hole wearing a status."""
    for entry in _inventory()["entries"]:
        assert entry.get("reason", "").strip(), f"{entry['file']} has a status but no reason"
        if entry["status"] == "excluded":
            assert entry.get("exclusion"), f"{entry['file']} is excluded without naming the kind"


@pytest.mark.xfail(
    strict=True,
    reason="one converge target remains (app/serializers/issue.py); flips to a hard failure when it lands",
)
def test_every_converge_target_routes_through_the_service():
    """Guarantee 3 — a COUNTDOWN again, and that is the honest state.

    It became a live green guard when the last known writer converged. Rowan
    then executed the app issue route and demonstrated a live state writer the
    census could not see — a KNOWN instance of the declared serializer.save()
    blind spot. The census now sees that shape, the site is recorded, and the
    marker is back on with one target outstanding.

    A guard that went green because its instrument was blind is worse than one
    that admits it is counting.


    This carried `xfail(strict=True)` while writers remained, precisely so it
    would turn RED the day the last one converged and force the marker off
    rather than letting it rot. That day is 2026-08-16: both intake serializers
    call the door, and auto-close converges too, under John's
    attribution-follows-the-decision ruling — attributed to the entity whose
    completed act triggered the close, never onto a borrowed principal. (An
    earlier revision of this docstring said auto-close was EXCLUDED pending
    that ruling; the ruling came and this head implements it. Stale predecessor
    claim, swept per Rowan 3860.)

    STILL OUTSTANDING: the app issue serializer's writable state field, which
    the primary UI update route saves through. Recorded, visible to the census,
    and not yet routed through the door.

    From here it is an ordinary guard: any `converge` entry that stops calling
    the door fails this test.
    """
    remaining = []
    for entry in _inventory()["entries"]:
        if entry["status"] != "converge":
            continue
        if not _converged(entry):
            remaining.append(f"{entry['file']} — {entry['reason'].split('.')[0]}")
    assert not remaining, "still writing state directly:\n  " + "\n  ".join(remaining)


# ── positive controls: the census must SEE each shape ────────────────────────
# Guarantee 1 is satisfied perfectly by a scanner that finds nothing, so each
# write shape gets a fixture proving it is visible. Without these the whole
# file could pass while measuring an empty set.

SHAPES = {
    "attribute assignment": "def f(issue, s):\n    issue.state = s\n",
    "state_id assignment": "def f(issue, s):\n    issue.state_id = s\n",
    "queryset update": "def f(qs, s):\n    qs.update(state_id=s)\n",
    "bulk_update": "def f(qs, objs):\n    qs.bulk_update(objs, ['state'], batch_size=10)\n",
    "manager create": "def f(s):\n    Issue.objects.create(name='x', state_id=s)\n",
    "direct constructor": "def f(s):\n    return Issue(name='x', state_id=s)\n",
    # Django's `fields` is positional-OR-KEYWORD. The keyword form was missed
    # while the positional control passed, because the control was written from
    # the implementation rather than the contract (Vex, census review).
    "bulk_update fields= kwarg": "def f(qs, objs):\n    qs.bulk_update(objs, fields=['state'])\n",
    # A tuple target binds state as plainly as a single one.
    "tuple target": "def f(issue, s, n):\n    issue.state, issue.name = s, n\n",
}


@pytest.mark.parametrize("label,source", sorted(SHAPES.items()))
def test_the_census_sees_every_write_shape(tmp_path, label, source):
    (tmp_path / "writer.py").write_text(source)
    rows = _work_item_rows(_census(root=tmp_path))
    assert rows, f"the census is blind to {label} — guarantee 1 would pass on an empty set"


def test_the_census_reaches_nested_packages(tmp_path):
    """The house's own recorded blind spot: a flat glob answers 'nothing here'
    when it means 'I did not look there', and the two are indistinguishable."""
    package = tmp_path / "deep" / "deeper"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "writer.py").write_text("def f(issue, s):\n    issue.state = s\n")
    assert _work_item_rows(_census(root=tmp_path)), "census traversal is flat again"


def test_non_work_item_state_kwargs_are_classified_not_dropped(tmp_path):
    """OAuth takes a CSRF `state=`. It must be visible and separate — a hit
    filtered out for readability is how a real writer disappears."""
    (tmp_path / "oauth.py").write_text("def f(s):\n    return GitHubOAuthProvider(state=s)\n")
    rows = _census(root=tmp_path)
    assert rows, "the unrelated hit was dropped entirely"
    assert all(r["verb"] == "unrelated" for r in rows), rows
    assert not _work_item_rows(rows), "a CSRF state= must not count as a work-item write"


def test_the_blind_spots_are_declared():
    """The bound on the claim has to ship with the claim."""
    spots = _inventory()["blind_spots"]
    assert len(spots) >= 5, "the declared blind spots have shrunk without the scanner improving"
    joined = " ".join(spots).lower()
    for shape in ("serializer", "kwargs", "setattr", "raw sql", "signal"):
        assert shape in joined, f"{shape} is no longer declared as a blind spot"


# ── the false-positive control on convergence ────────────────────────────────
# Guarantee 3 asserts the milestone, so a false POSITIVE there is the dangerous
# polarity: it declares work done that is not. Each fixture below MENTIONS the
# door without calling it, and each must read as unconverged.

NOT_CALLS = {
    "TODO comment": "def f(issue, s):\n    # TODO: route through execute_transition\n    issue.state = s\n",
    "docstring mention": 'def f(issue, s):\n    """Should use execute_transition."""\n    issue.state = s\n',
    "dead import": "from plane.board.service import execute_transition\n\n\ndef f(issue, s):\n    issue.state = s\n",
    "name in a string": "def f(issue, s):\n    log('execute_transition')\n    issue.state = s\n",
    # CALL-IDENTITY false positives (Rowan, executed): matching the terminal
    # callee name certified both of these as converged. Syntax is not identity.
    "local homonym": (
        "def execute_transition(**kw):\n    pass\n\n\n"
        "def f(issue, s):\n    execute_transition(principal=1, envelope=2)\n    issue.state = s\n"
    ),
    "unrelated object's method": "def f(o, issue, s):\n    o.execute_transition()\n    issue.state = s\n",
}


@pytest.mark.parametrize("label,source", sorted(NOT_CALLS.items()))
def test_mentioning_the_door_is_not_calling_it(label, source):
    assert not _calls_the_door(source), (
        f"{label} counts as converged — guarantee 3 would declare a direct writer done"
    )


def test_a_real_call_still_counts_however_it_is_imported():
    """Guard the guard: tightening must not make convergence undetectable."""
    attribute_call = (
        "from plane.board import service as board_service\n\n\n"
        "def f():\n    board_service.execute_transition(principal=p, envelope=e)\n"
    )
    bare_call = (
        "from plane.board.service import execute_transition\n\n\n"
        "def f():\n    execute_transition(principal=p, envelope=e)\n"
    )
    for source in (attribute_call, bare_call):
        assert _calls_the_door(source), source
