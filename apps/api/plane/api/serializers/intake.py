# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Module imports
from .base import BaseSerializer
from .issue import IssueExpandSerializer
from plane.db.models import IntakeIssue, Issue, State, StateGroup
from rest_framework import serializers


class IssueForIntakeSerializer(BaseSerializer):
    """
    Serializer for work item data within intake submissions.

    Handles essential work item fields for intake processing including
    content validation and priority assignment for triage workflows.
    """

    description = serializers.JSONField(source="description_json", required=False, allow_null=True)

    class Meta:
        model = Issue
        fields = [
            "name",
            "description",  # Deprecated
            "description_json",
            "description_html",
            "priority",
        ]
        read_only_fields = [
            "id",
            "workspace",
            "project",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class IntakeIssueCreateSerializer(BaseSerializer):
    """
    Serializer for creating intake work items with embedded issue data.

    Manages intake work item creation including nested issue creation,
    status assignment, and source tracking for issue queue management.
    """

    issue = IssueForIntakeSerializer(help_text="Issue data for the intake issue")

    class Meta:
        model = IntakeIssue
        fields = ["issue"]


class IntakeIssueSerializer(BaseSerializer):
    """
    Comprehensive serializer for intake work items with expanded issue details.

    Provides full intake work item data including embedded issue information,
    status tracking, and triage metadata for issue queue management.
    """

    issue_detail = IssueExpandSerializer(read_only=True, source="issue")
    inbox = serializers.UUIDField(source="intake.id", read_only=True)

    class Meta:
        model = IntakeIssue
        fields = "__all__"
        read_only_fields = [
            "id",
            "workspace",
            "project",
            "issue",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class IntakeIssueUpdateSerializer(BaseSerializer):
    """
    Serializer for updating intake work items and their associated issues.

    Handles intake work item modifications including status changes, triage decisions,
    and embedded issue updates for issue queue processing workflows.
    """

    issue = IssueForIntakeSerializer(required=False, help_text="Issue data to update in the intake issue")

    class Meta:
        model = IntakeIssue
        fields = [
            "status",
            "snoozed_till",
            "duplicate_to",
            "source",
            "source_email",
            "issue",
        ]
        read_only_fields = [
            "id",
            "workspace",
            "project",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

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
        """
        Update intake issue and transition associated issue state if accepted.
        """

        # Update the intake issue with validated data
        instance = super().update(instance, validated_data)

        # If status is accepted (1), update the associated issue state from TRIAGE to default
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
                            "source": "api.intake",
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


class IssueDataSerializer(serializers.Serializer):
    """
    Serializer for nested work item data in intake request payloads.

    Validates core work item fields within intake requests including
    content formatting, priority levels, and metadata for issue creation.
    """

    name = serializers.CharField(max_length=255, help_text="Issue name")
    description_html = serializers.CharField(required=False, allow_null=True, help_text="Issue description HTML")
    priority = serializers.ChoiceField(choices=Issue.PRIORITY_CHOICES, default="none", help_text="Issue priority")
