# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json
import logging
from datetime import timedelta

# Third party imports
from celery import shared_task
from django.db.models import Q

# Django imports
from django.utils import timezone

# Module imports
from plane.bgtasks.issue_activities_task import issue_activity
from plane.board import service as board_service
from plane.board.service import (
    BoardOperationConflict,
    BoardOperationNotFound,
    BoardOperationPermissionDenied,
    BoardOperationPreconditionFailed,
)
from plane.db.models import Issue, Project, State
from plane.utils.exception_logger import log_exception

logger = logging.getLogger("plane.bgtasks")


@shared_task
def archive_and_close_old_issues():
    archive_old_issues()
    close_old_issues()


def archive_old_issues():
    try:
        # Get all the projects whose archive_in is greater than 0
        projects = Project.objects.filter(archive_in__gt=0)

        for project in projects:
            project_id = project.id
            archive_in = project.archive_in

            # Get all the issues whose updated_at in less that the archive_in month
            issues = Issue.issue_objects.filter(
                Q(
                    project=project_id,
                    archived_at__isnull=True,
                    updated_at__lte=(timezone.now() - timedelta(days=archive_in * 30)),
                    state__group__in=["completed", "cancelled"],
                ),
                Q(issue_cycle__isnull=True)
                | (Q(issue_cycle__cycle__end_date__lt=timezone.now()) & Q(issue_cycle__isnull=False)),
                Q(issue_module__isnull=True)
                | (Q(issue_module__module__target_date__lt=timezone.now()) & Q(issue_module__isnull=False)),
            ).filter(
                Q(issue_intake__status=1)
                | Q(issue_intake__status=-1)
                | Q(issue_intake__status=2)
                | Q(issue_intake__isnull=True)
            )

            # Check if Issues
            if issues:
                # Set the archive time to current time
                archive_at = timezone.now().date()

                issues_to_update = []
                for issue in issues:
                    issue.archived_at = archive_at
                    issues_to_update.append(issue)

                # Bulk Update the issues and log the activity
                if issues_to_update:
                    Issue.objects.bulk_update(issues_to_update, ["archived_at"], batch_size=100)
                    _ = [
                        issue_activity.delay(
                            type="issue.activity.updated",
                            requested_data=json.dumps({"archived_at": str(archive_at), "automation": True}),
                            actor_id=str(project.created_by_id),
                            issue_id=issue.id,
                            project_id=project_id,
                            current_instance=json.dumps({"archived_at": None}),
                            subscriber=False,
                            epoch=int(timezone.now().timestamp()),
                            notification=True,
                        )
                        for issue in issues_to_update
                    ]
        return
    except Exception as e:
        log_exception(e)
        return


