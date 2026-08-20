# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""GitHub polling transport for the git bridge (BIP-46 PR-B2).

Polling is outbound: the server asks GitHub's API for new push/merged-PR
activity on watched repos, normalizes each observation into the SAME payload
shape the webhook path stores, and inserts it into the SAME ForgejoDelivery
inbox — keyed by the SAME semantic event key (PR-B1). So a webhook and a poll
of one real event collapse to one outcome, and the existing processing path
(claim/lease, refs, transitions) runs unchanged.

Lossless by construction:
  - Durable insert precedes cursor advance: `poll_repo_page` inserts every
    observation on a page THEN advances the cursor. A crash in between re-polls
    and re-ingests idempotently (the unique semantic key dedups), losing
    nothing.
  - Overlap: the next poll re-fetches back past the boundary; re-seen events
    dedup, so an event landing exactly on the boundary is never skipped.
  - A retention-window gap fails closed (marks the cursor, stops advancing);
    it is never papered over by advancing past unread history.

This module is transport-blind at the contract seam: it produces normalized
BridgeEvent-shaped payloads and never reaches into the processing path. The
live GitHub HTTP client is a separate slice; the functions here take already-
fetched observations so the lossless logic is testable without network.
"""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from plane.bridge import forges, inbox, instance_config
from plane.bridge import semantic_key as skey
from plane.db.models import PollCursor

FORGE_NAME = "github"


def _forge():
    """The forge PERSONALITY object. `instance_config.resolve` takes the forge,
    not its name — it reads `forge.instance_id_setting`. Passing the string
    raises AttributeError, which is a fail-closed crash rather than a wrong
    key, but a crash all the same."""
    return forges.by_name(FORGE_NAME)


class GapDetected(RuntimeError):
    """The cursor is gapped, or no longer matches configured instance identity.

    Raised rather than returned: a gapped cursor must stop the caller, and a
    return value is something a caller can ignore. Fails toward "no ingest, no
    advance, operator named" rather than toward silently skipping history.
    """


class StaleCursor(RuntimeError):
    """This page was computed from a boundary another worker has since moved.

    Distinct from GapDetected on purpose: a gap means history was lost and an
    operator must re-seed, while a stale cursor means a concurrent worker is
    simply ahead of us and the correct response is to re-poll. Collapsing the
    two would send an operator hunting for lost history after an ordinary race.
    Both fail closed on the boundary; neither discards what was already stored.
    """


_UNMEASURED = object()  # sentinel: this field was never read, as distinct from read-as-empty


def _push_payload(repo_full_name, repo_id, ref, before, after, commits,
                  commits_seen=None, commits_total=None):
    """A github push envelope, matching what GitHubForge accessors read.

    `commits_seen`/`commits_total` DECLARE a short commit list (BIP-46). The
    events feed caps a PushEvent's `commits` array while its `size` counts the
    whole push, so a large push arrives short with no error at all — the reader
    cannot tell "three commits" from "three of forty". Carrying both numbers
    turns that silent shortfall into a stated one.

    They are omitted, not defaulted, when nothing declared them. A payload that
    always claims `commits_total` would let a webhook-shaped envelope assert a
    completeness nobody measured. Absent means unmeasured; present means
    measured; equal means complete.

    Neither field participates in the semantic key (instance/repo/ref/before/
    after), so a truncated and an untruncated observation of ONE push still
    collapse to one execution — which is the point: truncation is a property of
    the reading, not of the event.
    """
    payload = {
        "repository": {"full_name": repo_full_name, "id": repo_id},
        "ref": ref,
        "before": before,
        "after": after,
        "commits": [{"id": c.get("id"), "message": c.get("message")} for c in (commits or [])],
    }
    if commits_seen is not None and commits_total is not None:
        payload["commits_seen"] = commits_seen
        payload["commits_total"] = commits_total
    return payload


def _actor_payload(actor, field):
    """One actor as stored: null for a measured absence, or id + login.

    Refuses a login without an id rather than storing a name that cannot
    identify anyone (Rowan RC 3712) — the boundary that will exclude a pull
    request's author from its own approval count needs an identity, and a login
    is not one.
    """
    if actor is None:
        return None
    if not isinstance(actor, dict):
        raise ValueError(f"{field} must be an actor object or None, got {type(actor).__name__}")
    actor_id, login = actor.get("id"), actor.get("login")
    if not (isinstance(actor_id, int) and not isinstance(actor_id, bool) and actor_id > 0):
        raise ValueError(f"{field} carries no positive provider id; a login alone is not identity")
    if not (isinstance(login, str) and login):
        raise ValueError(f"{field} carries no login")
    return {"id": actor_id, "login": login}


def _merged_pr_payload(repo_full_name, repo_id, number, merge_sha, title, body,
                       author=None, merged_by=_UNMEASURED):
    """A github merged-PR envelope.

    `author` and `merged_by` carry the ATTRIBUTION this ticket requires: who
    proposed the work and who accepted it. They do not enter the semantic key —
    identity is (instance, repo, PR number, merge sha) — so a webhook and a poll
    of one merge still collapse to a single execution regardless of what either
    could see about people.

    THREE STATES, NOT TWO. `merged_by` is nullable by schema on both providers,
    and a merge is not guaranteed to name a user. So:

        absent                    we did not measure it
        None                      measured, and the provider named nobody
        {"id": N, "login": "x"}   measured, and this is who accepted it

    AN ACTOR IS AN ID PLUS A LOGIN, never a login alone (Rowan RC 3712). Logins
    are display and mapping data — renameable and reusable — so two different
    accounts can present the same one, and a payload carrying only logins made
    two distinct actors byte-identical. The author-exclusion rule in the write
    boundary has to know WHO, and only the provider's stable id answers that.

    Collapsing the first two would let a payload assert "nobody merged this" from
    a field we simply never read; collapsing the last two would report the author
    as their own merger. Either one suppresses exactly the confirmation the
    author-is-not-merger case exists to produce.
    """
    pull = {
        "merged": True,
        "number": number,
        "merge_commit_sha": merge_sha,
        "title": title or "",
        "body": body or "",
    }
    if merged_by is not _UNMEASURED:
        # BOTH KEYS APPEAR TOGETHER, present-or-null, and this is a correctness
        # requirement rather than tidiness. The poll path's delivery id is
        # SYNTHETIC — derived from the semantic key — so two observations of one
        # merge share an id, and the inbox compares their body digests. A payload
        # whose SHAPE varies with what the provider happened to return therefore
        # produces a COLLISION on the second poll: refused, and the cursor
        # gapped. Caught by its own test, which failed the moment attribution
        # became optional.
        #
        # Null still means "measured, nobody named" and is distinguishable from
        # the whole block being absent, which means the caller never measured.
        pull["user"] = _actor_payload(author, "author")
        pull["merged_by"] = _actor_payload(merged_by, "merged_by")
    return {
        "action": "closed",
        "repository": {"full_name": repo_full_name, "id": repo_id},
        "pull_request": pull,
    }


def _canonical_for(event, payload, provider_instance):
    """The semantic key primitives, extracted from a github-shaped payload.
    Mirrors the webhook path's extraction so both transports feed one key.

    THE NAMESPACE IS THE PROVIDER INSTANCE, and this is the whole reason the
    transport collapses with the webhook. An earlier revision passed
    ``FORGE_NAME`` — the product family — where the merged contract requires
    the configured instance id (ADR 010 §1, landed via PR #65 after this file
    was written). That is not a cosmetic difference: `forgejo_bridge` keys on
    `instance_config.resolve(forge)`, so a poll keyed on "github" produces a
    DIFFERENT hash for the same real event, the dedup misses, and one event
    executes twice — the exact outcome this transport exists to prevent.
    """
    repo = payload.get("repository") or {}
    repo_id = repo.get("id")
    if not skey.is_identity_int(repo_id):
        return None
    if event == "push":
        return skey.push_key(
            provider_instance, repo_id, payload.get("ref"),
            payload.get("before"), payload.get("after"),
        )
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        if pr.get("merged") is not True:
            return None
        return skey.merged_pr_key(
            provider_instance, repo_id, pr.get("number"), pr.get("merge_commit_sha")
        )
    return None


def ingest_observation(observation, provider_instance=None) -> str:
    """Durably insert ONE poll observation as a pending delivery, idempotently.

    Returns "inserted" (new), or "duplicate" (a webhook or prior poll already
    holds this real event — the semantic key collapses them), or "skipped"
    (the observation asserts no dedupable transition). Never raises on a
    duplicate: the unique semantic-key constraint is the dedup surface.
    """
    # The instance is CONFIG, resolved through the single owner the webhook
    # path uses. Never a payload value, never the family (invariant 7 / ADR
    # 010 §1). Unresolvable config raises rather than ingesting under a
    # namespace nothing else will match.
    if provider_instance is None:
        provider_instance = instance_config.resolve(_forge())

    kind = observation.get("kind")
    repo_full_name = observation.get("repo_full_name")
    repo_id = observation.get("repo_id")
    if kind == "push":
        event = "push"
        payload = _push_payload(
            repo_full_name, repo_id, observation.get("ref"),
            observation.get("before"), observation.get("after"), observation.get("commits"),
            commits_seen=observation.get("commits_seen"),
            commits_total=observation.get("commits_total"),
        )
    elif kind == "merged_pr":
        event = "pull_request"
        payload = _merged_pr_payload(
            repo_full_name, repo_id, observation.get("number"),
            observation.get("merge_sha"), observation.get("title"), observation.get("body"),
            author=observation.get("author"),
            merged_by=observation.get("merged_by", _UNMEASURED),
        )
    else:
        return "skipped"

    canonical = _canonical_for(event, payload, provider_instance)
    if canonical is None:
        return "skipped"
    skhash = skey.key_hash(canonical)

    body_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    # THE ONE WRITER (BIP-56). This path used to carry its own storage: an
    # exists() pre-check, a bare create(), and an IntegrityError catch with no
    # savepoint. That re-implementation is why the poll path DROPPED the second
    # observation of an event a webhook had already settled — ADR 010 §3
    # requires every observation stored, the second as a non-holder alias, with
    # coalescing applied to EXECUTION rather than storage (Morrow RC 3533).
    #
    # It re-implemented because the lifecycle was not callable, not because
    # anyone chose to. Now it is, so this asks for the verdict instead of
    # deriving one, and the savepoint that makes recovery survive an enclosing
    # transaction lives in the one place that owns it.
    synthetic_delivery_id = f"poll:{skhash}"  # audit-only; unique per event
    recorded = inbox.record_observation(
        delivery_id=synthetic_delivery_id,
        event=event,
        payload=payload,
        repository=repo_full_name or "?",
        digest=body_digest,
        forge_name=FORGE_NAME,
        canonical_key=canonical,
        key_hash=skhash,
    )
    if recorded.outcome == inbox.CREATED:
        return "inserted"
    if recorded.outcome == inbox.COLLISION:
        # Same synthetic id, different content: refuse rather than coalesce.
        #
        # Report the id from our OWN local, never `recorded.delivery` — on
        # COLLISION that attribute is None by contract (inbox.Recorded: "None
        # only for COLLISION, where nothing of ours was written"). Dereferencing
        # it raised AttributeError on the one path whose entire job is to report
        # the collision, so the operator got a traceback instead of the id
        # (Morrow RC 3559).
        raise GapDetected(
            f"poll refused: delivery id collision on {synthetic_delivery_id!r} "
            "with different content; nothing was ingested."
        )
    # ALIAS or EXISTING — the observation is durably stored either way and
    # EXECUTION belongs to the holder. "duplicate" is the poller's word for
    # "stored, not mine to run".
    return "duplicate"


def poll_repo_page(cursor: PollCursor, observations, new_position, watermark_at):
    """Ingest a page of observations, THEN advance the cursor.

    The ordering is the lossless guarantee: every observation is durably in the
    inbox before the boundary moves past it. Returns the per-observation
    outcomes. If ingestion raises, the cursor is NOT advanced and the caller
    re-polls the same range (idempotent).
    """
    # A GAPPED CURSOR REFUSES. `mark_gap` used to set a flag nothing read, so
    # any ordinary caller could ingest and advance straight past unread history
    # — the documented STOP condition was a comment (Rowan RC on PR #42,
    # defect 2). Read against the CURRENT row under lock, not the caller's
    # object, which may predate another worker's mark_gap.
    with transaction.atomic():
        locked = PollCursor.objects.select_for_update().get(pk=cursor.pk)
        if locked.gap_detected:
            raise GapDetected(
                f"poll refused: cursor {locked.provider_instance}:{locked.repo_stable_id} "
                "is gapped. An operator must reseed it from a known-good boundary "
                "(a full re-scan) and clear gap_detected before polling resumes."
            )

    # The instance is the CURSOR's, not re-derived per observation: one page is
    # one instance's history, and a config change mid-page must not split a
    # page across two namespaces. It is cross-checked against the configured
    # owner, so a cursor left behind by a renamed instance fails closed rather
    # than ingesting under a namespace nothing else will match.
    configured = instance_config.resolve(_forge())
    if cursor.provider_instance != configured:
        raise GapDetected(
            f"poll refused: cursor instance {cursor.provider_instance!r} does not match "
            f"the configured instance {configured!r}; reconcile the cursor before polling."
        )

    # EVERY OBSERVATION MUST BELONG TO THIS CURSOR'S REPOSITORY, checked before
    # a single insert (Rowan RC 3532). Nothing bound the page to the cursor, so
    # a page from repo 2 handed to repo 1's cursor ingested repo 2's deliveries
    # AND advanced repo 1's boundary — both halves are losslessness failures:
    # repo 1 moves past history it never read, and repo 2's events land under a
    # cursor that does not own them. The whole page is refused rather than
    # filtered, because a mixed page means the caller's pagination is wrong and
    # silently dropping the foreign half would hide that.
    #
    # EQUALITY IS NOT ENOUGH: `True == 1` in Python, so a boolean repo id passed
    # repository 1's guard, was then rejected as an identity downstream, came
    # back "skipped", and the cursor advanced past an observation nothing stored
    # — silence where the lossless guarantee requires a refusal (Rowan RC on PR
    # #72). The id must satisfy the identity rule ITSELF, asked of the one owner
    # of that rule, before its value is compared to anything.
    for obs in observations:
        obs_repo = obs.get("repo_id")
        if not skey.is_identity_int(obs_repo) or obs_repo != cursor.repo_stable_id:
            raise GapDetected(
                f"poll refused: observation for repository {obs_repo!r} was handed to the "
                f"cursor for {cursor.provider_instance}:{cursor.repo_stable_id}; "
                "nothing was ingested and the boundary did not move."
            )

    outcomes = []
    for obs in observations:
        outcomes.append(ingest_observation(obs, configured))

    # A SKIPPED OBSERVATION MUST NOT BE ADVANCED PAST (Vex, on the poll-fetcher
    # slice). `ingest_observation` returns "skipped" when the observation forms
    # no semantic key — a missing merge_commit_sha, an absent ref, an unknown
    # kind. Nothing is stored for it. Advancing anyway moves the boundary past
    # history that was never written, with no error and no row: the same
    # silent-skip-then-advance class Rowan caught in RC 3557, reached through a
    # different door.
    #
    # The refusal lives HERE, at the boundary's owner, rather than as a
    # per-field check in each caller's translation. A caller that forgets one
    # field would otherwise reopen the whole class, and a rule enforced by every
    # caller remembering it is not enforced.
    skipped = sum(1 for outcome in outcomes if outcome == "skipped")
    if skipped:
        raise GapDetected(
            f"poll refused: {skipped} of {len(outcomes)} observations formed no semantic key "
            f"and were stored NOWHERE, so the boundary for {cursor.provider_instance}:"
            f"{cursor.repo_stable_id} was not advanced past them. An observation that cannot "
            "be identified must be fixed at its source, not skipped: advancing would lose it "
            "permanently and silently."
        )

    with transaction.atomic():
        locked = PollCursor.objects.select_for_update().get(pk=cursor.pk)
        # Re-check under the advancing lock: a concurrent worker may have
        # marked the gap while this page was ingesting. Ingest is idempotent,
        # so the inserts stand; the BOUNDARY must not move past unread history.
        if locked.gap_detected:
            raise GapDetected(
                f"poll refused: cursor {locked.provider_instance}:{locked.repo_stable_id} "
                "was gapped while this page was ingesting; the boundary was not advanced."
            )
        # Compare-and-set on the boundary. This page was computed FROM
        # `cursor.position`; if the stored boundary is no longer that value, a
        # concurrent worker advanced it while we were ingesting and our
        # new_position is derived from a stale read. Writing it anyway moves the
        # boundary BACKWARD (observed d... -> c..., Morrow RC 3569).
        #
        # THE BOUNDARY IS THE PAIR, NOT THE POSITION (Morrow RC 3630). This
        # block writes `watermark_at` as well, and it is a durable overlap
        # boundary in its own right — the next page's refetch window starts
        # there. Comparing only `position` leaves the other half unguarded, and
        # the regression is reachable without any position conflict: two workers
        # read the same cursor, B commits watermark T, then A commits T-5m. A's
        # position check passes because B's position was equal (both advanced
        # from the same read, or neither moved it), and the watermark silently
        # walks backward — the refetch window widens every round and the same
        # history is re-read forever. Guarding one field of a two-field boundary
        # is not a compare-and-set; it is a compare-and-set on a projection.
        #
        # A regressed boundary does not delete anything already inserted — it
        # invalidates the durable boundary, so the same history is re-read
        # repeatedly, a fast producer can starve the cursor, and far enough back
        # it becomes a retention gap (Morrow's precision on my overstatement).
        # Inserts stand either way: ingest is idempotent and the observations
        # were already durably stored above. Only the boundary is refused.
        if locked.position != cursor.position or locked.watermark_at != cursor.watermark_at:
            raise StaleCursor(
                f"poll refused: cursor {locked.provider_instance}:{locked.repo_stable_id} "
                f"advanced from (position={cursor.position!r}, watermark={cursor.watermark_at!r}) "
                f"to (position={locked.position!r}, watermark={locked.watermark_at!r}) while this "
                "page was ingesting; the boundary was not moved backward. The observations on "
                "this page are stored; re-poll from the current boundary."
            )
        locked.position = new_position
        locked.watermark_at = watermark_at
        locked.last_polled_at = timezone.now()
        locked.last_error = None
        locked.save(update_fields=["position", "watermark_at", "last_polled_at", "last_error", "updated_at"])
    return outcomes


_KEEP = object()  # sentinel: "do not touch this field"


def record_tick(cursor: PollCursor, *, position=_KEEP, error=_KEEP, gap: bool = False) -> bool:
    """THE ONE WRITER of a cursor outside `poll_repo_page` (Rowan RC 3665).

    The cursor had four writers — the advance, the no-change refresh, the gap,
    and the error note — and each enforced the invariants its author happened to
    be thinking about. That is why three review rounds each closed one hole and
    opened another somewhere else: a compare-and-set added to stop a boundary
    regression gave the quiet path a new way to erase an operator's re-seed
    instruction, because the CAS guarded the field I was thinking about and not
    the state I was not.

    Four writers each remembering the rules is not enforcement. So the rules
    live here, once:

    * **A GAPPED CURSOR IS FROZEN.** Its `last_error` names the operator recovery
      and nothing may overwrite or clear it, and its boundary may not move. A
      gap is a stop, and a stop that a concurrent quiet tick can erase is not one.
    * **A POSITION WRITE IS ALWAYS A COMPARE-AND-SET**, even when it carries only
      metadata like an ETag. "Nothing new" is still a write, and a stale worker's
      no-change tick was able to send a concurrently committed boundary backwards.
    * **`last_polled_at` ALWAYS MOVES.** It is the only field that distinguishes
      "we tried and could not" from "nothing has ever run", and blurring those
      costs an operator the first hour of any investigation.

    Returns True if the intended write landed, False if another worker's state
    won. False is an ordinary outcome, not an error: the other worker's boundary
    is newer than ours and there is nothing of ours worth preserving.
    """
    with transaction.atomic():
        locked = PollCursor.objects.select_for_update().get(pk=cursor.pk)
        fields = ["last_polled_at", "updated_at"]
        locked.last_polled_at = timezone.now()

        if locked.gap_detected:
            # Frozen. Record that we looked and change nothing else.
            locked.save(update_fields=fields)
            return False

        # EVERY write here is conditioned on the boundary the caller reasoned
        # FROM, and on BOTH halves of it. A gap is diagnosed from a boundary and
        # is therefore only valid for that boundary — the same rule a position
        # write already obeyed, now applied to the writer that used to bypass it
        # (Morrow RC 3683).
        if (position is not _KEEP or gap) and (
            locked.position != cursor.position or locked.watermark_at != cursor.watermark_at
        ):
            locked.save(update_fields=fields)
            return False

        if position is not _KEEP:
            locked.position = position
            fields.append("position")

        if gap:
            locked.gap_detected = True
            fields.append("gap_detected")

        if error is not _KEEP:
            locked.last_error = error
            fields.append("last_error")

        locked.save(update_fields=fields)
        return True


def mark_gap(cursor: PollCursor, detail: str) -> None:
    """History moved beyond the retention window before we read it. Fail closed:
    record the gap, name the recovery, and STOP advancing this cursor.

    A GAP IS DIAGNOSED FROM A BOUNDARY, SO IT IS ONLY VALID FOR THAT BOUNDARY
    (Morrow RC 3683; Rowan RC 3665 re-affirmed rather than superseded). This was
    a SECOND writer with no compare-and-set: worker A diagnosed a gap from a
    stale read at 1000 / sixty days old, worker B advanced to 2000 / current,
    and A's mark_gap then FROZE the healthy row — with an operator instruction
    telling someone to re-seed a boundary nobody was at any more.

    It now goes through `record_tick`, the one owner, and inherits its
    compare-and-set: a gap diagnosed from a boundary that has since moved is
    refused, because the evidence for it was about a position that no longer
    exists.

    Refusing is safe precisely because polling repeats: if the gap is real it
    will be diagnosed again from the CURRENT boundary by whichever worker reads
    next. Refusing costs one cycle; freezing a healthy cursor costs an operator.

    THE REFUSAL RAISES RATHER THAN RETURNING (Morrow RC 3687, reproduced by
    Rowan and Sable). It first reported the refusal as a bool, and both call
    sites in the driver discarded it and returned "gap" regardless — so the row
    correctly said "healthy" while the sweep result told an operator history was
    gone. A return value is something every caller must remember to inspect;
    this outcome is the one the module exists to keep distinct, so it travels on
    the path that cannot be ignored. `StaleCursor` is that path already, and the
    driver already translates it to "stale", so both call sites inherit the
    right answer without either one knowing about it.
    """
    recorded = record_tick(
        cursor,
        gap=True,
        error=(
            f"retention-window gap: {detail}. Operator recovery: re-seed this repo's "
            "cursor from a known-good boundary (a full re-scan) before polling resumes."
        ),
    )
    if not recorded:
        raise StaleCursor(
            f"gap diagnosis for {cursor.repo_full_name} was refused: the boundary it was "
            "diagnosed from has since moved, so a concurrent worker is ahead of this one. "
            "History is NOT known to be lost; the next poll re-reads from the current "
            "boundary and re-diagnoses if the gap is real."
        )
