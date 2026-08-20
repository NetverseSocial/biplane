# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-46: the GitHub events transport, and the sweep that drives it.

The defect this file first existed against was that the transport had NO caller
at all — `poll_repo_page` was complete, correct and never invoked. That is the
first test here.

The rest are Morrow's cold pass (RC 3656), which found seven ways the first
implementation still believed the events feed was an authority. They are one
defect with seven faces, so they are tested as one property each rather than as
seven guards: nothing is read from the feed that the REST API owns, hydration
completes before anything advances, malformed input moves neither half of the
boundary, the window overlaps, and cadence is durable.

Every case below is a way a poller lies QUIETLY. None of them errors in the
version that has the bug; each returns success and loses something.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone

from plane.bgtasks import poll_github_task as driver
from plane.bridge import github_events
from plane.db.models import ForgejoDelivery, PollCursor

REPO = "acme/app"
REPO_ID = 90210
INSTANCE = "github.com/acme"


@pytest.fixture(autouse=True)
def _configured_instance():
    with override_settings(GITHUB_INSTANCE_ID=INSTANCE, GITHUB_ACCESS_TOKEN="ghp-test"):
        yield


def _cursor(**kw):
    return PollCursor.objects.create(
        provider_instance=kw.pop("provider_instance", INSTANCE),
        repo_stable_id=kw.pop("repo_stable_id", REPO_ID),
        forge="github",
        repo_full_name=kw.pop("repo_full_name", REPO),
        **kw,
    )


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _push_event(ident, created=None, before="a" * 40, head="b" * 40):
    """A push event in the CURRENT trimmed shape: identity and coordinates only,
    no commits and no size. That is what GitHub sends today."""
    return {
        "id": str(ident),
        "type": "PushEvent",
        "created_at": created or _iso(timezone.now()),
        "payload": {"ref": "refs/heads/main", "before": before, "head": head},
    }


def _compare(n_commits, total=None):
    return {
        "total_commits": total if total is not None else n_commits,
        "commits": [
            {"sha": f"{i:040d}", "commit": {"message": f"fix, refs ACME-{i}"}}
            for i in range(n_commits)
        ],
    }


class _Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Session:
    """The NETWORK, which is the one thing a unit test may not have. Responses
    are matched by URL fragment so a test states what GitHub answers rather than
    what order the code happens to ask in."""

    def __init__(self, events=None, **by_fragment):
        self.events = events
        self.by_fragment = by_fragment
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        for fragment, response in self.by_fragment.items():
            if fragment in url:
                return response
        if "/events" in url:
            return self.events if self.events is not None else _Response(200, [])
        # The bare repository object, for a new ref's divergence point. Matched
        # last so a more specific fragment always wins.
        if url.rstrip("/").endswith(REPO):
            return _Response(200, {"default_branch": "main"})
        return _Response(404, {})


def _patched(session):
    return mock.patch.object(github_events, "http_requests", session)


# ---------------------------------------------------------------------------
# The transport is REACHED. Everything else refines this.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_sweep_actually_ingests__the_transport_had_no_caller():
    """The original defect: a correct poller nothing ever called."""
    cursor = _cursor()
    session = _Session(
        events=_Response(200, [_push_event(1002)], {"ETag": 'W/"abc"'}),
        compare=_Response(200, _compare(2)),
    )
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("advanced") == 1, outcomes
    assert ForgejoDelivery.objects.filter(forge="github").count() == 1
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1002"


# ---------------------------------------------------------------------------
# The feed is a NOTIFICATION. Nothing is read from it that the API owns.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_commit_messages_come_from_the_API_even_when_the_feed_offers_its_own():
    """The feed's commit array is never consulted, even when present. Reading it
    "when available" makes this behave differently against two GitHub
    deployments, and the one we can test is not the one we will meet."""
    _cursor()
    event = _push_event(1002)
    event["payload"]["commits"] = [{"sha": "z" * 40, "message": "STALE, refs WRONG-9"}]
    event["payload"]["size"] = 1
    session = _Session(
        events=_Response(200, [event]),
        compare=_Response(200, _compare(1)),
    )
    with _patched(session):
        driver.poll_github_repositories()

    row = ForgejoDelivery.objects.get(forge="github")
    messages = [c["message"] for c in row.payload["commits"]]
    assert messages == ["fix, refs ACME-0"], "the feed's own commit array was trusted"


@pytest.mark.django_db
def test_a_missing_size_never_becomes_seen_equals_total():
    """Morrow RC 3656. The first version defaulted `commits_total` to the number
    of commits it happened to hold, MANUFACTURING completeness out of absence —
    in the same file whose docstring said absent means unmeasured. Both numbers
    now come from one authority, so equal means measured-equal."""
    _cursor()
    session = _Session(
        events=_Response(200, [_push_event(1002)]),
        compare=_Response(200, _compare(3, total=3)),
    )
    with _patched(session):
        driver.poll_github_repositories()

    row = ForgejoDelivery.objects.get(forge="github")
    assert row.payload["commits_seen"] == 3
    assert row.payload["commits_total"] == 3


