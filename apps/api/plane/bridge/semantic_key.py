# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Provider-qualified semantic event keys for the git bridge (BIP-46, PR-B1).

Every observation of a real git event — whether it arrived by WEBHOOK or was
found by the POLLER — collapses to ONE outcome iff both transports compute the
SAME key from the SAME immutable content. So the key is built here, once, from
already-extracted primitives; each transport does its own extraction (webhook
via the Forge accessors, poll via the GitHub API response) and feeds this
constructor. Provider delivery IDs are transport/replay provenance and NEVER
part of the key (Morrow 3309).

Constructors (doc 9d8a2e9 §M3, verbatim):
  push      = (provider instance, stable repo id, ref, before, after)
  merged PR = (provider instance, stable repo id, PR number, merge-commit SHA)
  review    = (provider instance, stable repo id, PR number, review id)  [BIP-50]

The canonical key is a PLAINTEXT string (kept in full on the row so "which real
event was this?" is answerable at audit time — Aria); its sha256 is the unique
index (compact, collision-free for the dedup constraint).
"""

import hashlib

# Field separator that cannot appear in any component: refs, SHAs, numbers and
# forge names are all free of the unit separator (0x1f).
_SEP = "\x1f"


class IncompleteEvent(ValueError):
    """A key was requested for an event whose complete, immutable identity
    tuple is not present. The caller assigns NO key and falls back to
    delivery_id dedup (Morrow 3329): a key must be derived only from a fully
    proven tuple, never a partial one that could alias another event."""


def _str_field(value, name):
    """A string component: refs, commit/merge SHAs, the provider instance. Not
    an int/bool/object (Morrow RC 3348 — the boundary is FIELD-typed, not
    'str-or-int for everything')."""
    if value is None:
        raise IncompleteEvent(f"{name} is missing")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    if value == "":
        raise IncompleteEvent(f"{name} is empty")
    if _SEP in value:
        raise ValueError(f"{name} contains the reserved separator")
    return value


def is_identity_int(value) -> bool:
    """Does `value` qualify as an integer identity component under THIS module's
    rule? Predicate form of `_int_field` for callers that must decide before a
    key exists — the page/cursor boundary compares raw ids for equality, and
    `True == 1` in Python, so a boolean repo id otherwise passes repository 1's
    ownership guard. One rule, one owner: callers ask, they do not re-derive."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _int_field(value, name):
    """An integer identity component: the forge's stable repo id, a PR number.
    A strict positive int — never a bool (int subclass) or a numeric string.

    The accept/reject DECISION is `is_identity_int` and only `is_identity_int`.
    This function adds the diagnosis a key-builder owes its caller — which of
    missing, wrong type, or non-positive — and nothing else. Previously both
    carried their own copy of the rule, so a change to one would silently leave
    the predicate and the key-builder disagreeing about what an identity is
    (Morrow RC 3569)."""
    if is_identity_int(value):
        return str(value)
    # Not an identity int. Everything below only explains why.
    if value is None:
        raise IncompleteEvent(f"{name} is missing")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}")
    raise ValueError(f"{name} must be a positive integer, got {value}")


def push_key(provider_instance, repo_id, ref, before, after) -> str:
    """Canonical semantic key for a push. Field-typed: provider/ref/anchors are
    strings; repo_id is the forge's immutable POSITIVE-int repo id."""
    return _SEP.join(
        ["push", _str_field(provider_instance, "provider_instance"),
         _int_field(repo_id, "repo_id"), _str_field(ref, "ref"),
         _str_field(before, "before"), _str_field(after, "after")]
    )


def merged_pr_key(provider_instance, repo_id, pr_number, merge_sha) -> str:
    """Canonical semantic key for a merged pull request. Field-typed: provider
    and merge SHA are strings; repo_id and pr_number are positive ints."""
    return _SEP.join(
        ["merged_pr", _str_field(provider_instance, "provider_instance"),
         _int_field(repo_id, "repo_id"), _int_field(pr_number, "pr_number"),
         _str_field(merge_sha, "merge_sha")]
    )


def review_key(provider_instance, repo_id, pr_number, review_id) -> str:
    """Canonical semantic key for a pull-request REVIEW observation (BIP-50,
    Rowan's rejection-move seam). Field-typed: provider is a string; repo_id,
    pr_number and review_id are positive ints. Identity is the review
    OBSERVATION, never its verdict, exactly as merged_pr_key does not encode who
    merged. The verdict is supplied by the SIGNED event rather than re-read from
    the forge, and it drives no move logic — there is no move (BIP-67); it
    selects which refusal is recorded. Flows the SAME holder-alias lifecycle, so
    a webhook and a poll of one review collapse to a single outcome."""
    return _SEP.join(
        ["review", _str_field(provider_instance, "provider_instance"),
         _int_field(repo_id, "repo_id"), _int_field(pr_number, "pr_number"),
         _int_field(review_id, "review_id")]
    )


def key_hash(canonical_key: str) -> str:
    """sha256 hex of the canonical key — the value the unique index binds."""
    return hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
