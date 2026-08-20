# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""M8 outcome ledger, step 1 (BIP-37): the outcome-by-key read, through the
real route with a real APIToken — the retry discipline every caller builds on
(query-before-retry, §M8.1) is tested against the endpoint it will use.

Principal scope is asserted the hard way: another principal's stored outcome
answers 404 exactly like a never-committed key, because the principal comes
from the server-bound token identity (M7), never from the request."""

import uuid as uuid_lib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections
from rest_framework.test import APIClient

from plane.board.service import execute_transition
from plane.db.models import (
    AuditOutbox,
    BoardOperation,
    Issue,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.db.models.api import APIToken


def _principal_client():
    user = User.objects.create(email=f"bo-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    token = APIToken.objects.create(user=user, label="t", token=f"tok-{uuid_lib.uuid4().hex}")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return user, client


def _board_fixture(names=("one", "two", "three")):
    user, client = _principal_client()
    suffix = uuid_lib.uuid4().hex[:8]
    workspace = Workspace.objects.create(slug=f"board-{suffix}", name="Board reads", owner=user)
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=20)
    project = Project.objects.create(
        workspace=workspace,
        name="Board project",
        identifier=f"B{suffix[:5].upper()}",
    )
    ProjectMember.objects.create(
        workspace=workspace,
        project=project,
        member=user,
        role=20,
        is_active=True,
    )
    state = State.objects.create(
        workspace=workspace,
        project=project,
        name="Todo",
        color="#60646C",
        group="unstarted",
        default=True,
    )
    rows = [
        Issue.objects.create(
            workspace=workspace,
            project=project,
            state=state,
            name=name,
            description_html=f"<p>{name} body</p>",
        )
        for name in names
    ]
    path = f"/api/v1/board/work-items/{workspace.slug}/{project.identifier}/"
    return user, client, workspace, project, rows, path


def _transition_envelope(user, workspace, project, issue, state, *, op_key="transition-1"):
    return {
        "op_key": op_key,
        "expected_principal_id": str(user.id),
        "source": "api",
        "verb": "transition",
        "workspace": workspace.slug,
        "project": project.identifier,
        "payload": {"sequence_id": issue.sequence_id, "state_id": str(state.id)},
    }


@pytest.mark.django_db
class TestBoardOperationOutcomeRead:
    def test_unknown_key_is_a_safe_404(self):
        _user, client = _principal_client()
        r = client.get("/api/v1/board/ops/never-committed-key/")
        assert r.status_code == 404

    def test_stored_outcome_is_returned_by_key(self):
        user, client = _principal_client()
        BoardOperation.objects.create(
            principal=str(user.id),
            op_key="op-abc",
            source="api",
            request_digest="0" * 64,
            verb="transition",
            workspace_slug="w",
            project_identifier="P",
            outcome={"moved": "P-1", "to": "Review"},
        )
        r = client.get("/api/v1/board/ops/op-abc/")
        assert r.status_code == 200
        body = r.json()
        assert body["outcome"] == {"moved": "P-1", "to": "Review"}
        assert body["verb"] == "transition"
        assert body["replayed"] is True

    def test_another_principals_outcome_is_an_indistinguishable_404(self):
        owner, _owner_client = _principal_client()
        BoardOperation.objects.create(
            principal=str(owner.id),
            op_key="op-abc",
            source="api",
            request_digest="0" * 64,
            verb="transition",
            workspace_slug="w",
            outcome={"moved": "P-1"},
        )
        _probe_user, probe_client = _principal_client()
        r_theirs = probe_client.get("/api/v1/board/ops/op-abc/")
        r_nothing = probe_client.get("/api/v1/board/ops/no-such-key/")
        assert r_theirs.status_code == 404
        assert r_nothing.status_code == 404
        assert r_theirs.json() == r_nothing.json(), "wrong-principal and never-committed must be indistinguishable"

    def test_no_token_is_refused(self):
        r = APIClient().get("/api/v1/board/ops/op-abc/")
        assert r.status_code in (401, 403)

    def test_key_uniqueness_is_per_principal_not_global(self):
        """Two principals may durably hold the SAME op_key — the scope is
        (principal, op_key), so caller-side key minting needs no global
        coordination. Each reads back only its own outcome."""
        u1, c1 = _principal_client()
        u2, c2 = _principal_client()
        BoardOperation.objects.create(
            principal=str(u1.id),
            op_key="shared-key",
            source="api",
            request_digest="1" * 64,
            verb="transition",
            workspace_slug="w",
            outcome={"who": "one"},
        )
        BoardOperation.objects.create(
            principal=str(u2.id),
            op_key="shared-key",
            source="api",
            request_digest="2" * 64,
            verb="transition",
            workspace_slug="w",
            outcome={"who": "two"},
        )
        assert c1.get("/api/v1/board/ops/shared-key/").json()["outcome"] == {"who": "one"}
        assert c2.get("/api/v1/board/ops/shared-key/").json()["outcome"] == {"who": "two"}


@pytest.mark.django_db
class TestBoardOperationTransition:
    def test_transition_commits_mutation_outcome_and_audit_together(self):
        user, client, workspace, project, rows, _path = _board_fixture(("move me",))
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Done",
            color="#46A758",
            group="completed",
        )
        envelope = _transition_envelope(user, workspace, project, rows[0], target)

        response = client.post("/api/v1/board/ops/", envelope, format="json")

        assert response.status_code == 201, response.content
        rows[0].refresh_from_db()
        assert rows[0].state_id == target.id
        assert rows[0].completed_at is not None
        operation = BoardOperation.objects.get(principal=str(user.id), op_key="transition-1")
        assert operation.source == "api"
        assert len(operation.request_digest) == 64
        assert operation.outcome["changed"] is True
        assert operation.outcome["work_item"]["state"] == {
            "id": str(target.id),
            "name": "Done",
            "group": "completed",
        }
        audit = AuditOutbox.objects.get()
        assert audit.task == "issue_activity"
        assert audit.payload["actor_id"] == str(user.id)
        assert audit.payload["issue_id"] == str(rows[0].id)

    def test_exact_retry_returns_the_stored_outcome_without_second_write(self):
        user, client, workspace, project, rows, _path = _board_fixture(("move once",))
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Review",
            color="#F59E0B",
            group="started",
        )
        envelope = _transition_envelope(user, workspace, project, rows[0], target)

        first = client.post("/api/v1/board/ops/", envelope, format="json")
        second = client.post("/api/v1/board/ops/", envelope, format="json")

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["replayed"] is True
        assert second.json()["outcome"] == first.json()["outcome"]
        assert BoardOperation.objects.count() == 1
        assert AuditOutbox.objects.count() == 1

    def test_key_reuse_with_a_different_request_is_a_conflict(self):
        user, client, workspace, project, rows, _path = _board_fixture(("bound",))
        review = State.objects.create(
            workspace=workspace,
            project=project,
            name="Review",
            color="#F59E0B",
            group="started",
        )
        done = State.objects.create(
            workspace=workspace,
            project=project,
            name="Done",
            color="#46A758",
            group="completed",
        )
        original = _transition_envelope(user, workspace, project, rows[0], review)
        changed = _transition_envelope(user, workspace, project, rows[0], done)

        assert client.post("/api/v1/board/ops/", original, format="json").status_code == 201
        response = client.post("/api/v1/board/ops/", changed, format="json")

        assert response.status_code == 409
        rows[0].refresh_from_db()
        assert rows[0].state_id == review.id
        assert BoardOperation.objects.count() == 1
        assert AuditOutbox.objects.count() == 1

    def test_audit_failure_rolls_back_claim_and_domain_write(self, monkeypatch):
        user, _client, workspace, project, rows, _path = _board_fixture(("atomic",))
        original_state_id = rows[0].state_id
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Done",
            color="#46A758",
            group="completed",
        )

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr("plane.board.service.enqueue_audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            execute_transition(
                principal=user,
                envelope=_transition_envelope(user, workspace, project, rows[0], target),
            )

        rows[0].refresh_from_db()
        assert rows[0].state_id == original_state_id
        assert BoardOperation.objects.count() == 0
        assert AuditOutbox.objects.count() == 0

    def test_membership_and_scope_are_revalidated_server_side(self):
        user, client, workspace, project, rows, _path = _board_fixture(("protected",))
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Review",
            color="#F59E0B",
            group="started",
        )
        ProjectMember.objects.filter(project=project, member=user).update(is_active=False)

        response = client.post(
            "/api/v1/board/ops/",
            _transition_envelope(user, workspace, project, rows[0], target),
            format="json",
        )

        assert response.status_code == 403
        rows[0].refresh_from_db()
        assert rows[0].state_id != target.id
        assert BoardOperation.objects.count() == 0
        assert AuditOutbox.objects.count() == 0

    @pytest.mark.parametrize(
        "mutation",
        [
            {"unexpected": True},
            {"principal": "caller-asserted"},
        ],
    )
    def test_unknown_or_asserted_identity_fields_are_refused(self, mutation):
        user, client, workspace, project, rows, _path = _board_fixture(("strict",))
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Review",
            color="#F59E0B",
            group="started",
        )
        envelope = _transition_envelope(user, workspace, project, rows[0], target)
        envelope.update(mutation)

        response = client.post("/api/v1/board/ops/", envelope, format="json")

        assert response.status_code == 400
        assert BoardOperation.objects.count() == 0

    def test_expected_principal_mismatch_refuses_before_the_operation_claim(self):
        user, client, workspace, project, rows, _path = _board_fixture(("identity-bound",))
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Review",
            color="#F59E0B",
            group="started",
        )
        envelope = _transition_envelope(user, workspace, project, rows[0], target)
        envelope["expected_principal_id"] = str(uuid_lib.uuid4())

        response = client.post("/api/v1/board/ops/", envelope, format="json")

        assert response.status_code == 403
        assert BoardOperation.objects.count() == 0
        assert AuditOutbox.objects.count() == 0

    def test_legacy_token_patch_cannot_bypass_the_operation_door(self):
        _user, client, workspace, project, rows, _path = _board_fixture(("one door",))
        target = State.objects.create(
            workspace=workspace,
            project=project,
            name="Review",
            color="#F59E0B",
            group="started",
        )
        legacy_path = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/issues/{rows[0].id}/"

        response = client.patch(legacy_path, {"state": str(target.id)}, format="json")

        assert response.status_code == 400
        rows[0].refresh_from_db()
        assert rows[0].state_id != target.id
        assert BoardOperation.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_two_workers_racing_one_key_execute_exactly_once(monkeypatch):
    user, _client, workspace, project, rows, _path = _board_fixture(("raced",))
    target = State.objects.create(
        workspace=workspace,
        project=project,
        name="Review",
        color="#F59E0B",
        group="started",
    )
    envelope = _transition_envelope(user, workspace, project, rows[0], target)
    original_create = BoardOperation.objects.create
    both_at_insert = Barrier(2)

    def synchronized_create(*args, **kwargs):
        both_at_insert.wait(timeout=10)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(BoardOperation.objects, "create", synchronized_create)

    def execute():
        close_old_connections()
        try:
            return execute_transition(principal=user, envelope=envelope).replayed
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: execute(), range(2)))

    rows[0].refresh_from_db()
    assert sorted(results) == [False, True]
    assert rows[0].state_id == target.id
    assert BoardOperation.objects.count() == 1
    assert AuditOutbox.objects.count() == 1


@pytest.mark.django_db
class TestBoardWorkItemReads:
    def test_default_list_is_complete_not_a_plausible_first_page(self):
        _user, client, _workspace, _project, _rows, path = _board_fixture()

        response = client.get(path)

        assert response.status_code == 200, response.content
        body = response.json()
        assert [item["name"] for item in body["items"]] == ["one", "two", "three"]
        assert body["count"] == 3
        assert body["complete"] is True
        assert body["truncated"] is False
        assert body["next_cursor"] is None

    def test_explicit_bound_is_honestly_truncated_and_resumable(self):
        _user, client, _workspace, _project, _rows, path = _board_fixture()

        first = client.get(path, {"limit": "2"})
        assert first.status_code == 200, first.content
        assert [item["name"] for item in first.json()["items"]] == ["one", "two"]
        assert first.json()["complete"] is False
        assert first.json()["truncated"] is True
        assert first.json()["next_cursor"] == "2"

        rest = client.get(path, {"limit": "2", "cursor": first.json()["next_cursor"]})
        assert rest.status_code == 200, rest.content
        assert [item["name"] for item in rest.json()["items"]] == ["three"]
        assert rest.json()["complete"] is True
        assert rest.json()["truncated"] is False
        assert rest.json()["next_cursor"] is None

    @pytest.mark.parametrize("field,value", [("limit", "0"), ("limit", "01"), ("limit", "x"), ("cursor", "-1")])
    def test_bounds_use_one_strict_positive_integer_grammar(self, field, value):
        _user, client, _workspace, _project, _rows, path = _board_fixture(())
        response = client.get(path, {field: value})
        assert response.status_code == 400
        assert response.json() == {"error": f"{field} must be a positive integer"}

    @pytest.mark.parametrize(
        "field,value,maximum",
        [
            ("limit", "1001", 1000),
            ("cursor", "2147483648", 2_147_483_647),
        ],
    )
    def test_database_numeric_domains_are_bounded(self, field, value, maximum):
        _user, client, _workspace, _project, _rows, path = _board_fixture(())
        response = client.get(path, {field: value})
        assert response.status_code == 400
        assert response.json() == {"error": f"{field} must not exceed {maximum}"}

    def test_detail_is_store_readback_and_exposes_plain_text(self):
        _user, client, _workspace, _project, rows, path = _board_fixture(("read me",))

        response = client.get(f"{path}{rows[0].sequence_id}/")

        assert response.status_code == 200, response.content
        assert response.json()["name"] == "read me"
        assert response.json()["description_stripped"] == "read me body"
        assert client.get(f"{path}999/").status_code == 404

    def test_project_membership_is_checked_at_the_path_scope(self):
        user, client, workspace, _project, _rows, _path = _board_fixture(())
        other = Project.objects.create(
            workspace=workspace,
            name="Not this principal's project",
            identifier=f"X{uuid_lib.uuid4().hex[:5].upper()}",
        )
        path = f"/api/v1/board/work-items/{workspace.slug}/{other.identifier}/"

        assert client.get(path).status_code == 403
        assert not ProjectMember.objects.filter(project=other, member=user).exists()

    def test_no_token_is_refused(self):
        _user, _client, _workspace, _project, _rows, path = _board_fixture(())
        assert APIClient().get(path).status_code in (401, 403)
