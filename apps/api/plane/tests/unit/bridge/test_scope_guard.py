# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-38 scope guard: repo -> project(s) mapping (docs/scope-a-architecture.md §M2).

The live defect this closes (Morrow 3275, 7of9 3276): the map granted a whole
WORKSPACE, so `refs SB-3` in one repo really moved a ticket in another team's
project. The map value is now the explicit list of project UUIDs a repo may
move items in. A ref outside that scope is REJECTED: zero writes for that ref,
durably recorded in the delivery result (ticket, repo, reason) — never just
process logs — and never looked up, so the guard is also not an existence
oracle for other tenants' projects.

Also here: the delivery_result contract unit tests (BIP-38 owns the shape —
see plane/bridge/delivery_result.py) and the alias-discriminator ordering pin
(Vex: `coalesced_to` decides whether a row EXECUTES; the holder's spread must
never overwrite the alias's own pointer)."""

import json
import logging as logging_mod
import uuid as uuid_lib

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from plane.bgtasks.forgejo_bridge_task import reconcile_forgejo_deliveries
from plane.bridge import delivery_result
from plane.bridge.forgejo_bridge import (
    _resolve_alias,
    claim_delivery,
    process_delivery,
)
from plane.db.models import ForgejoDelivery, Issue, IssueActivity, Project, State, User

from .test_forgejo_bridge import (
    REPO,
    SECRET,
    _due_now,
    _fixture,
    _post,
    _push_payload,
)


@pytest.fixture(autouse=True)
def _bridge_secret(settings):
    # autouse fixtures do not cross module boundaries: without this, every
    # delivery below is refused 403 and the tests would test the wrong gate.
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    settings.GITHUB_INSTANCE_ID = "github"
    settings.GITLAB_INSTANCE_ID = "gitlab"


def _second_project(u, ws, identifier=None):
    """A SECOND project in the same workspace — the pre-BIP-38 mover's target."""
    ident = identifier or ("SG" + uuid_lib.uuid4().hex[:3].upper())
    proj = Project.objects.create(workspace=ws, name="P2", identifier=ident)
    seqs = {}
    for i, (name, group) in enumerate(
        [("Backlog", "backlog"), ("Todo", "unstarted"), ("Review", "started"), ("Done", "completed")]
    ):
        seqs[name] = State.objects.create(
            name=name, project=proj, workspace=ws, group=group, sequence=(i + 1) * 100,
            color="#000", default=(name == "Backlog"), created_by=u,
        )
    issue = Issue.objects.create(workspace=ws, project=proj, name="other-team", state=seqs["Todo"])
    return proj, issue, seqs, ident


def _scope_only(proj):
    return override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: [str(proj.id)]}))


REASON = "project not in this repository's mapped scope"


def _refusals(row=None):
    """Write-boundary refusals on a delivery (BIP-67).

    THE SCOPE GUARD'S OBSERVABLE CHANGED, AND IT CHANGED FOR THE BETTER. Before
    the boundary, "in scope" showed up as a state move and "out of scope" as a
    cross_project record — two different KINDS of evidence, only one of which
    was in the delivery result. Now both are records in the same dict: an
    in-scope ref produces an `unverified` refusal, an out-of-scope ref produces
    a `cross_project` rejection, and which key the ticket lands under IS the
    scope decision.

    That matters here more than anywhere else in the suite, because a boundary
    that refuses every write makes "the ticket did not move" true for every
    reason at once. A scope test whose only assertion is that nothing moved
    would pass with the scope guard deleted. Every conversion below therefore
    asserts WHICH key the ticket was recorded under, never merely that it
    stayed put."""
    row = row or ForgejoDelivery.objects.get()
    return ((row.result or {}).get("ignored") or {}).get("unverified") or []


def _refused_tickets(row=None):
    return [entry["ticket"] for entry in _refusals(row)]


