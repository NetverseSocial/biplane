# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-50: an official current-head rejection opens one exact rework edge."""

import hashlib
import hmac
import json
import uuid
from unittest import mock

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from plane.bgtasks.forgejo_bridge_task import reconcile_forgejo_deliveries
from plane.bridge import reply
from plane.bridge import semantic_key as skey
from plane.bridge.forgejo_bridge import (
    _apply_review_rejection,
    claim_delivery,
    process_delivery,
)
from plane.db.models import (
    ForgejoDelivery,
    Issue,
    IssueActivity,
    Project,
    State,
    User,
    Workspace,
)

URL = "/api/public/git-bridge/forgejo/"
SECRET = "test-review-secret"
REPO = "acme/reviews"
REPO_ID = 42
HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    settings.FORGEJO_INSTANCE_ID = "review-test"
    settings.FORGEJO_BASE_URL = "http://forgejo:3000"
    settings.FORGEJO_BRIDGE_API_TOKEN = "review-api-token"


REWORK_REFUSED = "rework-write-superseded"


def _refused(result) -> set:
    """The tickets whose REWORK WRITE was refused, from the durable record.

    BIP-67 conversion of this file. ADR 009's automatic Review -> Code & TDD
    write is **withdrawn outright, not deferred**: a changes-requested review is
    neither a merge nor an approval, so no future field makes it qualify. These
    tests must therefore assert the refusal is RECORDED, never that it is
    pending something.

    Why the observable moved. The claim of nearly every test here is that a
    review event REACHED the rework site through the shared delivery lifecycle
    — the right shape validation, the right semantic key, the right
    dedup. Board state used to witness that, and cannot any more: with every
    write refused, "the ticket did not move" is true for every reason at once,
    including "the delivery never arrived". A recorded refusal naming the
    ticket is the same evidence one layer earlier, and it is the evidence these
    tests always actually wanted.

    Distinguish this from ``result == {"moved": []}`` with NO ``ignored`` key,
    which several tests below assert deliberately: that is the stronger claim
    that nothing reached the boundary at all, and it is how an inert delivery
    stays distinguishable from a refused one.
    """
    ignored = (result or {}).get("ignored") or {}
    return {entry["ticket"] for entry in ignored.get("unverified") or []}


def _project_ids(ws):
    # BIP-38: map values are explicit project-UUID lists, never workspace slugs.
    from plane.db.models import Project

    return [str(pid) for pid in Project.objects.filter(workspace=ws).values_list("id", flat=True)]


def _fixture(initial="Review"):
    user = User.objects.create(
        email=f"review-{uuid.uuid4().hex[:8]}@example.com",
        username=uuid.uuid4().hex[:12],
    )
    workspace = Workspace.objects.create(slug=f"r{uuid.uuid4().hex[:10]}", name="Review", owner=user)
    identifier = "RV" + uuid.uuid4().hex[:3].upper()
    project = Project.objects.create(workspace=workspace, name="Review", identifier=identifier)
    states = {}
    for sequence, (name, group) in enumerate(
        [
            ("Backlog", "backlog"),
            ("Todo", "unstarted"),
            ("Code & TDD", "started"),
            ("Review", "started"),
            ("Integration Test", "started"),
            ("Done", "completed"),
            ("Deploy", "completed"),
            ("Cancelled", "cancelled"),
        ],
        start=1,
    ):
        states[name] = State.objects.create(
            name=name,
            project=project,
            workspace=workspace,
            group=group,
            sequence=sequence * 100,
            color="#000",
            default=name == "Backlog",
            created_by=user,
        )
    issue = Issue.objects.create(
        workspace=workspace,
        project=project,
        name="target",
        state=states[initial],
    )
    return workspace, project, issue, states, identifier


def _payload(review_id=701, pull_number=53, body="__default__"):
    """A signed review event. SELECTION comes from pull_request.body IN THE
    EVENT — the forge authority re-read is deleted (Morrow: official/current/
    open were ADR 009 write-permission facts; the write is gone, and an
    authenticated changes-requested event is an ask case). `body` accepts the
    sentinel to mean "caller substitutes the ref later", an explicit None (a
    genuine empty selection — a PR with no description), or omission via
    _payload_without_body for the absence case."""
    return {
        "repository": {"full_name": REPO, "id": REPO_ID},
        "pull_request": {"number": pull_number, "body": body},
        "review": {"id": review_id},
    }


