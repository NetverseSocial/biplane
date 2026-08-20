# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
"""BIP-46 PR-B1: semantic event key on the delivery inbox.

Adds semantic_key (plaintext, audit-answerable) + semantic_key_hash (the unique
dedup index) and backfills existing rows.

TWO THINGS THIS MIGRATION IS CAREFUL ABOUT (Morrow RC 3329):

1. It is SAFE over its legal predecessor state. Before this migration, two
   different delivery IDs for the SAME real event are legal (a webhook replayed
   under a fresh delivery id, or an org+repo hook both firing). Such rows share
   a semantic key, so a naive backfill would assign the same hash to both and
   the unique constraint would fail. The backfill CONSOLIDATES instead: within
   a group of rows sharing a key, exactly one (the PROCESSED row — the one
   carrying the authoritative outcome — else the earliest) holds the
   hash; the rest keep the plaintext key (audit history is retained — every row
   still answers "which event was this?") but a NULL hash, which the partial
   unique constraint permits. No row is deleted; one is chosen as the dedup
   holder.

2. Its key logic is FROZEN INLINE — it does NOT import plane.bridge.*. A future
   fresh install replays THIS migration with THESE bytes, so importing mutable
   runtime code would let historical migration semantics drift to whatever
   those modules later become. The field paths and the canonical encoding are
   copied here and pinned.

Nullable classes are DELIBERATE and permanent (see 0128 model note): an event
with no dedupable transition (unmerged PR) and a row whose repo carries no
stable id both legitimately have NULL semantic_key. This is not "temporary
until backfilled" — it is the runtime invariant too.
"""
import hashlib

from django.conf import settings
from django.utils import timezone
from django.db import migrations, models

# --- frozen, inline: never import runtime plane.bridge here -----------------
_SEP = "\x1f"
# forge -> (stable-repo-id path). Forgejo/GitHub share the github shape;
# GitLab is a different family. Frozen at BIP-46 PR-B1.
_STABLE_ID_PATH = {
    "forgejo": ("repository", "id"),
    "github": ("repository", "id"),
    "gitlab": ("project", "id"),
}
# Per-forge-INSTANCE identity setting (ADR 010 §1). Frozen inline like the key
# rules. The migration reads the CONFIGURED instance id and assigns it to every
# historical row, so the backfilled hashes are instance-scoped from the start
# (no prior instance-scoped hashes existed). A forge with KEYABLE historical
# rows and no configured instance id FAILS THE MIGRATION CLOSED (RC 3466 —
# see _backfill; an earlier version of this comment said such rows were left
# unkeyed, which contradicted the code below it). Only a forge with no keyable
# history may migrate without an id; its rows dedup by delivery_id and the
# runtime path refuses new deliveries until it is configured (4e).
_INSTANCE_SETTING = {
    "forgejo": "FORGEJO_INSTANCE_ID",
    "github": "GITHUB_INSTANCE_ID",
    "gitlab": "GITLAB_INSTANCE_ID",
}


def _dig(payload, *path):
    cur = payload
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


# FIELD-TYPED, frozen to match the runtime constructors (Morrow RC 3348): a
# component's type is fixed by its ROLE. A wrong-typed historical value (e.g. a
# ref or merge SHA stored as an object, which the 0127 validator did not
# reject) yields None -> the row stays UNKEYED, exactly as the runtime rejects
# it. runtime-equals-migration in the violating direction.
def _str_field(value):
    if not isinstance(value, str) or value == "" or _SEP in value:
        return None
    return value


