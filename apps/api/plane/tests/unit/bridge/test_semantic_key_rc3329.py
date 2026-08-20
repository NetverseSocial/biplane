# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-46 PR-B1 — the RC 3329 violating-direction regressions.

Each test is one of Morrow's concrete witnesses: delivery-id binding wins over
semantic coalescing (b1); the key is injective and per-forge (b2); and the
0128 migration is safe over a legal pre-migration state with two delivery ids
for one event (b3)."""

from unittest import mock
import hashlib
import hmac
import json
import threading
import uuid as uuid_lib

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings

from plane.bridge import forges, semantic_key as skey
from plane.bridge.forgejo_bridge import _semantic_key
from plane.db.models import ForgejoDelivery, Issue, Project, State, User, Workspace

URL = "/api/public/git-bridge/forgejo/"
SECRET = "test-bridge-secret"
REPO = "acme/x"
REPO_ID = 4242


@pytest.fixture(autouse=True)
def _bridge_secret(settings):
    settings.FORGEJO_WEBHOOK_SECRET = SECRET
    # provider-instance ids (ADR 010 §1). Using the family name as the id
    # reproduces the pre-instance keys, so value assertions hold; 4d proves
    # distinct instances differ.
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    settings.GITHUB_INSTANCE_ID = "github"
    settings.GITLAB_INSTANCE_ID = "gitlab"


def _fixture():
    u = User.objects.create(email=f"gb-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"g{uuid_lib.uuid4().hex[:10]}", name="G", owner=u)
    proj = Project.objects.create(workspace=ws, name="P", identifier="GB")
    seqs = {}
    for i, (name, group) in enumerate(
        [("Backlog", "backlog"), ("Todo", "unstarted"), ("Review", "started"), ("Done", "completed")]
    ):
        seqs[name] = State.objects.create(
            name=name, project=proj, workspace=ws, group=group, sequence=(i + 1) * 100,
            color="#000", default=(name == "Backlog"), created_by=u,
        )
    i1 = Issue.objects.create(workspace=ws, project=proj, name="t1", state=seqs["Todo"])
    i2 = Issue.objects.create(workspace=ws, project=proj, name="t2", state=seqs["Todo"])
    return u, ws, proj, i1, i2, seqs


def _scoped(ws):
    # BIP-38: values are explicit project-UUID lists, never workspace slugs.
    from plane.db.models import Project

    ids = [str(pid) for pid in Project.objects.filter(workspace=ws).values_list("id", flat=True)]
    return override_settings(FORGEJO_BRIDGE_REPO_MAP=json.dumps({REPO: ids}))


def _push(seq, before="b" * 40, after="c" * 40, ref="refs/heads/main"):
    return {
        "repository": {"full_name": REPO, "id": REPO_ID},
        "ref": ref, "before": before, "after": after,
        "commits": [{"id": after, "message": f"refs GB-{seq}"}],
    }


def _post(payload, delivery_id):
    body = json.dumps(payload).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return Client().post(
        URL, data=body, content_type="application/json",
        HTTP_X_FORGEJO_EVENT="push", HTTP_X_FORGEJO_SIGNATURE=sig, HTTP_X_FORGEJO_DELIVERY=delivery_id,
    )


# ---------------------------------------------------- b1: delivery-id binding
@pytest.mark.django_db
def test_delivery_id_binding_wins_over_semantic_coalesce():
    """D1<-E1 and D2<-E2 both processed; submit D1 with E2's signed body.
    Must be 409 (D1 is bound to E1), NOT a semantic 200 against D2."""
    u, ws, proj, i1, i2, seqs = _fixture()
    e1 = _push(i1.sequence_id, after="1" * 40)
    e2 = _push(i2.sequence_id, after="2" * 40)
    with _scoped(ws):
        d1 = str(uuid_lib.uuid4()); d2 = str(uuid_lib.uuid4())
        assert _post(e1, d1).status_code == 200
        assert _post(e2, d2).status_code == 200
        r3 = _post(e2, d1)  # reused id d1, different (E2) content
    assert r3.status_code == 409, f"expected 409, got {r3.status_code}"


# --------------------------------------------------------- b2: injective key
def test_missing_and_empty_components_raise_and_yield_no_key():
    with pytest.raises(skey.IncompleteEvent):
        skey.push_key("forgejo", 42, "refs/heads/main", "", "c" * 40)   # empty before
    with pytest.raises(skey.IncompleteEvent):
        skey.push_key("forgejo", 42, None, "b" * 40, "c" * 40)          # missing ref
    incomplete = {"repository": {"full_name": REPO, "id": REPO_ID},
                  "ref": "", "before": "b" * 40, "after": "c" * 40, "commits": []}
    assert _semantic_key("push", incomplete, forges.ForgejoForge) is None  # falls back to delivery_id


def test_empty_is_not_missing_is_not_present():
    # three distinct anchor states must never collapse to one key.
    present = skey.push_key("forgejo", 42, "refs/heads/main", "b" * 40, "c" * 40)
    with pytest.raises(skey.IncompleteEvent):
        skey.push_key("forgejo", 42, "refs/heads/main", "", "c" * 40)
    with pytest.raises(skey.IncompleteEvent):
        skey.push_key("forgejo", 42, "refs/heads/main", None, "c" * 40)
    assert present  # only the fully-present tuple yields a key


def _gitlab_merge(iid, merge_sha):
    return {
        "object_attributes": {"action": "merge", "iid": iid, "merge_commit_sha": merge_sha,
                              "title": "t", "description": ""},
        "project": {"path_with_namespace": "grp/app", "id": 555},
    }


def test_gitlab_merge_reads_object_attributes_not_pull_request():
    # GitLab has no pull_request family; the key must come from object_attributes.
    k = _semantic_key("pull_request", _gitlab_merge(5, "a" * 40), forges.GitLabForge)
    assert k is not None and k.startswith("merged_pr")


def test_two_distinct_gitlab_merges_do_not_alias():
    k1 = _semantic_key("pull_request", _gitlab_merge(5, "a" * 40), forges.GitLabForge)
    k2 = _semantic_key("pull_request", _gitlab_merge(6, "b" * 40), forges.GitLabForge)
    assert k1 and k2 and k1 != k2


# --------------------------------------------------- b3: migration is safe
def _load_migration():
    import importlib
    return importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")


@pytest.mark.django_db
def test_migration_backfill_consolidates_two_ids_one_event():
    """The exact legal pre-0128 state — two delivery ids, ONE real event — run
    through the migration's OWN frozen _backfill. It must NOT leave two rows
    sharing a non-null hash (which is what would make AddConstraint fail): both
    rows retained, exactly one holds the hash, both keep the plaintext key.

    (This invokes the migration's frozen functions directly against the real
    model. The literal MigrationExecutor replay EXISTS —
    test_migration_0128_replay.py, via django-test-migrations, run explicitly
    with `-m migration_replay --migrations` (BIP-47) — but that suite is
    deselected from the default --nomigrations run. This direct call is the
    version of the check that runs on every default invocation, pinning the
    frozen function on the exact state Morrow named.)"""
    from django.apps import apps as global_apps

    mig = _load_migration()
    payload = {"repository": {"full_name": REPO, "id": REPO_ID}, "ref": "refs/heads/main",
               "before": "b" * 40, "after": "c" * 40, "commits": []}
    # two delivery ids, same event; both start unkeyed (a direct ORM create does
    # not compute the key), so the partial unique constraint permits both.
    ForgejoDelivery.objects.create(delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
                                   payload=payload, repository=REPO, body_digest="same", status="processed")
    ForgejoDelivery.objects.create(delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
                                   payload=payload, repository=REPO, body_digest="same", status="processed")

    mig._backfill(global_apps, None)

    rows = list(ForgejoDelivery.objects.all())
    assert len(rows) == 2, "both rows survive (audit history retained)"
    hashes = [r.semantic_key_hash for r in rows]
    assert hashes.count(None) == 1 and sum(h is not None for h in hashes) == 1, "exactly one hash holder"
    assert all(r.semantic_key for r in rows), "both retain the plaintext key"
    # the invariant AddConstraint needs: no two rows share a non-null hash.
    non_null = [h for h in hashes if h is not None]
    assert len(non_null) == len(set(non_null))


@pytest.mark.django_db
def test_migration_backfill_frozen_key_matches_runtime_key():
    """The migration's INLINE frozen key must equal the runtime constructor for
    the same event, or a fresh install would dedup differently than a live one."""
    mig = _load_migration()
    frozen = mig._canonical("forgejo", "push",
                            {"repository": {"id": REPO_ID}, "ref": "refs/heads/main",
                             "before": "b" * 40, "after": "c" * 40})
    runtime = skey.push_key("forgejo", REPO_ID, "refs/heads/main", "b" * 40, "c" * 40)
    assert frozen == runtime and frozen is not None

# ------------------------------------------ b2b: separator -> clean 4xx (RC 3335)
@pytest.mark.django_db
def test_separator_in_component_is_clean_400_not_500():
    """A reserved separator (0x1f) in a signed push component is malformed
    input; the endpoint must fail CLOSED with a clean 400, never a 500."""
    u, ws, proj, i1, i2, seqs = _fixture()
    payload = _push(i1.sequence_id)
    payload["ref"] = "refs/heads/ma\x1fin"  # 0x1f in a component
    with _scoped(ws):
        r = _post(payload, str(uuid_lib.uuid4()))
    assert r.status_code == 400, f"expected clean 400, got {r.status_code}"


# ------------------------------------------ b3b: typed primitive boundary (RC 3343)
def test_component_rejects_non_primitives():
    for bad in ({"a": 1}, ["x"], True, 3.14):
        with pytest.raises(ValueError):
            skey.push_key("forgejo", 42, bad, "b" * 40, "c" * 40)


@pytest.mark.django_db
def test_signed_ref_object_is_400_not_a_key():
    """A signed push whose ref is an OBJECT is malformed — 400 at the typed
    boundary, never stringified into a semantic key (Morrow RC 3343)."""
    u, ws, proj, i1, i2, seqs = _fixture()
    payload = _push(i1.sequence_id)
    payload["ref"] = {"nested": "object"}
    with _scoped(ws):
        r = _post(payload, str(uuid_lib.uuid4()))
    assert r.status_code == 400
    assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
def test_second_observation_is_durably_stored_not_erased():
    """Morrow RC 3343 b1: two delivery ids for one event -> BOTH rows remain
    queryable (audit); execution coalesces to one.

    BIP-67 conversion. The coalescing claim was witnessed by counting activity
    rows, and under the write boundary a push writes none — so the countable
    outcome is the recorded REFUSAL, which is the execution itself rather than
    a side effect of it. The audit half is untouched.

    This file was in neither converter's assigned list; it surfaced only from a
    full-suite run and is noted rather than quietly absorbed."""
    u, ws, proj, i1, i2, seqs = _fixture()
    e = _push(i1.sequence_id)
    d1 = str(uuid_lib.uuid4()); d2 = str(uuid_lib.uuid4())
    with _scoped(ws):
        assert _post(e, d1).status_code == 200
        r2 = _post(e, d2)
    assert r2.status_code == 200 and r2.json().get("coalesced_to") == d1
    assert set(ForgejoDelivery.objects.values_list("delivery_id", flat=True)) == {d1, d2}
    holders = ForgejoDelivery.objects.exclude(semantic_key_hash=None)
    assert holders.count() == 1 and holders.first().delivery_id == d1
    from plane.db.models import IssueActivity
    refusals = ((holders.first().result or {}).get("ignored") or {}).get("unverified") or []
    assert len(refusals) == 1, "one real event, executed once"
    assert IssueActivity.objects.filter(issue=i1, comment__contains="git bridge").count() == 0


# ================= RC 3348 =================
from plane.db.models import ForgejoDelivery as _FD


def test_constructors_are_field_typed():
    with pytest.raises(ValueError):
        skey.push_key("forgejo", 42, 7, 8, 9)           # int ref/anchors
    with pytest.raises(ValueError):
        skey.push_key("forgejo", "42", "r", "b", "c")   # str repo_id
    with pytest.raises(ValueError):
        skey.push_key("forgejo", 0, "r", "b", "c")      # non-positive repo_id
    with pytest.raises(ValueError):
        skey.merged_pr_key("forgejo", 42, "7", "s")     # str pr_number
    with pytest.raises(ValueError):
        skey.merged_pr_key("forgejo", 42, 7, 8)         # int merge_sha
    with pytest.raises(ValueError):
        skey.review_key("forgejo", 42, 7, "9")          # str review_id
    with pytest.raises(ValueError):
        skey.review_key("forgejo", 42, 7, True)         # bool review_id
    with pytest.raises(ValueError):
        skey.review_key("forgejo", 42, 7, 0)            # non-positive review_id
    assert skey.push_key("forgejo", 42, "r", "b", "c")
    assert skey.merged_pr_key("forgejo", 42, 7, "s")
    assert skey.review_key("forgejo", 42, 7, 9)
    # a review and a merge of the same PR are DISTINCT events (verb differs)
    assert skey.review_key("forgejo", 42, 7, 9) != skey.merged_pr_key("forgejo", 42, 7, "9")


def test_runtime_and_migration_both_refuse_object_valued_rows():
    import importlib
    mig = importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")
    good = {"repository": {"id": REPO_ID, "full_name": REPO}, "ref": "refs/heads/main",
            "before": "b" * 40, "after": "c" * 40}
    assert mig._canonical("forgejo", "push", good) == _semantic_key("push", good, forges.ForgejoForge)
    obj_ref = dict(good, ref={"nested": 1})
    assert mig._canonical("forgejo", "push", obj_ref) is None          # migration: unkeyed
    with pytest.raises(ValueError):
        skey.push_key("forgejo", REPO_ID, {"nested": 1}, "b" * 40, "c" * 40)  # runtime: refuses
    plm = {"action": "closed", "repository": {"id": REPO_ID, "full_name": REPO},
           "pull_request": {"merged": True, "number": 5, "merge_commit_sha": {"x": 1}}}
    assert mig._canonical("forgejo", "pull_request", plm) is None


@pytest.mark.django_db
def test_pending_holder_alias_retry_returns_real_result_when_holder_completes():
    u, ws, proj, i1, i2, seqs = _fixture()
    e = _push(i1.sequence_id)
    canonical = _semantic_key("push", e, forges.ForgejoForge)
    h = skey.key_hash(canonical)
    holder = _FD.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push", payload=e,
        repository=REPO, body_digest="dh", semantic_key=canonical, semantic_key_hash=h, status="pending")
    dalias = str(uuid_lib.uuid4())
    with _scoped(ws):
        r = _post(e, dalias)  # holder pending -> alias pending, 202
        assert r.status_code == 202 and r.json().get("coalesced_to") == holder.delivery_id
        a = _FD.objects.get(delivery_id=dalias)
        assert a.status == "pending" and a.semantic_key_hash is None
        holder.status = "processed"; holder.result = {"moved": ["GB-1"]}; holder.save()
        r2 = _post(e, dalias)  # retry after holder completes -> REAL result
    assert r2.status_code == 200 and r2.json().get("moved") == ["GB-1"]
    a.refresh_from_db()
    assert a.status == "processed" and a.result.get("moved") == ["GB-1"] and a.result.get("coalesced_to") == holder.delivery_id


@pytest.mark.django_db
def test_missing_holder_stays_retryable_never_processed():
    u, ws, proj, i1, i2, seqs = _fixture()
    e = _push(i1.sequence_id)
    canonical = _semantic_key("push", e, forges.ForgejoForge)
    h = skey.key_hash(canonical)
    holder = _FD.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push", payload=e,
        repository=REPO, body_digest="dh", semantic_key=canonical, semantic_key_hash=h, status="pending")
    dalias = str(uuid_lib.uuid4())
    with _scoped(ws):
        assert _post(e, dalias).status_code == 202
    with mock.patch("plane.db.mixins.soft_delete_related_objects.delay"):
        holder.delete()  # the holder vanishes (rare race); the soft-delete publish is incidental (BIP-63)
    with _scoped(ws):
        r = _post(e, dalias)
    assert r.status_code == 202  # retryable, NOT processed
    a = _FD.objects.get(delivery_id=dalias)
    assert a.status != "processed"


@pytest.mark.django_db(transaction=True)
def test_alias_binding_race_loser_gets_409():
    """Morrow RC 3348 b2: two concurrent posts of the SAME fresh delivery id D
    with different (E1/E2) signed bodies, each already held by a processed
    holder. Both lose their semantic unique races; exactly one BINDS D and
    coalesces to its holder (200 duplicate), the other finds D already bound to
    the other event content and MUST 409 — never masquerade as coalesced.

    The witness itself is made sound (Morrow RC 3348 b2): Future.result() joins
    each worker AND re-raises any exception it hit, so a crashed or still-live
    worker FAILS the test here instead of leaving a silent hole; and the
    assertion pins the full outcome MULTISET [200, 409], not mere 409
    membership (which a double-409 or a swallowed-exception worker satisfies)."""
    from concurrent.futures import ThreadPoolExecutor
    from django.db import connection
    u, ws, proj, i1, i2, seqs = _fixture()
    e1 = _push(i1.sequence_id, after="1" * 40)
    e2 = _push(i2.sequence_id, after="2" * 40)
    # pre-store processed holders for both events
    for e in (e1, e2):
        c = _semantic_key("push", e, forges.ForgejoForge)
        _FD.objects.create(delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
            payload=e, repository=REPO, body_digest="H", semantic_key=c, semantic_key_hash=skey.key_hash(c),
            status="processed", result={"moved": []})
    D = str(uuid_lib.uuid4())
    barrier = threading.Barrier(2)

    def fire(payload):
        try:
            barrier.wait(timeout=10)
            with _scoped(ws):
                return _post(payload, D).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fire, e1), pool.submit(fire, e2)]
        # .result(timeout) blocks until each worker finishes (join) and RE-RAISES
        # any exception it hit — a hung worker raises TimeoutError, a crashed one
        # re-raises its exception; either way the test fails rather than passing
        # on a partial result set.
        codes = sorted(f.result(timeout=30) for f in futures)

    # exact outcome multiset: one winner binds D and coalesces to its processed
    # holder (200 duplicate); one loser hits the delivery-id binding 409.
    assert codes == [200, 409], f"expected one 200 (coalesced winner) + one 409 (binding loser), got {codes}"
    # D exists exactly once, bound to ONE event content
    assert _FD.objects.filter(delivery_id=D).count() == 1


# ================= ADR 010 reconciliation: provider_instance + acceptance 4d/4e/5 =================
def test_provider_instance_scopes_the_key_4d():
    """ADR 010 acceptance 4d: two provider INSTANCES numbering the same repo
    produce distinct keys (no cross-instance collision); one instance produces
    the same key deterministically ('restart' = same config, no clock/random)."""
    a = skey.push_key("inst-a", 42, "refs/heads/main", "b" * 40, "c" * 40)
    b = skey.push_key("inst-b", 42, "refs/heads/main", "b" * 40, "c" * 40)
    assert a != b and skey.key_hash(a) != skey.key_hash(b), "two instances must not collide"
    again = skey.push_key("inst-a", 42, "refs/heads/main", "b" * 40, "c" * 40)
    assert a == again and skey.key_hash(a) == skey.key_hash(again), "same instance -> same key across restart"


def test_runtime_key_scoped_by_configured_instance_not_family_4d(settings):
    """The runtime reads the instance from CONFIG, not forge.name — flipping the
    configured id changes the key for the identical event."""
    payload = {"repository": {"id": 42, "full_name": REPO}, "ref": "refs/heads/main",
               "before": "b" * 40, "after": "c" * 40}
    settings.FORGEJO_INSTANCE_ID = "inst-a"
    ka = _semantic_key("push", payload, forges.ForgejoForge)
    settings.FORGEJO_INSTANCE_ID = "inst-b"
    kb = _semantic_key("push", payload, forges.ForgejoForge)
    assert ka and kb and ka != kb, "runtime key must be instance-scoped, not family-scoped"


@pytest.mark.django_db
def test_empty_instance_config_refuses_before_parsing_zero_writes_4e(settings):
    """ADR 010 acceptance 4e: an empty configured provider_instance is a
    DEPLOYMENT DEFECT, not an incomplete event. BOTH directions: the endpoint
    refuses BEFORE parsing (an unparseable-but-signed body still gets the config
    refusal, not a 400 parse error) and writes NO delivery row."""
    u, ws, proj, i1, i2, seqs = _fixture()
    settings.FORGEJO_INSTANCE_ID = ""  # deployment defect

    def _post_raw(body, delivery_id):
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return Client().post(URL, data=body, content_type="application/json",
            HTTP_X_FORGEJO_EVENT="push", HTTP_X_FORGEJO_SIGNATURE=sig, HTTP_X_FORGEJO_DELIVERY=delivery_id)

    with _scoped(ws):
        # a body that would ALSO fail JSON parsing: a parse-first impl returns
        # 400, so a 500 here proves the config check runs BEFORE the parser.
        r = _post_raw(b"{ this is not valid json", str(uuid_lib.uuid4()))
    assert r.status_code == 500, f"empty instance id must refuse before parsing (500), got {r.status_code}"
    assert ForgejoDelivery.objects.count() == 0, "no delivery row may be written on a config refusal"


@pytest.mark.django_db(transaction=True)
def test_binding_recovery_savepoint_holds_under_atomic_requests_5():
    """ADR 010 §6 / acceptance 5: with ATOMIC_REQUESTS on, the whole view runs
    inside a transaction. The binding-recovery catches an IntegrityError; WITHOUT
    the explicit savepoint that catch poisons the request transaction and the
    coalesce fails 500. Prove it returns the coalesced 200 and the transaction
    survives (both rows queryable)."""
    from django.db import connections
    u, ws, proj, i1, i2, seqs = _fixture()
    e = _push(i1.sequence_id)
    d1, d2 = str(uuid_lib.uuid4()), str(uuid_lib.uuid4())
    conn = connections["default"]
    prior = conn.settings_dict.get("ATOMIC_REQUESTS", False)
    conn.settings_dict["ATOMIC_REQUESTS"] = True
    try:
        with _scoped(ws):
            assert _post(e, d1).status_code == 200         # holder created + processed
            r2 = _post(e, d2)                              # NEW id, same event -> hash IntegrityError -> savepoint recovery
    finally:
        conn.settings_dict["ATOMIC_REQUESTS"] = prior
    assert r2.status_code == 200, f"coalesce must survive ATOMIC_REQUESTS via the savepoint, got {r2.status_code}"
    assert r2.json().get("coalesced_to") == d1
    assert set(ForgejoDelivery.objects.values_list("delivery_id", flat=True)) == {d1, d2}


@pytest.mark.django_db
def test_alias_stored_shape_is_field_by_field():
    """ADR 010 §4: the runtime-created coalesced alias has a specific stored
    shape — hash NULL, plaintext key retained, result.coalesced_to = holder's
    delivery_id, status pending/processed. post() and the migration must write
    the IDENTICAL shape; this pins the runtime side field-by-field."""
    u, ws, proj, i1, i2, seqs = _fixture()
    e = _push(i1.sequence_id)
    d1, d2 = str(uuid_lib.uuid4()), str(uuid_lib.uuid4())
    with _scoped(ws):
        assert _post(e, d1).status_code == 200
        assert _post(e, d2).status_code == 200
    holder = ForgejoDelivery.objects.get(delivery_id=d1)
    alias = ForgejoDelivery.objects.get(delivery_id=d2)
    assert holder.semantic_key_hash is not None, "holder owns the hash"
    assert alias.semantic_key_hash is None, "alias hash is NULL"
    assert alias.semantic_key == holder.semantic_key, "alias retains the plaintext key"
    assert (alias.result or {}).get("coalesced_to") == d1, "coalesced_to -> holder delivery_id"
    assert alias.status in ("pending", "processed"), "alias pending or resolved-processed"


@pytest.mark.django_db
def test_static_check_e001_missing_is_fail_closed(settings):
    """E001 (static, consumed from the owner): an enabled forge with no instance
    id is a fail-closed Error; silent for a DISABLED forge."""
    from django.core.checks import Error
    from plane.bridge.checks import check_provider_instance_config as chk
    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "fj"
    settings.GITHUB_WEBHOOK_SECRET = None; settings.GITLAB_WEBHOOK_TOKEN = None
    assert chk(None) == []
    settings.FORGEJO_INSTANCE_ID = ""
    es = chk(None)
    assert len(es) == 1 and isinstance(es[0], Error) and es[0].id == "bridge.E001" and "FORGEJO_INSTANCE_ID" in es[0].msg
    settings.FORGEJO_WEBHOOK_SECRET = None
    assert chk(None) == []


@pytest.mark.django_db
def test_static_check_e004_separator_is_malformed(settings):
    """E004: a configured id carrying the reserved separator is malformed config
    (ADR 010 §1a) — the SAME predicate the runtime and migration use, in ONE owner."""
    from plane.bridge.checks import check_provider_instance_config as chk
    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "a\x1fb"
    settings.GITHUB_WEBHOOK_SECRET = None; settings.GITLAB_WEBHOOK_TOKEN = None
    es = chk(None)
    assert len(es) == 1 and es[0].id == "bridge.E004" and "separator" in es[0].msg


@pytest.mark.django_db
def test_static_check_e002_shared_id_collides(settings):
    """E002: two enabled forges with the same instance id collide."""
    from plane.bridge.checks import check_provider_instance_config as chk
    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "shared"
    settings.GITHUB_WEBHOOK_SECRET = "s2"; settings.GITHUB_INSTANCE_ID = "shared"
    settings.GITLAB_WEBHOOK_TOKEN = None
    assert [p for p in chk(None) if p.id == "bridge.E002"]
    settings.GITHUB_INSTANCE_ID = "gh"
    assert [p for p in chk(None) if p.id == "bridge.E002"] == []


@pytest.mark.django_db
def test_stability_check_e003_rename_after_rows(settings):
    """E003 (stability, a SEPARATE Tags.database check): a rename after rows exist
    is refused; not run when the DB is out of scope."""
    from plane.bridge.checks import check_provider_instance_stability as chk
    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "old-id"
    settings.GITHUB_WEBHOOK_SECRET = None; settings.GITLAB_WEBHOOK_TOKEN = None
    key = skey.push_key("old-id", REPO_ID, "refs/heads/main", "b" * 40, "c" * 40)
    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload={"repository": {"id": REPO_ID}}, repository=REPO, body_digest="d",
        semantic_key=key, semantic_key_hash=skey.key_hash(key), status="processed")
    assert chk(None, databases=["default"]) == []
    settings.FORGEJO_INSTANCE_ID = "new-id"
    es = chk(None, databases=["default"])
    assert len(es) == 1 and es[0].id == "bridge.E003" and "old-id" in es[0].msg and "new-id" in es[0].msg
    assert chk(None, databases=None) == []  # DB out of scope -> not run


@pytest.mark.django_db
def test_stability_check_does_not_swallow_a_real_error(settings, monkeypatch):
    """Forced-error CONTROL: the stability check must NOT swallow a real query
    error and report clean — only an absent table/column (pre-0128) is skipped."""
    from plane.bridge import checks
    from plane.db.models import ForgejoDelivery
    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "fj"
    settings.GITHUB_WEBHOOK_SECRET = None; settings.GITLAB_WEBHOOK_TOKEN = None
    def _boom(*a, **k):
        raise RuntimeError("forced query error")
    monkeypatch.setattr(ForgejoDelivery.objects, "filter", _boom)
    with pytest.raises(RuntimeError, match="forced query error"):
        checks.check_provider_instance_stability(None, databases=["default"])


@pytest.mark.django_db
def test_migration_backfill_fails_closed_without_instance(settings):
    """RC 3466 #3: the 0128 backfill REFUSES to complete when a forge has rows
    that would be semantic-keyed but no configured instance id — it must not
    silently leave real history unkeyed (the config-load refusal, 4e, one layer
    down). Exercised by calling _backfill directly on the migrated test DB (a
    raising migration through the migrator fixture leaves a half-applied schema
    its teardown cannot clean — the raise itself is what we assert)."""
    import importlib
    from django.apps import apps as global_apps
    mig = importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")
    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload={"repository": {"id": REPO_ID}, "ref": "refs/heads/main",
                 "before": "b" * 40, "after": "c" * 40},
        repository=REPO, body_digest="d", status="processed",
    )
    settings.FORGEJO_INSTANCE_ID = ""  # deployment defect
    with pytest.raises(RuntimeError) as exc:
        mig._backfill(global_apps, None)
    assert "FORGEJO_INSTANCE_ID" in str(exc.value)
    # a CONFIGURED instance id completes without error (keys the row)
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    mig._backfill(global_apps, None)
    row = ForgejoDelivery.objects.get()
    assert row.semantic_key and row.semantic_key.split("\x1f")[1] == "forgejo"


def test_runtime_and_migration_share_the_forge_corpus():
    """Invariant 8 pinned: the frozen migration tables EQUAL the runtime forge
    corpus (forges.FORGES), so runtime and migration cannot classify a forge or
    its namespace differently — drift is detectable, not merely discouraged."""
    import importlib
    from plane.bridge import forges as _forges
    mig = importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")
    assert mig._INSTANCE_SETTING == {f.name: f.instance_id_setting for f in _forges.FORGES}
    assert mig._STABLE_ID_PATH == {f.name: f.stable_id_path for f in _forges.FORGES}


def test_rendered_compose_scopes_credentials_and_ids_per_service():
    """RENDERED-compose control (Morrow RC 3484 #4 — the prior version
    `yaml.safe_load`ed the override alone, which cannot see merge or
    interpolation and never inspected worker/beat): run REAL
    `docker compose config` with distinct sentinel values and assert the FINAL
    per-service environment — presences AND absences, every backend service.
    Skips are environmental (no full checkout / no docker) and say so; a skip
    is not a pass and the same control runs unskipped from a full checkout."""
    import json
    import os
    import subprocess
    import tempfile

    here = os.path.dirname(__file__).replace(os.sep, "/")
    if "apps/api/plane" not in here:
        pytest.skip("compose control runs from a full source checkout, not the /code container")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 6)))
    selfhost = os.path.join(root, "deployments", "selfhost")
    if not os.path.exists(os.path.join(selfhost, "docker-compose.override.yml")):
        pytest.skip("compose override not present on this checkout")
    try:
        have_compose = subprocess.run(
            ["docker", "compose", "version"], capture_output=True
        ).returncode == 0
    except FileNotFoundError:
        have_compose = False
    if not have_compose:
        pytest.skip("docker compose unavailable; rendered control needs the real resolver")

    sentinels = {
        "FORGEJO_WEBHOOK_SECRET": "SENTINEL-FJ-SECRET",
        "GITHUB_WEBHOOK_SECRET": "SENTINEL-GH-SECRET",
        "GITLAB_WEBHOOK_TOKEN": "SENTINEL-GL-TOKEN",
        "FORGEJO_INSTANCE_ID": "SENTINEL-FJ-ID",
        "GITHUB_INSTANCE_ID": "SENTINEL-GH-ID",
        "GITLAB_INSTANCE_ID": "SENTINEL-GL-ID",
        "BRIDGE_ALLOW_UNSIGNED_BODY_FORGES": "SENTINEL-FLAG",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as env_file:
        for key, value in sentinels.items():
            env_file.write(f"{key}={value}\n")
        env_path = env_file.name
    try:
        rendered = subprocess.run(
            ["docker", "compose", "--project-directory", selfhost,
             "--env-file", env_path,
             "-f", os.path.join(selfhost, "docker-compose.yml"),
             "-f", os.path.join(selfhost, "docker-compose.override.yml"),
             "config", "--format", "json"],
            capture_output=True, text=True,
        )
        assert rendered.returncode == 0, f"compose config failed: {rendered.stderr[-500:]}"
        services = json.loads(rendered.stdout)["services"]
    finally:
        os.unlink(env_path)

    def env_of(service):
        return services[service].get("environment") or {}

    creds = {"FORGEJO_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET", "GITLAB_WEBHOOK_TOKEN"}
    ids = {"FORGEJO_INSTANCE_ID", "GITHUB_INSTANCE_ID", "GITLAB_INSTANCE_ID"}

    # api authenticates webhooks AND computes keys: sentinel VALUES must arrive.
    api = env_of("api")
    for key in creds | ids:
        assert api.get(key) == sentinels[key], f"api must receive {key} (got {api.get(key)!r})"
    assert api.get("BRIDGE_ALLOW_UNSIGNED_BODY_FORGES") == sentinels["BRIDGE_ALLOW_UNSIGNED_BODY_FORGES"]

    # worker reconciles ALREADY-KEYED deliveries: semantic keys are minted at
    # ingest on api (Aria RC 3518, AST-verified — process_delivery touches no
    # key/instance machinery), so the worker gets NEITHER credentials NOR
    # instance ids; only the repo map and bridge API creds it actually uses.
    worker = env_of("worker")
    for key in creds | ids | {"BRIDGE_ALLOW_UNSIGNED_BODY_FORGES"}:
        assert key not in worker, f"worker must NOT receive {key}"
    assert worker.get("FORGEJO_BRIDGE_REPO_MAP") is not None

    # beat only schedules: nothing bridge-shaped at all.
    beat = env_of("beat-worker")
    for key in creds | ids | {"BRIDGE_ALLOW_UNSIGNED_BODY_FORGES", "FORGEJO_BRIDGE_REPO_MAP",
                              "FORGEJO_BASE_URL", "FORGEJO_BRIDGE_API_TOKEN"}:
        assert key not in beat, f"beat-worker must NOT receive {key}"

    # migrator runs 0128: ids yes, credentials no.
    migrator = env_of("migrator")
    for key in ids:
        assert migrator.get(key) == sentinels[key], f"migrator must receive {key}"
    for key in creds:
        assert key not in migrator, f"migrator must NOT receive {key}"
    api_env = set(services["api"].get("environment", {}))
    assert {"FORGEJO_INSTANCE_ID", "FORGEJO_WEBHOOK_SECRET"} <= api_env


@pytest.mark.django_db
def test_separator_instance_id_refuses_at_runtime(settings):
    """RC 3481 #3 (runtime boundary): a separator-bearing configured id is
    malformed config — refused at the door (500, before parse, zero rows)."""
    u, ws, proj, i1, i2, seqs = _fixture()
    settings.FORGEJO_INSTANCE_ID = "inst\x1fevil"
    with _scoped(ws):
        r = _post(_push(i1.sequence_id), str(uuid_lib.uuid4()))
    assert r.status_code == 500
    assert ForgejoDelivery.objects.count() == 0


@pytest.mark.django_db
def test_migration_unknown_forge_never_aborts(settings):
    """RC 3483 #1: a stored row for an UNKNOWN forge with a valid tuple is
    UNKEYABLE — the backfill treats it as unkeyed, never aborts citing [None]."""
    import importlib
    from django.apps import apps as global_apps
    mig = importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")
    settings.FORGEJO_INSTANCE_ID = "forgejo"
    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="bitbucket", event="push",
        payload={"repository": {"id": REPO_ID}, "ref": "refs/heads/main",
                 "before": "b" * 40, "after": "c" * 40},
        repository=REPO, body_digest="d", status="processed",
    )
    mig._backfill(global_apps, None)
    assert ForgejoDelivery.objects.get().semantic_key is None


@pytest.mark.django_db
def test_migration_separator_instance_fails_closed(settings):
    """RC 3481 #3 (migration boundary): a separator-bearing configured id makes
    the backfill FAIL CLOSED (treated as malformed/unconfigured), never keys."""
    import importlib
    from django.apps import apps as global_apps
    mig = importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")
    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload={"repository": {"id": REPO_ID}, "ref": "refs/heads/main",
                 "before": "b" * 40, "after": "c" * 40},
        repository=REPO, body_digest="d", status="processed",
    )
    settings.FORGEJO_INSTANCE_ID = "inst\x1fevil"
    with pytest.raises(RuntimeError):
        mig._backfill(global_apps, None)


@pytest.mark.django_db
def test_stability_check_propagates_introspection_failure(settings, monkeypatch):
    """RC 3484 #2 / RC 3485 #1 — the control AT the former catch site, not
    downstream of it: an OperationalError from INTROSPECTION (outage, denied
    metadata query) must propagate, never be laundered into pre-0128 absence.
    The prior forced-error control monkeypatched objects.filter, which sits
    after the old broad catch and was structurally unable to see this swallow;
    this one forces the failure at table_names() itself."""
    from django.db import connection
    from django.db.utils import OperationalError

    from plane.bridge import checks

    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "fj"
    settings.GITHUB_WEBHOOK_SECRET = None; settings.GITLAB_WEBHOOK_TOKEN = None

    def _outage(*a, **k):
        raise OperationalError("database unavailable during introspection")

    monkeypatch.setattr(connection.introspection, "table_names", _outage)
    with pytest.raises(OperationalError, match="unavailable during introspection"):
        checks.check_provider_instance_stability(None, databases=["default"])


@pytest.mark.django_db
def test_database_scoped_check_command_refuses_a_stored_rename(settings):
    """RC 3484 #1, the INVOCATION half: the exact command the entrypoints run —
    `manage.py check --database default` — must surface E003 as a refusal.
    Registration alone proved the function; this proves the enforcement path
    the deployment actually takes."""
    from django.core.management import call_command
    from django.core.management.base import SystemCheckError

    settings.FORGEJO_WEBHOOK_SECRET = "s"; settings.FORGEJO_INSTANCE_ID = "old-id"
    settings.GITHUB_WEBHOOK_SECRET = None; settings.GITLAB_WEBHOOK_TOKEN = None
    key = skey.push_key("old-id", REPO_ID, "refs/heads/main", "b" * 40, "c" * 40)
    ForgejoDelivery.objects.create(
        delivery_id=str(uuid_lib.uuid4()), forge="forgejo", event="push",
        payload={"repository": {"id": REPO_ID}}, repository=REPO, body_digest="d",
        semantic_key=key, semantic_key_hash=skey.key_hash(key), status="processed")

    call_command("check", "--database", "default")  # same namespace: passes

    settings.FORGEJO_INSTANCE_ID = "new-id"
    with pytest.raises(SystemCheckError, match="bridge.E003"):
        call_command("check", "--database", "default")


EXPECTED_CHECK = "python manage.py check --database default"


def _strip_comment(line):
    """Cut a shell line at its first comment `#` — quote-aware, and `#` opens
    a comment only at a WORD BOUNDARY (Aria RC 3518 follow-up, tested 12/12
    against the real entrypoints and nine violating mutants): `${VAR#prefix}`
    and quoted `#` are code, `true  # cmd` is not."""
    out, quote, prev_ws = [], None, True
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            prev_ws = False
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_ws = False
            continue
        if ch == "#" and prev_ws:
            break
        out.append(ch)
        prev_ws = ch.isspace()
    return "".join(out)


def _live(text):
    """The LIVE commands of a shell script — comments removed BEFORE any
    matching, then EXACT equality against these, never substring search
    (Morrow RC 3521: a check that exists only inside a trailing comment
    passes bash syntax while removing the check from execution)."""
    return [c for c in (_strip_comment(l).strip() for l in text.splitlines()) if c]


@pytest.mark.parametrize(
    "mutant, description",
    [
        ("# " + EXPECTED_CHECK, "leading comment"),
        ("    # " + EXPECTED_CHECK, "indented comment"),
        ("true  # " + EXPECTED_CHECK,
         "TRAILING comment on a live no-op — Morrow RC 3521's required case: "
         "valid bash, check removed from execution, previous filter passed it"),
        ("python manage.py wait_for_migrations # then " + EXPECTED_CHECK,
         "trailing comment on a real command"),
        ("echo '" + EXPECTED_CHECK + "'", "quoted string, not an invocation"),
        (EXPECTED_CHECK + " --fail-level WARNING", "similar-but-different command"),
        ("python manage.py check --database", "prefix of the command, arg dropped"),
        ("", "deleted line"),
    ],
)
def test_live_command_detector_refuses_every_masked_form(mutant, description):
    """The committed mutant table (Morrow RC 3521 / Rowan RC 3522): the
    detector must yield no live EXPECTED_CHECK for every masked form —
    the `true  # <cmd>` case by name — with positive controls that the real
    line, added indentation, and a legitimate quoted/expansion `#` elsewhere
    all stay GREEN (Aria's 12-row matrix, committed)."""
    text = "python manage.py wait_for_migrations\n" + mutant + "\n"
    assert EXPECTED_CHECK not in _live(text), description
    # Positive controls: detected exactly; indentation tolerated; a quoted `#`
    # and a ${VAR#prefix} expansion elsewhere do not swallow the line.
    assert EXPECTED_CHECK in _live("x\n" + EXPECTED_CHECK + "\n")
    assert EXPECTED_CHECK in _live("   " + EXPECTED_CHECK + "\n")
    assert EXPECTED_CHECK in _live('echo "a # b"\n' + EXPECTED_CHECK + "\n")
    assert 'echo "${X#p}"' in _live('echo "${X#p}"\n')


def test_entrypoints_invoke_the_database_scoped_check():
    """RC 3484 #1, the WIRING half (the companion executable half is the
    call_command test above): every production entrypoint must run the
    database-scoped check AFTER migrations are known-ready — Django's
    BaseCommand self-checks never pass `databases`, so only this explicit
    invocation makes E003 run at startup. Live-command equality via
    _live_commands (RC 3519/3521), with the mutant table above pinning the
    detector itself."""
    import os

    here = os.path.dirname(__file__).replace(os.sep, "/")
    if "apps/api/plane" not in here:
        pytest.skip("entrypoint control runs from a source checkout with bin/ present")
    bin_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 4), "bin"))
    # The CHECK matches by EXACT equality; the ANCHOR is prefix-matched ON
    # PURPOSE (Aria: the migrator's live line is `python manage.py migrate $1`
    # — an equality anchor failed the UNMODIFIED tree, caught only by running
    # the real entrypoints through the control before any mutant).
    ordering_anchor = {
        "docker-entrypoint-api.sh": "python manage.py wait_for_migrations",
        "docker-entrypoint-worker.sh": "python manage.py wait_for_migrations",
        "docker-entrypoint-beat.sh": "python manage.py wait_for_migrations",
        "docker-entrypoint-migrator.sh": "python manage.py migrate",
    }
    for name, anchor in ordering_anchor.items():
        path = os.path.join(bin_dir, name)
        assert os.path.exists(path), f"{name} missing"
        live = _live(open(path).read())
        check_idx = [i for i, c in enumerate(live) if c == EXPECTED_CHECK]
        anchor_idx = [i for i, c in enumerate(live) if c == anchor or c.startswith(anchor + " ")]
        assert check_idx, f"{name}: no LIVE `{EXPECTED_CHECK}` command"
        assert anchor_idx, f"{name}: no LIVE `{anchor}` to order against"
        assert check_idx[0] > anchor_idx[0], f"{name}: check must run AFTER {anchor}"


@pytest.mark.django_db
def test_migration_backfill_refuses_colliding_instance_ids(settings):
    """RC 3485 #2: the migrator holds no webhook credentials, so runtime E002
    (which iterates ENABLED forges) cannot protect the historical boundary —
    0128 itself must fail closed BEFORE any write when configured known-forge
    instance ids collide, else one Forgejo push row and one GitHub push row
    with the same stable repo id/ref/shas would alias ACROSS PROVIDERS.
    Exercised via _backfill directly, the same shape as the unconfigured-id
    refusal above (a raising migration leaves a half-applied schema)."""
    import importlib

    from django.apps import apps as global_apps

    mig = importlib.import_module("plane.db.migrations.0128_forgejodelivery_semantic_key")
    settings.FORGEJO_INSTANCE_ID = "shared"
    settings.GITHUB_INSTANCE_ID = "shared"
    settings.GITLAB_INSTANCE_ID = None
    with pytest.raises(RuntimeError, match="instance ids collide"):
        mig._backfill(global_apps, None)

    # Control: distinct ids pass the uniqueness gate (empty table: no further work).
    settings.GITHUB_INSTANCE_ID = "distinct"
    mig._backfill(global_apps, None)