def _payload_for(identifier, sequence_id, **kw):
    return _payload(body=f"Refs {identifier}-{sequence_id}", **kw)


def _payload_without_body(**kw):
    p = _payload(**kw)
    del p["pull_request"]["body"]
    return p


def _delivery(payload):
    return ForgejoDelivery.objects.create(
        delivery_id=str(uuid.uuid4()),
        forge="forgejo",
        event="review_rejected",
        payload=payload,
        repository=REPO,
        body_digest="0" * 64,
    )


def _process(workspace, issue, identifier, *, body="__from_issue__"):
    if body == "__from_issue__":
        payload = _payload_for(identifier, issue.sequence_id)
    else:
        payload = _payload(body=body)
    delivery = _delivery(payload)
    lease = claim_delivery(delivery)
    delivery.refresh_from_db()
    scope = json.dumps({f"review-test:{REPO_ID}": _project_ids(workspace)})
    with override_settings(FORGEJO_BRIDGE_REPO_MAP=scope):
        result = process_delivery(delivery, lease)
    delivery.refresh_from_db()
    issue.refresh_from_db()
    return result, delivery


def _post_review(payload, delivery_id):
    body = json.dumps(payload).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_FORGEJO_EVENT="pull_request_review_rejected",
        HTTP_X_FORGEJO_SIGNATURE=signature,
        HTTP_X_FORGEJO_DELIVERY=delivery_id,
    )


