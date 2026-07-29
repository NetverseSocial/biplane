# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from plane.utils.workflow_template_validation import (
    MAX_TEMPLATE_STATES,
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

    # -- type-canonical cases (Sable/Morrow round 2: junk types must be clean
    # errors, never a TypeError 500) --

    def test_unhashable_group_is_clean_error_not_typeerror(self):
        bad = VALID + [{"name": "X", "group": {"a": 1}}]
        assert isinstance(_validate_states(bad), str)

    def test_non_string_group_scalars_rejected(self):
        for g in (7, True, None, ["started"]):
            assert isinstance(_validate_states(VALID + [{"name": "X", "group": g}]), str)

    def test_non_string_name_rejected(self):
        for n in ({"a": 1}, 123, None, ["Todo"]):
            assert isinstance(_validate_states(VALID + [{"name": n, "group": "started"}]), str)

    def test_non_string_color_rejected(self):
        bad = VALID + [{"name": "X", "group": "started", "color": {"x": 1}}]
        assert isinstance(_validate_states(bad), str)
        # absent/None color stays fine (normalize fills the fallback)
        assert _validate_states(VALID + [{"name": "X", "group": "started", "color": None}]) is None

    def test_over_long_color_rejected(self):
        bad = VALID + [{"name": "X", "group": "started", "color": "#" + "f" * 300}]
        err = _validate_states(bad)
        assert err is not None and "color" in err.lower()

    def test_non_bool_default_rejected(self):
        # Morrow RC 3017: JSON default:"false" is a truthy STRING — truthiness
        # normalization flipped it to a default. Must be a real boolean or 400.
        for d in ("false", "true", 1, 0, {"a": 1}):
            bad = [dict(VALID[0], default=d)] + VALID[1:]
            err = _validate_states(bad)
            assert err is not None and "default" in err.lower(), repr(d)
        # real booleans stay fine
        assert _validate_states([dict(VALID[0], default=True)] + VALID[1:]) is None
        assert _validate_states([dict(VALID[0], default=False)] + VALID[1:]) is None

    def test_state_count_cap(self):
        many = VALID + _states(*[(f"S{i}", "started") for i in range(MAX_TEMPLATE_STATES)])
        err = _validate_states(many)
        assert err is not None and "Too many" in err


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

    def test_normalize_default_is_strict_boolean(self):
        # A truthy non-bool that slipped past older validators must NOT become a default.
        states = [
            {"name": "Backlog", "group": "backlog"},
            {"name": "Todo", "group": "unstarted", "default": "false"},
        ]
        out = _normalize_states(states)
        defaults = [s["name"] for s in out if s.get("default")]
        assert defaults == ["Backlog"]  # fallback default, not the string-flagged one

    def test_normalize_collapses_multiple_defaults_to_first(self):
        # Sable round 2: two flagged defaults both survived — now only the first may.
        states = [
            {"name": "Backlog", "group": "backlog", "default": True},
            {"name": "Todo", "group": "unstarted", "default": True},
            {"name": "Done", "group": "completed", "default": True},
        ]
        out = _normalize_states(states)
        defaults = [s["name"] for s in out if s.get("default")]
        assert defaults == ["Backlog"]
