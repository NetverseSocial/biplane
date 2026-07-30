# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# biplane: ONE name policy for every surface (Morrow RC 3027 item 2) — the shared
# validator plus both serializers (app + public API), both directions (create +
# update), including the multiline/control bypass the old regex allowed.

import pytest

from plane.db.models import Project


@pytest.mark.unit
class TestSharedProjectNameValidator:
    def test_display_punctuation_allowed(self):
        for name in ("TEST-MyProj", "O'Brien & Sons", "Phase (2), v1.5 + extras!", "Ægir 迁移"):
            assert Project.is_valid_project_name(name), name

    def test_injection_chars_rejected(self):
        for name in ("a<b", "a>b", "a{b", "a}b", "a[b", "a]b", "a$b", "a^b", "a*b", "a=b", "a?b", "a@b", "a#b", "a|b", "a;b"):
            assert not Project.is_valid_project_name(name), name

    def test_multiline_and_control_bypass_closed(self):
        # The old regex used dot-matching: "safe\n<script>" passed because the
        # forbidden char sat on a second line. Line boundaries must not matter.
        assert not Project.is_valid_project_name("safe\n<script>")
        assert not Project.is_valid_project_name("safe\x00name")
        assert not Project.is_valid_project_name("bell\x07name")


@pytest.mark.contract
class TestNamePolicyBothSurfacesBothDirections:
    @pytest.mark.django_db
    def test_app_serializer_create_and_update(self, workspace):
        from plane.app.serializers.project import ProjectSerializer as AppProjectSerializer

        ctx = {"workspace_id": workspace.id}
        good = AppProjectSerializer(data={"name": "TEST-MyProj", "identifier": "APPGOOD"}, context=ctx)
        assert good.is_valid(), good.errors

        bad = AppProjectSerializer(data={"name": "bad\n<script>", "identifier": "APPBAD"}, context=ctx)
        assert not bad.is_valid()

        project = Project.objects.create(name="Base", identifier="BASEA", workspace=workspace)
        update_good = AppProjectSerializer(
            project, data={"name": "Renamed-Fine (v2)"}, partial=True, context=ctx
        )
        assert update_good.is_valid(), update_good.errors
        update_bad = AppProjectSerializer(project, data={"name": "nope\x00"}, partial=True, context=ctx)
        assert not update_bad.is_valid()

    @pytest.mark.django_db
    def test_public_api_serializer_create_and_update(self, workspace):
        from plane.api.serializers.project import ProjectSerializer as PublicProjectSerializer

        ctx = {"workspace_id": workspace.id}
        good = PublicProjectSerializer(data={"name": "TEST-MyProj", "identifier": "PUBGOOD"}, context=ctx)
        assert good.is_valid(), good.errors

        bad = PublicProjectSerializer(data={"name": "multi\n<line>", "identifier": "PUBBAD"}, context=ctx)
        assert not bad.is_valid()

        project = Project.objects.create(name="Base Two", identifier="BASEB", workspace=workspace)
        update_good = PublicProjectSerializer(
            project, data={"name": "Agent's Project - phase 1"}, partial=True, context=ctx
        )
        assert update_good.is_valid(), update_good.errors
        update_bad = PublicProjectSerializer(project, data={"name": "x\x1fy"}, partial=True, context=ctx)
        assert not update_bad.is_valid()

        # identifiers remain STRICT on the public surface too
        strict = PublicProjectSerializer(data={"name": "Fine Name", "identifier": "BAD-ID"}, context=ctx)
        assert not strict.is_valid()