@pytest.mark.django_db
class TestReviewRejectionBoundary:
    def test_review_event_is_reachable_through_the_shared_delivery_lifecycle(self):
        workspace, _project, issue, _states, identifier = _fixture()
        scope = json.dumps({f"review-test:{REPO_ID}": _project_ids(workspace)})
        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
                    ):
            response = _post_review(_payload_for(identifier, issue.sequence_id), str(uuid.uuid4()))

        assert response.status_code == 200
        # REACHABILITY is the claim, and the refusal is what now witnesses it:
        # only a directive that travelled the whole shared lifecycle — endpoint,
        # shape validation, scope, forge authority, the rework site — can
        # produce a refusal naming this ticket.
        assert response.json()["moved"] == []
        assert _refused(response.json()) == {f"{identifier}-{issue.sequence_id}"}
        issue.refresh_from_db()
        assert issue.state.name == "Review"
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed"
        assert row.semantic_key == f"review\x1freview-test\x1f{REPO_ID}\x1f53\x1f701"
        assert row.semantic_key_hash

    def test_same_review_new_delivery_id_replays_the_stored_holder_outcome(self):
        workspace, _project, issue, _states, identifier = _fixture()
        scope = json.dumps({f"review-test:{REPO_ID}": _project_ids(workspace)})
        first_id, replay_id = str(uuid.uuid4()), str(uuid.uuid4())
        # NO FORGE READS AT ALL on the review path now — the network guard is
        # a hard AssertionError, so a reintroduced authority call reds loudly.
        network = mock.Mock(side_effect=AssertionError("review path made a forge read"))
        payload = _payload_for(identifier, issue.sequence_id)
        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
            mock.patch("plane.bridge.forgejo_bridge.http_requests.get", network),
        ):
            first = _post_review(payload, first_id)
            replay = _post_review(payload, replay_id)

        ticket = f"{identifier}-{issue.sequence_id}"
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert replay.json()["moved"] == first.json()["moved"]
        # THE REPLAY MUST CARRY THE REFUSAL TOO, and this is the assertion the
        # conversion turns on: the alias exists to hand back the holder's
        # terminal outcome, and the outcome is now a refusal rather than a move.
        # An alias that replayed only `moved` would report an empty success and
        # the reason would reach nobody on a redelivery.
        assert _refused(replay.json()) == {ticket}
        assert _refused(first.json()) == {ticket}
        assert network.call_count == 0, "the review path made a forge read"
        holder = ForgejoDelivery.objects.get(delivery_id=first_id)
        alias = ForgejoDelivery.objects.get(delivery_id=replay_id)
        assert holder.semantic_key_hash is not None
        assert alias.semantic_key == holder.semantic_key
        assert alias.semantic_key_hash is None
        assert alias.result["coalesced_to"] == first_id
        assert alias.result["moved"] == first.json()["moved"]
        assert _refused(alias.result) == {ticket}
        assert IssueActivity.objects.filter(issue=issue, field="state").count() == 0

    def test_apply_stop_expire_reconcile_and_alias_preserve_one_terminal_outcome(self):
        """RC 3526: stopping immediately after the board apply must not leave a
        retryable holder. The old two-commit path reclaimed it, observed the
        issue already outside Review, overwrote moved with [], and made later
        aliases lie about the completed outcome."""
        workspace, _project, issue, _states, identifier = _fixture()
        payload = _payload_for(identifier, issue.sequence_id)
        key = skey.review_key("review-test", REPO_ID, 53, 701)
        holder = _delivery(payload)
        holder.semantic_key = key
        holder.semantic_key_hash = skey.key_hash(key)
        holder.save(update_fields=["semantic_key", "semantic_key_hash", "updated_at"])
        lease = claim_delivery(holder)
        holder.refresh_from_db()
        scope = json.dumps({f"review-test:{REPO_ID}": _project_ids(workspace)})

        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
                    ):
            applied = _apply_review_rejection(holder, lease, {identifier: _project}, REPO, payload)

        # Simulated process stop exactly here. Even with a past lease timestamp,
        # reconciliation cannot reclaim a holder committed terminal atomically.
        ForgejoDelivery.objects.filter(pk=holder.pk).update(
            lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )
        holder.refresh_from_db()
        assert claim_delivery(holder) is None
        with override_settings(FORGEJO_BRIDGE_REPO_MAP=scope):
            assert reconcile_forgejo_deliveries() == 0

        network = mock.Mock(side_effect=AssertionError("alias made a forge read"))
        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
            mock.patch("plane.bridge.forgejo_bridge.http_requests.get", network),
        ):
            replay = _post_review(payload, str(uuid.uuid4()))

        holder.refresh_from_db()
        issue.refresh_from_db()
        ticket = f"{identifier}-{issue.sequence_id}"
        assert holder.status == "processed"
        assert holder.attempts == 1
        assert holder.processed_at is not None
        assert holder.lease_token is None
        # RC 3526's defect was a holder whose terminal outcome could be
        # OVERWRITTEN by a reclaim, making later aliases lie. That risk does not
        # go away when the outcome becomes a refusal — it gets sharper, because
        # an overwritten refusal reads as "nothing to report" rather than as a
        # wrong move. So the terminal outcome is compared whole, both ways.
        assert holder.result == applied
        assert applied["moved"] == []
        assert _refused(applied) == {ticket}
        assert issue.state.name == "Review"
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert replay.json()["moved"] == applied["moved"]
        assert _refused(replay.json()) == {ticket}, (
            "the alias must replay the REFUSAL, not just an empty moved list"
        )
        assert network.call_count == 0
        assert IssueActivity.objects.filter(issue=issue, field="state").count() == 0

    @pytest.mark.parametrize(
        "initial",
        ["Backlog", "Todo", "Code & TDD", "Integration Test", "Done", "Deploy", "Cancelled"],
    )
    def test_rejection_opens_no_other_backward_or_forward_edge(self, initial):
        """Fix 3 (Morrow cold-read) inverted this test's silence. The state
        gate that skipped non-Review tickets was superseded ADR 009 machinery
        AND a silent drop — no refusal recorded, in exactly the case the
        ruling says the bridge asks. Now every existing in-scope ticket gets an
        ANSWER whatever state it is in; what must still never happen is a
        WRITE. So: refusal recorded, ticket unmoved, zero activity — the edge
        stays closed while the ask now exists."""
        workspace, _project, issue, _states, identifier = _fixture(initial=initial)
        result, _delivery_row = _process(workspace, issue, identifier)

        assert result["moved"] == []
        assert _refused(result) == {f"{identifier}-{issue.sequence_id}"}, \
            f"a rework on a {initial} ticket must be refused, not silently dropped"
        entry = result["ignored"]["unverified"][0]
        assert entry["reason"] == REWORK_REFUSED
        assert issue.state.name == initial
        assert not IssueActivity.objects.filter(issue=issue, field="state").exists()
    def test_removing_the_rework_refusal_STILL_cannot_write(self):
        """The rework twin of the advance mutant pin (Morrow, blocker 7): the
        mutation code this path used to hold is DELETED, so nulling
        decide_rework loses the record and nothing else."""
        workspace, _project, issue, _states, identifier = _fixture()
        with mock.patch(
            "plane.bridge.forgejo_bridge.write_boundary.decide_rework", return_value=None
        ):
            result, delivery = _process(workspace, issue, identifier)
        assert delivery.status == "processed"
        issue.refresh_from_db()
        assert issue.state.name == "Review", "nulling the refusal moved the ticket"
        assert result["moved"] == []
        assert IssueActivity.objects.filter(issue=issue).count() == 0

    def test_the_board_write_is_never_ATTEMPTED_so_it_cannot_fail(self):
        """Converted from test_failed_activity_rolls_back_move_and_retries_delivery.

        THE ONLY TEST IN THIS FILE WHOSE SUBJECT THE BOUNDARY REMOVED, so it is
        the one that could most easily have been reshaped into something
        meaningless. The original armed a failure in the activity write and
        proved the move rolled back and the delivery retried. There is no
        activity write on this path any more, so that failure has nothing to
        arm: left alone the test raises nothing and fails, and 'fixed' by
        deleting the raises it would assert only that a refused delivery is
        processed — which four other tests already say.

        Rather than reshape it around the gate or delete it, it keeps its
        subject — THE WRITE SITE — and asserts the stronger current truth: the
        write is not merely rolled back on failure, it is never reached. The
        sabotaged `create` stays exactly where it was, and the assertion is
        that it is NEVER CALLED. If a future change lets the rework path write
        again, this test fails loudly with the RuntimeError still wired up.
        """
        workspace, _project, issue, _states, identifier = _fixture()
        delivery = _delivery(_payload_for(identifier, issue.sequence_id))
        lease = claim_delivery(delivery)
        delivery.refresh_from_db()
        scope = json.dumps({f"review-test:{REPO_ID}": _project_ids(workspace)})
        sabotaged = mock.Mock(side_effect=RuntimeError("activity write failed"))
        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
                        mock.patch("plane.db.models.IssueActivity.objects.create", sabotaged),
        ):
            result = process_delivery(delivery, lease)

        issue.refresh_from_db()
        delivery.refresh_from_db()
        assert sabotaged.call_count == 0, (
            "the rework path attempted a board write; the boundary must refuse "
            "BEFORE the write, not roll it back after"
        )
        assert issue.state.name == "Review"
        assert delivery.status == "processed", "a refusal is terminal, not retryable"
        assert delivery.last_error is None
        assert _refused(result) == {f"{identifier}-{issue.sequence_id}"}