def close_old_issues():
    try:
        # Get all the projects whose close_in is greater than 0
        projects = Project.objects.filter(close_in__gt=0).select_related("default_state", "workspace")

        for project in projects:
            project_id = project.id
            close_in = project.close_in

            # Get all the issues whose updated_at in less that the close_in month
            issues = Issue.issue_objects.select_related("updated_by", "created_by").filter(
                Q(
                    project=project_id,
                    archived_at__isnull=True,
                    updated_at__lte=(timezone.now() - timedelta(days=close_in * 30)),
                    state__group__in=["backlog", "unstarted", "started"],
                ),
                Q(issue_cycle__isnull=True)
                | (Q(issue_cycle__cycle__end_date__lt=timezone.now()) & Q(issue_cycle__isnull=False)),
                Q(issue_module__isnull=True)
                | (Q(issue_module__module__target_date__lt=timezone.now()) & Q(issue_module__isnull=False)),
            ).filter(
                Q(issue_intake__status=1)
                | Q(issue_intake__status=-1)
                | Q(issue_intake__status=2)
                | Q(issue_intake__isnull=True)
            )

            # Check if Issues
            if issues:
                if project.default_state is None:
                    close_state = State.objects.filter(group="cancelled").first()
                else:
                    close_state = project.default_state

                # CONVERGED ON THE BOARD SERVICE (BIP-37 M8.3), under John's
                # ruling of 2026-08-16: ATTRIBUTION FOLLOWS THE DECISION. An
                # automated transition is attributed to the entity whose
                # completed act triggered it — here the last entity that
                # finished work on the issue before it went stale, because
                # auto-close is a CONSEQUENCE of their act and of nobody
                # else's. An auto-deterministic function earns no credit; it
                # reacted to a decision someone made.
                #
                # This is honest where the previous attribution was not: it
                # used project.created_by_id, a person who in general never
                # touched the issue at all.
                #
                # `source` marks the operation automatic, so the ledger row can
                # never be read as that person's manual click.
                for issue in issues:
                    decider = issue.updated_by or issue.created_by
                    if decider is None:
                        # No decider to attribute to. Refuse and record rather
                        # than borrow anyone — the BIP-67 precedent: absent a
                        # verified actor, write nothing.
                        logger.warning(
                            "auto-close REFUSED for issue %s: no triggering decider recorded",
                            issue.id,
                        )
                        continue
                    try:
                        board_service.execute_transition(
                            principal=decider,
                            envelope={
                                # Anchored to the staleness the close is based
                                # on. What this buys is OCCASION-DISTINCTNESS:
                                # an issue touched and gone stale again later is
                                # a genuinely new operation rather than a replay
                                # of the old one.
                                #
                                # It does NOT buy cross-request retry
                                # idempotence, and the same rule applies here as
                                # at the intake sites (Vex 3857): an op key
                                # anchored to a value the operation itself
                                # mutates cannot, because a completed transition
                                # moves the anchor. Replay within one sweep works
                                # only because this in-memory `issue` still holds
                                # its pre-transition `updated_at` — true, but a
                                # narrower guarantee than "retries replay", and
                                # not one to lean on.
                                "op_key": f"auto-close:{issue.id}:{issue.updated_at.isoformat()}",
                                "source": "automation.auto_close",
                                "verb": "work_item.state.transition",
                                "workspace": project.workspace.slug,
                                "project": project.identifier,
                                "payload": {
                                    "sequence_id": issue.sequence_id,
                                    "state_id": str(close_state.id),
                                },
                                # THE SWEEP'S PREMISE, CARRIED AND RE-CHECKED
                                # UNDER THE LOCK (Rowan 12118/12125). The
                                # staleness was measured when the query ran; the
                                # issue can be touched before this transitions.
                                # Both facts travel because either alone is
                                # insufficient — a second edit by the SAME
                                # person leaves the decider equal while moving
                                # the clock, and freshness alone would credit
                                # someone who did not perform the act.
                                "precondition": {
                                    "issue_updated_at": issue.updated_at.isoformat(),
                                    "decider_id": str(decider.id),
                                },
                            },
                        )
                    except BoardOperationPermissionDenied:
                        # TWO CAUSES, and this message must not pick one (Vex
                        # B2): the service raises this both for a principal
                        # that is not an active member with a write role AND
                        # for an expected-principal mismatch. Naming only the
                        # first was accurate solely because the mismatch is
                        # currently unreachable — so the message and that
                        # unreachability were propping each other up, and the
                        # log would have started lying the moment the check
                        # gained content.
                        logger.warning(
                            "auto-close REFUSED for issue %s: the board service denied the "
                            "transition for triggering decider %s — either they are no longer an "
                            "active member with a write role, or the operation's expected "
                            "principal did not match. Either way the close rested on their "
                            "authority and is not reattributed to anyone else.",
                            issue.id,
                            decider.id,
                        )
                    except BoardOperationPreconditionFailed as exc:
                        # The issue moved after the sweep measured it. The close
                        # rested on a premise that has expired, so it is refused
                        # and recorded — not retried against the new state, and
                        # not reattributed to whoever touched it.
                        logger.warning(
                            "auto-close REFUSED for issue %s: the premise expired between the "
                            "sweep and the transition (%s)",
                            issue.id,
                            exc,
                        )
                    except (BoardOperationNotFound, BoardOperationConflict) as exc:
                        logger.warning(
                            "auto-close REFUSED for issue %s: %s", issue.id, exc.__class__.__name__
                        )
                    except Exception as exc:  # noqa: BLE001 — see below
                        # ONE BAD ISSUE MUST NOT CANCEL THE SWEEP (Vex B4).
                        # Converting a single bulk_update into a per-issue loop
                        # moved the blast radius: any unexpected error now
                        # escaped to the outer handler, which logs once and
                        # RETURNS, so every remaining issue AND every remaining
                        # project went unprocessed — silently, because the sweep
                        # has no other voice. Per-issue containment restores the
                        # old blast radius while keeping the per-issue
                        # attribution.
                        logger.exception(
                            "auto-close FAILED for issue %s (%s); continuing the sweep",
                            issue.id,
                            exc.__class__.__name__,
                        )
        return
    except Exception as e:
        log_exception(e)
        return
