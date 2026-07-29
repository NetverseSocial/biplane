# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from plane.app.views.workspace.workflow_template import (
    STATE_NAME_MAX_LENGTH,
    _normalize_states,
    _validate_states,
)


def _states(*pairs):
    return [{"name": n, "group": g} for n, g in pairs]

VALID = _states(
    ("Backlog", "backlog"),
    ("Todo", "unstarted"),
    ("In Progress", "started"),
    ("Done", "completed"),
    ("Cancelled", "cancelled"),
)


@pytest.mark.unit
class TestValidateStates:
    def test_valid_minimal_workflow_passes(self):
        assert _validate_states(VALID) is None

    def test_empty_or_non_list_rejected(self):
        assert _validate_states([]) is not None
        assert _validate_states(None) is not None
        assert _validate_states("nope") is not None

    def test_missing_required_group_rejected(self):
        err = _validate_states(VALID[:-1])  # drop cancelled
        assert err is not None and "cancelled" in err

    def test_duplicate_state_name_rejected(self):
        # State has a unique (name, project) constraint — a template with duplicate
        # names would blow up every project created from it.
        dup = VALID + _states(("Todo", "started"))
        err = _validate_states(dup)
        assert err is not None and "Duplicate" in err

    def test_duplicate_name_detected_case_insensitively(self):
        dup = VALID + _states(("todo", "started"))
        assert _validate_states(dup) is not None

    def test_whitespace_only_name_rejected(self):
        bad = VALID + _states(("   ", "started"))
        assert _validate_states(bad) is not None

    def test_over_long_name_rejected(self):
        bad = VALID + _states(("x" * (STATE_NAME_MAX_LENGTH + 1), "started"))
        err = _validate_states(bad)
        assert err is not None and "too long" in err

    def test_unknown_group_rejected(self):
        bad = VALID + _states(("Weird", "not_a_real_group"))
        err = _validate_states(bad)
        assert err is not None and "not_a_real_group" in err

    def test_triage_is_a_valid_group(self):
        assert _validate_states(VALID + _states(("Triage", "triage"))) is None


@pytest.mark.unit
class TestNormalizeStates:
    def test_normalize_strips_names_and_sequences(self):
        out = _normalize_states(_states(("  Backlog  ", "backlog"), ("Todo", "unstarted")))
        assert out[0]["name"] == "Backlog"
        assert out[0]["sequence"] < out[1]["sequence"]

    def test_normalize_guarantees_exactly_one_default(self):
        out = _normalize_states(VALID)
        assert sum(1 for s in out if s.get("default")) == 1
        # first backlog state gets the default
        assert next(s for s in out if s.get("default"))["group"] == "backlog"