@pytest.mark.django_db
def test_a_capped_push_does_NOT_advance_past_commits_it_never_read():
    """Morrow RC 3656, and it overturns my own reasoning on the ticket.

    I argued a declared truncation must not wedge the cursor. That is right
    about wedging and wrong about advancing: unseen commits carry unseen
    DIRECTIVES, so advancing past 20 of 40 loses them permanently and silently.
    The resolution is neither — hydrate completely, or do not advance.
    """
    cursor = _cursor(position={"last_event_id": "1000"})
    session = _Session(
        events=_Response(200, [_push_event(1002)]),
        compare=_Response(200, _compare(20, total=40)),
    )
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000", "advanced past unread commits"
    assert cursor.gap_detected is False, "an unresolvable range is not lost history"
    assert ForgejoDelivery.objects.count() == 0, "a partially-read push must not be ingested"


@pytest.mark.django_db
def test_a_closed_pull_request_is_re_read_rather_than_believed():
    """GitHub trimmed `pull_request` to id/url/number/head/base on 2025-10-07.
    Gating on `pr.get("merged")` against that payload is always falsy, so every
    merge is dropped with no error and nothing to notice."""
    _cursor()
    trimmed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8,
                    "pull_request": {"id": 9, "url": "u", "number": 8,
                                     "head": {"sha": "x" * 40}, "base": {"ref": "main"}}},
    }
    session = _Session(
        events=_Response(200, [trimmed]),
        # `user` and `merged_by` are present because a real pull object always
        # carries them; since RC 3712 an absent actor field is an incomplete
        # read rather than a measured absence, so a fixture omitting them was
        # asserting something the API never returns.
        pulls=_Response(200, {"number": 8, "merged": True, "merge_commit_sha": "d" * 40,
                              "title": "t", "body": "Closes ACME-2",
                              "user": {"id": 11, "login": "aria"},
                              "merged_by": {"id": 22, "login": "morrow"}}),
    )
    with _patched(session):
        driver.poll_github_repositories()

    row = ForgejoDelivery.objects.get(forge="github")
    assert row.payload["pull_request"]["merge_commit_sha"] == "d" * 40
    assert row.payload["pull_request"]["body"] == "Closes ACME-2"


@pytest.mark.django_db
def test_a_closed_UNMERGED_pull_request_still_yields_nothing():
    """Asking the authority must not turn every closed PR into a merge. A bridge
    that completes tickets for abandoned work is worse than one that misses."""
    _cursor()
    trimmed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8, "pull_request": {"number": 8}},
    }
    session = _Session(
        events=_Response(200, [trimmed]),
        pulls=_Response(200, {"number": 8, "merged": False, "merge_commit_sha": None}),
    )
    with _patched(session):
        driver.poll_github_repositories()
    assert ForgejoDelivery.objects.count() == 0


# ---------------------------------------------------------------------------
# Malformed input moves NEITHER half of the boundary.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "broken",
    [
        {"id": "not-a-number", "created_at": "2026-08-14T10:00:00Z"},
        {"id": "1002", "created_at": "yesterday-ish"},
        {"id": "1002", "created_at": None},
    ],
)
def test_a_malformed_event_cannot_synthesize_a_boundary(broken):
    """Morrow RC 3656. An unreadable id or timestamp is an unreadable PAGE, not
    a smaller one. Skipping past it and advancing would step over whatever we
    could not read."""
    watermark = timezone.now() - timedelta(hours=1)
    cursor = _cursor(position={"last_event_id": "1000"}, watermark_at=watermark)
    event = _push_event(1002)
    event.update(broken)
    session = _Session(events=_Response(200, [event]), compare=_Response(200, _compare(1)))

    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000", "position moved on unreadable input"
    assert cursor.watermark_at == watermark, "watermark moved on unreadable input"
    assert cursor.gap_detected is False


# ---------------------------------------------------------------------------
# The window OVERLAPS. Delivery is not strictly ordered.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_read_window_overlaps_the_watermark_rather_than_stopping_exactly_on_it():
    """Morrow RC 3656. The feed can deliver out of order under load, so an exact
    stop steps over a late arrival. We read back past the watermark and let the
    inbox's idempotent ingest collapse the duplicates — dedup is already its
    job, and a few wasted reads cost less than one lost push."""
    now = timezone.now()
    _cursor(position={"last_event_id": "1000"}, watermark_at=now - timedelta(minutes=1))
    # Both events are OLDER than the watermark but inside the overlap window.
    inside = _push_event(1002, created=_iso(now - timedelta(minutes=2)))
    outside = _push_event(1001, created=_iso(now - timedelta(minutes=30)), head="c" * 40)
    session = _Session(
        events=_Response(200, [inside, outside]),
        compare=_Response(200, _compare(1)),
    )
    with _patched(session):
        driver.poll_github_repositories()

    # BOTH are ingested. This assertion used to expect 1, back when the boundary
    # decided what to KEEP as well as when to stop paging — and that expectation
    # was the defect Rowan found (RC 3658), not a property worth preserving.
    # Everything read is kept; the inbox's idempotent ingest is what stops
    # re-reads from duplicating, so over-reading is free and under-reading is
    # permanent loss.
    assert ForgejoDelivery.objects.count() == 2, (
        "an event read from the feed was discarded rather than handed to the inbox"
    )


