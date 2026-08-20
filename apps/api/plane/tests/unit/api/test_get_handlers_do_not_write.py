# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""
biplane (BIP-28): route-level proof that no GET handler writes, and that the
write paths which now carry the create are correct.

These run against the real URLconf and a real database on purpose. The defect
being fixed is a *storage* effect behind a safe method, so a test that stubbed
the ORM could not see it: every assertion below counts rows.

Coverage map (each bar is a class):
  * six GET endpoints, zero rows written           -> TestGetHandlersWriteNothing
  * every first PATCH creates its own row          -> TestFirstWriteCreates
  * one user's PATCH cannot touch another's row    -> TestPatchIsUserScoped
  * unknown keys rejected, no residue              -> TestUnknownKeysRejected
  * a rejected request leaves nothing behind       -> TestRejectedRequestLeavesNoRow
  * the read-only fallback is honest about itself  -> TestFallbackShape
"""

import pytest
from types import SimpleNamespace

from rest_framework.test import APIClient

from plane.db.models import (
    Cycle,
    CycleUserProperties,
    Module,
    ModuleUserProperties,
    ProjectUserProperty,
    WorkspaceHomePreference,
    WorkspaceUserPreference,
    WorkspaceUserProperties,
)
from plane.tests.factories import (
    ProjectFactory,
    ProjectMemberFactory,
    UserFactory,
    WorkspaceFactory,
    WorkspaceMemberFactory,
)


@pytest.fixture
def stack(db):
    """A workspace + project + cycle + module with one admin member."""
    owner = UserFactory()
    workspace = WorkspaceFactory(owner=owner)
    WorkspaceMemberFactory(workspace=workspace, member=owner, role=20)
    project = ProjectFactory(workspace=workspace)
    ProjectMemberFactory(project=project, member=owner, role=20)
    cycle = Cycle.objects.create(name="Cycle 1", project=project, workspace=workspace, owned_by=owner)
    module = Module.objects.create(name="Module 1", project=project, workspace=workspace)

    # Creating a project seeds a ProjectUserProperty for its owner
    # (plane/db/models/project.py). That is a legitimate write on a write
    # path, not the defect under test — but it means "no rows exist" is not
    # true at the start. Clear it so the counts below mean what they say.
    ProjectUserProperty.objects.all().delete()

    return SimpleNamespace(
        owner=owner,
        workspace=workspace,
        project=project,
        cycle=cycle,
        module=module,
    )


@pytest.fixture
def client(stack):
    api_client = APIClient()
    api_client.force_authenticate(user=stack.owner)
    return api_client


def urls(stack):
    """The six endpoints under test, as (name, url, model) triples."""
    slug = stack.workspace.slug
    pid = stack.project.id
    return [
        (
            "project-user-properties",
            f"/api/workspaces/{slug}/projects/{pid}/user-properties/",
            ProjectUserProperty,
        ),
        (
            "cycle-user-properties",
            f"/api/workspaces/{slug}/projects/{pid}/cycles/{stack.cycle.id}/user-properties/",
            CycleUserProperties,
        ),
        (
            "module-user-properties",
            f"/api/workspaces/{slug}/projects/{pid}/modules/{stack.module.id}/user-properties/",
            ModuleUserProperties,
        ),
        (
            "workspace-user-properties",
            f"/api/workspaces/{slug}/user-properties/",
            WorkspaceUserProperties,
        ),
        (
            "workspace-home-preferences",
            f"/api/workspaces/{slug}/home-preferences/",
            WorkspaceHomePreference,
        ),
        (
            "workspace-sidebar-preferences",
            f"/api/workspaces/{slug}/sidebar-preferences/",
            WorkspaceUserPreference,
        ),
    ]


@pytest.mark.django_db
class TestGetHandlersWriteNothing:
    """A GET is a safe method. Prefetchers, crawlers and link scanners issue
    it freely, so it must not create rows. Each of these six used to."""

    def test_each_get_writes_no_rows(self, stack, client):
        for name, url, model in urls(stack):
            assert model.objects.count() == 0, f"{name}: dirty before"

            response = client.get(url)

            assert response.status_code == 200, f"{name}: {response.status_code} {response.content!r}"
            assert model.objects.count() == 0, f"{name}: GET created a row"

    def test_repeated_gets_still_write_nothing(self, stack, client):
        """The old code was idempotent, so one GET looked fine. Hammer it."""
        for name, url, model in urls(stack):
            for _ in range(3):
                assert client.get(url).status_code == 200
            assert model.objects.count() == 0, f"{name}: repeated GETs created rows"


@pytest.mark.django_db
class TestFirstWriteCreates:
    """With the GET no longer creating, the first PATCH must. Cycle and module
    regressed here: their PATCH was a bare .get() that only ever worked
    because the GET had created the row first."""

    def test_first_patch_creates_project_property(self, stack, client):
        slug, pid = stack.workspace.slug, stack.project.id
        assert ProjectUserProperty.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/projects/{pid}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 200, response.content
        row = ProjectUserProperty.objects.get(user=stack.owner, project_id=pid)
        assert row.filters == {"priority": ["urgent"]}

    def test_first_patch_creates_cycle_property(self, stack, client):
        """REGRESSION GUARD: this returned DoesNotExist once the GET stopped
        creating the row."""
        slug, pid, cid = stack.workspace.slug, stack.project.id, stack.cycle.id
        assert CycleUserProperties.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/projects/{pid}/cycles/{cid}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 201, response.content
        row = CycleUserProperties.objects.get(user=stack.owner, cycle_id=cid)
        assert row.filters == {"priority": ["urgent"]}
        # ProjectBaseModel.save() fills workspace from the project; if that
        # ever changes, the row would be orphaned from its tenant.
        assert row.workspace_id == stack.workspace.id

    def test_first_patch_creates_module_property(self, stack, client):
        """REGRESSION GUARD: same shape as the cycle case."""
        slug, pid, mid = stack.workspace.slug, stack.project.id, stack.module.id
        assert ModuleUserProperties.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/projects/{pid}/modules/{mid}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 201, response.content
        row = ModuleUserProperties.objects.get(user=stack.owner, module_id=mid)
        assert row.filters == {"priority": ["urgent"]}
        assert row.workspace_id == stack.workspace.id

    def test_first_patch_creates_workspace_property(self, stack, client):
        slug = stack.workspace.slug
        assert WorkspaceUserProperties.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 200, response.content
        assert WorkspaceUserProperties.objects.filter(user=stack.owner).count() == 1

    def test_first_patch_creates_home_preference(self, stack, client):
        slug = stack.workspace.slug
        assert WorkspaceHomePreference.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/home-preferences/quick_links/",
            {"is_enabled": False},
            format="json",
        )

        assert response.status_code == 200, response.content
        row = WorkspaceHomePreference.objects.get(user=stack.owner, key="quick_links")
        assert row.is_enabled is False

    def test_first_patch_creates_sidebar_preference(self, stack, client):
        slug = stack.workspace.slug
        assert WorkspaceUserPreference.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/sidebar-preferences/",
            [{"key": "drafts", "is_pinned": False, "sort_order": 1.0}],
            format="json",
        )

        assert response.status_code == 200, response.content
        row = WorkspaceUserPreference.objects.get(user=stack.owner, key="drafts")
        assert row.is_pinned is False
        assert row.sort_order == 1.0

    def test_get_then_patch_agrees_with_patch_alone(self, stack, client):
        """The read-only GET must not change what a later PATCH produces."""
        slug = stack.workspace.slug
        client.get(f"/api/workspaces/{slug}/sidebar-preferences/")
        client.patch(
            f"/api/workspaces/{slug}/sidebar-preferences/",
            [{"key": "views", "is_pinned": True, "sort_order": 5.0}],
            format="json",
        )

        assert WorkspaceUserPreference.objects.filter(user=stack.owner).count() == 1
        row = WorkspaceUserPreference.objects.get(user=stack.owner, key="views")
        assert row.is_pinned is True


@pytest.mark.django_db
class TestPatchIsUserScoped:
    """The sidebar PATCH matched on key + workspace with no `user=`, so
    `.first()` could return another member's row and this handler would write
    to it — one member silently reordering another's sidebar."""

    def test_patch_cannot_touch_another_members_row(self, stack, client):
        # username is unique and UserFactory does not set it, so a second
        # user needs an explicit one.
        other = UserFactory(username="other-member-sidebar")
        WorkspaceMemberFactory(workspace=stack.workspace, member=other, role=20)
        victim = WorkspaceUserPreference.objects.create(
            workspace=stack.workspace, user=other, key="drafts", is_pinned=True, sort_order=42.0
        )

        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [{"key": "drafts", "is_pinned": False, "sort_order": 1.0}],
            format="json",
        )
        assert response.status_code == 200, response.content

        victim.refresh_from_db()
        assert victim.is_pinned is True, "another member's preference was overwritten"
        assert victim.sort_order == 42.0

        # The caller got their own row, not the victim's.
        mine = WorkspaceUserPreference.objects.get(user=stack.owner, key="drafts")
        assert mine.pk != victim.pk
        assert mine.is_pinned is False

    def test_home_patch_cannot_touch_another_members_row(self, stack, client):
        other = UserFactory(username="other-member-home")
        WorkspaceMemberFactory(workspace=stack.workspace, member=other, role=20)
        victim = WorkspaceHomePreference.objects.create(
            workspace=stack.workspace, user=other, key="quick_links", is_enabled=True
        )

        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/home-preferences/quick_links/",
            {"is_enabled": False},
            format="json",
        )
        assert response.status_code == 200, response.content

        victim.refresh_from_db()
        assert victim.is_enabled is True, "another member's home preference was overwritten"


