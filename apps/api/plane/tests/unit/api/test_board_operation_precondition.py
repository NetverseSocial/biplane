# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The optional expected-principal and the locked precondition (Rowan 12125).

Two facts that fail differently and must not share a name:

  * `expected_principal_id` is IDENTITY INTENT. It has content only where the
    stated identity and the authenticated one have independent origins — the
    HTTP adapter. In-process callers pass a trusted principal, so requiring it
    there would compare a value with itself. Optional here; compared when sent.
  * the precondition is ATTRIBUTION FRESHNESS. Auto-close decides on a
    staleness measurement taken before the transition, and the issue can move
    in between. Both halves are required: identity alone misses a second edit
    by the SAME person (decider equal, clock advanced), and freshness alone
    would credit someone who did not perform the act.

Ordering is pinned too: exact replay is evaluated BEFORE the precondition, so a
committed retry still returns its stored outcome even though the row has since
advanced. Without that, a successful operation would start failing on retry.
"""

import uuid as uuid_lib

import pytest

from plane.board.service import (
    BoardOperationPermissionDenied,
    BoardOperationPreconditionFailed,
    execute_transition,
)
from plane.db.models import Issue, Project, ProjectMember, State, User, Workspace, WorkspaceMember


def _fixture():
    suffix = uuid_lib.uuid4().hex[:8]
    user = User.objects.create(
        email=f"pre-{suffix}@example.com", username=uuid_lib.uuid4().hex[:16]
    )
    workspace = Workspace.objects.create(slug=f"pre-{suffix}", name="Precondition", owner=user)
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=20)
    project = Project.objects.create(
        workspace=workspace, name="Precondition project", identifier=f"P{suffix[:5].upper()}"
    )
    ProjectMember.objects.create(
        workspace=workspace, project=project, member=user, role=20, is_active=True
    )
    start = State.objects.create(
        workspace=workspace, project=project, name="Todo", color="#60646C",
        group="unstarted", default=True,
    )
    target = State.objects.create(
        workspace=workspace, project=project, name="Done", color="#60646C", group="completed",
    )
    issue = Issue.objects.create(
        workspace=workspace, project=project, state=start, name="stale-check",
        description_html="<p>x</p>",
    )
    return user, workspace, project, issue, target


def _envelope(user, workspace, project, issue, target, *, op_key, expected=None, precondition=None):
    envelope = {
        "op_key": op_key,
        "source": "test",
        "verb": "transition",
        "workspace": workspace.slug,
        "project": project.identifier,
        "payload": {"sequence_id": issue.sequence_id, "state_id": str(target.id)},
    }
    if expected is not None:
        envelope["expected_principal_id"] = expected
    if precondition is not None:
        envelope["precondition"] = precondition
    return envelope


@pytest.mark.django_db(transaction=True)
class TestExpectedPrincipalIsOptional:
    def test_an_in_process_caller_may_omit_it(self):
        """The whole point of making it optional: no manufactured tautology."""
        user, ws, project, issue, target = _fixture()
        result = execute_transition(
            principal=user,
            envelope=_envelope(user, ws, project, issue, target, op_key="omit-1"),
        )
        assert result.replayed is False
        issue.refresh_from_db()
        assert issue.state_id == target.id

    def test_it_still_refuses_a_mismatch_when_sent(self):
        """Optional must not mean ignored — the HTTP adapter relies on this."""
        user, ws, project, issue, target = _fixture()
        stranger = uuid_lib.uuid4()
        with pytest.raises(BoardOperationPermissionDenied):
            execute_transition(
                principal=user,
                envelope=_envelope(
                    user, ws, project, issue, target, op_key="mismatch-1", expected=str(stranger)
                ),
            )
        issue.refresh_from_db()
        assert issue.state_id != target.id, "a refused mismatch must not transition"


@pytest.mark.django_db(transaction=True)
class TestTheLockedPrecondition:
    def test_a_matching_premise_proceeds(self):
        user, ws, project, issue, target = _fixture()
        result = execute_transition(
            principal=user,
            envelope=_envelope(
                user, ws, project, issue, target, op_key="fresh-1",
                precondition={
                    "issue_updated_at": issue.updated_at.isoformat(),
                    "decider_id": str(issue.updated_by_id or issue.created_by_id),
                },
            ),
        )
        assert result.replayed is False
        issue.refresh_from_db()
        assert issue.state_id == target.id

    def test_a_stale_updated_at_refuses_before_any_mutation(self):
        """The premise expired: the issue moved after the sweep measured it."""
        user, ws, project, issue, target = _fixture()
        before = issue.state_id
        with pytest.raises(BoardOperationPreconditionFailed):
            execute_transition(
                principal=user,
                envelope=_envelope(
                    user, ws, project, issue, target, op_key="stale-1",
                    precondition={
                        "issue_updated_at": "2020-01-01T00:00:00+00:00",
                        "decider_id": str(issue.created_by_id),
                    },
                ),
            )
        issue.refresh_from_db()
        assert issue.state_id == before, "a stale premise must not transition"

    def test_a_changed_decider_refuses_even_when_fresh(self):
        """Freshness alone would credit someone who did not perform the act."""
        user, ws, project, issue, target = _fixture()
        before = issue.state_id
        with pytest.raises(BoardOperationPreconditionFailed):
            execute_transition(
                principal=user,
                envelope=_envelope(
                    user, ws, project, issue, target, op_key="decider-1",
                    precondition={
                        "issue_updated_at": issue.updated_at.isoformat(),
                        "decider_id": str(uuid_lib.uuid4()),
                    },
                ),
            )
        issue.refresh_from_db()
        assert issue.state_id == before

    def test_identity_alone_would_miss_a_second_edit_by_the_same_person(self):
        """Rowan's sharpest point, as an executable case.

        The decider is unchanged — the SAME person edited again — so a check
        comparing only identity would pass while the staleness clock has moved.
        The pair catches it; either half alone does not.
        """
        user, ws, project, issue, target = _fixture()
        # A REAL decider, seeded (Rowan 3868). The first version created an
        # issue with no created_by or updated_by, measured `decider` as None,
        # and then asserted `... == decider or True` — unconditionally green.
        # It carried the name of the race that motivated the two-field pair and
        # established nothing about it.
        issue.updated_by = user
        issue.save(disable_auto_set_user=True)
        issue.refresh_from_db()
        stale_stamp = issue.updated_at.isoformat()
        decider = str(issue.updated_by_id)
        assert decider == str(user.id), "the decider must be a real participant, not None"

        # The SAME participant edits again: identity holds, the clock moves.
        issue.name = "edited again by the same person"
        issue.updated_by = user
        issue.save(disable_auto_set_user=True)
        issue.refresh_from_db()
        assert str(issue.updated_by_id) == decider, "the decider must be unchanged"
        assert issue.updated_at.isoformat() != stale_stamp, "the clock must have moved"

        before = issue.state_id
        with pytest.raises(BoardOperationPreconditionFailed):
            execute_transition(
                principal=user,
                envelope=_envelope(
                    user, ws, project, issue, target, op_key="second-edit-1",
                    precondition={"issue_updated_at": stale_stamp, "decider_id": decider},
                ),
            )
        issue.refresh_from_db()
        assert issue.state_id == before


@pytest.mark.django_db(transaction=True)
class TestReplayIsEvaluatedBeforeThePrecondition:
    def test_a_committed_retry_replays_even_though_the_row_advanced(self):
        """Rowan's ordering requirement, and it is not obvious.

        The successful transition MOVES `updated_at` — so the premise the
        caller sent is, by the time it retries, necessarily stale. If the
        precondition were checked first, every successful operation would fail
        on retry: the caller could never learn its own outcome. Replay first
        makes the retry return the stored outcome instead.
        """
        user, ws, project, issue, target = _fixture()
        premise = {
            "issue_updated_at": issue.updated_at.isoformat(),
            "decider_id": str(issue.updated_by_id or issue.created_by_id),
        }
        envelope = _envelope(
            user, ws, project, issue, target, op_key="replay-1", precondition=premise
        )
        first = execute_transition(principal=user, envelope=envelope)
        assert first.replayed is False

        issue.refresh_from_db()
        assert issue.updated_at.isoformat() != premise["issue_updated_at"], (
            "the transition must have moved the anchor, or this test proves nothing"
        )

        second = execute_transition(principal=user, envelope=envelope)
        assert second.replayed is True, (
            "the retry raised or re-executed instead of replaying — the precondition "
            "is being evaluated before the claim's replay short-circuit"
        )
        assert second.row.id == first.row.id


@pytest.mark.django_db(transaction=True)
class TestHalfAPreconditionIsRefused:
    """Rowan 3866, and my witness set's blind spot.

    Every case above sends BOTH halves, so my mutation evidence covered
    deleting the whole block and never covered supplying half of it. Rowan's
    omission controls executed the transition: `if key is not None` twice means
    a caller sending one key gets one check, silently, while the envelope still
    looks like it carries a precondition.

    Same shape as the branch's other findings — a guard that appears to protect
    and can be satisfied partially — and my tests could not see it because they
    all sat on one axis.
    """

    def test_freshness_without_a_decider_is_refused(self):
        user, ws, project, issue, target = _fixture()
        before = issue.state_id
        with pytest.raises(BoardOperationPreconditionFailed):
            execute_transition(
                principal=user,
                envelope=_envelope(
                    user, ws, project, issue, target, op_key="half-fresh-1",
                    precondition={"issue_updated_at": issue.updated_at.isoformat()},
                ),
            )
        issue.refresh_from_db()
        assert issue.state_id == before, "half a precondition executed the transition"

    def test_a_decider_without_freshness_is_refused(self):
        user, ws, project, issue, target = _fixture()
        before = issue.state_id
        with pytest.raises(BoardOperationPreconditionFailed):
            execute_transition(
                principal=user,
                envelope=_envelope(
                    user, ws, project, issue, target, op_key="half-decider-1",
                    precondition={"decider_id": str(issue.created_by_id)},
                ),
            )
        issue.refresh_from_db()
        assert issue.state_id == before, "half a precondition executed the transition"

    def test_no_precondition_at_all_is_still_allowed(self):
        """Guard the guard: requiring both halves must not make it mandatory."""
        user, ws, project, issue, target = _fixture()
        result = execute_transition(
            principal=user,
            envelope=_envelope(user, ws, project, issue, target, op_key="none-1"),
        )
        assert result.replayed is False
        issue.refresh_from_db()
        assert issue.state_id == target.id


@pytest.mark.django_db(transaction=True)
class TestASuppliedPreconditionMustBeComplete:
    """Rowan 3868: the contract is ABSENT-OR-COMPLETE, not falsey-or-complete.

    `envelope.get("precondition") or {}` collapsed an explicitly supplied empty
    object to absence, so a caller stating a precondition and supplying nothing
    executed unchecked. Key absence and a supplied value are different facts.
    """

    def _refuses(self, value, op_key):
        user, ws, project, issue, target = _fixture()
        before = issue.state_id
        envelope = _envelope(user, ws, project, issue, target, op_key=op_key)
        envelope["precondition"] = value
        with pytest.raises(BoardOperationPreconditionFailed):
            execute_transition(principal=user, envelope=envelope)
        issue.refresh_from_db()
        assert issue.state_id == before, f"precondition={value!r} executed the transition"

    def test_an_explicitly_empty_object_is_refused(self):
        self._refuses({}, "empty-1")

    def test_an_explicit_null_is_refused(self):
        self._refuses(None, "null-1")

    def test_a_malformed_precondition_is_refused(self):
        self._refuses("not-a-mapping", "malformed-1")

    def test_key_absence_is_still_allowed(self):
        """Guard the guard: absent must remain unchecked, not refused."""
        user, ws, project, issue, target = _fixture()
        envelope = _envelope(user, ws, project, issue, target, op_key="absent-1")
        assert "precondition" not in envelope
        result = execute_transition(principal=user, envelope=envelope)
        assert result.replayed is False
        issue.refresh_from_db()
        assert issue.state_id == target.id