# ---------------------------------------------------------------------------
# Lost history vs an incomplete read.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_history_that_fell_out_of_the_window_fails_closed_as_a_GAP():
    now = timezone.now()
    cursor = _cursor(position={"last_event_id": "500"}, watermark_at=now - timedelta(days=60))
    recent = [_push_event(2000 + i, created=_iso(now - timedelta(minutes=i))) for i in range(5)]
    session = _Session(events=_Response(200, recent), compare=_Response(200, _compare(1)))

    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("gap") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.gap_detected is True
    assert "re-seed" in (cursor.last_error or "").lower(), "a gap must name the operator recovery"


@pytest.mark.django_db
def test_a_rate_limit_is_NOT_a_gap_and_leaves_the_boundary_alone():
    watermark = timezone.now() - timedelta(days=60)
    cursor = _cursor(position={"last_event_id": "500"}, watermark_at=watermark)
    session = _Session(events=_Response(403, [], {"X-RateLimit-Remaining": "0"}))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.gap_detected is False, "a rate limit was reported as lost history"
    assert cursor.position["last_event_id"] == "500"
    assert cursor.last_polled_at is not None


@pytest.mark.django_db
def test_a_quiet_repository_is_not_declared_gapped_for_having_no_news():
    now = timezone.now()
    cursor = _cursor(position={"last_event_id": "999999"}, watermark_at=now)
    old = _push_event(1, created=_iso(now - timedelta(days=3)))
    session = _Session(events=_Response(200, [old]), compare=_Response(200, _compare(1)))
    with _patched(session):
        driver.poll_github_repositories()
    cursor.refresh_from_db()
    assert cursor.gap_detected is False, "a quiet repository was declared gapped"


@pytest.mark.django_db
def test_an_empty_feed_is_nothing_happened_not_something_lost():
    # The watermark must be INSIDE the retention window for "empty means nothing
    # happened" to hold at all. This test originally used 60 days, which is now
    # correctly gapped instead — the premise was wrong, not the new rule
    # (Morrow RC 3659).
    cursor = _cursor(position={"last_event_id": "500"}, watermark_at=timezone.now() - timedelta(days=2))
    session = _Session(events=_Response(200, [], {"ETag": 'W/"e"'}))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("empty") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.gap_detected is False
    assert cursor.position["etag"] == 'W/"e"'
    assert cursor.position["last_event_id"] == "500"


@pytest.mark.django_db
def test_a_gapped_cursor_is_excluded_from_the_sweep_entirely():
    _cursor(gap_detected=True, position={"last_event_id": "500"})
    session = _Session(events=_Response(200, [_push_event(9999)]))
    with _patched(session):
        outcomes = driver.poll_github_repositories()
    assert outcomes == {}, outcomes
    assert session.calls == [], "a gapped cursor was polled"


# ---------------------------------------------------------------------------
# Cadence is DURABLE and enforced.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_poll_interval_is_persisted_and_then_HONOURED():
    """Morrow RC 3656. The first version parsed `X-Poll-Interval` and ignored
    it — a value read for decoration, which looks like compliance."""
    cursor = _cursor()
    session = _Session(events=_Response(200, [], {"ETag": 'W/"e"', "X-Poll-Interval": "3600"}))
    with _patched(session):
        driver.poll_github_repositories()

    cursor.refresh_from_db()
    assert cursor.position.get("next_poll_at"), "the interval was read and thrown away"

    second = _Session(events=_Response(200, [_push_event(1002)]), compare=_Response(200, _compare(1)))
    with _patched(second):
        outcomes = driver.poll_github_repositories()
    assert outcomes.get("not-due") == 1, outcomes
    assert second.calls == [], "GitHub asked us to wait an hour and we asked again immediately"


# ---------------------------------------------------------------------------
# The sweep survives one repository's bad day.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_unexpected_exception_is_contained_to_its_own_cursor():
    """Morrow RC 3656: `_poll_one`'s "never raises" was asserted, not true —
    translation now reaches the network and raises in more ways than the fetch
    alone. One repository's bad day must not take the sweep down with it."""
    _cursor(repo_full_name="acme/broken", repo_stable_id=1)
    healthy = _cursor(repo_full_name=REPO, repo_stable_id=REPO_ID)

    real = github_events.fetch_events

    def explode(repo_full_name, **kw):
        if repo_full_name == "acme/broken":
            raise RuntimeError("something nobody anticipated")
        return real(repo_full_name, **kw)

    session = _Session(events=_Response(200, [_push_event(1002)]), compare=_Response(200, _compare(1)))
    with _patched(session), mock.patch.object(github_events, "fetch_events", explode):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("error") == 1, outcomes
    assert outcomes.get("advanced") == 1, "a healthy repository was taken down with a broken one"
    healthy.refresh_from_db()
    assert healthy.position.get("last_event_id") == "1002"


