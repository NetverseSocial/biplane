# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-15 slice 3 WIRING (Morrow RC 3170 + 10147), witnessed end to end.

The accessors existing is not GitLab support; these tests witness the seam
through the real endpoint: an opted-in GitLab delivery, in GitLab's own
payload shape, under GitLab's OWN credential, scoped by a PROVIDER-QUALIFIED
map key, moves an issue through the same durable inbox as a Forgejo one — and
malformed, mis-scoped, capped-truncated or unknown-forge deliveries fail
exactly as loudly as the design demands.
"""

import hashlib
import hmac
import json
import uuid as uuid_lib

import pytest
from django.test import Client, override_settings
from django.utils import timezone as dj_timezone

from plane.bgtasks.forgejo_bridge_task import reconcile_forgejo_deliveries
from plane.bridge import write_boundary
from plane.db.models import ForgejoDelivery, IssueActivity

from .test_forgejo_bridge import (
    REPO,
    SECRET,
    URL,
    _due_now,
    _fixture,
    _post,
)

GL_TOKEN = "a-gitlab-token-long-enough"
GH_SECRET = "a-github-secret-long-enough"


def _refusals(row=None):
    """The write-boundary refusals recorded on a delivery (BIP-67).

    Every test in this file that used to prove the seam by watching a ticket
    MOVE now proves it by watching a refusal get RECORDED. That substitution is
    faithful because none of these tests is about the transition: each asks
    whether a delivery reached the shared processing path at all, and a
    refusal naming the ticket is that same evidence, one layer earlier.

    THE SUBSTITUTION IS NOT FREE, AND THIS HELPER IS WHY IT IS SAFE. A boundary
    that refuses everything makes "the ticket did not move" true for every
    reason at once, so a NEGATIVE test asserting a ticket stayed put stops
    discriminating the moment the boundary lands — it would pass with the
    scope guard, the credential check, and the shape validator all deleted.
    Where that happens below, the assertion is strengthened to say the ref was
    never PROCESSED (no refusal recorded), which the boundary cannot fake.
    """
    row = row or ForgejoDelivery.objects.get()
    return ((row.result or {}).get("ignored") or {}).get("unverified") or []


def _refused_tickets(row=None):
    return [entry["ticket"] for entry in _refusals(row)]

OPT_IN = override_settings(BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True)


@pytest.fixture(autouse=True)
def _bridge_credentials(settings):
    # autouse fixtures do not cross module boundaries — and per Morrow 10146
    # each personality now has its OWN credential; without these, every
    # delivery is refused 403 and the tests below would test the wrong gate.
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    settings.GITLAB_WEBHOOK_TOKEN = GL_TOKEN
    settings.GITHUB_WEBHOOK_SECRET = GH_SECRET
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    settings.GITHUB_INSTANCE_ID = "github"
    settings.GITLAB_INSTANCE_ID = "gitlab"


GL_PROJECT_ID = 1337
GH_REPO_ID = 4242


def _project_ids(ws):
    from plane.db.models import Project

    return [str(pid) for pid in Project.objects.filter(workspace=ws).values_list("id", flat=True)]


def _gl_scoped(ws):
    # STABLE-ID key (Morrow, PR 18 gate): the tenancy boundary is the
    # provider-qualified immutable repository id, never the mutable path.
    # BIP-38: the value is the explicit project-UUID scope.
    return override_settings(
        FORGEJO_BRIDGE_REPO_MAP=json.dumps({f"gitlab:{GL_PROJECT_ID}": _project_ids(ws)})
    )


def _gitlab_post(client, event_header, payload, delivery_id=None):
    body = json.dumps(payload).encode()
    return client.post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_GITLAB_TOKEN=GL_TOKEN,
        HTTP_X_GITLAB_EVENT=event_header,
        HTTP_X_GITLAB_EVENT_UUID=delivery_id or str(uuid_lib.uuid4()),
    )


def _github_post(client, event_header, payload, delivery_id=None):
    body = json.dumps(payload).encode()
    sig = hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=f"sha256={sig}",
        HTTP_X_GITHUB_EVENT=event_header,
        HTTP_X_GITHUB_DELIVERY=delivery_id or str(uuid_lib.uuid4()),
    )


def _gitlab_push(message):
    return {
        "project": {"path_with_namespace": REPO, "id": GL_PROJECT_ID},
        "commits": [{"id": "a" * 40, "message": message}],
    }


def _gitlab_merge(description, title=""):
    return {
        "project": {"path_with_namespace": REPO, "id": GL_PROJECT_ID},
        "object_attributes": {
            "action": "merge",
            "iid": 7,
            "title": title,
            "description": description,
        },
    }


@pytest.mark.django_db
class TestGitLabEndToEnd:
    def test_an_opted_in_push_reaches_the_shared_processing_path(self):
        """Renamed from ..._moves_the_issue: the write boundary refuses every
        push, so the witness is the recorded refusal rather than the move. What
        this proves is unchanged — a GitLab-shaped push, under GitLab's own
        credential and a provider-qualified map key, was READ in GitLab's field
        names and its ref resolved to a real ticket."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", _gitlab_push(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed" and row.forge == "gitlab" and row.repository == REPO
        assert _refused_tickets(row) == [f"{ident}-{issue.sequence_id}"]

    def test_an_opted_in_merge_request_reaches_the_shared_processing_path(self):
        """The merge-request half of the seam. A `closes` on a merge produces
        the COMPLETION refusal rather than the advance one, so this also pins
        that the two events are still told apart after the boundary."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Merge Request Hook", _gitlab_merge(f"closes {ident}-{issue.sequence_id}"))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"]
        assert _refusals()[0]["reason"] == write_boundary.BINDING_UNAVAILABLE

    def test_opted_in_merge_request_title_is_INERT(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(
                Client(),
                "Merge Request Hook",
                _gitlab_merge("", title=f"closes {ident}-{issue.sequence_id}"),
            )
        assert r.status_code == 200, r.content
        assert r.json()["moved"] == []
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_without_the_opt_in_gitlab_is_still_refused(self):
        """Unconverted on purpose, and it still discriminates: it asserts NO
        DELIVERY ROW EXISTS, which the write boundary cannot produce — the
        boundary runs long after a row is stored."""
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws):
            r = _gitlab_post(Client(), "Push Hook", _gitlab_push(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 403
        assert ForgejoDelivery.objects.count() == 0
        issue.refresh_from_db()
        assert issue.state.name == "Todo"


@pytest.mark.django_db
class TestProviderScopedTenancy:
    """Morrow 10147 blocking 2: a display path is not an identity. The same
    org/repo spelling on another forge must not inherit the workspace."""

    def test_a_bare_legacy_key_never_grants_gitlab_authority(self):
        # The violating direction: the operator mapped their FORGEJO repo with
        # the historical bare key. A correctly-authenticated GitLab delivery
        # whose project spells the same path must be inert — accepted, stored,
        # zero moves.
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: _project_ids(ws)})), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", _gitlab_push(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"  # no move: unscoped for THIS provider
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed" and ((row.result or {}).get("ignored") or {}).get("unscoped_repo") == REPO

    def test_the_bare_key_still_works_for_forgejo(self):
        # The legacy contract this preserves is the bare path KEY (Forgejo
        # only, warned). The VALUE is not grandfathered: BIP-38 retired the
        # workspace-slug value as a config defect, because the workspace-wide
        # grant IS the cross-project mover — so this map pairs the legacy key
        # with the current project-UUID scope.
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: _project_ids(ws)})):
            r = _post(
                Client(),
                "push",
                {"repository": {"full_name": REPO}, "commits": [{"id": "c" * 40, "message": f"refs {ident}-{issue.sequence_id}"}]},
            )
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"], (
            "the legacy bare path key must still SELECT the scoped project"
        )


@pytest.mark.django_db
class TestGitLabShapeIsValidatedInGitLabTerms:
    """Laundering GitLab's shape through Forgejo's would turn malformed into
    empty. The validator must reject in GitLab's own field names."""

    def test_missing_project_is_400_nothing_stored(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", {"commits": []})
        assert r.status_code == 400
        assert b"project" in r.content
        assert ForgejoDelivery.objects.count() == 0

    def test_missing_object_attributes_is_400_nothing_stored(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(
                Client(), "Merge Request Hook", {"project": {"path_with_namespace": REPO}}
            )
        assert r.status_code == 400
        assert b"object_attributes" in r.content
        assert ForgejoDelivery.objects.count() == 0

    def test_a_forgejo_shaped_body_does_not_pass_as_gitlab(self):
        # The exact laundering case: a payload with repository.full_name but
        # no project. GitLab's validator must not find a repo in it.
        u, ws, proj, issue, seqs, ident = _fixture()
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(
                Client(),
                "Push Hook",
                {"repository": {"full_name": REPO}, "commits": []},
            )
        assert r.status_code == 400
        assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
class TestForgeBindingAndTruncation:
    def test_reused_delivery_id_from_another_forge_is_409(self):
        # A body deliberately valid under BOTH shapes, so digest, event and
        # repo all match and ONLY the forge differs — the one axis the old
        # binding check could not see.
        u, ws, proj, issue, seqs, ident = _fixture()
        chimera = {
            "repository": {"full_name": REPO},
            "project": {"path_with_namespace": REPO, "id": GL_PROJECT_ID},
            "commits": [],
        }
        shared_id = str(uuid_lib.uuid4())
        with _gl_scoped(ws), OPT_IN:
            first = _gitlab_post(Client(), "Push Hook", chimera, delivery_id=shared_id)
            assert first.status_code == 200, first.content
            second = _post(Client(), "push", chimera, delivery_id=shared_id)
        assert second.status_code == 409
        assert ForgejoDelivery.objects.count() == 1
        assert ForgejoDelivery.objects.get().forge == "gitlab"

    def test_truncated_gitlab_push_defers_then_stays_pending_loudly(self):
        # GitLab declares its count under total_commits_count. There is no
        # GitLab range resolver, so the delivery must never be processed
        # partially (silent ref loss) — it defers, and the reconciler records
        # WHY it cannot proceed.
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = _gitlab_push(f"refs {ident}-{issue.sequence_id}")
        payload["total_commits_count"] = 5
        payload["before"] = "c" * 40
        payload["after"] = "d" * 40
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", payload)
            assert r.status_code == 202, r.content
            _due_now()
            assert reconcile_forgejo_deliveries() == 0
        row = ForgejoDelivery.objects.get()
        assert row.status == "pending"
        assert "range resolution" in (row.last_error or "")
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_github_push_at_the_cap_is_treated_as_possibly_truncated(self):
        # Morrow 10147 blocking 1, the real GitHub contract: no total field,
        # commits capped at 2048. The ONLY ref lives in a commit the cap would
        # have dropped — processing the delivered array would silently lose
        # it, so the delivery must defer and stay pending loudly instead.
        u, ws, proj, issue, seqs, ident = _fixture()
        noise = [{"id": f"{n:04d}" + "e" * 36, "message": f"noise {n}"} for n in range(2048)]
        payload = {
            "repository": {"full_name": REPO, "id": GH_REPO_ID},
            "commits": noise,  # the "refs {ident}-N" commit was beyond the cap
            "before": "a" * 40,
            "after": "b" * 40,
        }
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({f"github:{GH_REPO_ID}": _project_ids(ws)})):
            r = _github_post(Client(), "push", payload)
            assert r.status_code == 202, r.content
            _due_now()
            assert reconcile_forgejo_deliveries() == 0
        row = ForgejoDelivery.objects.get()
        assert row.status == "pending" and row.forge == "github"
        assert "cap of 2048" in (row.last_error or "")
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_a_github_push_below_the_cap_processes_normally(self):
        # Without this, deferring EVERYTHING would pass the cap test above.
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = {
            "repository": {"full_name": REPO, "id": GH_REPO_ID},
            "commits": [{"id": "d" * 40, "message": f"refs {ident}-{issue.sequence_id}"}],
        }
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({f"github:{GH_REPO_ID}": _project_ids(ws)})):
            r = _github_post(Client(), "push", payload)
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        # The POSITIVE CONTROL for the cap test above, and it has to stay one:
        # if this asserted only that nothing moved, deferring EVERY push would
        # pass both. A recorded refusal proves the push was actually PROCESSED.
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"]


@pytest.mark.django_db
class TestUnknownStoredForgeFailsClosed:
    def test_an_unknown_forge_name_stays_pending_with_zero_writes(self):
        # Morrow 10147 blocking 3: by_name(...) or ForgejoForge silently
        # interpreted a corrupt/removed/future personality with Forgejo's
        # payload semantics. It must stay pending as a loud data defect.
        u, ws, proj, issue, seqs, ident = _fixture()
        ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()),
            forge="ghost-forge",
            event="push",
            payload={"repository": {"full_name": REPO}, "commits": []},
            repository=REPO,
            body_digest="0" * 64,
            status="pending",
            next_attempt_at=dj_timezone.now() - dj_timezone.timedelta(seconds=1),
        )
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: _project_ids(ws)})):
            assert reconcile_forgejo_deliveries() == 0
        row = ForgejoDelivery.objects.get()
        assert row.status == "pending"
        assert "stores unknown forge" in (row.last_error or "")
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert IssueActivity.objects.count() == 0


