# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers


class ExactFieldsSerializer(serializers.Serializer):
    """Reject request keys the operation contract does not define."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("expected an object")
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({key: ["unknown field"] for key in unknown})
        return super().to_internal_value(data)


class BoardTransitionPayloadSerializer(ExactFieldsSerializer):
    sequence_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    state_id = serializers.UUIDField()


class BoardOperationCreateSerializer(ExactFieldsSerializer):
    op_key = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    expected_principal_id = serializers.UUIDField()
    source = serializers.RegexField(r"^[a-z][a-z0-9_-]{0,63}$")
    verb = serializers.ChoiceField(choices=("transition",))
    workspace = serializers.CharField(min_length=1, max_length=255, trim_whitespace=False)
    project = serializers.CharField(min_length=1, max_length=12, trim_whitespace=False)
    payload = BoardTransitionPayloadSerializer()
