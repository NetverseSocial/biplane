# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Do write-boundary refusals actually REACH the durable result? (BIP-67)

Sable flagged this against her own branch and was right to: the rework path
appends to a local list that goes into ``delivery_result.build`` at completion
and she has WITNESSED that one arriving. **The push and merge path is
different** — it saves onto the leased row inside the per-ref transaction and
relies on completion re-reading with ``ForgejoDelivery.objects.get(pk=...)``.
That asymmetry is where a break would live, and it had been traced rather than
witnessed.

WHY THIS IS A PRECONDITION AND NOT A FOOTNOTE. ``reply.refusal_comment`` fires
off ``result`` at completion for a pull_request delivery. If a merge refusal
never lands in ``result``, the reply path is INERT for exactly the case John
ruled on — someone merges a pull request, the bridge declines to move the
ticket, and nobody is told — while both halves look finished. Every defect this
project has chased for two days has that shape.

So this file asserts the SEAM, not the decision. It does not care which reason
code comes back or whether the gate is right; ``write_boundary`` owns that and
Sable's own files test it. It cares only that a refusal produced deep inside a
per-ref transaction survives to the durable row AND to the synchronous response
body a caller reads.

The near-miss and cross-project cases here are not redundant with the scope
guard's own tests: they exist because the completion path REBUILDS or MERGES
the result at several points, and a rebuild that drops a key is invisible until
something else is in the dict beside it.
"""

import json

import pytest
from django.test import Client

from plane.bridge import write_boundary
from plane.db.models import ForgejoDelivery

from .test_forgejo_bridge import (
    SECRET,
    _fixture,
    _merge_payload_body_only,
    _post,
    _push_payload,
    _scoped,
)


@pytest.fixture(autouse=True)
def _bridge_secret(settings):
    # autouse fixtures do not cross module boundaries. Without this every
    # delivery below is refused 403 at the signature check and the file goes
    # uniformly red — which looks exactly like the defect it is hunting.
    # The first run of this file did precisely that; the tell was that the
    # renderer case failed too, and it cannot reach a signature.
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    settings.GITHUB_INSTANCE_ID = "github"
    settings.GITLAB_INSTANCE_ID = "gitlab"

# Every reason the boundary can produce. Named here so that adding a refusal
# without deciding whether it reaches the record breaks this list, not a user.
# REVIEWERS_UNDETERMINED is gone with decide_completion's collapse (the
# requirement lives in Scope A prose until the four-fact decision lands whole).
ALL_REASONS = {
    write_boundary.BINDING_UNAVAILABLE,
    write_boundary.ADVANCE_NOT_AUTHORISED,
    write_boundary.REWORK_SUPERSEDED,
    write_boundary.TICKET_NOT_FOUND,
}


def _unverified(result):
    return ((result or {}).get("ignored") or {}).get("unverified") or []


def _row():
    return ForgejoDelivery.objects.get()


@pytest.mark.django_db
class TestARefusalSurvivesToTheDurableRow:
    """The row is what the reply is built from — and what anything that tells
    a person will be built from later. The nudge/notification half has NO caller
    in this release, so the row is currently the ONLY place a refusal survives:
    on a push, or wherever no write token is configured, nothing else records
    that the bridge declined."""

    def test_a_merged_pull_request_refusal_reaches_the_result(self):
        """THE case John ruled on: someone merged, the bridge declined, and the
        reason has to exist somewhere a person can be shown it."""
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        ticket = f"{ident}-{issue.sequence_id}"
        with _scoped(_ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ticket}"))
        assert r.status_code == 200, r.content

        issue.refresh_from_db()
        assert issue.state.name == "Todo", "the gate must leave the ticket where it was"

        entries = _unverified(_row().result)
        assert entries, f"a merge refusal never reached the durable result: {_row().result!r}"
        assert entries[0]["ticket"] == ticket
        assert entries[0]["reason"] in ALL_REASONS
        assert entries[0]["detail"], "a refusal with no sentence cannot be handed to anyone"

    def test_a_push_refusal_reaches_the_result(self):
        """Same seam, other event. A push shares the per-ref save path with a
        merge, so proving one and assuming the other is how the asymmetry that
        started this investigation got missed in the first place."""
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        ticket = f"{ident}-{issue.sequence_id}"
        with _scoped(_ws):
            r = _post(Client(), "push", _push_payload(f"refs {ticket}"))
        assert r.status_code == 200, r.content

        issue.refresh_from_db()
        assert issue.state.name == "Todo"
        entries = _unverified(_row().result)
        assert entries, f"a push refusal never reached the durable result: {_row().result!r}"
        assert entries[0]["ticket"] == ticket

    def test_the_row_is_processed_not_left_pending(self):
        """A refusal is a decision, not a failure. If it parked the row pending,
        the delivery would retry forever and re-refuse on every attempt."""
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        with _scoped(_ws):
            _post(Client(), "pull_request",
                  _merge_payload_body_only(f"closes {ident}-{issue.sequence_id}"))
        row = _row()
        assert row.status == "processed", row.last_error
        assert row.last_error is None


@pytest.mark.django_db
class TestTheRecordSurvivesEverythingElseInTheDict:
    """The completion path rebuilds or merges the result at several points. A
    rebuild that drops a key is invisible until something sits beside it."""

    def test_two_refusals_on_one_delivery_both_survive(self):
        """Each ref saves its own refusal onto the leased row in turn. If the
        second save rebuilt from anything but the stored value, the first would
        vanish — the exact defect add_moved was written to fix, one key over."""
        _u, _ws, _proj, issue, seqs, ident = _fixture()
        from plane.db.models import Issue

        second = Issue.objects.create(
            workspace=_ws, project=_proj, name="second", state=seqs["Todo"]
        )
        a = f"{ident}-{issue.sequence_id}"
        b = f"{ident}-{second.sequence_id}"
        with _scoped(_ws):
            _post(Client(), "pull_request", _merge_payload_body_only(f"closes {a}\ncloses {b}"))

        tickets = {e["ticket"] for e in _unverified(_row().result)}
        assert tickets == {a, b}, f"a refusal was dropped between per-ref saves: {tickets}"

    def test_a_refusal_survives_beside_a_near_miss(self):
        """Near misses are merged in at completion from a separate accumulator.
        Merging must preserve what the per-ref path already stored."""
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        ticket = f"{ident}-{issue.sequence_id}"
        with _scoped(_ws):
            _post(Client(), "pull_request",
                  _merge_payload_body_only(f"closes {ticket}\n\nCloses {ident}-999999 once CI is green"))
        ignored = (_row().result or {}).get("ignored") or {}
        assert _unverified(_row().result), f"refusal lost when a near miss was recorded: {ignored}"

    def test_moved_stays_present_and_empty_rather_than_absent(self):
        """`moved` is the one key every reader assumes. A refusal-only delivery
        is the case most likely to produce a result without it."""
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        with _scoped(_ws):
            _post(Client(), "pull_request",
                  _merge_payload_body_only(f"closes {ident}-{issue.sequence_id}"))
        assert _row().result.get("moved") == []


@pytest.mark.django_db
class TestTheCallerIsToldSynchronously:
    """Sable's specific doubt: the refusal reaching the ROW is not the same as
    it reaching the RESPONSE BODY, because completion re-reads the row and the
    body is spread from that read."""

    def test_the_response_body_carries_the_refusal(self):
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        ticket = f"{ident}-{issue.sequence_id}"
        with _scoped(_ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ticket}"))
        body = json.loads(r.content)
        assert _unverified(body), f"the caller was told nothing: {body!r}"
        assert _unverified(body)[0]["ticket"] == ticket

    def test_the_response_and_the_row_agree(self):
        """Two records of one decision that disagree is worse than one record."""
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        with _scoped(_ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"closes {ident}-{issue.sequence_id}"))
        assert _unverified(json.loads(r.content)) == _unverified(_row().result)


@pytest.mark.django_db
class TestWhatTheReplyPathWillActuallyRead:
    """reply.refusal_comment consumes this dict directly. These assert the
    CONTRACT BETWEEN THE TWO HALVES, so that a rename on either side fails here
    rather than silently producing a comment with an empty body."""

    def test_every_entry_has_the_four_keys_the_renderer_reads(self):
        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        with _scoped(_ws):
            _post(Client(), "pull_request",
                  _merge_payload_body_only(f"closes {ident}-{issue.sequence_id}"))
        entries = _unverified(_row().result)
        # A `for` over an empty list asserts nothing. Found by disabling
        # recognition wholesale and noticing this case stayed GREEN while every
        # other case in the file went red — the same vacuity Sable found in her
        # parametrized inert/live pairs, where the inert half goes quiet
        # invisibly under a name that still reads as covered.
        assert entries, "vacuous: no entries to check the shape of"
        for entry in entries:
            assert set(entry) >= {"ticket", "reason", "detail"}, entry

    def test_the_rendered_comment_is_not_empty_for_a_real_refusal(self):
        """End to end across the seam: a real delivery's stored result, handed
        to the real renderer, produces a comment with the ticket in it."""
        # The reply module lands on a sibling branch (#96). Skipping rather
        # than failing keeps this file honest about what it has WITNESSED here,
        # and the assertion arms itself the moment the two halves are in one
        # tree — which is the first moment the contract can actually break.
        reply = pytest.importorskip("plane.bridge.reply")

        _u, _ws, _proj, issue, _seqs, ident = _fixture()
        ticket = f"{ident}-{issue.sequence_id}"
        with _scoped(_ws):
            _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ticket}"))

        body = reply._render((_row().result or {}).get("ignored") or {})
        assert body is not None, "the renderer had nothing to say about a real refusal"
        assert ticket in body
        assert "did not move a ticket" in body


@pytest.mark.django_db
class TestAMissingTicketIsAnsweredNotSkipped:
    """Scope A 181-186: "a missing or unmatched ticket" is a case the bridge
    ANSWERS. Until Morrow's third cold blocker, both write sites silently
    dropped it — `issue is None` returned before any decision, so no refusal
    reached the result and nothing said anything about the mistake an author
    most plausibly makes: a typo'd ticket number. (Said generally on purpose:
    the reply is the only telling that exists today, and the notification half
    that would also have been silent is cut from this release.)

    Distinct from the unknown-PROJECT case on purpose: a ref whose identifier
    the scope cannot resolve is rejected before the boundary and leaves nothing
    (the guard is not an existence oracle). A ref into a KNOWN project with a
    number that matches no ticket got past the scope, was recognised, and the
    author deserves to hear why nothing happened.
    """

    def _missing(self, ident):
        return f"{ident}-424242"

    def test_a_merged_pr_naming_a_nonexistent_ticket_is_refused(self):
        _u, ws, proj, _issue, _seqs, ident = _fixture()
        ghost = self._missing(ident)
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ghost}"))
        assert r.status_code == 200
        entries = _unverified(r.json())
        assert [e["ticket"] for e in entries] == [ghost], "the typo'd number was silently dropped"
        assert entries[0]["reason"] == "ticket-not-found"
        assert "notify" not in entries[0], (
            "recipient coordinates were removed with the notification half; "
            "the delivery slice re-derives them at delivery time"
        )
        assert ForgejoDelivery.objects.get().status == "processed"

    def test_a_push_naming_a_nonexistent_ticket_is_refused_too(self):
        _u, ws, proj, _issue, _seqs, ident = _fixture()
        ghost = self._missing(ident)
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {ghost}"))
        assert r.status_code == 200
        assert [e["ticket"] for e in _unverified(r.json())] == [ghost]

    def test_the_reply_can_say_it_on_the_pull_request(self):
        """The actor-present half still works: the person who TYPED the number
        is on the pull request, and the renderer must have a sentence for
        them."""
        from plane.bridge import reply

        _u, ws, proj, _issue, _seqs, ident = _fixture()
        ghost = self._missing(ident)
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"closes {ghost}"))
        body = reply._render((r.json().get("ignored") or {}))
        assert body is not None, "the renderer had nothing to say about a typo'd ticket"
        assert ghost in body

    def test_an_unknown_PROJECT_still_leaves_no_refusal(self):
        """The control that keeps the two cases distinct: the scope guard must
        not become an existence oracle through this change."""
        _u, ws, proj, _issue, _seqs, _ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload("refs ZZZZZ-424242"))
        assert r.status_code == 200
        assert _unverified(r.json()) == []


@pytest.mark.django_db
class TestZeroNominationIsAnswered:
    """Morrow's expansion of blocker 3: a merged PR or changes-requested review
    whose body nominates NOTHING is also an ask case — the actor is on the PR,
    and silence to them is indistinguishable from a broken bridge. Event-level
    diagnostic, no per-ticket refusal."""

    def test_a_merged_pr_with_no_directive_gets_the_event_diagnostic(self):
        _u, ws, _proj, _issue, _seqs, _ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only("just a merge"))
        assert r.status_code == 200
        ignored = (r.json().get("ignored") or {})
        assert "no ticket" in ((ignored.get("no_ticket") or {}).get("detail") or "").lower(), ignored
        assert _unverified(r.json()) == [], "event-level, not a per-ticket refusal"

    def test_the_renderer_says_it_to_the_actor(self):
        from plane.bridge import reply

        _u, ws, _proj, _issue, _seqs, _ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only("just a merge"))
        body = reply._render(r.json().get("ignored") or {})
        assert body is not None and "no ticket" in body.lower()

    def test_an_UNMERGED_pr_with_no_directive_stays_silent(self):
        """Morrow residue 2: the event-level diagnostic is gated to a MERGED
        pull request. An unmerged close intentionally yields no refs, and
        stamping it no-nomination would comment durable noise on work in
        progress."""
        _u, ws, _proj, _issue, _seqs, _ident = _fixture()
        payload = _merge_payload_body_only("wip, nothing named")
        payload["pull_request"]["merged"] = False
        with _scoped(ws):
            r = _post(Client(), "pull_request", payload)
        assert (r.json().get("ignored") or {}).get("no_ticket") is None

    def test_a_push_with_no_directive_stays_silent(self):
        """The boundary of the rule: a push has no present actor and no PR to
        speak on — recording no-nomination there would be noise nobody reads."""
        _u, ws, _proj, _issue, _seqs, _ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload("just a commit"))
        assert (r.json().get("ignored") or {}).get("no_ticket") is None


@pytest.mark.django_db
class TestTheParseReachesTheRecordWhole:
    """Morrow's pair of canonical-datum blockers: the parse produces more than
    tickets, and every gap between what it produces and what the collector
    carries has been a place where the bridge knew something and said nothing.

    Near miss: `Closes BIP-7 after QA` named a ticket malformedly — answering
    "no ticket was named" is confidently false. Conflict: Closes+Refs on one
    ticket demotes to advance, and Scope A 110-112 says the demotion is
    RECORDED, not silent."""

    def test_a_merged_pr_near_miss_is_recorded_not_called_no_ticket(self):
        _u, ws, _proj, issue, _seqs, ident = _fixture()
        body = f"Closes {ident}-{issue.sequence_id} after QA"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(body))
        assert r.status_code == 200
        ignored = r.json().get("ignored") or {}
        assert ignored.get("near_misses") == [body], "the near miss must be durable on the forward path"
        assert ignored.get("no_ticket") is None, (
            "a near miss IS a named ticket — 'no ticket was named' would be false"
        )
        assert _unverified(r.json()) == [], "a disqualified directive selects nothing"

    def test_the_reply_tells_the_author_about_the_near_miss(self):
        from plane.bridge import reply

        _u, ws, _proj, issue, _seqs, ident = _fixture()
        body = f"Closes {ident}-{issue.sequence_id} after QA"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(body))
        rendered = reply._render(r.json().get("ignored") or {})
        assert rendered is not None and body in rendered, (
            "the author must be told their line looked like a directive and did not qualify"
        )

    def test_a_push_near_miss_is_durable(self):
        _u, ws, _proj, issue, _seqs, ident = _fixture()
        with _scoped(ws):
            r = _post(Client(), "push",
                      _push_payload(f"Closes {ident}-{issue.sequence_id} once CI is green"))
        assert r.status_code == 200
        row = ForgejoDelivery.objects.get()
        assert (row.result.get("ignored") or {}).get("near_misses"), (
            "a push near miss vanished from the durable record (Scope A 108-109)"
        )

    def test_a_same_ticket_conflict_is_demoted_AND_recorded(self):
        _u, ws, _proj, issue, _seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request",
                      _merge_payload_body_only(f"Closes {t}\nRefs {t}"))
        ignored = r.json().get("ignored") or {}
        assert ignored.get("conflicts") == [t], "the demotion must be recorded, not silent"
        entries = _unverified(r.json())
        assert [e["ticket"] for e in entries] == [t]
        assert entries[0]["reason"] == "advance-not-authorised", "the WEAKER class was proposed"


@pytest.mark.django_db
class TestTheRefusalSentenceIsTrueOfItsEvent:
    """Morrow: proposed effect != event kind. A merged PR carrying `Refs` is an
    advance proposal on a MERGE — a public comment claiming 'a push determines
    nothing' about it is factually false. Event is passed explicitly, never
    inferred from the keyword class."""

    def test_a_merged_pr_refs_refusal_does_not_claim_a_push(self):
        _u, ws, _proj, issue, _seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "pull_request", _merge_payload_body_only(f"Refs {t}"))
        entry = _unverified(r.json())[0]
        assert entry["reason"] == "advance-not-authorised"
        assert "push" not in entry["detail"].lower(), entry["detail"]
        assert "did merge" in entry["detail"], "the sentence must own the event that happened"

    def test_a_push_refs_refusal_still_speaks_of_the_push(self):
        _u, ws, _proj, issue, _seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        with _scoped(ws):
            r = _post(Client(), "push", _push_payload(f"refs {t}"))
        entry = _unverified(r.json())[0]
        assert "push" in entry["detail"].lower()

    def test_two_commits_conflicting_on_one_ticket_record_ONE_conflict(self):
        """Exact multiplicity, first order preserved (Morrow): a push whose two
        commits each carry Closes+Refs for the same ticket is one fact about
        one ticket — and a set-shaped assertion here would hide a regression to
        appending, which is the same masking the dedup blocker was about."""
        _u, ws, _proj, issue, _seqs, ident = _fixture()
        t = f"{ident}-{issue.sequence_id}"
        payload = _push_payload("ignored")
        payload["commits"] = [
            {"id": "a" * 40, "message": f"Closes {t}\nRefs {t}"},
            {"id": "b" * 40, "message": f"Closes {t}\nRefs {t}"},
        ]
        with _scoped(ws):
            r = _post(Client(), "push", payload)
        assert (r.json().get("ignored") or {}).get("conflicts") == [t]