def test_polling_without_a_token_is_refused_rather_than_attempted_anonymously():
    with override_settings(GITHUB_ACCESS_TOKEN=None):
        with pytest.raises(github_events.MissingCredential):
            github_events.fetch_events(REPO, session=_Session())


# ---------------------------------------------------------------------------
# A skipped observation is not an advanced-past observation (Vex).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_observation_that_forms_no_key_does_not_advance_the_boundary():
    """Vex, on the poll-fetcher slice, and confirmed at source before fixing.

    `ingest_observation` returns "skipped" when an observation forms no semantic
    key, and NOTHING is stored for it. `poll_repo_page` then advanced anyway —
    so the boundary moved past history that was never written, with no error and
    no row. The same silent-skip-then-advance class Rowan caught in RC 3557,
    reached through a different door.

    The refusal belongs to the boundary's owner rather than to each caller's
    translation: a caller that forgets one field would otherwise reopen the
    whole class, and a rule enforced by every caller remembering it is not
    enforced at all.
    """
    from plane.bridge import poller

    cursor = _cursor(position={"last_event_id": "1000"}, watermark_at=timezone.now())
    # An observation kind the ingester does not handle. This is the REACHABLE
    # form of "skipped": a missing merge_sha does NOT skip — it raises
    # IncompleteEvent, which is already fail-closed — so the mechanism arrives
    # through an unhandled kind rather than through a missing key component.
    # Checked rather than assumed; the first version of this test asserted the
    # missing-sha precondition and failed in BOTH arms, which is a broken
    # falsifier, not a finding.
    unidentifiable = {
        "kind": "issue_comment", "repo_full_name": REPO, "repo_id": REPO_ID,
    }
    assert poller.ingest_observation(unidentifiable) == "skipped", (
        "precondition: this observation must be one the inbox stores nowhere"
    )
    assert ForgejoDelivery.objects.count() == 0

    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(cursor, [unidentifiable], {"last_event_id": "1002"}, timezone.now())

    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000", "advanced past an observation stored nowhere"


@pytest.mark.django_db
def test_a_push_missing_a_key_component_names_the_field_rather_than_skipping():
    """The translation refuses first and says WHICH component was absent. The
    boundary owner's refusal above is the backstop, not the only guard."""
    cursor = _cursor(position={"last_event_id": "1000"})
    event = _push_event(1002)
    del event["payload"]["ref"]
    session = _Session(events=_Response(200, [event]), compare=_Response(200, _compare(1)))

    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000"


# ---------------------------------------------------------------------------
# RC 3658 (Rowan) and RC 3659 (Morrow): six faces of ONE rule —
# the only way past an event is to STORE it. Everything else refuses.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_no_change_tick_cannot_overwrite_a_concurrently_committed_boundary():
    """Rowan RC 3658. `_touch` wrote `position` with a plain update(), so a
    worker holding a stale read at 1000 sent a boundary another worker had
    committed at 2000 backwards. A "nothing new" write is still a write, and the
    quiet path escaped the compare-and-set because it did not look like one."""
    cursor = _cursor(position={"last_event_id": "1000"})
    stale_view = PollCursor.objects.get(pk=cursor.pk)

    PollCursor.objects.filter(pk=cursor.pk).update(position={"last_event_id": "2000"})

    driver._touch(stale_view, {"etag": 'W/"new"'})

    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "2000", "a no-change tick sent the boundary backwards"


@pytest.mark.django_db
def test_an_event_delivered_late_is_INGESTED_not_returned_before_collection_forever():
    """Rowan RC 3658. An event six minutes late fell one minute outside an
    overlap constant chosen by feel, and was excluded from the returned page —
    on every subsequent poll. Zero deliveries, no error, under a losslessness
    claim. The boundary now decides when to stop PAGING, never what to keep."""
    now = timezone.now()
    _cursor(position={"last_event_id": "1000"}, watermark_at=now)
    late = _push_event(1002, created=_iso(now - timedelta(minutes=6)))
    session = _Session(events=_Response(200, [late]), compare=_Response(200, _compare(1)))

    with _patched(session):
        driver.poll_github_repositories()

    assert ForgejoDelivery.objects.count() == 1, "the late event was never ingested at all"


@pytest.mark.django_db
def test_a_new_branch_push_is_ingested_rather_than_dropped():
    """Rowan RC 3658, reproduced against the real GitHub API. A push that
    CREATES a ref carries forty zeros as `before`, the compare endpoint 404s on
    that range, and the code returned None and advanced — so every first push to
    a new branch vanished silently."""
    cursor = _cursor()
    event = _push_event(1002, before="0" * 40)
    session = _Session(
        events=_Response(200, [event]),
        compare=_Response(200, _compare(1)),
    )
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("advanced") == 1, outcomes
    row = ForgejoDelivery.objects.get(forge="github")
    assert row.payload["commits"][0]["message"] == "fix, refs ACME-0"
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1002"


