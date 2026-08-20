# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Read what happened in a GitHub repository, as poll observations.

THE EVENTS FEED IS A NOTIFICATION. IT IS NOT AN AUTHORITY.

That sentence is the whole design, and the first version of this module did not
believe it. It read `commits`, `size`, `merged`, `title` and `merge_commit_sha`
straight out of the feed, treating it as a source of truth that occasionally
lacked a field. GitHub trimmed these payloads on 2025-10-07 (changelog
2025-08-08): a `PullRequestEvent`'s `pull_request` now carries only id, url,
number, head and base, and push events lost their commit summaries and counts.
Every field this bridge actually needs — whether a merge happened, which commit
it produced, and the messages where directives live — is gone from the feed and
still present in the REST API. The changelog says so itself: "all removed fields
are still available through the main REST API".

So the division is absolute, and it is not conditional on what a payload happens
to contain today:

* The feed supplies **identity and ordering only** — event id, type, created_at,
  and the repository/ref/sha coordinates needed to ask a question.
* The REST API supplies **every fact**. Always. Not "when the field is missing",
  because a version of this code that reads the field when present is a version
  that behaves differently against two GitHub deployments and cannot be tested
  against the one it will meet.

Consequences that follow from that and are NOT separate rules:

**Hydration is complete before the boundary moves.** A page whose facts we could
not fully resolve does not advance anything. A partially-read push is not "a
small push" — unseen commits carry unseen directives, and advancing past them
loses those permanently and silently. This is why a 20-of-40 push must not
advance: the earlier version declared the shortfall and moved on, which is
correct about not wedging the cursor and wrong about not losing work.

**Malformed input moves neither half of the boundary.** An id that is not an
integer or a created_at that will not parse is not a smaller boundary — it is an
unreadable page, and synthesising a boundary from it would skip whatever we
could not read.

**The boundary decides when to STOP READING, never what to keep.** The feed is
not strictly ordered under delay, so a late arrival can carry a timestamp behind
the boundary. An earlier version excluded such an event from the page it
returned — and then did so on every subsequent poll, so an event delivered six
minutes late, one minute past an overlap constant chosen by feel, was never
ingested at all (Rowan RC 3658). Everything read is now kept and the inbox's
idempotent ingest collapses the re-reads; the overlap only decides how far back
to page, so the constant is no longer load-bearing.

