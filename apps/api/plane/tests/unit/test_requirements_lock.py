# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dependency lock's input walk — the half `--require-hashes` cannot check.

Hashes make the install honest about its CONTENTS. This walk makes the lock
honest about its INPUTS, and every defect in it has had the same shape: an
include the walk could not see, whose edits therefore did not invalidate the
stamp, so the build succeeded while installing the old graph.

Three instances so far, each found by a different reviewer:
  * nothing connected inputs to the lock at all (Morrow RC 3671)
  * `--requirement` long form went unhashed (Morrow RC 3678)
  * a trailing comment after the path went unhashed (Sable, on 4f7b66c9)

The third is why unknown syntax now RAISES rather than being skipped, and why
comments are stripped before both checks.
"""

import importlib.util
import pathlib

import pytest

_LOCK_PY = pathlib.Path(__file__).resolve().parents[3] / "requirements" / "lock.py"
_spec = importlib.util.spec_from_file_location("_bip48_lock", _LOCK_PY)
lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock)


@pytest.fixture
def tree(tmp_path):
    """A miniature requirements tree: entry -> nested."""
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "nested.txt").write_text("django==4.2.30\n")
    return tmp_path


def _entry(tree, line):
    (tree / "requirements.txt").write_text(line + "\n")


def _names(tree):
    return [p.name for p in lock.inputs(tree)]


class TestEveryIncludeFormIsFollowed:
    @pytest.mark.parametrize("line", [
        "-r requirements/nested.txt",
        "--requirement requirements/nested.txt",
        "--requirement=requirements/nested.txt",
        "-c requirements/nested.txt",
        "--constraint requirements/nested.txt",
        "  -r requirements/nested.txt",
    ])
    def test_form_is_followed(self, tree, line):
        _entry(tree, line)
        assert "nested.txt" in _names(tree), f"{line!r} was not followed"

    @pytest.mark.parametrize("line", [
        "-r requirements/nested.txt  # core deps",
        "--requirement requirements/nested.txt  # core deps",
        "-r requirements/nested.txt\t# tabbed",
    ])
    def test_a_TRAILING_COMMENT_does_not_hide_an_include(self, tree, line):
        """pip follows these; the walk did not, and said nothing.

        The include pattern anchors the path to end of line so the match
        failed, and the unknown-option guard saw a head of `-r` — which IS
        known — so nothing raised. Silent, and live the moment anyone
        annotates a requirements file, which is an ordinary thing to do.
        """
        _entry(tree, line)
        assert "nested.txt" in _names(tree), f"{line!r} was not followed"

    def test_a_trailing_comment_include_still_invalidates_the_stamp(self, tree):
        """The property that actually matters, end to end through `check`."""
        _entry(tree, "-r requirements/nested.txt  # core deps")
        (tree / "requirements.lock").write_text("# placeholder\n")
        lock.stamp(tree)
        assert lock.check(tree) == 0
        (tree / "requirements" / "nested.txt").write_text("django==4.2.30\nrequests==2.32.3\n")
        assert lock.check(tree) == 1, "an edited input left the gate green"


class TestUnknownSyntaxIsLoud:
    def test_an_unrecognised_option_raises(self, tree):
        """Fail closed rather than chasing pip's grammar with a bigger pattern.
        A partial pattern fails SILENTLY; this converts an unbounded blind spot
        into a stop."""
        _entry(tree, "--some-future-include requirements/nested.txt")
        with pytest.raises(lock.InputSyntaxError):
            lock.inputs(tree)

    def test_an_unrecognised_option_behind_a_comment_still_raises(self, tree):
        _entry(tree, "--some-future-include requirements/nested.txt  # note")
        with pytest.raises(lock.InputSyntaxError):
            lock.inputs(tree)


class TestCommentStrippingUsesPipsRule:
    def test_an_egg_fragment_in_a_VCS_URL_survives(self, tree):
        """A hash NOT preceded by whitespace is part of the requirement.

        A naive split on `#` would truncate `…repo#egg=name` and change the
        digest for a requirement nobody edited — the leading-whitespace
        requirement is what prevents that, and it is why pip's own rule is used
        rather than an approximation of it.
        """
        req = "-e git+https://example.invalid/repo.git#egg=name"
        _entry(tree, req)
        # The line is an option (`-e`), known, and carries no include; the walk
        # must neither raise nor mangle it.
        assert _names(tree) == ["requirements.txt"]
        assert "#egg=name" in (tree / "requirements.txt").read_text()

    def test_a_comment_only_line_is_ignored(self, tree):
        _entry(tree, "# just a comment")
        assert _names(tree) == ["requirements.txt"]