@pytest.mark.django_db
def test_a_compare_404_on_a_REAL_range_refuses_rather_than_dropping():
    """Gone means UNREADABLE, not empty. We cannot tell "this push had no
    commits" from "we could not read what it had", and only one of those is safe
    to advance past."""
    cursor = _cursor(position={"last_event_id": "1000"})
    session = _Session(events=_Response(200, [_push_event(1002)]), compare=_Response(404, {}))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000"
    assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
def test_a_cursor_older_than_the_retention_window_is_gapped_not_reassured():
    """Morrow RC 3659. An empty feed was read as proof nothing happened —
    "anything after our boundary would still BE here". That holds only while the
    boundary is INSIDE the retention window. A 60-day-old cursor against a
    30-day window cannot tell a quiet repository from one whose missed events
    have already aged out."""
    cursor = _cursor(position={"last_event_id": "500"},
                     watermark_at=timezone.now() - timedelta(days=60))
    session = _Session(events=_Response(200, [], {"ETag": 'W/"e"'}))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("gap") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.gap_detected is True
    assert "re-seed" in (cursor.last_error or "").lower()


@pytest.mark.django_db
def test_a_pull_request_authority_404_refuses_rather_than_advancing():
    """Morrow RC 3659. A 404 on the pull request is not evidence it did not
    merge — it is evidence we could not read it, and advancing makes the
    difference permanent."""
    cursor = _cursor(position={"last_event_id": "1000"})
    closed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8, "pull_request": {"number": 8}},
    }
    session = _Session(events=_Response(200, [closed]), pulls=_Response(404, {}))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000"
    assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("bad", [
    {"sha": None, "commit": {"message": "m"}},
    {"sha": "a" * 40, "commit": {}},
    {"sha": "a" * 40},
])
def test_a_malformed_authoritative_commit_refuses_rather_than_advancing(bad):
    """Morrow RC 3659. A commit with no sha or no message is MALFORMED, not
    empty. Ingesting it stores a commit whose message — where directives live —
    was never read, and advancing past it makes that permanent."""
    cursor = _cursor(position={"last_event_id": "1000"})
    session = _Session(
        events=_Response(200, [_push_event(1002)]),
        compare=_Response(200, {"total_commits": 1, "commits": [bad]}),
    )
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000"
    assert ForgejoDelivery.objects.count() == 0


# ---------------------------------------------------------------------------
# RC 3665 (Rowan): round three. Not three more patches — one cursor-transition
# owner, full-page retention, and a completeness proof.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_quiet_tick_cannot_erase_a_gapped_cursors_reseed_instruction():
    """Rowan RC 3665. `mark_gap` does not change `position`, so a no-change
    tick's compare-and-set MATCHED and its `last_error=None` wiped the one
    message telling an operator what to do. The CAS I added the round before
    guarded the field I was thinking about and not the state I was not.

    A gap is a stop, and a stop a concurrent quiet tick can erase is not one.
    """
    from plane.bridge import poller

    cursor = _cursor(position={"last_event_id": "1000"})
    poller.mark_gap(cursor, "history aged out")
    cursor.refresh_from_db()
    instruction = cursor.last_error
    assert "re-seed" in instruction.lower(), "precondition: the gap names a recovery"

    stale_view = PollCursor.objects.get(pk=cursor.pk)
    driver._touch(stale_view, {"etag": 'W/"new"'})

    cursor.refresh_from_db()
    assert cursor.gap_detected is True, "a quiet tick un-gapped the cursor"
    assert cursor.last_error == instruction, "a quiet tick erased the operator's re-seed instruction"


@pytest.mark.django_db
def test_two_delayed_events_in_ONE_page_are_both_kept():
    """Rowan RC 3665. The crossing returned mid-page, so a second event already
    fetched and sitting later in the same page was discarded — and discarded
    again on every subsequent poll. Two delayed events cost one of them
    permanently. Finish the page we already paid for, then stop paging."""
    now = timezone.now()
    _cursor(position={"last_event_id": "1000"}, watermark_at=now)
    first_crossing = _push_event(1003, created=_iso(now - timedelta(minutes=6)))
    also_late = _push_event(1002, created=_iso(now - timedelta(minutes=7)), head="c" * 40)
    session = _Session(
        events=_Response(200, [first_crossing, also_late]),
        compare=_Response(200, _compare(1)),
    )
    with _patched(session):
        driver.poll_github_repositories()

    assert ForgejoDelivery.objects.count() == 2, (
        "an event already fetched in the same page was discarded by the pagination stop"
    )