@pytest.mark.django_db
class TestStableIdIsTheTenancyBoundary:
    """Morrow, PR 18 gate: a display path is mutable — rename + path reuse
    would transfer workspace authority to a DIFFERENT repository. The id-keyed
    entry must match the id, never the spelling."""

    def test_same_path_different_stable_id_is_inert(self):
        # The violating rename/reuse case: the mapped project was renamed
        # away, a NEW project (different id) now spells the same path, and a
        # correctly-authenticated delivery arrives from it. Zero moves.
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = _gitlab_push(f"refs {ident}-{issue.sequence_id}")
        payload["project"]["id"] = GL_PROJECT_ID + 1  # same path, different repo
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", payload)
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert ((ForgejoDelivery.objects.get().result or {}).get("ignored") or {}).get("unscoped_repo") == REPO

    def test_the_mapped_stable_id_still_selects_whatever_the_path_says(self):
        # The rename in the other direction: same repository (same id), new
        # display path. Authority follows the repository.
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = _gitlab_push(f"refs {ident}-{issue.sequence_id}")
        payload["project"]["path_with_namespace"] = "totally/renamed"
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", payload)
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"], (
            "authority follows the repository id, not the display path"
        )

    def test_a_payload_with_no_stable_id_matches_no_id_keyed_entry(self):
        """CONVERTED THOUGH IT WAS ALREADY GREEN, and that is the point.

        This is the negative arm of the pair above, and its only assertion was
        that the ticket stayed in Todo. Under the write boundary every ticket
        stays in Todo, so this test began passing for a reason unrelated to
        what it tests: it would have gone on passing with the id-keyed scope
        lookup deleted entirely, and nobody would have re-run a green test to
        find out.

        The discriminating observable is now whether the ref was PROCESSED. A
        payload with no stable id must match no entry, so nothing reaches the
        boundary and NO refusal is recorded — distinct from the positive arm,
        which records one."""
        u, ws, proj, issue, seqs, ident = _fixture()
        payload = _gitlab_push(f"refs {ident}-{issue.sequence_id}")
        del payload["project"]["id"]
        with _gl_scoped(ws), OPT_IN:
            r = _gitlab_post(Client(), "Push Hook", payload)
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"  # fail closed to inert, no path guess
        assert _refusals() == [], "an unscoped delivery must not even reach the boundary"

    def test_the_legacy_forgejo_path_key_warns_loudly(self, caplog):
        import logging as logging_mod

        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: _project_ids(ws)})), caplog.at_level(
            logging_mod.WARNING, logger="plane.worker"
        ):
            r = _post(
                Client(),
                "push",
                {"repository": {"full_name": REPO}, "commits": [{"id": "e" * 40, "message": f"refs {ident}-{issue.sequence_id}"}]},
            )
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        # ...migration path still works — the ref was selected and processed...
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"]
        assert any("LEGACY path key" in rec.getMessage() for rec in caplog.records)  # ...but never silently