@pytest.mark.django_db
class TestTheReviewerIsToldOnThePullRequest:
    """John's ruling reaches the case it describes most directly.

    `process_delivery` RETURNS EARLY for review deliveries, so the reply call at
    the normal completion point covered push and merge and a review delivery
    reached it never. That excluded exactly the situation the ruling is about —
    someone requests changes, the bridge declines to move the ticket, and the
    pull request they are looking at says nothing — even though a review event
    carries a pull-request number and is the event where a person is most
    certainly present.

    These assert the CALL SITE, not the renderer: `reply` already has 34 cases
    covering what it posts, when it stays silent, and which forges it will
    answer. What was missing was anyone calling it here. Patching the HTTP layer
    instead would test the renderer a second time AND collide with the forge
    authority mock, which patches the same `requests` module object — that
    collision is how the first version of this test failed, with an exhausted
    side_effect list rather than a real finding.
    """

    def test_a_refused_review_is_reported_on_the_pull_request(self):
        """Renamed from ..._an_inert_review_...: the inert reasons died with
        the authority reread. The reportable outcome is now the rework
        REFUSAL itself, which every changes-requested event with an in-scope
        ticket produces."""
        workspace, _project, issue, _states, identifier = _fixture()
        with mock.patch.object(reply, "refusal_comment") as told:
            result, _delivery = _process(workspace, issue, identifier)

        assert _refused(result) == {f"{identifier}-{issue.sequence_id}"}, "precondition: a reportable refusal"
        assert told.called, "the reviewer was told nothing on the pull request they were looking at"
        kwargs = told.call_args.kwargs
        assert kwargs["number"] == 53, "the reply must name the pull request the review was on"
        assert kwargs["repo"] == REPO
        assert kwargs["forge"] == "forgejo"
        assert kwargs["result"] is result, "the reply must carry THIS delivery's outcome"

    def test_the_ALIAS_EXIT_does_not_comment(self):
        """THE THIRD EXIT, driven directly, because the endpoint never reaches it.

        A coalesced alias returns from `process_delivery` before either call
        site, and it must stay that way: `reply` keys its idempotency marker on
        the DELIVERY id, so an alias would not recognise the holder's comment as
        its own and would post a second comment about one real event.

        MY FIRST VERSION OF THIS TEST PROVED NOTHING. It posted the same review
        twice through the endpoint and asserted one comment — but the inbox seam
        answers a duplicate BEFORE `process_delivery` is invoked, so the alias
        branch was never executed. Adding the third call site as a mutant left
        the test GREEN. The alias exit is only reached by a worker claiming the
        stored alias row, so that is what this drives.
        """
        workspace, _project, issue, _states, identifier = _fixture()
        scope = json.dumps({f"review-test:{REPO_ID}": _project_ids(workspace)})

        holder = _delivery(_payload_for(identifier, issue.sequence_id))
        lease = claim_delivery(holder)
        holder.refresh_from_db()
        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
                    ):
            holder_result = process_delivery(holder, lease)
        assert _refused(holder_result) == {f"{identifier}-{issue.sequence_id}"}, "precondition: reportable"

        alias = _delivery(_payload_for(identifier, issue.sequence_id))
        alias.result = {"coalesced_to": holder.delivery_id}
        alias.save(update_fields=["result", "updated_at"])
        alias_lease = claim_delivery(alias)
        alias.refresh_from_db()
        with (
            override_settings(FORGEJO_BRIDGE_REPO_MAP=scope),
            mock.patch.object(reply, "refusal_comment") as told,
        ):
            alias_result = process_delivery(alias, alias_lease)

        # The alias carries its own discriminator ON TOP of the holder's outcome
        # — `coalesced_to` is what marks the row as non-executing (BIP-56), so
        # equality of the whole dict is the wrong assertion. What must match is
        # the outcome it replays.
        assert alias_result.get("coalesced_to") == holder.delivery_id
        assert alias_result.get("ignored") == holder_result.get("ignored")
        assert alias_result.get("moved") == holder_result.get("moved")
        assert told.call_count == 0, (
            "the alias exit reported a second time about one real event"
        )
        # (The in-app notification half is CUT from this release — see the
        # spec status. When it returns, its alias-exit zero-call assertion
        # returns with it: notification idempotency keys on the delivery id
        # exactly like reply, so an alias would notify twice.)

    def test_the_rendered_review_body_is_not_about_a_merge(self):
        """Morrow residue 3: the reply header used to open "this merge did not
        move a ticket", which is FALSE on this exit — changes-requested happens
        on an OPEN pull request. Renders the real body, not just the call."""
        workspace, _project, issue, _states, identifier = _fixture()
        result, _delivery = _process(workspace, issue, identifier)
        body = reply._render(result.get("ignored") or {})
        assert body is not None
        assert "this merge" not in body, "review refusals must not claim a merge happened"
        assert "did not move a ticket" in body

    def test_a_reply_failure_cannot_make_the_delivery_retry(self):
        """The load-bearing restraint, re-asserted at the NEW call site: the
        DECISION and its durable REFUSAL are already recorded — there is no
        board outcome, because no board row is ever written — so telling
        someone is best-effort and must never turn a processed delivery into a
        retry."""
        workspace, _project, issue, _states, identifier = _fixture()
        with mock.patch.object(reply, "refusal_comment", side_effect=RuntimeError("forge on fire")):
            result, delivery = _process(workspace, issue, identifier)
        assert delivery.status == "processed"
        assert delivery.last_error is None
        assert _refused(result), "a reportable refusal exists"