@pytest.mark.django_db
def test_a_multi_commit_first_push_does_not_claim_to_be_one_commit():
    """Rowan RC 3665. The head commit proves the HEAD, not the push. Asserting
    `total=1` for a new ref is manufactured completeness — the same defect fixed
    in `_commits_and_total` two rounds earlier, reintroduced twenty lines away.
    A first push carrying five commits lost four, and the directives in their
    messages with them.

    A new ref's new work is its divergence from the default branch, which is
    also what the webhook path would have seen — so poll/webhook parity holds
    for exactly the case a feature branch is.
    """
    cursor = _cursor()
    event = _push_event(1002, before="0" * 40)
    session = _Session(
        events=_Response(200, [event]),
        compare=_Response(200, _compare(5)),
    )
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("advanced") == 1, outcomes
    row = ForgejoDelivery.objects.get(forge="github")
    assert len(row.payload["commits"]) == 5, "a multi-commit first push was reduced to its head"
    assert row.payload["commits_total"] == 5
    assert row.payload["commits_seen"] == 5
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1002"


@pytest.mark.django_db
def test_every_cursor_write_outside_the_advance_goes_through_ONE_owner():
    """The structural claim, asserted rather than described. Three rounds each
    closed one hole and opened another because the cursor had four writers, each
    enforcing the invariants its author happened to be thinking about."""
    import inspect

    # SCAN THE BOUNDARY'S OWN MODULE, NOT THE DRIVER (Rowan RC 3665, re-affirmed).
    # The previous version scanned `poll_github_task` only and passed for a whole
    # round while the property it claimed to enforce was false: `mark_gap` lives
    # in `poller` and was still a second writer. A structural test scoped to the
    # wrong module proves the wrong boundary, and passing is exactly what it does
    # while doing so.
    from plane.bridge import poller as poller_mod

    driver_body = inspect.getsource(driver)
    driver_body = driver_body[driver_body.index("def _poll_one("):]
    assert "PollCursor.objects.filter(pk=" not in driver_body, (
        "the driver writes a cursor directly instead of through poller.record_tick"
    )
    assert ".update(" not in driver_body, "the driver still performs its own cursor update"

    # In `poller`, exactly ONE function may write a cursor row.
    writers = []
    for name, fn in vars(poller_mod).items():
        if not callable(fn) or getattr(fn, "__module__", None) != poller_mod.__name__:
            continue
        try:
            body = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "select_for_update" in body or "PollCursor.objects.filter(" in body:
            writers.append(name)
    assert sorted(writers) == ["poll_repo_page", "record_tick"], (
        f"cursor writers in poller are {sorted(writers)} — every write must go through "
        "record_tick or the advance, or the compare-and-set is optional again"
    )


# ---------------------------------------------------------------------------
# BIP-46 slice 2: author-vs-merger attribution.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_merge_carries_who_proposed_it_and_who_accepted_it():
    """The ticket's attribution requirement. Verified live before building:
    Forgejo's PR object distinguishes them — on this project's own PR #72,
    `user.login=aria` and `merged_by.login=morrow`. GitHub's REST pull object
    carries the same pair; the events feed carries NEITHER, since the
    2025-10-07 trim removed `merged_by` along with `merged` and `title`. So
    hydration is the only place these exist at all."""
    _cursor()
    closed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8, "pull_request": {"number": 8}},
    }
    session = _Session(
        events=_Response(200, [closed]),
        pulls=_Response(200, {
            "number": 8, "merged": True, "merge_commit_sha": "d" * 40,
            "title": "t", "body": "Closes ACME-2",
            "user": {"id": 11, "login": "aria"}, "merged_by": {"id": 22, "login": "morrow"},
        }),
    )
    with _patched(session):
        driver.poll_github_repositories()

    pull = ForgejoDelivery.objects.get(forge="github").payload["pull_request"]
    assert pull["user"] == {"id": 11, "login": "aria"}
    assert pull["merged_by"] == {"id": 22, "login": "morrow"}


@pytest.mark.django_db
def test_a_merge_naming_NOBODY_is_distinguishable_from_one_we_never_read():
    """`merged_by` is nullable by schema on both providers. Absent means we did
    not measure it; present-and-null means we measured and the provider named
    nobody. Collapsing them would let a payload assert "nobody merged this" from
    a field never read — and treating null as "the author merged it" would
    suppress exactly the confirmation the author-is-not-merger case produces."""
    _cursor()
    closed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8, "pull_request": {"number": 8}},
    }
    session = _Session(
        events=_Response(200, [closed]),
        pulls=_Response(200, {
            "number": 8, "merged": True, "merge_commit_sha": "d" * 40,
            "title": "t", "body": "b", "user": {"id": 11, "login": "aria"}, "merged_by": None,
        }),
    )
    with _patched(session):
        driver.poll_github_repositories()

    pull = ForgejoDelivery.objects.get(forge="github").payload["pull_request"]
    assert "merged_by" in pull, "a measured-but-empty merger must not look unmeasured"
    assert pull["merged_by"] is None
    assert pull["user"] == {"id": 11, "login": "aria"}, "the author must not be reported as their own merger"


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["user", "merged_by"])
def test_an_ABSENT_actor_field_refuses_rather_than_recording_a_measurement(field):
    """Rowan RC 3712. The three states were claimed and two were implemented: a
    missing key, a malformed actor and an explicit null all collapsed to None,
    so "we never read this" was stored as "the provider named nobody" — and the
    cursor advanced past it. An unread field must never be recorded as a
    measurement, so an absent key refuses BEFORE anything is stored."""
    cursor = _cursor(position={"last_event_id": "1000"})
    closed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8, "pull_request": {"number": 8}},
    }
    record = {
        "number": 8, "merged": True, "merge_commit_sha": "d" * 40,
        "title": "t", "body": "b",
        "user": {"id": 11, "login": "aria"}, "merged_by": {"id": 22, "login": "morrow"},
    }
    del record[field]
    session = _Session(events=_Response(200, [closed]), pulls=_Response(200, record))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    assert ForgejoDelivery.objects.count() == 0, "stored an observation with an unread actor"
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000", "advanced past an unread actor"


