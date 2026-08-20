# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party frameworks
from rest_framework import serializers

# Module imports
from .base import BaseSerializer
from .issue import IssueIntakeSerializer, LabelLiteSerializer, IssueDetailSerializer
from .project import ProjectLiteSerializer
from .state import StateLiteSerializer
from .user import UserLiteSerializer
from plane.db.models import Intake, IntakeIssue, Issue, StateGroup, State


class IntakeSerializer(BaseSerializer):
    project_detail = ProjectLiteSerializer(source="project", read_only=True)
    pending_issue_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Intake
        fields = "__all__"
        read_only_fields = ["project", "workspace"]


class IntakeIssueSerializer(BaseSerializer):
    issue = IssueIntakeSerializer(read_only=True)

    class Meta:
        model = IntakeIssue
        fields = [
            "id",
            "status",
            "duplicate_to",
            "snoozed_till",
            "source",
            "issue",
            "created_by",
        ]
        read_only_fields = ["project", "workspace"]

    def validate(self, attrs):
        """
        Validate that if status is being changed to accepted (1),
        the project has a default state to transition to.
        """

        # Check if status is being updated to accepted
        if attrs.get("status") == 1:
            intake_issue = self.instance
            issue = intake_issue.issue

            # Check if issue is in TRIAGE state
            if issue.state and issue.state.group == StateGroup.TRIAGE.value:
                # Verify default state exists before allowing the update
                default_state = State.objects.filter(
                    workspace=intake_issue.workspace, project=intake_issue.project, default=True
                ).first()

                if not default_state:
                    raise serializers.ValidationError(
                        {"status": "Cannot accept intake issue: No default state found for the project"}
                    )

        return attrs

    def update(self, instance, validated_data):
        # Update the intake issue
        instance = super().update(instance, validated_data)

        # If status is accepted (1), transition the issue state from TRIAGE to default
        if validated_data.get("status") == 1:
            issue = instance.issue
            if issue.state and issue.state.group == StateGroup.TRIAGE.value:
                # Get the default project state
                default_state = State.objects.filter(
                    workspace=instance.workspace, project=instance.project, default=True
                ).first()
                if default_state:
                    # CONVERGED ON THE BOARD SERVICE (BIP-37 M8.3). This used to
                    # assign and save directly, producing a state change with no
                    # outcome row — the mutation-without-a-receipt M8 exists to
                    # remove.
                    #
                    # WHAT THE OP KEY DOES AND DOES NOT BUY (corrected, Vex 3857).
                    # An earlier version of this comment said the key makes a
                    # retried acceptance REPLAY the stored outcome. That was true
                    # of the unanchored key and is FALSE of this one: `updated_at`
                    # is auto_now, and a completed transition saves the issue, so
                    # a retry computes a DIFFERENT key and cannot replay. The
                    # general form is worth remembering — an op key anchored to a
                    # value the operation itself mutates cannot give cross-request
                    # retry idempotence. What it does give is OCCASION-
                    # DISTINCTNESS: telling a genuine later acceptance from an
                    # earlier one, which is what this anchor was added for.
                    #
                    # RE-ENTRY IS PREVENTED BY THE TRIAGE GUARD ABOVE, not by the
                    # key: after the first transition the issue is no longer in
                    # the triage group, so this block does not run again. That
                    # guard is load-bearing — relax it and the protection this
                    # comment used to promise is not somewhere else.
                    from plane.board import service as board_service

                    principal = self.context["request"].user
                    board_service.execute_transition(
                        principal=principal,
                        envelope={
                            # ANCHORED to the ISSUE, and the reason is CONCURRENCY
                            # (Vex 3858, correcting his own B3). The scenario the
                            # anchor was originally added for — accept, revert,
                            # re-accept — cannot happen: `State.objects` excludes
                            # the triage group entirely (StateManager, state.py),
                            # the only triage writes are the intake CREATION
                            # paths, and the guard above blocks re-entry anyway.
                            #
                            # What the anchor actually defends is two acceptances
                            # of the same intake issue IN FLIGHT AT ONCE. Both
                            # read this pre-transition `updated_at`, so both
                            # compute the SAME key; `_claim` inserts-and-catches
                            # under a savepoint, so one claims and the other
                            # replays — one operation row, one transition.
                            # Anchoring to the intake row instead would break
                            # that: `super().update()` runs per request first, so
                            # the two compute DIFFERENT keys, both claim, and the
                            # second serialises behind the row lock, sees
                            # `changed=False`, and records an operation row for a
                            # transition that never happened.
                            #
                            # PRECISELY: `_claim` is keyed on (principal, op_key),
                            # so this collapses concurrent acceptances by the SAME
                            # principal — the double-submit and the in-flight
                            # retry. Two different users racing still take one row
                            # each whatever the anchor is; the second simply
                            # records `changed=False` honestly.
                            "op_key": f"intake-accept:{instance.id}:{issue.updated_at.isoformat()}",
                            "source": "app.intake",
                            "verb": "work_item.state.transition",
                            "workspace": instance.workspace.slug,
                            "project": instance.project.identifier,
                            "payload": {
                                "sequence_id": issue.sequence_id,
                                "state_id": str(default_state.id),
                            },
                        },
                    )

        return instance

    def to_representation(self, instance):
        # Pass the annotated fields to the Issue instance if they exist
        if hasattr(instance, "label_ids"):
            instance.issue.label_ids = instance.label_ids
        return super().to_representation(instance)


class IntakeIssueDetailSerializer(BaseSerializer):
    issue = IssueDetailSerializer(read_only=True)
    duplicate_issue_detail = IssueIntakeSerializer(read_only=True, source="duplicate_to")

    class Meta:
        model = IntakeIssue
        fields = [
            "id",
            "status",
            "duplicate_to",
            "snoozed_till",
            "duplicate_issue_detail",
            "source",
            "issue",
        ]
        read_only_fields = ["project", "workspace"]

    def to_representation(self, instance):
        # Pass the annotated fields to the Issue instance if they exist
        if hasattr(instance, "assignee_ids"):
            instance.issue.assignee_ids = instance.assignee_ids
        if hasattr(instance, "label_ids"):
            instance.issue.label_ids = instance.label_ids

        return super().to_representation(instance)


class IntakeIssueLiteSerializer(BaseSerializer):
    class Meta:
        model = IntakeIssue
        fields = ["id", "status", "duplicate_to", "snoozed_till", "source"]
        read_only_fields = fields


class IssueStateIntakeSerializer(BaseSerializer):
    state_detail = StateLiteSerializer(read_only=True, source="state")
    project_detail = ProjectLiteSerializer(read_only=True, source="project")
    label_details = LabelLiteSerializer(read_only=True, source="labels", many=True)
    assignee_details = UserLiteSerializer(read_only=True, source="assignees", many=True)
    sub_issues_count = serializers.IntegerField(read_only=True)
    issue_intake = IntakeIssueLiteSerializer(read_only=True, many=True)

    class Meta:
        model = Issue
        fields = "__all__"