@pytest.mark.django_db
class TestCrossProjectRejection:
    def test_the_violating_case_cross_project_ref_is_rejected_and_recorded(self):
        """THE acceptance test: same workspace, repo scoped to project A only —
        a ref to project B moves nothing and the rejection is durable."""
        u, ws, proj, issue, seqs, ident = _fixture()
        _proj_b, issue_b, _seqs_b, ident_b = _second_project(u, ws)
        with _scope_only(proj):
            r = _post(Client(), "push", _push_payload(f"refs {ident_b}-{issue_b.sequence_id}"))
        assert r.status_code == 200, r.content
        issue_b.refresh_from_db()
        assert issue_b.state.name == "Todo", "the cross-project ticket must not move"
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed"
        assert row.result == {
            "moved": [],
            "ignored": {
                "cross_project": [
                    {"ticket": f"{ident_b}-{issue_b.sequence_id}", "repo": REPO, "reason": REASON}
                ]
            },
        }

    def test_zero_writes_for_a_fully_rejected_delivery(self):
        """§M2: forbidden refs produce ZERO state or backlink writes — not even
        the bridge actor user is materialized."""
        u, ws, proj, issue, seqs, ident = _fixture()
        _proj_b, issue_b, _seqs_b, ident_b = _second_project(u, ws)
        with _scope_only(proj):
            _post(Client(), "push", _push_payload(f"refs {ident_b}-{issue_b.sequence_id}"))
        assert not IssueActivity.objects.exists()
        # The synthetic actor helper is DELETED outright (Morrow: no product
        # job once board writes and notifications are both gone). Literal email
        # kept as a regression tripwire against anything recreating it.
        assert not User.objects.filter(email="git-bridge@biplane.invalid").exists()

    def test_in_scope_refs_are_unaffected(self):
        """The scope guard does not touch an in-scope ref: it reaches the write
        boundary and is recorded there, NOT under cross_project. The name still
        holds — 'unaffected' has always meant 'unaffected BY THE GUARD'."""
        u, ws, proj, issue, seqs, ident = _fixture()
        _second_project(u, ws)
        with _scope_only(proj):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        result = ForgejoDelivery.objects.get().result
        assert result["moved"] == []
        assert "cross_project" not in (result.get("ignored") or {})
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"]

    def test_mixed_event_allowed_ref_proceeds_forbidden_ref_writes_nothing(self):
        """§M2 mixed events, pinned: one delivery, one allowed + one forbidden
        ref — each recorded under its OWN key, with zero writes for either.

        THE SHARPEST TEST IN THIS FILE AFTER THE BOUNDARY, because both tickets
        now end in the same state and only the record tells them apart. If the
        guard stopped distinguishing them, the states would still look right
        and this assertion is the only thing that would notice."""
        u, ws, proj, issue, seqs, ident = _fixture()
        _proj_b, issue_b, _seqs_b, ident_b = _second_project(u, ws)
        message = f"refs {ident}-{issue.sequence_id}\n\nrefs {ident_b}-{issue_b.sequence_id}"
        with _scope_only(proj):
            r = _post(Client(), "push", _push_payload(message))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        issue_b.refresh_from_db()
        assert issue.state.name == "Todo"
        assert issue_b.state.name == "Todo"
        row = ForgejoDelivery.objects.get()
        assert row.result["moved"] == []
        assert row.result["ignored"]["cross_project"] == [
            {"ticket": f"{ident_b}-{issue_b.sequence_id}", "repo": REPO, "reason": REASON}
        ], "the forbidden ref must still be a SCOPE rejection, not a boundary refusal"
        assert _refused_tickets(row) == [f"{ident}-{issue.sequence_id}"], (
            "the allowed ref must still reach the boundary, and only it"
        )
        assert not IssueActivity.objects.filter(issue=issue_b).exists()
        assert not IssueActivity.objects.filter(issue=issue).exists()

    def test_rejection_is_not_an_existence_oracle(self):
        """A ref to a real project outside scope and a ref to a project that
        exists NOWHERE produce byte-identical rejection records: out-of-scope
        tickets are never looked up, existence included (_project_scope)."""
        u, ws, proj, issue, seqs, ident = _fixture()
        _proj_b, issue_b, _seqs_b, ident_b = _second_project(u, ws)

        def _rejection(ref_ident, seq):
            ForgejoDelivery.objects.all().delete()
            with _scope_only(proj):
                _post(Client(), "push", _push_payload(f"refs {ref_ident}-{seq}"))
            row = ForgejoDelivery.objects.get()
            entry = dict(row.result["ignored"]["cross_project"][0])
            entry.pop("ticket")
            return entry

        real_but_out_of_scope = _rejection(ident_b, issue_b.sequence_id)
        exists_nowhere = _rejection("ZZZZZ", 999)
        assert real_but_out_of_scope == exists_nowhere == {"repo": REPO, "reason": REASON}


