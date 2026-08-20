# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The server-owned board mutation seam (BIP-37).

Every accepted operation is claimed before its domain write, and the claim,
write, durable outcome and audit intent share one transaction. Callers may
retry an unknown transport result with the same key and exact request; they
must never reconstruct the mutation themselves.
"""

import hashlib
import json
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from plane.api.audit import enqueue_audit
from plane.db.models import BoardOperation, Issue, Project, ProjectMember, State
from plane.db.models.project import ROLE


class BoardOperationConflict(Exception):
    """An operation key was already bound to a different request."""


class BoardOperationNotFound(Exception):
    """The scoped project, work item, or target state does not exist."""


class BoardOperationPreconditionFailed(Exception):
    """The caller's stated precondition no longer holds under the row lock.

    A DISTINCT failure from permission (Rowan 12118/12125): identity intent and
    attribution freshness are different facts with different meanings. An
    auto-close whose issue moved after the sweep measured it is not a
    permission problem — the premise it was decided on has expired.
    """


class BoardOperationPermissionDenied(Exception):
    """The server-bound principal cannot mutate this project."""


@dataclass(frozen=True)
class OperationResult:
    row: BoardOperation
    replayed: bool


def canonical_request_digest(envelope):
    encoded = json.dumps(
        envelope,
        cls=DjangoJSONEncoder,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _claim(*, principal, envelope, request_digest):
    """Claim a key, or return its exact committed replay under the row lock."""

    lookup = {"principal": str(principal.id), "op_key": envelope["op_key"]}
    try:
        # The savepoint is intentional: a unique-key race rolls back only the
        # failed INSERT, leaving the outer transaction usable for readback.
        with transaction.atomic():
            row = BoardOperation.objects.create(
                **lookup,
                source=envelope["source"],
                request_digest=request_digest,
                verb=envelope["verb"],
                workspace_slug=envelope["workspace"],
                project_identifier=envelope["project"],
                outcome={},
                created_by_id=principal.id,
            )
        return row, False
    except IntegrityError:
        row = BoardOperation.objects.select_for_update().get(**lookup)
        if row.request_digest != request_digest:
            raise BoardOperationConflict
        return row, True


def _project_for_mutation(*, principal, workspace_slug, project_identifier):
    project = (
        Project.objects.select_for_update()
        .filter(workspace__slug=workspace_slug, identifier=project_identifier)
        .first()
    )
    if project is None:
        raise BoardOperationNotFound

    membership = (
        ProjectMember.objects.select_for_update()
        .filter(
            workspace_id=project.workspace_id,
            project=project,
            member=principal,
            role__in=(ROLE.ADMIN.value, ROLE.MEMBER.value),
            is_active=True,
        )
        .first()
    )
    if membership is None:
        raise BoardOperationPermissionDenied
    return project


def _stored_work_item(issue):
    """Read the committed domain shape back from the primary store."""

    return {
        "id": str(issue.id),
        "identifier": f"{issue.project.identifier}-{issue.sequence_id}",
        "sequence_id": issue.sequence_id,
        "state": {
            "id": str(issue.state_id),
            "name": issue.state.name,
            "group": issue.state.group,
        },
        "completed_at": issue.completed_at.isoformat() if issue.completed_at else None,
        "updated_at": issue.updated_at.isoformat(),
    }


def execute_transition(*, principal, envelope):
    """Execute or exactly replay one work-item state transition.

    Transition direction is intentionally unrestricted here: project roles may
    move work in either direction. The server authenticates the participant,
    revalidates membership and scope under locks, and never trusts an asserted
    actor from the request.
    """

    # OPTIONAL, and deliberately so (Rowan 12113/12125). At the public
    # /api/v1/board/ops/ adapter this field compares CLIENT-STATED intent
    # against the independently token-authenticated principal, and there it has
    # content. In-process callers pass a trusted principal object, so
    # manufacturing the envelope field from that same object compares a value
    # with itself — a check that cannot fail, teaching a vacuous pattern in the
    # milestone about attribution (Vex B1). Compare when present; never
    # require it here.
    expected = envelope.get("expected_principal_id")
    if expected is not None and str(expected) != str(principal.id):
        raise BoardOperationPermissionDenied

    request_digest = canonical_request_digest(envelope)
    with transaction.atomic():
        row, replayed = _claim(
            principal=principal,
            envelope=envelope,
            request_digest=request_digest,
        )
        if replayed:
            return OperationResult(row=row, replayed=True)

        project = _project_for_mutation(
            principal=principal,
            workspace_slug=envelope["workspace"],
            project_identifier=envelope["project"],
        )
        payload = envelope["payload"]
        issue = (
            Issue.objects.select_for_update(of=("self",))
            .select_related("workspace", "project", "state")
            .filter(project=project, sequence_id=payload["sequence_id"])
            .first()
        )
        target = State.objects.select_for_update().filter(project=project, id=payload["state_id"]).first()
        if issue is None or target is None:
            raise BoardOperationNotFound

        # THE LOCKED PRECONDITION (Rowan 12118/12125). Optional, and checked
        # here — after the claim's exact-replay short-circuit above, so a
        # committed retry still returns its stored outcome even though the row
        # has since advanced, and after the row lock, so what it compares is
        # what the mutation will act on.
        #
        # BOTH comparisons are required, and the pair is the point. Identity
        # alone misses a SECOND EDIT BY THE SAME PERSON: `updated_by_id` stays
        # equal while the staleness clock advances, so an auto-close would fire
        # on a premise that had already expired. Freshness alone would let the
        # credit land on someone who did not perform the act being attributed.
        # ABSENT OR COMPLETE — not falsey-or-complete (Rowan 3866, then 3868).
        # Two rounds here, and the second is the instructive one:
        #
        #   1. Each half was checked only `if present`, so a caller supplying
        #      one key got one check while the envelope still looked like it
        #      carried a precondition.
        #   2. `envelope.get("precondition") or {}` then collapsed an
        #      EXPLICITLY SUPPLIED empty object to absence, so a caller stating
        #      a precondition and supplying nothing executed unchecked. Key
        #      absence and a supplied value are different facts, and `or {}`
        #      erased the difference.
        #
        # So: absent means no precondition and is allowed. Supplied means it
        # must be a complete two-field object — an empty dict, an explicit
        # null, or a non-mapping are all refusals, because each is a caller
        # stating a premise it did not provide.
        expected_updated_at = expected_decider_id = None
        if "precondition" in envelope:
            precondition = envelope["precondition"]
            if not isinstance(precondition, dict):
                raise BoardOperationPreconditionFailed(
                    "a supplied precondition must be a mapping carrying issue_updated_at "
                    f"and decider_id (got {type(precondition).__name__})"
                )
            expected_updated_at = precondition.get("issue_updated_at")
            expected_decider_id = precondition.get("decider_id")
            if expected_updated_at is None or expected_decider_id is None:
                raise BoardOperationPreconditionFailed(
                    "a supplied precondition must carry BOTH issue_updated_at and "
                    "decider_id — freshness alone credits someone who did not act, and "
                    "identity alone misses a second edit by the same person "
                    f"(got keys: {sorted(precondition)})"
                )
        if expected_updated_at is not None:
            stored = issue.updated_at.isoformat() if issue.updated_at else None
            if stored != str(expected_updated_at):
                raise BoardOperationPreconditionFailed(
                    f"issue {issue.id} moved since the caller measured it "
                    f"(expected updated_at {expected_updated_at}, stored {stored})"
                )
        if expected_decider_id is not None:
            current = issue.updated_by_id or issue.created_by_id
            if str(current) != str(expected_decider_id):
                raise BoardOperationPreconditionFailed(
                    f"issue {issue.id} is no longer attributable to "
                    f"{expected_decider_id} (now {current})"
                )

        previous_state = {
            "id": str(issue.state_id),
            "name": issue.state.name,
            "group": issue.state.group,
        }
        changed = issue.state_id != target.id
        if changed:
            current_instance = json.dumps(
                {
                    "id": str(issue.id),
                    "state": str(issue.state_id),
                },
                separators=(",", ":"),
            )
            issue.state = target
            issue.save()
            enqueue_audit(
                "issue_activity",
                type="issue.activity.updated",
                requested_data=json.dumps({"state": str(target.id)}, separators=(",", ":")),
                actor_id=str(principal.id),
                issue_id=str(issue.id),
                project_id=str(project.id),
                current_instance=current_instance,
                epoch=int(timezone.now().timestamp()),
            )

        # This query is deliberate store readback, not a projection of the
        # in-memory object that was just assigned.
        stored = Issue.objects.select_related("project", "state").get(id=issue.id)
        row.outcome = {
            "changed": changed,
            "from_state": previous_state,
            "work_item": _stored_work_item(stored),
        }
        row.save(update_fields=["outcome", "updated_at"])
        return OperationResult(row=row, replayed=False)