@pytest.mark.django_db(transaction=True)
class TestBoardStateDoesNotGateTheAsk:
    """Morrow cold read (BIP-67): a rework refusal must not depend on state.

    The review path used to require ``state == "Review"`` before going any
    further — superseded ADR 009 machinery. Under it a changes-requested
    review naming a ticket in ANY other state recorded no refusal and notified
    nobody: a silent drop, in the one case the ruling names as exactly when the
    bridge should ask. Every other rework test here uses a Review-state
    fixture, so none of them can see this (Aria).
    """

    def test_a_rework_on_a_non_Review_ticket_still_refuses_and_asks(self):
        ws, _proj, issue, _states, identifier = _fixture(initial="Todo")
        # No recipient setup is needed or possible: `who_to_ask` and the whole
        # of recipient selection were CUT with the notification half, so a
        # refusal names no one. An earlier version created a reviewer assignee
        # here to populate that field; it is removed rather than left as
        # scenery, because setup that no longer feeds an assertion reads as a
        # precondition and is not one.
        assert issue.state.name == "Todo", "fixture must not be in Review or this tests nothing"
        result, delivery = _process(ws, issue, identifier)
        entries = ((result or {}).get("ignored") or {}).get("unverified") or []
        by_ticket = {e["ticket"]: e for e in entries}
        key = f"{identifier}-{issue.sequence_id}"
        assert key in by_ticket, "a rework on a non-Review ticket was silently dropped"
        assert by_ticket[key]["reason"] == "rework-write-superseded"
        assert issue.state.name == "Todo"
        assert IssueActivity.objects.filter(issue=issue).count() == 0