def _id_payload(ident, seq, repo_id=4242):
    """A push payload CARRYING the stable repo id, so resolution takes the
    id-key path (the module's _push_payload omits it and exercises the legacy
    path-key fallback instead)."""
    return {
        "repository": {"full_name": REPO, "id": repo_id},
        "commits": [{"id": "a" * 40, "message": f"refs {ident}-{seq}"}],
    }


@pytest.mark.django_db
class TestInstanceQualifiedTenancyKeys:
    """M2's authority tuple is (provider INSTANCE, stable repo id) — Morrow's
    ruling on Vex's #76 finding. The map and the semantic event keys share ONE
    notion of provider identity: the configured instance id."""

    def test_configured_instance_id_key_is_accepted(self, settings):
        settings.FORGEJO_INSTANCE_ID = "pi5-forgejo"
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({"pi5-forgejo:4242": [str(proj.id)]})
        ):
            r = _post(Client(), "push", _id_payload(ident, issue.sequence_id))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        # ACCEPTED means the ref got through the map to the boundary. Asserting
        # only that nothing moved would make this indistinguishable from the
        # refusal case two tests down, which is the whole point of the pair.
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"]

    def test_family_name_key_is_refused_when_it_differs_and_the_refusal_is_loud(
        self, settings, caplog
    ):
        """The family prefix grants nothing when it differs from the configured
        id — and because an unmapped repo is a LEGITIMATE inert no-op (200,
        moves nothing), the refusal must be loud or it presents as 'the bridge
        is quiet', not 'the bridge is broken' (Vex; Aria's near-miss)."""
        settings.FORGEJO_INSTANCE_ID = "pi5-forgejo"
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({"forgejo:4242": [str(proj.id)]})
        ), caplog.at_level(logging_mod.WARNING, logger="plane.worker"):
            r = _post(Client(), "push", _id_payload(ident, issue.sequence_id))
        assert r.status_code == 200, r.content  # inert, not an error
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        row = ForgejoDelivery.objects.get()
        assert ((row.result or {}).get("ignored") or {}).get("unscoped_repo") == REPO
        # Exact-form assertions: "forgejo:4242" is a SUBSTRING of
        # "pi5-forgejo:4242", so a bare `in` check on both strings is
        # satisfiable by the lookup key alone. The found key appears in the
        # list repr, the lookup key single-quoted.
        assert any(
            "['forgejo:4242']" in rec.getMessage() and "'pi5-forgejo:4242'" in rec.getMessage()
            for rec in caplog.records
        ), "the wrong-prefix diagnostic must name both the found key and the lookup key"

    def test_carried_over_instance_key_is_also_refused_and_loud(self, settings, caplog):
        """The OTHER direction of the same silent-200 failure (Aria): the map
        holds a key for a DIFFERENT instance id — e.g. carried across hosts,
        or stale after an instance rename — while the live id differs. A
        one-directional pin would let this half back in."""
        settings.FORGEJO_INSTANCE_ID = "prod-forgejo"
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({"pi5-forgejo:4242": [str(proj.id)]})
        ), caplog.at_level(logging_mod.WARNING, logger="plane.worker"):
            r = _post(Client(), "push", _id_payload(ident, issue.sequence_id))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        row = ForgejoDelivery.objects.get()
        assert ((row.result or {}).get("ignored") or {}).get("unscoped_repo") == REPO
        assert any(
            "['pi5-forgejo:4242']" in rec.getMessage() and "'prod-forgejo:4242'" in rec.getMessage()
            for rec in caplog.records
        ), "the wrong-prefix diagnostic must fire in this direction too"

    def test_a_superstring_prefixed_key_grants_nothing(self, settings):
        """The boundary LOOKUP is EXACT, pinned (Aria, review 3608 on #76):
        dict.get is exact by construction today, but this key is a TENANCY
        boundary — a future refactor toward prefix tolerance (an endswith
        match) would let evil-pi5-forgejo:4242 satisfy a lookup meant for
        pi5-forgejo:4242. Same substring trap as the diagnostic assertions,
        one layer down. Green under exact lookup; red under endswith."""
        settings.FORGEJO_INSTANCE_ID = "pi5-forgejo"
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({"evil-pi5-forgejo:4242": [str(proj.id)]})
        ):
            r = _post(Client(), "push", _id_payload(ident, issue.sequence_id))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo", "a superstring-prefixed key must grant nothing"
        row = ForgejoDelivery.objects.get()
        assert ((row.result or {}).get("ignored") or {}).get("unscoped_repo") == REPO

    def test_another_providers_valid_key_with_the_same_repo_id_is_not_flagged(
        self, settings, caplog
    ):
        """Morrow RC 3600: repo ids are per-instance sequences, so a VALID
        other-provider mapping sharing the number (gitlab-prod:4242 alongside
        an unmapped Forgejo repo 4242) is normal, not stale — a diagnostic
        that tells the operator to migrate a correct key is the same class of
        defect it exists to catch. Family/stale-instance cases stay loud
        (pinned above); this case stays quiet."""
        settings.FORGEJO_INSTANCE_ID = "prod-forgejo"
        settings.GITLAB_INSTANCE_ID = "gitlab-prod"
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({"gitlab-prod:4242": [str(proj.id)]})
        ), caplog.at_level(logging_mod.WARNING, logger="plane.worker"):
            r = _post(Client(), "push", _id_payload(ident, issue.sequence_id))
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        assert issue.state.name == "Todo"  # unmapped for THIS forge: legitimately inert
        row = ForgejoDelivery.objects.get()
        assert ((row.result or {}).get("ignored") or {}).get("unscoped_repo") == REPO
        assert not any(
            "gitlab-prod:4242" in rec.getMessage() for rec in caplog.records
        ), "a valid other-provider key must not be flagged as stale"

    def test_same_family_same_repo_id_instances_are_isolated(self, settings):
        """The synthetic collision: two same-family instances, SAME numeric
        repo id (per-instance sequences make this likely, not exotic). Each
        instance's key grants only its own scope — under instance A, a ref to
        B's project is a recorded cross-project rejection, and vice versa."""
        u1, ws1, proj_a, issue_a, _s1, ident_a = _fixture()
        u2, ws2, proj_b, issue_b, _s2, ident_b = _fixture()
        both = json.dumps({"inst-a:4242": [str(proj_a.id)], "inst-b:4242": [str(proj_b.id)]})

        settings.FORGEJO_INSTANCE_ID = "inst-a"
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=both):
            r = _post(
                Client(), "push",
                _id_payload(ident_a, issue_a.sequence_id) | {
                    "commits": [{
                        "id": "a" * 40,
                        "message": f"refs {ident_a}-{issue_a.sequence_id}\n\nrefs {ident_b}-{issue_b.sequence_id}",
                    }]
                },
            )
        assert r.status_code == 200, r.content
        issue_a.refresh_from_db()
        issue_b.refresh_from_db()
        assert issue_a.state.name == "Todo"
        assert issue_b.state.name == "Todo"
        row = ForgejoDelivery.objects.get()
        assert row.result["ignored"]["cross_project"][0]["ticket"] == f"{ident_b}-{issue_b.sequence_id}", (
            "instance B's project is outside A's scope"
        )
        assert _refused_tickets(row) == [f"{ident_a}-{issue_a.sequence_id}"], (
            "instance A's own project is in scope and reaches the boundary"
        )

        ForgejoDelivery.objects.all().delete()
        settings.FORGEJO_INSTANCE_ID = "inst-b"
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=both):
            r = _post(Client(), "push", _id_payload(ident_b, issue_b.sequence_id))
        assert r.status_code == 200, r.content
        issue_b.refresh_from_db()
        assert issue_b.state.name == "Todo"
        assert _refused_tickets() == [f"{ident_b}-{issue_b.sequence_id}"], (
            "instance B reaches its own project under its own key — the isolation "
            "is symmetric, and without this half the test would pass with inst-b "
            "granted nothing at all"
        )