@pytest.mark.django_db
@pytest.mark.parametrize("bad", [
    {"login": "aria"},                    # no id at all — a name is not an identity
    {"id": 0, "login": "aria"},           # not a positive id
    {"id": -1, "login": "aria"},
    {"id": True, "login": "aria"},        # bool is an int subclass
    {"id": "11", "login": "aria"},        # a numeric string is not an id
    {"id": 11, "login": ""},              # no login
    {"id": 11},
    "aria",                               # not an actor object at all
    42,
])
def test_a_MALFORMED_actor_refuses_rather_than_being_guessed_at(bad):
    """Malformed is not the same as absent-of-person, and it must not degrade to
    it. Each of these previously became None and was stored as a measured
    absence."""
    cursor = _cursor(position={"last_event_id": "1000"})
    closed = {
        "id": "1002", "type": "PullRequestEvent", "created_at": _iso(timezone.now()),
        "payload": {"action": "closed", "number": 8, "pull_request": {"number": 8}},
    }
    session = _Session(events=_Response(200, [closed]), pulls=_Response(200, {
        "number": 8, "merged": True, "merge_commit_sha": "d" * 40,
        "title": "t", "body": "b", "user": {"id": 11, "login": "aria"}, "merged_by": bad,
    }))
    with _patched(session):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("incomplete") == 1, outcomes
    assert ForgejoDelivery.objects.count() == 0
    cursor.refresh_from_db()
    assert cursor.position["last_event_id"] == "1000"


@pytest.mark.django_db
def test_two_actors_differing_ONLY_by_id_are_not_the_same_person():
    """Rowan RC 3712, the half that matters most for the write boundary. Logins
    are display and mapping data — renameable and reusable — so two accounts can
    present the same one. Carrying only logins made two distinct REST records
    produce byte-identical payloads, which cannot support excluding a pull
    request's author from its own approval count: the rule needs to know WHO."""
    from plane.bridge import poller

    one = poller._merged_pr_payload(REPO, REPO_ID, 8, "d" * 40, "t", "b",
                                    author={"id": 11, "login": "sameName"},
                                    merged_by={"id": 22, "login": "morrow"})
    other = poller._merged_pr_payload(REPO, REPO_ID, 8, "d" * 40, "t", "b",
                                      author={"id": 99, "login": "sameName"},
                                      merged_by={"id": 22, "login": "morrow"})
    assert one != other, "two different accounts sharing a login were indistinguishable"
    assert one["pull_request"]["user"]["id"] == 11
    assert other["pull_request"]["user"]["id"] == 99


@pytest.mark.django_db
def test_attribution_does_not_enter_the_semantic_key():
    """Identity is (instance, repo, PR number, merge sha). If attribution
    entered the key, a webhook that saw a merger and a poll that did not would
    produce DIFFERENT keys for one merge — and the event would execute twice,
    which is the exact outcome this transport exists to prevent."""
    from plane.bridge import poller

    from plane.bridge import instance_config

    instance = instance_config.resolve(poller._forge())
    with_people = poller._merged_pr_payload(REPO, REPO_ID, 8, "d" * 40, "t", "b",
                                            author={"id": 11, "login": "aria"},
                                            merged_by={"id": 22, "login": "morrow"})
    without = poller._merged_pr_payload(REPO, REPO_ID, 8, "d" * 40, "t", "b")
    assert (poller._canonical_for("pull_request", with_people, instance)
            == poller._canonical_for("pull_request", without, instance)), (
        "attribution changed the semantic key, so one merge would execute twice"
    )