**Cadence is durable.** `X-Poll-Interval` is GitHub asking us to slow down. It is
persisted on the cursor and honoured by the sweep; a value we read and ignore is
decoration that looks like compliance.
"""

import logging
from datetime import timedelta

import requests as http_requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

logger = logging.getLogger("plane.worker")

# GitHub's own ceiling on the events endpoint: beyond page 10 it returns 422
# rather than more history.
MAX_PAGES = 10
PER_PAGE = 100
FETCH_TIMEOUT_SECONDS = 20

# How far back past the stored watermark we re-read. The events feed can deliver
# out of order under load, so an exact stop can step over a late arrival. Ingest
# is idempotent, so the cost of overlap is a few wasted reads and the cost of no
# overlap is a lost push.
OVERLAP = timedelta(minutes=5)

# The compare endpoint returns at most 250 commits. A range above that cannot be
# resolved in one read, and a partially resolved range must not advance.
COMPARE_COMMIT_CEILING = 250

_API_VERSION = "2022-11-28"


class IncompleteRead(RuntimeError):
    """We did not finish reading. NOTHING is known to be lost.

    Rate limit, transport error, page ceiling, an unresolvable compare range, or
    a value we could not parse. The boundary stays exactly where it was and the
    next tick re-reads the same window. Distinct from `GapDetected` on purpose:
    telling an operator that history is gone because we hit a rate limit teaches
    them to ignore the one alert that matters.
    """


class MissingCredential(RuntimeError):
    """No outbound token configured. Polling is read-only but not anonymous:
    anonymous requests are rate limited per-IP at a level that makes a fleet of
    repositories unpollable, and a private repository is invisible entirely."""


def _headers(etag=None):
    token = getattr(settings, "GITHUB_ACCESS_TOKEN", None)
    if not token:
        raise MissingCredential(
            "GITHUB_ACCESS_TOKEN is required to poll GitHub; a read-only "
            "fine-grained token with Contents:read and Metadata:read is enough"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    if etag:
        # A conditional request that 304s does not count against the rate limit,
        # which is what makes a per-minute cadence affordable.
        headers["If-None-Match"] = etag
    return headers


def _strict_int(value):
    """An id is an integer or it is unreadable. Compared as INTEGERS, never as
    strings: "9" > "10" lexically, which would read a newer event as older and
    silently skip everything between."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _get(path, session=None, etag=None, params=None):
    get = (session or http_requests).get
    try:
        response = get(
            f"https://api.github.com/{path.lstrip('/')}",
            headers=_headers(etag),
            params=params or {},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except http_requests.RequestException as e:
        raise IncompleteRead(f"GitHub unreachable while reading {path}: {e}") from e
    return response


def _authority_json(path, session=None):
    """Read one object from the REST API, or None when it genuinely is not there.

    A 404 is an answer — deleted or invisible, nothing to ingest. Everything
    else that is not a clean 200 is an INCOMPLETE READ, never a fact.
    """
    response = _get(path, session=session)
    if response.status_code == 404:
        return None
    if response.status_code in (403, 429):
        raise IncompleteRead(f"GitHub rate-limited the read of {path} (HTTP {response.status_code})")
    if response.status_code >= 400:
        raise IncompleteRead(f"GitHub returned HTTP {response.status_code} for {path}")
    try:
        body = response.json()
    except ValueError as e:
        raise IncompleteRead(f"GitHub response for {path} was not JSON: {e}") from e
    if not isinstance(body, dict):
        raise IncompleteRead(f"GitHub response for {path} was not an object")
    return body


def fetch_events(repo_full_name, since=None, etag=None, session=None):
    """Every event newer than the `since` watermark, newest first.

    `since` is a datetime, not an id. Ordering by time with an explicit overlap
    is what survives out-of-order delivery; an exact id stop does not.

    Returns `(events, meta)` where meta carries `etag`, `poll_interval`,
    `oldest_created_at`, `feed_empty` and `reached_window` — the last being True
    when the walk saw an event at or before `since - OVERLAP`, i.e. proof that we
    read back far enough to be sure nothing between was missed.
    """
    boundary = (since - OVERLAP) if since is not None else None
    collected = []
    meta = {
        "etag": etag,
        "poll_interval": None,
        "oldest_created_at": None,
        "feed_empty": False,
        "reached_window": boundary is None,
    }

    for page in range(1, MAX_PAGES + 1):
        response = _get(
            f"repos/{repo_full_name}/events",
            session=session,
            etag=etag if page == 1 else None,
            params={"per_page": PER_PAGE, "page": page},
        )
        if page == 1:
            meta["etag"] = response.headers.get("ETag") or etag
            meta["poll_interval"] = _strict_int(response.headers.get("X-Poll-Interval"))

        if response.status_code == 304:
            meta["reached_window"] = True  # nothing changed; the boundary is intact
            return [], meta
        if response.status_code in (403, 429):
            raise IncompleteRead(
                f"GitHub rate-limited the events read for {repo_full_name} "
                f"(HTTP {response.status_code}); the boundary was not moved"
            )
        if response.status_code == 422:
            break  # past the API's page ceiling
        if response.status_code >= 400:
            raise IncompleteRead(f"GitHub returned HTTP {response.status_code} for {repo_full_name} events")

        try:
            page_events = response.json()
        except ValueError as e:
            raise IncompleteRead(f"GitHub events response for {repo_full_name} was not JSON: {e}") from e
        if not isinstance(page_events, list):
            raise IncompleteRead(f"GitHub events response for {repo_full_name} was not a list")
        if not page_events:
            if page == 1:
                # The feed holds nothing at all. Anything after our watermark
                # would still be HERE, so this is "nothing happened".
                meta["feed_empty"] = True
                meta["reached_window"] = True
            break

        for event in page_events:
            # A malformed identity is an UNREADABLE PAGE, not a smaller one.
            # Continuing past it would advance over whatever we could not read.
            if _strict_int(event.get("id")) is None:
                raise IncompleteRead(
                    f"event in {repo_full_name} has a non-integer id {event.get('id')!r}; "
                    "the boundary was not moved"
                )
            created = parse_datetime(event.get("created_at") or "")
            if created is None:
                raise IncompleteRead(
                    f"event {event.get('id')} in {repo_full_name} has an unparseable "
                    f"created_at {event.get('created_at')!r}; the boundary was not moved"
                )
            meta["oldest_created_at"] = created
            # THE BOUNDARY DECIDES WHEN TO STOP READING, NEVER WHAT TO KEEP
            # (Rowan RC 3658, Morrow RC 3659). The earlier version returned
            # `collected` at this point, excluding the event that crossed the
            # window — so an event delivered six minutes late, one minute past
            # an overlap constant I had picked by feel, was returned-before-
            # collection on EVERY subsequent poll and never ingested at all.
            # Zero deliveries, no error, under a losslessness claim.
            #
            # The fix is not a larger constant; a constant chosen by feel is the
            # defect whatever its value. Everything read is kept, and the inbox's
            # idempotent ingest collapses the re-reads — dedup is already its
            # job. The overlap now only decides how far back to PAGE.
            collected.append(event)
            if boundary is not None and created <= boundary:
                # THE CROSSING STOPS PAGING, NOT KEEPING (Rowan RC 3665). The
                # previous version returned here, mid-page — so a SECOND event
                # already fetched and sitting later in the same page was
                # discarded, and discarded again on every subsequent poll. Two
                # delayed events cost one of them permanently. Finish the page
                # we already paid for, then stop.
                meta["reached_window"] = True

        if meta["reached_window"] or len(page_events) < PER_PAGE:
            break  # feed exhausted, or we have read back far enough

    return collected, meta


def observations_from(events, repo_full_name, repo_id, session=None):
    """Translate events into observations, oldest first, every fact hydrated.

    Only two event types carry a dedupable transition, and they are the two the
    webhook path already handles. Everything else is dropped here rather than
    downstream, so the choice stays visible at the boundary that made it.

    Raises `IncompleteRead` rather than returning a partial observation. A push
    we could not fully resolve must not be ingested as a smaller push.
    """
    out = []
    for event in reversed(events or []):
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "PushEvent":
            observation = _push_observation(payload, repo_full_name, repo_id, session)
        elif kind == "PullRequestEvent":
            observation = _merged_pr_observation(payload, repo_full_name, repo_id, session)
        else:
            observation = None
        if observation is not None:
            out.append(observation)
    return out


def _commits_and_total(compare, before, head, repo_full_name):
    """The commit list and its true total from a compare response, or refuse."""
    raw = compare.get("commits")
    total = compare.get("total_commits")
    if not isinstance(raw, list) or not isinstance(total, int):
        raise IncompleteRead(
            f"compare {before}...{head} in {repo_full_name} did not return a commit list "
            "and a total; the boundary was not moved"
        )
    if len(raw) < total:
        # The compare endpoint caps at 250. A range above it is NOT a truncation
        # to declare and move past — the unseen commits carry unseen directives.
        raise IncompleteRead(
            f"compare {before}...{head} in {repo_full_name} returned {len(raw)} of {total} "
            f"commits (the API caps at {COMPARE_COMMIT_CEILING}); advancing would step over "
            "commits whose messages were never read"
        )
    return _normalised_commits(raw, repo_full_name), total


def _commits_for_new_ref(repo_full_name, head, session):
    """Commits a push that CREATED a ref actually added.

    A new branch has no `before` — GitHub sends forty zeros — and the compare
    endpoint 404s on that range. The previous version read the head commit and
    asserted `total=1`, which is MANUFACTURED COMPLETENESS (Rowan RC 3665): the
    head commit proves the head, not the push. A first push carrying five
    commits lost four of them and the directives in their messages, silently,
    and broke poll/webhook parity for exactly the case a feature branch is.

    The range that exists for a new ref is its divergence from the default
    branch — which is what "new work on this branch" means, and what the webhook
    path would have seen. Walking the ref's whole ancestry instead would
    re-ingest the entire history it was cut from; every one of those commits was
    already observed on the branch it came from.
    """
    repo = _authority_json(f"repos/{repo_full_name}", session)
    if repo is None:
        raise IncompleteRead(
            f"{repo_full_name} is unreadable (404) while resolving a new ref; "
            "the boundary was not moved"
        )
    base = repo.get("default_branch")
    if not isinstance(base, str) or not base:
        raise IncompleteRead(
            f"{repo_full_name} reports no default branch, so a new ref's divergence point "
            "cannot be established; the boundary was not moved"
        )
    compare = _authority_json(f"repos/{repo_full_name}/compare/{base}...{head}", session)
    if compare is None:
        raise IncompleteRead(
            f"the range {base}...{head} for a new ref in {repo_full_name} is unreadable (404); "
            "the boundary was not moved"
        )
    return _commits_and_total(compare, base, head, repo_full_name)


def _normalised_commits(raw, repo_full_name):
    """Commits in the shape the observation carries, or refuse.

    A commit with no sha or no message is MALFORMED, not empty (Morrow RC 3659).
    Ingesting it would store a commit whose message — where directives live —
    was never actually read, and advancing past it would make that permanent.
    """
    out = []
    for entry in raw:
        sha = (entry or {}).get("sha")
        message = ((entry or {}).get("commit") or {}).get("message")
        if not isinstance(sha, str) or not sha or not isinstance(message, str):
            raise IncompleteRead(
                f"a commit from {repo_full_name} is malformed (sha={sha!r}, message "
                f"{'absent' if message is None else 'not a string'}); a commit whose message "
                "was never read must not be ingested as one that referenced nothing"
            )
        out.append({"sha": sha, "message": message})
    return out


def _push_observation(payload, repo_full_name, repo_id, session):
    """A push, with its commits read from the compare range — always.

    The feed's own commit array is never consulted, even when present. Reading it
    "when available" would make this behave differently against two GitHub
    deployments, and the one we can test is not necessarily the one we meet.
    """
    before, head = payload.get("before"), payload.get("head")
    ref = payload.get("ref")
    # ref, before and after are ALL semantic-key components. An observation
    # missing any of them forms no key, is stored nowhere, and — before the
    # refusal in `poll_repo_page` — would have been advanced past silently.
    # Raising here says WHICH field was missing; the boundary owner's refusal is
    # the backstop that makes forgetting one of these non-fatal.
    if not before or not head or not ref:
        raise IncompleteRead(
            f"push event in {repo_full_name} is missing a key component "
            f"(ref={ref!r}, before={before!r}, head={head!r}); the boundary was not moved"
        )

    # A NEW BRANCH has no `before` — GitHub sends forty zeros, and the compare
    # endpoint 404s on that range (Rowan reproduced it against the real API).
    # The earlier version read that 404 as "nothing to ingest" and advanced, so
    # every first push to a new branch was lost silently. The range that exists
    # for a new ref is the ref itself.
    if set(before) == {"0"}:
        commits, total = _commits_for_new_ref(repo_full_name, head, session)
    else:
        compare = _authority_json(f"repos/{repo_full_name}/compare/{before}...{head}", session)
        if compare is None:
            # Gone means UNREADABLE, not empty. We cannot tell "this push had no
            # commits" from "we could not read what it had", and only one of
            # those is safe to advance past. Refuse: the cursor stalls with a
            # named error an operator can act on, rather than moving past
            # history nothing recorded (Rowan RC 3658, Morrow RC 3659).
            raise IncompleteRead(
                f"compare {before}...{head} in {repo_full_name} returned 404 — the range is "
                "unreadable, which is not the same as empty. The boundary was not moved; if "
                "the ref was force-pushed or deleted, re-seed this cursor from a known-good "
                "boundary rather than letting the push be skipped."
            )
        commits, total = _commits_and_total(compare, before, head, repo_full_name)

    return {
        "kind": "push",
        "repo_full_name": repo_full_name,
        "repo_id": repo_id,
        "ref": ref,
        "before": before,
        "after": head,
        "commits": [{"id": c["sha"], "message": c["message"]} for c in commits],
        # Measured, both from the same authority, and equal by construction —
        # because anything else raised above rather than being declared.
        "commits_seen": len(commits),
        "commits_total": total,
    }


def _merged_pr_observation(payload, repo_full_name, repo_id, session):
    """A pull request that actually merged, re-read from the API — always."""
    if payload.get("action") != "closed":
        return None
    pr = payload.get("pull_request") or {}
    number = _strict_int(pr.get("number")) or _strict_int(payload.get("number"))
    if number is None:
        raise IncompleteRead(
            f"closed pull-request event in {repo_full_name} carries no usable number; "
            "the boundary was not moved"
        )

    authoritative = _authority_json(f"repos/{repo_full_name}/pulls/{number}", session)
    if authoritative is None:
        # Morrow RC 3659. Gone means UNREADABLE, not "did not merge". We cannot
        # tell a deleted pull request from one we simply could not read, and
        # advancing past it makes the difference permanent. Refuse: the cursor
        # stalls with a named error rather than silently skipping a merge.
        raise IncompleteRead(
            f"pull request {number} in {repo_full_name} is unreadable (404) — that is not "
            "evidence it did not merge. The boundary was not moved; if the pull request was "
            "genuinely deleted, re-seed this cursor from a known-good boundary."
        )

    # A closed-unmerged PR asserts no transition, and treating it as one is how
    # a bridge completes a ticket for work that was abandoned.
    if not authoritative.get("merged"):
        return None
    merge_sha = authoritative.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not merge_sha:
        raise IncompleteRead(
            f"pull request {number} in {repo_full_name} reports merged with no merge "
            "commit sha; a partial identity must never form a semantic key"
        )
    # ATTRIBUTION, from the same authority read that established the merge — no
    # extra request, no second source. The events feed never carried these: the
    # 2025-10-07 trim removed `merged_by` along with `merged`, `title` and
    # `merge_commit_sha`, so hydration is the only place they exist at all.
    #
    # `merged_by` is nullable by schema. `_login_of` returns None for "the
    # provider named nobody" and the key is always present here, because absent
    # and null mean different things downstream: absent is "never read", null is
    # "read, and there was no one".
    return {
        "kind": "merged_pr",
        "repo_full_name": repo_full_name,
        "repo_id": repo_id,
        "number": number,
        "merge_sha": merge_sha,
        "title": authoritative.get("title"),
        "body": authoritative.get("body"),
        "author": _actor_of(authoritative, "user", repo_full_name, number),
        "merged_by": _actor_of(authoritative, "merged_by", repo_full_name, number),
    }


def _actor_of(record, field, repo_full_name, number):
    """One provider actor, as THREE distinguishable outcomes (Rowan RC 3712).

    The previous version claimed three states and implemented two: a missing
    key, a malformed actor object and an explicit null all collapsed to None,
    so "we never read this" was indistinguishable from "the provider named
    nobody" — and the cursor advanced past both. Executed, all three produced
    byte-identical payloads.

    - **key ABSENT** — we did not read it. RAISES, so nothing is stored and the
      boundary does not move. An unread field must never be recorded as a
      measurement.
    - **explicit null** — read, and the provider named nobody. Returns None,
      which is a MEASURED absence and is stored as one.
    - **an actor** — must carry BOTH a strict positive provider `id` and a
      non-empty `login`, or it is malformed and RAISES.

    THE ID IS THE IDENTITY, NOT THE LOGIN. Logins are display and mapping data:
    they are renameable and reusable, so two different accounts can present the
    same one. Dropping the id made two REST records with different positive ids
    and identical logins produce byte-identical observations — which cannot
    support an author-exclusion rule, because the rule needs to know WHO, and a
    login does not answer that stably.
    """
    if field not in record:
        raise IncompleteRead(
            f"pull request {number} in {repo_full_name} carries no {field!r} field; "
            "an unread actor must not be recorded as a measured absence"
        )
    actor = record[field]
    if actor is None:
        return None
    if not isinstance(actor, dict):
        raise IncompleteRead(
            f"pull request {number} in {repo_full_name} has a malformed {field!r} "
            f"({type(actor).__name__}); attribution must be refused rather than guessed"
        )
    actor_id = actor.get("id")
    login = actor.get("login")
    if not (isinstance(actor_id, int) and not isinstance(actor_id, bool) and actor_id > 0):
        raise IncompleteRead(
            f"pull request {number} in {repo_full_name} has {field!r} without a positive "
            "provider id; a login alone is not a stable identity"
        )
    if not (isinstance(login, str) and login):
        raise IncompleteRead(
            f"pull request {number} in {repo_full_name} has {field!r} without a login"
        )
    return {"id": actor_id, "login": login}