@pytest.mark.django_db
class TestScopeConfigDefects:
    def _503(self, map_value):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: map_value})):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        assert ForgejoDelivery.objects.get().status == "pending"  # retryable, not lost

    def test_legacy_workspace_slug_value_is_a_config_defect(self):
        """The retired schema is REFUSED, not honored: a workspace-wide grant
        is the live cross-project mover this guard exists to close."""
        self._503("some-workspace-slug")

    def test_empty_scope_list_is_a_config_defect(self):
        self._503([])

    def test_malformed_project_uuid_is_a_config_defect(self):
        self._503(["not-a-uuid"])

    def test_config_fix_recovers_the_pending_delivery(self):
        u, ws, proj, issue, seqs, ident = _fixture()
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: ws.slug})):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503
        _due_now()
        with _scope_only(proj):
            assert reconcile_forgejo_deliveries() == 1
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        # RECOVERY is the claim: the retried delivery got past the config
        # defect and was actually PROCESSED. Reconcile returning 1 says it ran;
        # the refusal says it ran to a decision rather than erroring again.
        assert _refused_tickets() == [f"{ident}-{issue.sequence_id}"]

    def test_two_scoped_projects_sharing_an_identifier_is_a_config_defect(self):
        """Ticket keys would be ambiguous — refused, never guessed."""
        u, ws, proj, issue, seqs, ident = _fixture()
        u2, ws2, proj2, _issue2, _seqs2, _ident2 = _fixture(identifier=ident)
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: [str(proj.id), str(proj2.id)]})
        ):
            r = _post(Client(), "push", _push_payload(f"refs {ident}-{issue.sequence_id}"))
        assert r.status_code == 503
        issue.refresh_from_db()
        assert issue.state.name == "Todo"

    def test_cross_workspace_scope_with_distinct_identifiers_is_allowed(self):
        """The boundary is the PROJECT list, not the workspace: a repo may be
        scoped to projects in different workspaces, and each resolves."""
        u, ws, proj, issue, seqs, ident = _fixture()
        u2, ws2, proj2, issue2, _seqs2, ident2 = _fixture()
        with override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: [str(proj.id), str(proj2.id)]})
        ):
            r = _post(
                Client(),
                "push",
                _push_payload(f"refs {ident}-{issue.sequence_id}\n\nrefs {ident2}-{issue2.sequence_id}"),
            )
        assert r.status_code == 200, r.content
        issue.refresh_from_db()
        issue2.refresh_from_db()
        assert issue.state.name == "Todo" and issue2.state.name == "Todo"
        # BOTH resolve — the claim is that neither was rejected for crossing a
        # workspace, so both must appear as boundary refusals and neither as a
        # scope rejection.
        assert sorted(_refused_tickets()) == sorted(
            [f"{ident}-{issue.sequence_id}", f"{ident2}-{issue2.sequence_id}"]
        )
        assert "cross_project" not in (ForgejoDelivery.objects.get().result.get("ignored") or {})