@pytest.mark.django_db
def test_a_stale_worker_cannot_FREEZE_a_cursor_another_worker_advanced():
    """Morrow RC 3683 / Rowan's re-seat boundary.

    `mark_gap` was a second writer with no compare-and-set. Worker A diagnoses a
    gap from a stale read at 1000 / sixty days old; worker B advances to 2000 /
    current; A's mark_gap then lands and FREEZES the healthy row — with an
    operator instruction to re-seed a boundary nobody is at any more.

    A gap is diagnosed FROM a boundary and is only valid FOR that boundary.
    Refusing costs one poll cycle, because a real gap is re-diagnosed from the
    current boundary by whichever worker reads next. Freezing a healthy cursor
    costs an operator.
    """
    from plane.bridge import poller

    now = timezone.now()
    cursor = _cursor(position={"last_event_id": "1000"}, watermark_at=now - timedelta(days=60))
    stale_view = PollCursor.objects.get(pk=cursor.pk)          # worker A's read

    # worker B advances to a current boundary
    assert poller.record_tick(cursor, position={"last_event_id": "2000"}) is True
    PollCursor.objects.filter(pk=cursor.pk).update(watermark_at=now)

    # worker A's gap, diagnosed from the boundary that no longer exists. The
    # refusal RAISES rather than returning False (RC 3687): a caller cannot
    # discard it, which is precisely how the driver came to report this benign
    # race as lost history.
    with pytest.raises(poller.StaleCursor):
        poller.mark_gap(stale_view, "history aged out")

    cursor.refresh_from_db()
    assert cursor.gap_detected is False, "a stale worker froze a healthy, advanced cursor"
    assert cursor.position["last_event_id"] == "2000"
    assert cursor.last_error is None, "a stale diagnosis left an operator instruction behind"


@pytest.mark.django_db
def test_a_gap_diagnosed_from_the_CURRENT_boundary_still_lands():
    """The control. Making mark_gap conditional must not make it impotent — a
    real gap, diagnosed from the boundary that is actually current, still stops
    the cursor and still names the recovery."""
    from plane.bridge import poller

    cursor = _cursor(position={"last_event_id": "1000"},
                     watermark_at=timezone.now() - timedelta(days=60))

    assert poller.mark_gap(cursor, "history aged out") is None  # recorded, nothing raised

    cursor.refresh_from_db()
    assert cursor.gap_detected is True
    assert "re-seed" in (cursor.last_error or "").lower()


@pytest.mark.django_db
def test_a_REFUSED_gap_is_reported_as_stale_rather_than_as_lost_history():
    """Morrow RC 3687, reproduced independently by Rowan (3688) and by Sable,
    who replaced her own approval of this head rather than noting it under one.

    The compare-and-set that refuses a superseded gap diagnosis got the STORAGE
    half right — the healthy row is preserved, unfrozen, with no operator
    instruction. Then the driver returned "gap" anyway, because both retention
    branches discarded the boolean that the same commit introduced. So the row
    says "nothing is wrong" and the sweep result says "history is gone", and the
    thing an operator actually reads is the second one.

    That collapses this module's three-outcome contract at the only place it is
    observable. A peer that merely got there first is `stale`; `gap` means an
    operator must re-seed. Reporting one as the other manufactures exactly the
    false incident the separation was built to prevent.

    The ordering is the real one: worker A reads a 60-day-old boundary, worker B
    advances the durable row while A is out at the network, and A comes back to
    diagnose retention loss from a boundary that no longer exists.
    """
    cursor = _cursor(position={"last_event_id": "1000"},
                     watermark_at=timezone.now() - timedelta(days=60))
    real_fetch = github_events.fetch_events

    def _peer_advances_then_fetch(*args, **kwargs):
        PollCursor.objects.filter(pk=cursor.pk).update(
            position={"last_event_id": "2000"}, watermark_at=timezone.now()
        )
        return real_fetch(*args, **kwargs)

    session = _Session(events=_Response(200, [], {"ETag": 'W/"e"'}))
    with _patched(session), mock.patch.object(github_events, "fetch_events", _peer_advances_then_fetch):
        outcomes = driver.poll_github_repositories()

    assert outcomes.get("stale") == 1, outcomes
    assert not outcomes.get("gap"), (
        "a benign peer-advance was reported to the operator as lost history"
    )

    cursor.refresh_from_db()
    assert cursor.gap_detected is False, "the refused diagnosis froze the row after all"
    assert cursor.last_error is None, "a refused gap left a re-seed instruction behind"
    assert cursor.position["last_event_id"] == "2000", "the peer's advance was clobbered"


@pytest.mark.django_db
def test_a_gap_is_refused_when_only_the_WATERMARK_moved_under_it():
    """The half the position check cannot see, and which survived a mutation
    until this test existed.

    `test_a_stale_worker_cannot_FREEZE_…` moves BOTH halves, so the position
    comparison alone catches it — dropping the watermark clause from the
    compare-and-set left every test green. A boundary is a PAIR, and a gap
    diagnosed before the watermark advanced rests on evidence about a window
    that has since moved, even when the position is untouched.
    """
    from plane.bridge import poller

    now = timezone.now()
    cursor = _cursor(position={"last_event_id": "1000"}, watermark_at=now - timedelta(days=60))
    stale_view = PollCursor.objects.get(pk=cursor.pk)

    # Another worker advances ONLY the watermark — the position is unchanged.
    PollCursor.objects.filter(pk=cursor.pk).update(watermark_at=now)

    with pytest.raises(poller.StaleCursor):
        poller.mark_gap(stale_view, "history aged out")
    cursor.refresh_from_db()
    assert cursor.gap_detected is False, "a stale watermark froze a healthy cursor"
    assert cursor.last_error is None
