# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-46 PR-B2: the GitHub polling transport's lossless ingest.

The violating cases are the point: durable-insert-BEFORE-cursor-advance (a
crash between them loses nothing), overlap re-fetch dedups, a webhook and a
poll of one event collapse (cross-transport, on the shared semantic key), and a
retention gap fails closed instead of advancing past unread history."""

import hashlib
import hmac
import json
import uuid as uuid_lib
from datetime import timedelta
from unittest import mock

import pytest
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from plane.bridge import poller
from plane.bridge.forgejo_bridge import claim_delivery, process_delivery
from plane.db.models import (
    ForgejoDelivery, Issue, PollCursor, Project, State, User, Workspace,
)

REPO = "acme/app"
REPO_ID = 90210


def _fixture():
    u = User.objects.create(email=f"p-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"g{uuid_lib.uuid4().hex[:10]}", name="G", owner=u)
    proj = Project.objects.create(workspace=ws, name="P", identifier="ACME")
    seqs = {}
    for i, (name, group) in enumerate(
        [("Backlog", "backlog"), ("Todo", "unstarted"), ("Review", "started"), ("Done", "completed")]
    ):
        seqs[name] = State.objects.create(
            name=name, project=proj, workspace=ws, group=group, sequence=(i + 1) * 100,
            color="#000", default=(name == "Backlog"), created_by=u,
        )
    issue = Issue.objects.create(workspace=ws, project=proj, name="t", state=seqs["Todo"])
    return u, ws, proj, issue, seqs


def _push_obs(ident_seq, before="b" * 40, after="c" * 40, ref="refs/heads/main"):
    return {
        "kind": "push", "repo_full_name": REPO, "repo_id": REPO_ID,
        "ref": ref, "before": before, "after": after,
        "commits": [{"id": after, "message": f"fix the widget\n\nrefs {ident_seq}"}],
    }


INSTANCE = "github.com/acme"


@pytest.fixture(autouse=True)
def _configured_instance():
    """Every ingest resolves the provider instance through instance_config, so
    the setting must exist. Previously the poller hardcoded the forge FAMILY
    and needed no config at all — which was the defect."""
    with override_settings(GITHUB_INSTANCE_ID=INSTANCE):
        yield


def _scope_map(project, instance=INSTANCE, repo_id=REPO_ID):
    """The repo→project map in the schema BIP-38 (#76) made authoritative.

    The key is "<configured provider INSTANCE>:<stable repo id>", not the forge
    FAMILY and not the display path: repo ids are per-instance sequences, so a
    family key would hand one instance another's projects, and a family-prefixed
    entry now grants nothing (loudly). The value is an explicit list of project
    UUIDs — the workspace-slug form these tests used is the retired pre-BIP-38
    schema and is refused at resolution time.

    This file's earlier shape passed against main before #76 merged and moved
    NOTHING after it: the delivery still 200s, the ticket simply never moves.
    That is the silent-inert failure the scope guard exists to make loud, so the
    map is built here, once, rather than spelled out per test.
    """
    return {f"{instance}:{repo_id}": [str(project.id)]}


def _cursor(instance=INSTANCE, repo_id=REPO_ID, full_name=REPO):
    return PollCursor.objects.create(
        provider_instance=instance, repo_stable_id=repo_id,
        forge="github", repo_full_name=full_name,
    )


@pytest.mark.django_db
def test_ingest_inserts_a_pending_github_delivery():
    _fixture()
    r = poller.ingest_observation(_push_obs("ACME-1"))
    assert r == "inserted"
    d = ForgejoDelivery.objects.get(forge="github")
    assert d.status == "pending" and d.event == "push" and d.semantic_key_hash
    assert d.delivery_id.startswith("poll:")


@pytest.mark.django_db
def test_ingest_is_idempotent():
    _fixture()
    assert poller.ingest_observation(_push_obs("ACME-1")) == "inserted"
    assert poller.ingest_observation(_push_obs("ACME-1")) == "duplicate"
    assert ForgejoDelivery.objects.count() == 1


# The strict xfail that stood here is REMOVED BY BEING SATISFIED, not deleted
# (Morrow RC 3536). It carried ADR 010 §3/§4 as a requirement the poll path
# violated: ingest_observation dropped the second observation of an event a
# webhook had already settled. The poller now consumes plane.bridge.inbox,
# the one writer, so both identities are durably queryable and coalescing
# applies to EXECUTION rather than storage.
@pytest.mark.django_db
def test_poll_dedups_against_a_prior_webhook_row():
    """Cross-transport: a webhook already stored this event; the poll of the
    same event coalesces EXECUTION to it while remaining queryable itself."""
    _fixture()
    obs = _push_obs("ACME-1")
    canonical = poller._canonical_for("push", poller._push_payload(
        REPO, REPO_ID, obs["ref"], obs["before"], obs["after"], obs["commits"]), INSTANCE)
    from plane.bridge import semantic_key as skey
    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="github", event="push",
        payload={"x": 1}, repository=REPO, body_digest="d",
        semantic_key=canonical, semantic_key_hash=skey.key_hash(canonical), status="processed",
    )
    assert poller.ingest_observation(obs) == "duplicate"
    # ADR 010 §3: "Every observation — webhook or poll — is durably inserted as
    # its own row." Two observations of one event leave TWO rows: the holder
    # with the unique hash, and a non-holder alias with a NULL hash pointing at
    # it. Coalescing applies to EXECUTION, not to storage.
    assert ForgejoDelivery.objects.count() == 2
    alias = ForgejoDelivery.objects.get(semantic_key_hash__isnull=True)
    assert alias.semantic_key == canonical
    assert (alias.result or {}).get("coalesced_to")


@pytest.mark.django_db
def test_durable_insert_precedes_cursor_advance():
    _fixture()
    c = _cursor()
    assert c.position == {} and c.last_polled_at is None
    poller.poll_repo_page(c, [_push_obs("ACME-1")], {"refs/heads/main": "c" * 40}, timezone.now())
    c.refresh_from_db()
    assert ForgejoDelivery.objects.filter(forge="github").count() == 1
    assert c.position == {"refs/heads/main": "c" * 40} and c.last_polled_at is not None


@pytest.mark.django_db
def test_crash_between_insert_and_advance_loses_nothing():
    """The lossless pin: the cursor save fails AFTER the durable insert. The
    cursor stays put; a re-poll of the same range re-ingests idempotently and
    ends with exactly one delivery and an advanced cursor."""
    _fixture()
    c = _cursor()
    obs = [_push_obs("ACME-1")]
    # Patch the SAVE, not the manager: the manager now also serves the
    # gap pre-check, and mocking it would crash before ingest — testing a
    # different property than the one this row is named for.
    with mock.patch.object(PollCursor, "save", side_effect=RuntimeError("crash before advance")):
        with pytest.raises(RuntimeError):
            poller.poll_repo_page(c, obs, {"refs/heads/main": "c" * 40}, timezone.now())
    c.refresh_from_db()
    assert ForgejoDelivery.objects.filter(forge="github").count() == 1  # durable
    assert c.position == {} and c.last_polled_at is None  # NOT advanced
    # re-poll: idempotent ingest, cursor now advances
    poller.poll_repo_page(c, obs, {"refs/heads/main": "c" * 40}, timezone.now())
    c.refresh_from_db()
    assert ForgejoDelivery.objects.filter(forge="github").count() == 1
    assert c.position == {"refs/heads/main": "c" * 40}


@pytest.mark.django_db
def test_overlap_refetch_dedups():
    """Next poll re-fetches back past the boundary; the re-seen event dedups."""
    _fixture()
    c = _cursor()
    first = _push_obs("ACME-1", after="c" * 40)
    poller.poll_repo_page(c, [first], {"refs/heads/main": "c" * 40}, timezone.now())
    c.refresh_from_db()
    # overlap: the next page re-includes `first` plus a new push
    second = _push_obs("ACME-1", before="c" * 40, after="e" * 40)
    outcomes = poller.poll_repo_page(c, [first, second], {"refs/heads/main": "e" * 40}, timezone.now())
    assert outcomes == ["duplicate", "inserted"]
    assert ForgejoDelivery.objects.filter(forge="github").count() == 2


@pytest.mark.django_db
def test_gap_fails_closed_and_stops_advancing():
    _fixture()
    c = _cursor()
    before = dict(c.position)
    poller.mark_gap(c, "newest commit predates the cursor boundary")
    c.refresh_from_db()
    assert c.gap_detected is True
    assert "Operator recovery" in c.last_error

    # NOT ADVANCED means the BOUNDARY did not move. This used to assert
    # `last_polled_at is None`, using "never polled" as a proxy for "not
    # advanced" — and those are different facts. Marking a gap IS a tick: we
    # looked, and what we found was a gap. `record_tick` always stamps
    # `last_polled_at` because it is the only field separating "we tried and
    # could not" from "nothing has ever run", and blurring those costs an
    # operator the first hour of any investigation.
    #
    # Flagging this as an edited test rather than burying it: I changed an
    # existing assertion to make my own change pass, which is exactly what a
    # reviewer should distrust. The claim is that the OLD assertion was measuring
    # the wrong thing, and the replacement measures what the test name says.
    assert c.position == before, "the boundary moved while marking a gap"


@pytest.mark.django_db
def test_polled_delivery_processes_and_records_its_refusal():
    """End-to-end: a polled push, once processed by the shared path, reaches
    exactly the same outcome a webhook would.

    BIP-67 conversion: the outcome is now a recorded refusal rather than a
    move, because the write boundary refuses every push. The CLAIM of this test
    is unchanged and is not about the transition — it is that the poller does
    not have a processing path of its own. So it asserts the shared path's
    current outcome, whatever that outcome is, on a POLLED row: the ticket
    stands still and the refusal is durable on the delivery."""
    u, ws, proj, issue, seqs = _fixture()
    poller.ingest_observation(_push_obs(f"ACME-{issue.sequence_id}"))
    d = ForgejoDelivery.objects.get(forge="github")
    with override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps(_scope_map(proj))):
        lease = claim_delivery(d)
        assert lease is not None
        result = process_delivery(d, lease)
    issue.refresh_from_db()
    assert issue.state.name == "Todo"
    refusals = ((result or {}).get("ignored") or {}).get("unverified") or []
    assert [e["ticket"] for e in refusals] == [f"ACME-{issue.sequence_id}"]
    assert result.get("moved") == []



# --- the three defects this takeover closes -------------------------------
# Ported from 7of9's PR #42 and re-fitted to the contract merged since (#65,
# ADR 010). Each row below is a defect that was live at 19037ea3.


@pytest.mark.django_db
def test_poll_and_webhook_of_one_event_produce_THE_SAME_key():
    """The whole promise of the transport, and it was broken by the re-fit gap.

    The poller keyed on FORGE_NAME ("github"); the merged webhook path keys on
    instance_config.resolve(forge). Same real event, two different hashes, two
    rows, two executions — the opposite of cross-transport collapse.
    """
    from plane.bridge import semantic_key as skey

    obs = _push_obs("ACME-1")
    poller.ingest_observation(obs)
    stored = ForgejoDelivery.objects.get(forge="github")

    webhook_key = skey.push_key(
        INSTANCE, REPO_ID, obs["ref"], obs["before"], obs["after"]
    )
    assert stored.semantic_key == webhook_key
    assert stored.semantic_key_hash == skey.key_hash(webhook_key)
    # and the namespace is the INSTANCE, never the family
    assert INSTANCE in stored.semantic_key
    assert not stored.semantic_key.startswith("github\x1f")


@pytest.mark.django_db
def test_ingest_refuses_when_the_instance_is_not_configured():
    """Unresolvable config must not ingest under a namespace nothing matches."""
    from plane.bridge import instance_config

    with override_settings(GITHUB_INSTANCE_ID=""):
        with pytest.raises(instance_config.InstanceConfigError):
            poller.ingest_observation(_push_obs("ACME-1"))
    assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
def test_mark_gap_then_poll_cannot_move_position():
    """Rowan's named regression: the STOP condition must be enforced, not set.

    Previously mark_gap wrote gap_detected and poll_repo_page never read it, so
    any ordinary caller ingested and advanced straight past unread history.
    """
    c = _cursor()
    poller.poll_repo_page(c, [_push_obs("ACME-1")], {"refs/heads/main": "c" * 40}, timezone.now())
    c.refresh_from_db()
    boundary = c.position

    poller.mark_gap(c, "newest commit predates the cursor boundary")
    c.refresh_from_db()
    assert c.gap_detected is True

    before = ForgejoDelivery.objects.count()
    # A GENUINELY NEW event: _push_obs varies only the commit message, so two
    # calls share ref/before/after and therefore one semantic key — the second
    # would dedup to "duplicate" and insert nothing, and the no-ingest
    # assertion below could never fire. The distinct `after` is load-bearing.
    fresh = _push_obs("ACME-2", before="c" * 40, after="d" * 40)
    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(c, [fresh], {"refs/heads/main": "z" * 40}, timezone.now())

    c.refresh_from_db()
    assert c.position == boundary, "a gapped cursor advanced past unread history"
    # Pins the PRE-check specifically, not just the advance guard. There are two
    # gap checks — one before ingest, one inside the advancing transaction — and
    # without this line either could be deleted with the suite still green
    # (found by mutating the pre-check away and watching this test pass).
    assert ForgejoDelivery.objects.count() == before, "a gapped cursor ingested work"


@pytest.mark.django_db
def test_gap_marked_by_another_worker_mid_page_still_blocks_the_advance():
    """The caller's object can predate another worker's mark_gap, so the
    advancing lock re-checks. Ingest is idempotent and stands; the BOUNDARY
    must not move."""
    c = _cursor()
    poller.poll_repo_page(c, [_push_obs("ACME-1")], {"refs/heads/main": "c" * 40}, timezone.now())
    c.refresh_from_db()
    boundary = c.position

    stale = PollCursor.objects.get(pk=c.pk)          # caller's object, not gapped
    PollCursor.objects.filter(pk=c.pk).update(gap_detected=True)   # another worker

    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(stale, [_push_obs("ACME-3")], {"refs/heads/main": "y" * 40}, timezone.now())
    c.refresh_from_db()
    assert c.position == boundary


@pytest.mark.django_db
def test_cursor_identity_is_instance_and_stable_id_not_the_display_path():
    """ADR 010 §1: display paths are never identity.

    A rename must not create a second cursor, and path reuse must not inherit a
    prior repository's boundary. The old key was (forge, repo_full_name) with a
    nullable stable id, which admitted both.
    """
    from django.db import IntegrityError as IE, transaction as tx

    c = _cursor()
    # rename: same instance + stable id, different display path -> SAME cursor.
    # The violating insert gets its own atomic block: an IntegrityError poisons
    # the enclosing transaction and every later query in this test would fail.
    with pytest.raises(IE), tx.atomic():
        PollCursor.objects.create(
            provider_instance=INSTANCE, repo_stable_id=REPO_ID,
            forge="github", repo_full_name="acme/app-renamed",
        )
    # path reuse: same display path, different repository -> a DIFFERENT cursor
    other = _cursor(repo_id=REPO_ID + 1, full_name=REPO)
    assert other.pk != c.pk
    # and two instances numbering the same repo do not collide
    third = _cursor(instance="github.com/other", repo_id=REPO_ID)
    assert third.pk != c.pk


@pytest.mark.django_db
def test_poll_refuses_a_cursor_whose_instance_no_longer_matches_config():
    """A cursor left behind by a renamed instance must fail closed rather than
    ingest under a namespace nothing else will match."""
    c = _cursor(instance="github.com/was-renamed")
    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(c, [_push_obs("ACME-1")], {"refs/heads/main": "c" * 40}, timezone.now())
    assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
def test_a_page_from_another_repository_is_refused_before_any_ingest():
    """Rowan RC 3532: nothing bound the page to the cursor.

    A page from repo 2 handed to repo 1's cursor ingested repo 2's deliveries
    and advanced repo 1's boundary. Both halves lose data: repo 1 moves past
    history it never read, repo 2's events land under a cursor that does not
    own them.
    """
    c = _cursor(repo_id=1, full_name="acme/one")
    foreign = dict(_push_obs("ACME-1"), repo_full_name="acme/two", repo_id=2)

    before_rows = ForgejoDelivery.objects.count()
    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(c, [foreign], {"refs/heads/main": "c" * 40}, timezone.now())

    c.refresh_from_db()
    assert ForgejoDelivery.objects.count() == before_rows, "ingested a foreign repository's event"
    assert c.position == {}, "advanced a cursor past history it never read"


@pytest.mark.django_db
def test_a_mixed_page_is_refused_whole_rather_than_filtered():
    """A page carrying one owned and one foreign observation refuses ENTIRELY.

    Filtering the foreign half would hide that the caller's pagination is
    wrong, and the owned half would still advance a boundary computed from a
    page we did not fully trust.
    """
    c = _cursor(repo_id=REPO_ID)
    owned = _push_obs("ACME-1")
    foreign = dict(_push_obs("ACME-2", after="d" * 40), repo_full_name="acme/two", repo_id=REPO_ID + 99)

    before_rows = ForgejoDelivery.objects.count()
    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(c, [owned, foreign], {"refs/heads/main": "d" * 40}, timezone.now())

    c.refresh_from_db()
    assert ForgejoDelivery.objects.count() == before_rows, "ingested the owned half of a bad page"
    assert c.position == {}


@pytest.mark.django_db
@pytest.mark.parametrize("bad_id", [True, 0, -1, "1", 1.0, None])
def test_an_id_that_is_not_an_identity_is_refused_by_the_page_guard(bad_id):
    """Rowan RC on PR #72: equality alone let `True` pass repository 1's guard.

    `True == 1` in Python, so a boolean repo id satisfied the ownership check,
    was then rejected as an identity downstream, returned "skipped", and the
    cursor advanced past an observation nothing stored — silence exactly where
    losslessness requires a refusal. The id must satisfy the identity rule
    itself before its value is compared. `1.0` and `"1"` also compare or coerce
    close enough to 1 to be worth pinning alongside the bool.
    """
    c = _cursor(repo_id=1, full_name="acme/one")
    malformed = dict(_push_obs("ACME-1"), repo_id=bad_id)

    before_rows = ForgejoDelivery.objects.count()
    with pytest.raises(poller.GapDetected):
        poller.poll_repo_page(c, [malformed], {"refs/heads/main": "d" * 40}, timezone.now())

    c.refresh_from_db()
    assert ForgejoDelivery.objects.count() == before_rows, "stored a delivery for a non-identity repo id"
    assert c.position == {}, "advanced the boundary past an observation it never stored"


# ---------------------------------------------------------------------------
# Morrow RC 3536's remaining bars: BOTH cross-transport directions, and the
# outer-atomic race. The webhook-then-poll direction is covered above (the
# former strict xfail). These are the two it did not reach.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_poll_first_then_signed_webhook_leaves_both_observations_queryable(client):
    """The OTHER direction, entered through the SAME real boundary (Morrow
    RC 3572).

    Coverage of one direction is not coverage of the pair: the poll path and
    the webhook path enter the inbox by different calls, so a defect can live
    in either ordering alone. But this test used to call
    `inbox.record_observation` DIRECTLY with a canonical key it computed
    itself — which made it a statement about the inbox, not about the webhook.
    It would have stayed green through any defect in the view: a wrong key
    derivation, a lost header, a signature check that admitted the wrong body.
    The direction it claimed to cover was the one direction it never exercised.

    So: poll first, then let the real HTTP surface, a valid HMAC and the view's
    own key derivation produce the second observation. The poll then meets
    whatever the webhook ACTUALLY stored, in both orderings, at the boundary a
    forge really reaches.
    """
    _u, _ws, proj, _issue, _seqs = _fixture()
    obs = _push_obs("ACME-1")
    payload = poller._push_payload(
        REPO, REPO_ID, obs["ref"], obs["before"], obs["after"], obs["commits"]
    )
    with override_settings(
        GITHUB_WEBHOOK_SECRET=GH_SECRET,
        GITHUB_INSTANCE_ID=INSTANCE,
        FORGEJO_BRIDGE_REPO_MAP=json.dumps(_scope_map(proj)),
    ):
        assert poller.ingest_observation(obs) == "inserted"
        poll_row = ForgejoDelivery.objects.get()
        assert poll_row.semantic_key_hash, "the poll row must hold the semantic key"

        # The SAME real event, now arriving as a signed webhook.
        response = _signed_github_post(client, "push", payload)
        assert response.status_code in (200, 202), response.content

    assert ForgejoDelivery.objects.count() == 2, "an observation was dropped"
    holders = ForgejoDelivery.objects.exclude(semantic_key_hash=None)
    assert holders.count() == 1, "exactly one row may hold the semantic key"
    assert holders.get().pk == poll_row.pk, "the first observation must stay the holder"
    alias = ForgejoDelivery.objects.get(semantic_key_hash__isnull=True)
    assert alias.semantic_key == poll_row.semantic_key, (
        "the view derived a DIFFERENT key than the poller for one event — the "
        "cross-transport collapse this transport promises does not hold"
    )
    assert (alias.result or {}).get("coalesced_to")


@pytest.mark.django_db(transaction=True)
def test_the_alias_path_survives_an_enclosing_transaction():
    """The outer-atomic race (Morrow RC 3536).

    The recovery that must survive an enclosing transaction is the ALIAS path —
    the one that raises IntegrityError on the semantic-key constraint and
    recovers. Re-polling the SAME event does not reach it: the poller's
    delivery id is synthetic and derived from the key, so a re-poll is
    idempotent by construction and returns EXISTING with one row. (My first
    version of this test asserted two rows for a re-poll and was simply wrong
    about the id.)

    So: a webhook holds the event, then a poll of it arrives INSIDE a caller's
    atomic block. The poll must become a durable alias and the transaction must
    still be usable — a savepointless recovery poisons it and raises
    TransactionManagementError instead.
    """
    _fixture()
    obs = _push_obs("ACME-1")
    canonical = poller._canonical_for(
        "push",
        poller._push_payload(REPO, REPO_ID, obs["ref"], obs["before"], obs["after"], obs["commits"]),
        INSTANCE,
    )
    from plane.bridge import semantic_key as skey

    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="github", event="push",
        payload={"x": 1}, repository=REPO, body_digest="webhook",
        semantic_key=canonical, semantic_key_hash=skey.key_hash(canonical), status="processed",
    )

    with transaction.atomic():
        assert poller.ingest_observation(obs) == "duplicate"
        # the transaction must still be USABLE — this is the assertion a
        # poisoned transaction breaks
        assert ForgejoDelivery.objects.count() == 2
    assert ForgejoDelivery.objects.count() == 2
    aliases = ForgejoDelivery.objects.filter(semantic_key_hash=None)
    assert aliases.count() == 1, "the poll observation was not stored as an alias"
    assert aliases.first().semantic_key == canonical, "alias lost its plaintext key"
    assert ForgejoDelivery.objects.count() == 2


@pytest.mark.django_db
def test_a_stale_worker_does_not_move_the_boundary_backward():
    """Morrow RC 3569. Two workers poll the same repo. Worker A reads the cursor,
    then worker B advances it to "d"; worker A finishes ingesting a page computed
    from the OLD boundary and tries to write "c".

    Before the compare-and-set, A's write landed and the boundary regressed
    d -> c. That does not delete the observations already inserted — it
    invalidates the durable boundary, so the same history is re-read repeatedly,
    a fast producer can starve the cursor, and far enough back it becomes a
    retention gap.
    """
    _fixture()
    c = _cursor()
    stale_view = PollCursor.objects.get(pk=c.pk)          # worker A's read

    poller.poll_repo_page(c, [], {"refs/heads/main": "d" * 40}, timezone.now())  # worker B
    c.refresh_from_db()
    assert c.position == {"refs/heads/main": "d" * 40}

    with pytest.raises(poller.StaleCursor):
        poller.poll_repo_page(
            stale_view, [_push_obs("ACME-1")], {"refs/heads/main": "c" * 40}, timezone.now()
        )

    c.refresh_from_db()
    assert c.position == {"refs/heads/main": "d" * 40}, "the boundary moved backward"
    # The refusal is on the BOUNDARY only: what A ingested is durably stored.
    assert ForgejoDelivery.objects.filter(forge="github").count() == 1


@pytest.mark.django_db
def test_a_stale_worker_does_not_move_the_WATERMARK_backward_at_an_equal_position():
    """Morrow RC 3630. The boundary is the PAIR, and the previous compare-and-set
    guarded only half of it.

    `watermark_at` is a durable overlap boundary in its own right — the next
    page's refetch window starts there — and this block writes it. The
    regression is reachable with NO position conflict at all: two workers read
    the same cursor at an equal position, B commits watermark T, then A commits
    T-5m. A's position check passed, because position never differed, and the
    watermark walked backward. The refetch window then widens every round and
    the same history is re-read forever.

    The position case is already pinned above. This is the case that case does
    not reach: same position, different watermark. Against a compare-and-set on
    `position` alone it goes green while the watermark regresses — which is
    exactly what makes it worth committing.
    """
    _fixture()
    now = timezone.now()
    earlier = now - timedelta(minutes=5)
    c = _cursor()
    stale_view = PollCursor.objects.get(pk=c.pk)          # worker A's read

    # Worker B advances the WATERMARK while leaving the position untouched.
    poller.poll_repo_page(c, [], stale_view.position, now)                      # worker B
    c.refresh_from_db()
    assert c.position == stale_view.position, "this case must not involve a position conflict"
    assert c.watermark_at == now

    with pytest.raises(poller.StaleCursor):
        poller.poll_repo_page(
            stale_view, [_push_obs("ACME-1")], stale_view.position, earlier    # worker A
        )

    c.refresh_from_db()
    assert c.watermark_at == now, "the watermark moved backward at an unchanged position"
    # Same contract as the position case: only the boundary is refused.
    assert ForgejoDelivery.objects.filter(forge="github").count() == 1


@pytest.mark.django_db
def test_a_delivery_id_collision_reports_the_id_rather_than_crashing():
    """Morrow RC 3559. inbox.Recorded carries delivery=None on COLLISION —
    "None only for COLLISION, where nothing of ours was written". The refusal
    path dereferenced it, so the one code path whose entire job is to report a
    collision raised AttributeError instead, and the operator got a traceback
    with no id in it."""
    _fixture()
    obs = _push_obs("ACME-1")
    assert poller.ingest_observation(obs) == "inserted"

    # Same synthetic id (same semantic key), different stored content.
    ForgejoDelivery.objects.filter(forge="github").update(body_digest="different" * 4)

    with pytest.raises(poller.GapDetected) as excinfo:
        poller.ingest_observation(obs)
    assert "poll:" in str(excinfo.value), "the collision must name the id it refused"


# ── Cross-transport collapse, entered through the SIGNED webhook boundary ────
#
# Morrow RC 3559, re-asserted on ac52c2c: this file claimed cross-transport
# collapse while every "webhook side" above enters BELOW the transport boundary
# — `ForgejoDelivery.objects.create(...)` fabricates the row the webhook would
# have written. That proves the poller dedups against a row we made ourselves,
# not against a webhook. Signature verification, header-driven forge selection,
# the view's own storage call and its response are all untested by it, and any
# of them could change the stored shape without a single test failing.
#
# These enter at the real HTTP surface with a valid HMAC, as GitHub, and let the
# view write the row. The poll then meets whatever the webhook ACTUALLY stored.
GH_SECRET = "a-github-secret-long-enough"
BRIDGE_URL = "/api/public/git-bridge/forgejo/"


def _signed_github_post(client, event, payload, delivery_id=None):
    """POST a GitHub webhook the way GitHub does: HMAC-SHA256 over the exact
    body, `sha256=` prefixed, with the delivery and event headers this forge
    declares (forges.GitHubForge.delivery_headers / event_headers)."""
    body = json.dumps(payload).encode()
    signature = hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        BRIDGE_URL,
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=f"sha256={signature}",
        HTTP_X_GITHUB_EVENT=event,
        HTTP_X_GITHUB_DELIVERY=delivery_id or str(uuid_lib.uuid4()),
    )


@pytest.mark.django_db
def test_a_signed_webhook_and_a_poll_of_one_event_collapse_to_one_execution(client):
    """The claim this file exists to make, proved through the real boundary."""
    _u, _ws, proj, _issue, _seqs = _fixture()
    obs = _push_obs("ACME-1")
    payload = poller._push_payload(
        REPO, REPO_ID, obs["ref"], obs["before"], obs["after"], obs["commits"]
    )
    with override_settings(
        GITHUB_WEBHOOK_SECRET=GH_SECRET,
        GITHUB_INSTANCE_ID=INSTANCE,
        FORGEJO_BRIDGE_REPO_MAP=json.dumps(_scope_map(proj)),
    ):
        response = _signed_github_post(client, "push", payload)
        assert response.status_code in (200, 202), response.content
        assert ForgejoDelivery.objects.count() == 1, "the webhook stored nothing"
        webhook_row = ForgejoDelivery.objects.get()
        assert webhook_row.semantic_key_hash, "the webhook row carries no semantic key"

        # The SAME real event, now seen by the poller.
        assert poller.ingest_observation(obs) == "duplicate"

    # ADR 010 §3: both observations stored; coalescing applies to EXECUTION.
    assert ForgejoDelivery.objects.count() == 2
    alias = ForgejoDelivery.objects.get(semantic_key_hash__isnull=True)
    assert alias.semantic_key == webhook_row.semantic_key
    assert (alias.result or {}).get("coalesced_to")


@pytest.mark.django_db
def test_an_unsigned_post_stores_nothing_for_a_poll_to_collapse_against(client):
    """The negative control the positive test needs. Without this, a boundary
    that accepted anything would still pass the test above — the poll would
    simply meet a row that should never have existed."""
    _u, _ws, proj, _issue, _seqs = _fixture()
    payload = poller._push_payload(REPO, REPO_ID, "refs/heads/main", "a" * 40, "b" * 40, [])
    with override_settings(
        GITHUB_WEBHOOK_SECRET=GH_SECRET,
        GITHUB_INSTANCE_ID=INSTANCE,
        FORGEJO_BRIDGE_REPO_MAP=json.dumps(_scope_map(proj)),
    ):
        body = json.dumps(payload).encode()
        response = client.post(
            BRIDGE_URL, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=" + "0" * 64,
            HTTP_X_GITHUB_EVENT="push",
            HTTP_X_GITHUB_DELIVERY=str(uuid_lib.uuid4()),
        )
    assert response.status_code == 403
    assert ForgejoDelivery.objects.count() == 0