class TestDeliveryResultContract:
    """Unit tests for the single result-shape owner (no DB)."""

    def test_moved_is_always_present(self):
        assert delivery_result.build() == {"moved": []}

    def test_diagnostics_nest_under_one_key(self):
        result = delivery_result.build(
            ["B-2", "A-1", "A-1"],
            review="why",
            near_misses=["line"],
            unscoped_repo="r",
            cross_project=[delivery_result.cross_project_entry("C-3", "r", "out")],
        )
        assert result == {
            "moved": ["A-1", "B-2"],
            "ignored": {
                "review": "why",
                "near_misses": ["line"],
                "unscoped_repo": "r",
                "cross_project": [{"ticket": "C-3", "repo": "r", "reason": "out"}],
            },
        }
    def test_merge_ignored_preserves_moved_and_appends(self):
        start = delivery_result.build(["A-1"], cross_project=[delivery_result.cross_project_entry("C-3", "r", "out")])
        merged = delivery_result.merge_ignored(
            start, cross_project=[delivery_result.cross_project_entry("D-4", "r", "out")]
        )
        assert merged["moved"] == ["A-1"]
        assert [e["ticket"] for e in merged["ignored"]["cross_project"]] == ["C-3", "D-4"]
    def test_constructor_cannot_emit_the_alias_discriminator(self):
        """`coalesced_to` decides whether a row EXECUTES (inbox.is_alias); the
        constructor has no parameter that could produce it."""
        result = delivery_result.build(
            ["A-1"], review="x", near_misses=["l"], unscoped_repo="r",
            cross_project=[delivery_result.cross_project_entry("C-3", "r", "out")],
        )
        assert delivery_result.COALESCED_KEY not in result
        assert delivery_result.COALESCED_KEY not in result["ignored"]


