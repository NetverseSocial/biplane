# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Drive the GitHub polling transport (BIP-46).

Until this existed the transport was complete and never invoked: `poll_repo_page`
had no caller outside its own module and tests, so a correct, well-tested ingest
path ingested nothing. This is the thing that calls it.

WHAT THIS OWNS: when to ask, which cursor, and what to do with each outcome.
WHAT IT DOES NOT: every rule about losslessness already has an owner in `poller`
— durable insert before the boundary moves, the compare-and-set on the
(position, watermark) pair, the gapped-cursor refusal, idempotent ingest.
Re-deciding any of them here would make a second authority for a rule that has
one, which is the defect this codebase keeps paying for.

THE THREE OUTCOMES STAY APART, because they call for different human responses:

* `GapDetected` — history is gone. The cursor stops until an operator re-seeds.
* `IncompleteRead` — we did not finish reading. Nothing lost, boundary untouched,
  next tick re-reads the same window. An ordinary event, not an incident.
* `StaleCursor` — another worker got there first. Expected under concurrency, and
  the inserts still stand.

Collapsing any two would send an operator hunting for lost history after a rate
limit, which is the failure this separation exists to prevent.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from plane.bridge import github_events, instance_config, poller
from plane.db.models import PollCursor

logger = logging.getLogger("plane.worker")

FORGE_NAME = "github"

# GitHub keeps 300 events / 30 days on the events feed. A cursor older than that
# cannot establish from the feed whether anything was missed — the evidence has
# aged out along with the events (Morrow RC 3659).
RETENTION = timedelta(days=30)


def _poll_one(cursor: PollCursor) -> str:
    """Poll a single cursor. Returns a short outcome word for the log line.

    NEVER RAISES — and unlike the first version, that is now true rather than
    asserted. Translation reaches the network, so it raises in more ways than
    the fetch alone did; an unexpected exception here would abort the whole
    sweep and take every other repository down with one repository's bad day.
    Every exit either advances the boundary or records, in `last_error`, why it
    did not.
    """
    try:
        return _poll_one_inner(cursor)
    except github_events.MissingCredential as e:
        _note(cursor, str(e))  # config, not history: an operator sets a token
        return "unconfigured"
    except github_events.IncompleteRead as e:
        _note(cursor, str(e))
        return "incomplete"
    except poller.StaleCursor as e:
        logger.info(f"git-bridge poll: {e}")  # another worker is ahead; inserts stand
        return "stale"
    except poller.GapDetected as e:
        logger.warning(f"git-bridge poll: {e}")
        return "gap"
    except Exception:
        # The catch-all is the point of the promise. Unknown failures are logged
        # with a traceback and contained to this cursor.
        logger.exception(
            f"git-bridge poll: unexpected failure on {cursor.repo_full_name}; "
            "the boundary was not moved and the sweep continues"
        )
        _note(cursor, "unexpected failure; see the worker log for the traceback")
        return "error"


def _poll_one_inner(cursor: PollCursor) -> str:
    if cursor.gap_detected:
        return "gapped"

    position = cursor.position if isinstance(cursor.position, dict) else {}
    if not _is_due(position):
        # GitHub asked us to slow down and we are honouring it durably. A
        # poll_interval that is read and ignored is decoration that looks like
        # compliance.
        return "not-due"

    events, meta = github_events.fetch_events(
        cursor.repo_full_name, since=cursor.watermark_at, etag=position.get("etag")
    )

    # We walked the whole feed and never read back to our window. That is not
    # "nothing new" — but it is not automatically lost history either, and the
    # difference decides whether an operator gets woken. If the oldest event the
    # feed still holds predates our watermark, the window reaches back past
    # where we left off and nothing can have gone missing in between. Only when
    # the window has moved PAST our position is history truly gone.
    if cursor.watermark_at is not None:
        stale_beyond_retention = timezone.now() - cursor.watermark_at > RETENTION
        if stale_beyond_retention:
            # Morrow RC 3659. An empty feed was treated as proof that nothing
            # happened — "anything after our boundary would still BE here". That
            # holds only while our boundary is INSIDE the retention window. A
            # cursor 60 days behind a 30-day window cannot distinguish "quiet
            # repository" from "everything we missed has already aged out", and
            # the safe reading of an ambiguity about lost history is not the
            # comfortable one.
            poller.mark_gap(
                cursor,
                f"the cursor for {cursor.repo_full_name} is at "
                f"{cursor.watermark_at.isoformat()}, older than GitHub's events retention "
                f"({RETENTION.days} days / 300 events), so whether anything was missed can no "
                "longer be established from the feed. Re-seed from a known-good boundary."
            )
            return "gap"
        if not meta["reached_window"] and not meta["feed_empty"]:
            oldest = meta.get("oldest_created_at")
            if oldest is None or oldest > cursor.watermark_at:
                poller.mark_gap(
                    cursor,
                    f"the events feed for {cursor.repo_full_name} no longer reaches back to "
                    f"{cursor.watermark_at.isoformat()} (GitHub keeps 300 events / 30 days)",
                )
                return "gap"

    if not events:
        _touch(cursor, meta)
        return "empty"

    # Hydration happens BEFORE anything advances. A page whose facts we could not
    # fully resolve raises, and the caller above turns that into an untouched
    # boundary — a partially-read push must never be ingested as a smaller push.
    observations = github_events.observations_from(
        events, cursor.repo_full_name, cursor.repo_stable_id, session=None
    )

    newest = events[0]
    watermark = _created_at(newest)
    if watermark is None:
        # Unreachable via fetch_events, which already refuses these — belt and
        # braces on the one value that must never be synthesised.
        raise github_events.IncompleteRead(
            f"newest event in {cursor.repo_full_name} has no usable created_at; "
            "the boundary was not moved"
        )
    new_position = dict(position)
    new_position["last_event_id"] = str(newest.get("id"))
    new_position["etag"] = meta.get("etag")
    _remember_cadence(new_position, meta)

    poller.poll_repo_page(cursor, observations, new_position, watermark)
    return "advanced"