@pytest.mark.django_db
class TestBodySelectionAtTheTypedBoundary:
    """Morrow's endpoint controls for the absent-vs-empty class. The original
    defect was `_validate_shape` NOT requiring pull_request.body, so absence
    reached processing and was read as an empty selection — manufacturing
    "this event named no ticket" from a field nobody had. These drive the real
    endpoint, not the helper, because the helper was never the missing check."""

    def _scope(self, ws):
        return override_settings(
            FORGEJO_BRIDGE_REPO_MAP=json.dumps({f"review-test:{REPO_ID}": _project_ids(ws)})
        )

    def test_absent_body_is_400_and_stores_no_row(self):
        ws, *_ = _fixture()
        with self._scope(ws):
            r = _post_review(_payload_without_body(), str(uuid.uuid4()))
        assert r.status_code == 400
        assert ForgejoDelivery.objects.count() == 0, "a malformed delivery must not be stored"

    def test_wrong_type_body_is_400_and_stores_no_row(self):
        ws, *_ = _fixture()
        with self._scope(ws):
            r = _post_review(_payload(body=42), str(uuid.uuid4()))
        assert r.status_code == 400
        assert ForgejoDelivery.objects.count() == 0

    def test_explicit_null_body_is_a_genuine_empty_selection(self):
        """A PR opened with no description: processed, answered as the
        event-level no-ticket case — NOT refused, NOT read as absent."""
        ws, *_ = _fixture()
        with self._scope(ws):
            r = _post_review(_payload(body=None), str(uuid.uuid4()))
        assert r.status_code == 200
        row = ForgejoDelivery.objects.get()
        assert row.status == "processed"
        assert (row.result.get("ignored") or {}).get("no_ticket"), row.result