@pytest.mark.django_db
class TestAdvanceCallSitePreservation:
    def test_advance_preserves_a_diagnostic_seeded_before_the_refusal(self):
        """Closes Vex's 3579 finding AT THE CALL SITE. His 0-red revert
        replaced _advance's write with the old inline rebuild — every
        FUNCTION-level test (mine and the one his review prescribed) stays
        green under that mutant, because they call add_moved directly. This
        test drives the production path with a diagnostic already on the row —
        the exact state BIP-54 slice 3 creates when near-misses are recorded
        before the ref loop — and reds if the per-ref write drops it."""
        u, ws, proj, issue, seqs, ident = _fixture()
        row = ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()),
            forge="forgejo",
            event="push",
            payload=_push_payload(f"refs {ident}-{issue.sequence_id}"),
            repository=REPO,
            body_digest="0" * 64,
            status="pending",
            result=delivery_result.build(
                cross_project=[delivery_result.cross_project_entry("SB-3", REPO, "out of scope")]
            ),
        )
        lease = claim_delivery(row)
        row.refresh_from_db()
        with _scope_only(proj):
            result = process_delivery(row, lease)
        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        # Vex's finding is UNCHANGED by the boundary and if anything is now
        # tested harder. _advance still writes to the leased row's result — a
        # refusal instead of a move — so the preservation property it broke is
        # exercised by the same code path, and this test drives it with a
        # diagnostic already present exactly as before.
        assert result["moved"] == []
        assert _refused_tickets(ForgejoDelivery.objects.get(pk=row.pk)) == [
            f"{ident}-{issue.sequence_id}"
        ]
        assert result["ignored"]["cross_project"] == [
            {"ticket": "SB-3", "repo": REPO, "reason": "out of scope"}
        ], "the diagnostic written before the per-ref write must survive it"


@pytest.mark.django_db
class TestRetryDurability:
    def test_retry_after_partial_processing_keeps_results_and_records_rejections(self):
        """7of9's witness (review 3585) for the durability comment in
        process_delivery: seeded exactly as a crash leaves the row — attempt-1
        move durable on the locked row, status=processing, lease held — then
        attempt 2 runs with an out-of-scope ref. The final processed result
        must keep the accumulated move AND record the rejection. Falsification
        proven by her: reverting completion's `result = dict(fresh.result or
        {"moved": []})` to `result = {"moved": []}` reds this. Sibling of the
        call-site test above — both drive process_delivery on a pre-seeded row."""
        u, ws, proj, issue, seqs, ident = _fixture()
        _proj_b, issue_b, _seqs_b, ident_b = _second_project(u, ws)
        prior_move = f"{ident}-{issue.sequence_id}"
        oos = f"{ident_b}-{issue_b.sequence_id}"
        lease = "held-lease"
        delivery = ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()),
            forge="forgejo",
            event="push",
            repository=REPO,
            payload=_push_payload(f"refs {oos}"),
            status="processing",
            lease_token=lease,
            lease_expires_at=timezone.now() + timezone.timedelta(seconds=300),
            result={"moved": [prior_move]},
        )
        with _scope_only(proj):
            process_delivery(delivery, lease)
        row = ForgejoDelivery.objects.get(pk=delivery.pk)
        assert row.status == "processed"
        assert row.result == {
            "moved": [prior_move],
            "ignored": {"cross_project": [{"ticket": oos, "repo": REPO, "reason": REASON}]},
        }, row.result
        issue_b.refresh_from_db()
        assert issue_b.state.name == "Todo"


@pytest.mark.django_db
class TestAliasDiscriminatorOrdering:
    def test_holder_result_cannot_overwrite_the_alias_pointer(self):
        """Vex's latent hazard, pinned: if a holder's stored result ever
        carries a `coalesced_to` key, finalizing an alias from it must keep
        the ALIAS's own pointer — the spread goes under the discriminator,
        never over it."""
        common = dict(forge="forgejo", event="push", payload={}, repository=REPO, body_digest="d" * 64)
        holder = ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()),
            status="processed",
            result={"moved": ["A-1"], "coalesced_to": "EVIL"},
            **common,
        )
        alias = ForgejoDelivery.objects.create(
            delivery_id=str(uuid_lib.uuid4()),
            status="pending",
            result={"coalesced_to": holder.delivery_id},
            **common,
        )
        state, final = _resolve_alias(alias)
        assert state == "processed"
        assert final["coalesced_to"] == holder.delivery_id
        assert final["moved"] == ["A-1"]
