# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""M5 classification — the UNKNOWN-honesty rules as assertions.

The decision is a semantic-version comparison now (the 2026-08-12 redesign
retired the signed-chain walk), but the honesty invariants are unchanged and
these tests pin them: unreachable, malformed or INCOMPARABLE is UNKNOWN;
only exact equality is current; running ahead of latest is NOT current."""

import pytest

from plane.updates.classify import (
    STATE_CURRENT,
    STATE_UNKNOWN,
    STATE_UPDATE_AVAILABLE,
    classify,
    status_payload,
)


def _latest(tag="v1.2.0", level="code", changelog_url="https://github.com/x/releases/tag/v1.2.0"):
    return {"tag": tag, "level": level, "changelog_url": changelog_url}


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "  v1.2.3  ",
        "v01.2.3",
        "v١.٢.٣",
        "v1000000000.0.0",
    ],
)
def test_foreign_release_grammar_is_unknown_in_both_classifier_inputs(tag):
    """Classification consumes the release authority in both directions.

    These spellings were deliberately accepted by the retired local parser;
    accepting either side would let the banner offer an update that apply
    refuses, or call a foreign running release current.
    """
    latest_foreign = classify("v1.0.0", _latest(tag=tag))
    running_foreign = classify(tag, _latest(tag="v2.0.0"))
    assert latest_foreign.state == STATE_UNKNOWN
    assert running_foreign.state == STATE_UNKNOWN


@pytest.mark.parametrize(
    "tag",
    [
        "v1.2",  # two-part
        "v1.2.3.4",  # four-part
        "v1.2.3-rc1",  # prerelease suffix — not a stable ordering we invent
        "pi5-06bcb6f",  # a BUILD ID (what biplane_installed_build holds today)
        "06bcb6f",
        "latest",
        "",
        None,
        17,
    ],
)
def test_everything_else_is_incomparable_not_guessed(tag):
    """A foreign tag is UNKNOWN, never ordered by a fallback comparator."""
    assert classify("v1.0.0", _latest(tag=tag)).state == STATE_UNKNOWN


def test_comparison_is_numeric_not_lexicographic():
    assert classify("v1.9.0", _latest(tag="v1.10.0")).state == STATE_UPDATE_AVAILABLE
    assert classify("v1.99.99", _latest(tag="v2.0.0")).state == STATE_UPDATE_AVAILABLE


# --- classify ----------------------------------------------------------------


def test_no_source_answer_is_unknown():
    verdict = classify("v1.0.0", None)
    assert verdict.state == STATE_UNKNOWN
    assert "no release source" in verdict.reason


def test_non_semver_latest_is_unknown():
    verdict = classify("v1.0.0", _latest(tag="nightly-build-7"))
    assert verdict.state == STATE_UNKNOWN
    assert "not a semantic version" in verdict.reason


def test_unavailable_running_version_is_unknown_with_the_availability_reason():
    """M4's biplane_installed_build is NULL today (nothing populates it until
    the publish/apply pipeline exists). NULL means UNKNOWN, never "up to
    date" — and the reason names availability, not a missing setting,
    because nothing is expected to declare a version outside M4."""
    verdict = classify(None, _latest())
    assert verdict.state == STATE_UNKNOWN
    assert verdict.reason == "running version not available"
    # The latest release stays visible — the operator can still see what IS out.
    assert verdict.latest_release["tag"] == "v1.2.0"


def test_non_semver_running_version_is_unknown():
    """Today the field holds a commit-derived build id (e.g. pi5-06bcb6f) —
    incomparable, so honestly UNKNOWN until releases stamp real versions."""
    verdict = classify("pi5-06bcb6f", _latest())
    assert verdict.state == STATE_UNKNOWN
    assert "not a semantic version" in verdict.reason


def test_exact_equality_is_current():
    verdict = classify("v1.2.0", _latest(tag="v1.2.0"))
    assert verdict.state == STATE_CURRENT
    assert verdict.reason is None
    assert verdict.running_release == "v1.2.0"


def test_newer_latest_is_update_available():
    verdict = classify("v1.9.0", _latest(tag="v1.10.0"))
    assert verdict.state == STATE_UPDATE_AVAILABLE


def test_running_ahead_of_latest_is_unknown_not_current():
    """A dev build, an upstream rollback, or a misdeclared version — none of
    which is "current". Only exact equality earns silence."""
    verdict = classify("v2.0.0", _latest(tag="v1.2.0"))
    assert verdict.state == STATE_UNKNOWN
    assert "ahead of" in verdict.reason


# --- status payload ----------------------------------------------------------


def test_payload_carries_the_banner_contract():
    payload = status_payload(classify("v1.0.0", _latest(tag="v1.2.0")), "2026-08-12T00:00:00Z")
    assert payload["state"] == STATE_UPDATE_AVAILABLE
    assert payload["latest_release"] == {
        "tag": "v1.2.0",
        "level": "code",
        "changelog_url": "https://github.com/x/releases/tag/v1.2.0",
    }
    assert payload["checked_at"] == "2026-08-12T00:00:00Z"


def test_payload_with_no_check_yet_renders_unknown_not_current():
    payload = status_payload(classify(None, None), None)
    assert payload["state"] == STATE_UNKNOWN
    assert payload["checked_at"] is None
    assert payload["latest_release"] is None