def _int_field(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return str(value)


def _push_key(instance, repo_id, ref, before, after):
    parts = [_str_field(instance), _int_field(repo_id), _str_field(ref),
             _str_field(before), _str_field(after)]
    if any(p is None for p in parts):
        return None
    return _SEP.join(["push"] + parts)


def _merged_pr_key(instance, repo_id, number, merge_sha):
    parts = [_str_field(instance), _int_field(repo_id), _int_field(number),
             _str_field(merge_sha)]
    if any(p is None for p in parts):
        return None
    return _SEP.join(["merged_pr"] + parts)


def _canonical(forge, event, payload):
    if not isinstance(payload, dict):
        return None
    # provider INSTANCE from config (ADR 010 §1) — never the forge family name.
    setting = _INSTANCE_SETTING.get(forge)
    instance = (getattr(settings, setting, None) or "").strip() if setting else ""
    if not instance:
        return None  # no configured instance id -> historical row stays UNKEYED
    repo_id = _dig(payload, *_STABLE_ID_PATH.get(forge, ("repository", "id")))
    if not isinstance(repo_id, int) or isinstance(repo_id, bool):
        return None
    if event == "push":
        return _push_key(instance, repo_id, payload.get("ref"), payload.get("before"), payload.get("after"))
    if event == "pull_request":
        if forge == "gitlab":
            attrs = payload.get("object_attributes") or {}
            if attrs.get("action") != "merge":
                return None
            return _merged_pr_key(instance, repo_id, attrs.get("iid"), attrs.get("merge_commit_sha"))
        pr = payload.get("pull_request") or {}
        if not (payload.get("action") == "closed" and pr.get("merged") is True):
            return None
        return _merged_pr_key(instance, repo_id, pr.get("number"), pr.get("merge_commit_sha"))
    return None


def _keyable(forge, event, payload):
    """Would this row produce a semantic key IF an instance were configured?
    Keyability sans namespace — used to FAIL CLOSED when real dedupable history
    exists but no instance id is set (Rowan/Morrow RC 3466)."""
    if forge not in _INSTANCE_SETTING:
        # An UNKNOWN historical forge has no instance setting and no known
        # stable-id path — it is never semantic-keyed (dedups by delivery_id).
        # Explicit policy, never an impossible [None] setting (Rowan RC 3483 #1).
        return False
    if not isinstance(payload, dict):
        return False
    repo_id = _dig(payload, *_STABLE_ID_PATH.get(forge, ("repository", "id")))
    if not isinstance(repo_id, int) or isinstance(repo_id, bool):
        return False
    if event == "push":
        return _push_key("x", repo_id, payload.get("ref"), payload.get("before"), payload.get("after")) is not None
    if event == "pull_request":
        if forge == "gitlab":
            attrs = payload.get("object_attributes") or {}
            return attrs.get("action") == "merge" and _merged_pr_key(
                "x", repo_id, attrs.get("iid"), attrs.get("merge_commit_sha")) is not None
        pr = payload.get("pull_request") or {}
        return (payload.get("action") == "closed" and pr.get("merged") is True
                and _merged_pr_key("x", repo_id, pr.get("number"), pr.get("merge_commit_sha")) is not None)
    return False


def _configured_instance(forge):
    setting = _INSTANCE_SETTING.get(forge)
    value = (getattr(settings, setting, None) or "").strip() if setting else ""
    # A separator-bearing configured id is MALFORMED (ADR 010 §1a); treat it as
    # UNCONFIGURED so the backfill FAILS CLOSED rather than silently keying
    # history under a boundary-forging namespace (RC 3481). Same predicate as the
    # runtime owner (instance_config.resolve), pinned by a parity test.
    if _SEP in value:
        return ""
    return value


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _backfill(apps, schema_editor):
    ForgejoDelivery = apps.get_model("db", "ForgejoDelivery")

    # Fail closed on COLLIDING instance ids BEFORE any write (Rowan RC 3485 #2):
    # the migrator deliberately receives no webhook credentials, so runtime
    # E002 (which iterates ENABLED forges) cannot protect this boundary here.
    # With FORGEJO_INSTANCE_ID == GITHUB_INSTANCE_ID, a Forgejo push row and a
    # GitHub push row carrying the same stable repo id/ref/before/after would
    # get byte-identical plaintext keys and alias ACROSS PROVIDERS — silently.
    # Uniqueness of every configured, non-empty known-forge id is therefore a
    # precondition of the write, not a runtime nicety.
    configured = {}
    for forge_name, setting in _INSTANCE_SETTING.items():
        value = (getattr(settings, setting, None) or "").strip()
        if value:
            configured.setdefault(value, []).append((forge_name, setting))
    collisions = {v: owners for v, owners in configured.items() if len(owners) > 1}
    if collisions:
        detail = "; ".join(
            f"{value!r} is shared by " + ", ".join(s for _, s in owners)
            for value, owners in sorted(collisions.items())
        )
        raise RuntimeError(
            "migration 0128 refuses to run: configured instance ids collide "
            f"({detail}). Distinct events from different forges would receive "
            "byte-identical semantic keys and alias across providers. Give every "
            "forge a unique instance id before migrating (ADR 010 §1)."
        )

    # Fail closed (Rowan/Morrow RC 3466): a forge with rows that WOULD be
    # semantic-keyed but no configured instance id must NOT migrate silently —
    # leaving real dedupable history unkeyed is the config-load refusal (4e) one
    # layer down: it looks healthy while dedup is quietly degraded. Refuse,
    # naming the env var.
    unconfigured = set()
    for row in ForgejoDelivery.objects.all().iterator():
        p = row.payload if isinstance(row.payload, dict) else {}
        if _keyable(row.forge, row.event, p) and not _configured_instance(row.forge):
            unconfigured.add(row.forge)
    if unconfigured:
        names = sorted(unconfigured)
        raise RuntimeError(
            "migration 0128 refuses to complete: delivery rows exist for forge(s) "
            f"{names} that must be semantic-keyed, but their instance id is not "
            f"configured or is malformed ({[_INSTANCE_SETTING.get(f) for f in names]}). Set a stable "
            "instance id per forge before migrating (ADR 010 §1); leaving history "
            "unkeyed silently degrades dedup."
        )

    # Pass 1 — choose the holder per semantic key. AUTHORITY FOLLOWS THE
    # PROCESSED ROW (Morrow RC 3335): the processed row carries the
    # authoritative outcome (its `result`), so it must own the dedup key — NOT
    # merely the earliest row, which may be an unprocessed duplicate. Among
    # processed rows the earliest wins; if none is processed, the earliest row.
    holder = {}  # hash -> (pk, delivery_id, is_processed, result, processed_at)
    for row in ForgejoDelivery.objects.all().order_by("created_at", "id").iterator():
        canonical = _canonical(row.forge, row.event, row.payload if isinstance(row.payload, dict) else {})
        if canonical is None:
            continue
        h = _hash(canonical)
        proc = row.status == "processed"
        cur = holder.get(h)
        if cur is None:
            holder[h] = (row.pk, row.delivery_id, proc, row.result, row.processed_at)
        elif proc and not cur[2]:
            holder[h] = (row.pk, row.delivery_id, True, row.result, row.processed_at)  # a processed row supersedes an earlier non-processed holder

    # Pass 2 — assign. Plaintext key on EVERY keyed row (audit retained); the
    # unique hash on the chosen holder only (NULL elsewhere, which the partial
    # constraint permits, so AddConstraint cannot fail).
    for row in ForgejoDelivery.objects.all().order_by("created_at", "id").iterator():
        canonical = _canonical(row.forge, row.event, row.payload if isinstance(row.payload, dict) else {})
        if canonical is None:
            continue  # legitimately NULL (no dedupable transition / no stable id)
        h = _hash(canonical)
        row.semantic_key = canonical
        if holder[h][0] == row.pk:
            # THE dedup holder: owns the unique hash, keeps its own result.
            row.semantic_key_hash = h
            row.save(update_fields=["semantic_key", "semantic_key_hash"])
        else:
            # A coalesced NON-HOLDER: NULL hash + a durable coalesced_to pointer to
            # the holder's delivery_id — the SAME shape post()/_resolve_alias write
            # at runtime, so _is_alias() recognizes it and it never re-executes.
            _hpk, hdid, hproc, hresult, hprocessed_at = holder[h]
            row.semantic_key_hash = None
            if hproc:
                # The holder already COMPLETED. At runtime the reconciler
                # finalizes an alias to the holder's result once the holder is
                # done; the migration KNOWS the holder's terminal state, so it
                # writes the FINAL shape DIRECTLY — processed + the holder's
                # result — not a pending shape a later step repairs (Morrow:
                # immediate parity, ADR 010 §4).
                row.status = "processed"
                row.result = {"coalesced_to": hdid, **(hresult or {"moved": []})}
                row.processed_at = hprocessed_at or timezone.now()
                row.last_error = None
                row.lease_token = None
                row.lease_expires_at = None
                row.save(update_fields=[
                    "semantic_key", "semantic_key_hash", "result", "status",
                    "processed_at", "last_error", "lease_token", "lease_expires_at",
                ])
            else:
                # Holder not finished — the alias stays PENDING with the pointer,
                # exactly what post() writes; the reconciler finalizes it later.
                row.status = "pending"
                row.result = {"coalesced_to": hdid}
                row.save(update_fields=["semantic_key", "semantic_key_hash", "result", "status"])


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("db", "0127_audit_outbox")]

    operations = [
        migrations.AddField(
            model_name="forgejodelivery",
            name="semantic_key",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="forgejodelivery",
            name="semantic_key_hash",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.RunPython(_backfill, _noop_reverse),
        migrations.AddIndex(
            model_name="forgejodelivery",
            index=models.Index(fields=["semantic_key_hash"], name="db_forgejod_semanti_idx"),
        ),
        migrations.AddConstraint(
            model_name="forgejodelivery",
            constraint=models.UniqueConstraint(
                fields=["semantic_key_hash"],
                condition=models.Q(semantic_key_hash__isnull=False),
                name="uniq_forgejo_delivery_semantic_key_hash",
            ),
        ),
    ]