@pytest.mark.django_db
class TestUnknownKeysRejected:
    """`key` is a plain CharField with no `choices`, so without an explicit
    check any string would create a row for a widget that does not exist."""

    def test_home_preference_rejects_unknown_key(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/home-preferences/not_a_widget/",
            {"is_enabled": False},
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceHomePreference.objects.count() == 0, "rejected key still created a row"

    def test_sidebar_preference_rejects_unknown_key(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [{"key": "not_a_preference", "is_pinned": True}],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0, "rejected key still created a row"

    def test_mixed_bulk_writes_nothing_at_all(self, stack, client):
        """A batch naming one bad key must leave ZERO residue — not the rows
        it got through before reaching the bad one."""
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [
                {"key": "drafts", "is_pinned": True, "sort_order": 1.0},
                {"key": "views", "is_pinned": True, "sort_order": 2.0},
                {"key": "not_a_preference", "is_pinned": True},
            ],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0, (
            "partial batch residue: the valid entries before the bad key were written"
        )

    def test_bad_shape_is_rejected(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            {"key": "drafts", "is_pinned": True},
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0


@pytest.mark.django_db
class TestRejectedRequestLeavesNoRow:
    """These handlers create the row and then run the serializer. Returning a
    400 from inside atomic() does NOT roll back on its own — DRF returns a
    response rather than raising — so the rollback has to be explicit."""

    def test_home_preference_400_leaves_no_row(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/home-preferences/quick_links/",
            {"sort_order": "not-a-number"},
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceHomePreference.objects.count() == 0, (
            "a rejected request left behind the row it created"
        )

    def test_workspace_property_400_leaves_no_row(self, stack, client):
        # navigation_control_preference is a choice field, so this is a value
        # the serializer genuinely rejects. The JSON fields on this model
        # accept almost anything, including a bare string.
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/user-properties/",
            {"navigation_control_preference": "NOT_A_CHOICE"},
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserProperties.objects.count() == 0, (
            "a rejected request left behind the row it created"
        )


@pytest.mark.django_db
class TestFallbackShape:
    """When no row exists the GET serialises an unsaved instance. BaseModel
    defaults its pk to a fresh uuid4, so without care the response carries a
    real-looking id for a row that does not exist — a client could store it
    or send it back."""

    def test_absent_row_reports_null_id(self, stack, client):
        slug, pid = stack.workspace.slug, stack.project.id

        response = client.get(f"/api/workspaces/{slug}/projects/{pid}/user-properties/")

        assert response.status_code == 200
        assert response.json()["id"] is None, "GET invented an id for a row that does not exist"

    def test_absent_cycle_row_reports_null_id(self, stack, client):
        slug, pid, cid = stack.workspace.slug, stack.project.id, stack.cycle.id

        response = client.get(
            f"/api/workspaces/{slug}/projects/{pid}/cycles/{cid}/user-properties/"
        )

        assert response.status_code == 200
        assert response.json()["id"] is None

    def test_stored_row_reports_its_real_id(self, stack, client):
        slug, pid = stack.workspace.slug, stack.project.id
        row = ProjectUserProperty.objects.create(user=stack.owner, project_id=pid)

        response = client.get(f"/api/workspaces/{slug}/projects/{pid}/user-properties/")

        assert response.status_code == 200
        assert response.json()["id"] == str(row.id)


@pytest.mark.django_db
class TestChildIdBoundToRoute:
    """The old handlers carried `workspace__slug` on a `.get()`, so a cycle or
    module id from somewhere else simply matched nothing. Converting to
    `get_or_create` removes that safety — a create would bind a foreign child
    id to the routed project — so the child is resolved against the route."""

    def test_patch_unknown_cycle_id_is_404_and_writes_nothing(self, stack, client):
        from uuid import uuid4

        slug, pid = stack.workspace.slug, stack.project.id
        response = client.patch(
            f"/api/workspaces/{slug}/projects/{pid}/cycles/{uuid4()}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 404, response.content
        assert CycleUserProperties.objects.count() == 0

    def test_patch_unknown_module_id_is_404_and_writes_nothing(self, stack, client):
        from uuid import uuid4

        slug, pid = stack.workspace.slug, stack.project.id
        response = client.patch(
            f"/api/workspaces/{slug}/projects/{pid}/modules/{uuid4()}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 404, response.content
        assert ModuleUserProperties.objects.count() == 0

    def test_patch_cross_project_cycle_is_404_and_writes_nothing(self, stack, client):
        """A cycle that exists, but not on the routed project."""
        other_project = ProjectFactory(workspace=stack.workspace, name="Other Project", identifier="OTHR1")
        ProjectMemberFactory(project=other_project, member=stack.owner, role=20)
        foreign_cycle = Cycle.objects.create(
            name="Foreign", project=other_project, workspace=stack.workspace, owned_by=stack.owner
        )

        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/cycles/{foreign_cycle.id}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 404, response.content
        assert CycleUserProperties.objects.count() == 0, "a foreign cycle was bound to the routed project"

    def test_get_cross_project_cycle_is_404(self, stack, client):
        """The read-only fallback must not answer 200 with invented defaults
        for a cycle that does not live on this route."""
        other_project = ProjectFactory(workspace=stack.workspace, name="Other Project 2", identifier="OTHR2")
        ProjectMemberFactory(project=other_project, member=stack.owner, role=20)
        foreign_cycle = Cycle.objects.create(
            name="Foreign 2", project=other_project, workspace=stack.workspace, owned_by=stack.owner
        )

        response = client.get(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/cycles/{foreign_cycle.id}/user-properties/"
        )

        assert response.status_code == 404, response.content


@pytest.mark.django_db
class TestMalformedSidebarItems:
    """These values go straight onto model fields with no serializer in the
    path, so their types are checked in the handler or not at all."""

    def test_non_object_item_is_400_not_500(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            ["drafts"],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0

    def test_non_boolean_is_pinned_is_rejected(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [{"key": "drafts", "is_pinned": "yes"}],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0

    def test_non_numeric_sort_order_is_rejected(self, stack, client):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [{"key": "drafts", "sort_order": "first"}],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0

    def test_hidden_home_widget_key_is_rejected(self, stack, client):
        """quick_tutorial and new_at_plane are in the model's choices but the
        GET filters them out, so a row for one is unreadable by design."""
        for hidden in ("quick_tutorial", "new_at_plane"):
            response = client.patch(
                f"/api/workspaces/{stack.workspace.slug}/home-preferences/{hidden}/",
                {"is_enabled": True},
                format="json",
            )

            assert response.status_code == 400, f"{hidden}: {response.content!r}"
            assert WorkspaceHomePreference.objects.count() == 0


@pytest.mark.django_db
class TestResponseContracts:
    """The GETs now compute defaults in memory and overlay stored rows. That
    is only correct if the response is identical to what the create-on-read
    version returned — same keys, same values, same order."""

    def test_home_all_fallback_returns_every_served_widget(self, stack, client):
        response = client.get(f"/api/workspaces/{stack.workspace.slug}/home-preferences/")

        assert response.status_code == 200
        body = response.json()
        keys = [row["key"] for row in body]

        assert set(keys) == {"quick_links", "recents", "my_stickies"}
        assert "quick_tutorial" not in keys and "new_at_plane" not in keys
        assert all(row["is_enabled"] is True for row in body)
        assert keys == sorted(keys, key=lambda k: [r["sort_order"] for r in body if r["key"] == k][0])

    def test_home_overlay_uses_stored_values_and_keeps_full_key_set(self, stack, client):
        WorkspaceHomePreference.objects.create(
            workspace=stack.workspace,
            user=stack.owner,
            key="recents",
            is_enabled=False,
            sort_order=1.0,
        )

        body = client.get(f"/api/workspaces/{stack.workspace.slug}/home-preferences/").json()
        by_key = {row["key"]: row for row in body}

        assert set(by_key) == {"quick_links", "recents", "my_stickies"}, "overlay dropped the defaults"
        assert by_key["recents"]["is_enabled"] is False, "stored row did not override its default"
        assert by_key["recents"]["sort_order"] == 1.0
        assert by_key["quick_links"]["is_enabled"] is True, "an untouched default was altered"
        assert [row["key"] for row in body][0] == "recents", "response is not sorted by sort_order"

    def test_sidebar_all_fallback_returns_every_key_with_defaults(self, stack, client):
        body = client.get(f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/").json()

        assert set(body) == {
            "views",
            "active_cycles",
            "analytics",
            "drafts",
            "your_work",
            "archives",
            "stickies",
        }
        # drafts, your_work and stickies are pinned by default; the rest are not.
        assert body["drafts"]["is_pinned"] is True
        assert body["your_work"]["is_pinned"] is True
        assert body["stickies"]["is_pinned"] is True
        assert body["views"]["is_pinned"] is False
        assert body["analytics"]["is_pinned"] is False

    def test_sidebar_overlay_uses_stored_values_and_keeps_full_key_set(self, stack, client):
        WorkspaceUserPreference.objects.create(
            workspace=stack.workspace, user=stack.owner, key="views", is_pinned=True, sort_order=1.0
        )

        body = client.get(f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/").json()

        assert len(body) == 7, "overlay dropped the defaults"
        assert body["views"]["is_pinned"] is True, "stored row did not override its default"
        assert body["views"]["sort_order"] == 1.0
        assert body["analytics"]["is_pinned"] is False, "an untouched default was altered"
        assert list(body)[0] == "views", "response is not sorted by sort_order"

    def test_sidebar_response_matches_upstream_when_all_rows_stored(self, stack, client):
        """With every row stored, the in-memory defaults contribute nothing
        and the response must be exactly the stored set."""
        for i, key in enumerate(["views", "active_cycles", "analytics", "drafts", "your_work", "archives", "stickies"]):
            WorkspaceUserPreference.objects.create(
                workspace=stack.workspace,
                user=stack.owner,
                key=key,
                is_pinned=(i % 2 == 0),
                sort_order=float(i),
            )

        body = client.get(f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/").json()

        assert list(body) == ["views", "active_cycles", "analytics", "drafts", "your_work", "archives", "stickies"]
        assert [row["is_pinned"] for row in body.values()] == [True, False, True, False, True, False, True]


@pytest.mark.django_db
class TestProjectPropertyRejectionLeavesNoRow:
    """The project handler created the row and then validated with
    raise_exception=True. DRF turns that into a 400 response rather than
    letting it propagate, so nothing rolled back."""

    def test_invalid_first_patch_leaves_no_row(self, stack, client):
        slug, pid = stack.workspace.slug, stack.project.id
        assert ProjectUserProperty.objects.count() == 0

        response = client.patch(
            f"/api/workspaces/{slug}/projects/{pid}/user-properties/",
            {"sort_order": "not-a-number"},
            format="json",
        )

        assert response.status_code == 400, response.content
        assert ProjectUserProperty.objects.count() == 0, (
            "a rejected first PATCH left behind the row it created"
        )


@pytest.mark.django_db
class TestSidebarKeyPresence:
    """A missing or empty key used to `continue`, so a malformed item was
    silently dropped while the rest of the batch applied and the caller still
    got a 200 — a partial apply reported as success."""

    @pytest.mark.parametrize("bad_key", [None, "", "   ", 7])
    def test_missing_or_empty_key_is_400(self, stack, client, bad_key):
        item = {"is_pinned": True} if bad_key is None else {"key": bad_key, "is_pinned": True}

        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [item],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0

    def test_mixed_batch_with_keyless_item_applies_nothing(self, stack, client):
        """The valid entries must not land while the keyless one is skipped."""
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [
                {"key": "drafts", "is_pinned": True, "sort_order": 1.0},
                {"is_pinned": True},
                {"key": "views", "is_pinned": True, "sort_order": 2.0},
            ],
            format="json",
        )

        assert response.status_code == 400, response.content
        assert WorkspaceUserPreference.objects.count() == 0, "a keyless item was skipped and the rest applied"


@pytest.mark.django_db
class TestStrictBooleanGuard:
    """is_pinned goes straight onto a BooleanField. Postgres and Django will
    happily coerce some strings, so a guard that only rejects obvious junk
    leaves the coercible cases writing a value the caller did not send."""

    @pytest.mark.parametrize("coercible", ["true", "True", "false", "1", "0", 1, 0])
    def test_coercible_is_pinned_is_still_rejected(self, stack, client, coercible):
        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/sidebar-preferences/",
            [{"key": "drafts", "is_pinned": coercible}],
            format="json",
        )

        assert response.status_code == 400, f"{coercible!r} was accepted: {response.content!r}"
        assert WorkspaceUserPreference.objects.count() == 0


@pytest.mark.django_db
class TestModuleRouteScopeAndContracts:
    """Module needed the same route-scope coverage cycle already had, on both
    methods, plus the fallback and stored-identity contracts."""

    def test_get_cross_project_module_is_404(self, stack, client):
        other_project = ProjectFactory(workspace=stack.workspace, name="Other Project 3", identifier="OTHR3")
        ProjectMemberFactory(project=other_project, member=stack.owner, role=20)
        foreign_module = Module.objects.create(
            name="Foreign Module", project=other_project, workspace=stack.workspace
        )

        response = client.get(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/modules/{foreign_module.id}/user-properties/"
        )

        assert response.status_code == 404, response.content

    def test_get_unknown_module_id_is_404(self, stack, client):
        from uuid import uuid4

        response = client.get(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/modules/{uuid4()}/user-properties/"
        )

        assert response.status_code == 404, response.content

    def test_get_unknown_cycle_id_is_404(self, stack, client):
        from uuid import uuid4

        response = client.get(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/cycles/{uuid4()}/user-properties/"
        )

        assert response.status_code == 404, response.content

    def test_module_absent_row_reports_null_id(self, stack, client):
        response = client.get(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/modules/{stack.module.id}/user-properties/"
        )

        assert response.status_code == 200
        assert response.json()["id"] is None

    def test_module_stored_row_reports_its_real_id(self, stack, client):
        row = ModuleUserProperties.objects.create(
            user=stack.owner, project_id=stack.project.id, module_id=stack.module.id
        )

        response = client.get(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/modules/{stack.module.id}/user-properties/"
        )

        assert response.json()["id"] == str(row.id)

    def test_workspace_absent_row_reports_null_id(self, stack, client):
        response = client.get(f"/api/workspaces/{stack.workspace.slug}/user-properties/")

        assert response.status_code == 200
        assert response.json()["id"] is None

    def test_workspace_stored_row_reports_its_real_id(self, stack, client):
        row = WorkspaceUserProperties.objects.create(workspace=stack.workspace, user=stack.owner)

        response = client.get(f"/api/workspaces/{stack.workspace.slug}/user-properties/")

        assert response.json()["id"] == str(row.id)

    def test_get_does_not_mutate_stored_timestamps(self, stack, client):
        """A read must not touch updated_at — that would be a write wearing a
        different name, and it is how a 'harmless' create-on-read hides."""
        row = ProjectUserProperty.objects.create(user=stack.owner, project_id=stack.project.id)
        before_updated = row.updated_at
        before_created = row.created_at

        for _ in range(3):
            client.get(
                f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}/user-properties/"
            )

        row.refresh_from_db()
        assert row.updated_at == before_updated, "GET mutated updated_at"
        assert row.created_at == before_created
        assert ProjectUserProperty.objects.count() == 1


@pytest.mark.django_db
class TestExistingChildCrossProject:
    """An id that does not exist is the easy case. The one that matters is a
    child that DOES exist, just not on the routed project."""

    def test_patch_existing_cross_project_module_is_404_and_writes_nothing(self, stack, client):
        other_project = ProjectFactory(workspace=stack.workspace, name="Other Project 4", identifier="OTHR4")
        ProjectMemberFactory(project=other_project, member=stack.owner, role=20)
        foreign_module = Module.objects.create(
            name="Foreign Module 2", project=other_project, workspace=stack.workspace
        )

        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/modules/{foreign_module.id}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 404, response.content
        assert ModuleUserProperties.objects.count() == 0, "a foreign module was bound to the routed project"

    def test_patch_cross_workspace_cycle_is_404_and_writes_nothing(self, stack, client):
        """Same shape, but the child lives in a different WORKSPACE."""
        other_owner = UserFactory(username="other-ws-owner")
        other_workspace = WorkspaceFactory(owner=other_owner, slug="other-workspace-x")
        WorkspaceMemberFactory(workspace=other_workspace, member=stack.owner, role=20)
        other_project = ProjectFactory(workspace=other_workspace, name="X Project", identifier="XPRJ1")
        ProjectMemberFactory(project=other_project, member=stack.owner, role=20)
        foreign_cycle = Cycle.objects.create(
            name="X Cycle", project=other_project, workspace=other_workspace, owned_by=stack.owner
        )

        response = client.patch(
            f"/api/workspaces/{stack.workspace.slug}/projects/{stack.project.id}"
            f"/cycles/{foreign_cycle.id}/user-properties/",
            {"filters": {"priority": ["urgent"]}},
            format="json",
        )

        assert response.status_code == 404, response.content
        assert CycleUserProperties.objects.count() == 0


@pytest.mark.django_db
class TestSingletonIdentityAndTimestamps:
    """The same two contracts across all four singleton endpoints: a stored
    row reports its real id, and a GET never moves its timestamps."""

    def singletons(self, stack):
        slug, pid = stack.workspace.slug, stack.project.id
        return [
            (
                "project",
                f"/api/workspaces/{slug}/projects/{pid}/user-properties/",
                ProjectUserProperty,
                {"user": stack.owner, "project_id": pid},
            ),
            (
                "cycle",
                f"/api/workspaces/{slug}/projects/{pid}/cycles/{stack.cycle.id}/user-properties/",
                CycleUserProperties,
                {"user": stack.owner, "project_id": pid, "cycle_id": stack.cycle.id},
            ),
            (
                "module",
                f"/api/workspaces/{slug}/projects/{pid}/modules/{stack.module.id}/user-properties/",
                ModuleUserProperties,
                {"user": stack.owner, "project_id": pid, "module_id": stack.module.id},
            ),
            (
                "workspace",
                f"/api/workspaces/{slug}/user-properties/",
                WorkspaceUserProperties,
                {"user": stack.owner, "workspace": stack.workspace},
            ),
        ]

    def test_stored_row_reports_real_id_on_every_singleton(self, stack, client):
        for name, url, model, kwargs in self.singletons(stack):
            model.objects.all().delete()
            row = model.objects.create(**kwargs)

            response = client.get(url)

            assert response.status_code == 200, f"{name}: {response.content!r}"
            assert response.json()["id"] == str(row.id), f"{name}: wrong id"

    def test_get_never_moves_timestamps_on_any_singleton(self, stack, client):
        for name, url, model, kwargs in self.singletons(stack):
            model.objects.all().delete()
            row = model.objects.create(**kwargs)
            before_updated, before_created = row.updated_at, row.created_at

            for _ in range(3):
                assert client.get(url).status_code == 200

            row.refresh_from_db()
            assert row.updated_at == before_updated, f"{name}: GET moved updated_at"
            assert row.created_at == before_created, f"{name}: GET moved created_at"
            assert model.objects.count() == 1, f"{name}: GET created an extra row"