def _created_at(event):
    from django.utils.dateparse import parse_datetime

    return parse_datetime((event or {}).get("created_at") or "")


def _is_due(position) -> bool:
    """Whether GitHub's requested interval has elapsed."""
    from django.utils.dateparse import parse_datetime

    nxt = parse_datetime(position.get("next_poll_at") or "")
    return nxt is None or timezone.now() >= nxt


def _remember_cadence(position, meta):
    interval = meta.get("poll_interval")
    if isinstance(interval, int) and interval > 0:
        position["next_poll_at"] = (
            timezone.now() + timezone.timedelta(seconds=interval)
        ).isoformat()
    return position


def _note(cursor: PollCursor, message: str):
    """A tick that did not move the boundary, recorded through the one writer.

    The boundary is untouched by construction here — `record_tick` is not given
    a position — so a refusal cannot advance anything even by accident.
    """
    logger.warning(f"git-bridge poll: {message}")
    poller.record_tick(cursor, error=message)


def _touch(cursor: PollCursor, meta):
    """Nothing new. Keep the ETag so the next request is conditional and costs
    no rate limit, and keep the cadence GitHub asked for.

    This is still a WRITE TO THE BOUNDARY and it goes through the same owner as
    every other write. Two defects lived here across two rounds — a plain
    `update()` that let a stale worker send a committed boundary backwards, and
    then a compare-and-set that still cleared `last_error` and erased a gapped
    cursor's re-seed instruction. Both were the quiet path being treated as not
    really a write. It is one.
    """
    position = dict(cursor.position if isinstance(cursor.position, dict) else {})
    if meta.get("etag"):
        position["etag"] = meta["etag"]
    _remember_cadence(position, meta)
    if not poller.record_tick(cursor, position=position, error=None):
        logger.info(
            f"git-bridge poll: {cursor.repo_full_name} was gapped or moved under a no-change "
            "tick; the ETag refresh was dropped rather than written over newer state"
        )


@shared_task
def poll_github_repositories():
    """One sweep over every ungapped GitHub cursor.

    Gapped cursors are excluded in the QUERY rather than skipped in the loop, so
    a gap is a state the sweep cannot accidentally poll past — the same reason
    `poll_repo_page` re-reads the flag under its own lock instead of trusting
    the caller's object.
    """
    try:
        instance_config.resolve(poller._forge())
    except instance_config.InstanceConfigError as e:
        # Ingesting under a namespace nothing else will match is worse than not
        # ingesting: the observations would be unreachable by the webhook path's
        # keys forever.
        logger.error(f"git-bridge poll: {e.detail}; no repository was polled")
        return {"polled": 0, "reason": "instance-config"}

    outcomes = {}
    for cursor in PollCursor.objects.filter(forge=FORGE_NAME, gap_detected=False):
        result = _poll_one(cursor)
        outcomes[result] = outcomes.get(result, 0) + 1
    if outcomes:
        logger.info(f"git-bridge poll sweep: {outcomes}")
    return outcomes
