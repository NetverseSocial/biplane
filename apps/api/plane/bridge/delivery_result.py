# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Single owner of the durable ``ForgejoDelivery.result`` shape (BIP-38).

Why this module exists: ``result`` grew five assembly sites and no declared
union, and it is NOT merely an audit record — it decides whether a delivery
EXECUTES. ``inbox.is_alias()`` classifies a stored row as a non-executing
alias when ``result`` carries ``coalesced_to``; any writer that puts a key by
that name into a live delivery's result silently converts it into a row that
never runs (Vex, BIP-38 seam agreement). So the shape needs one owner whose
constructor cannot produce that key by construction.

The shape::

    {"moved": ["BIP-7", ...],            # ALWAYS present, sorted, deduplicated
     "ignored": {                        # present only when non-empty
        "review":       str,            # review delivery ignored, and why
        "near_misses":  [str, ...],     # disqualified near-miss directive lines
        "unscoped_repo": str,           # delivery from a repo the map doesn't scope
        "cross_project": [              # scope-guard rejections (BIP-38):
            {"ticket": "SB-3",          #   every ref outside the repo's mapped
             "repo": "acme/x",           #   project scope, durably recorded
             "reason": str}, ...],
        "unverified": [                 # write-boundary refusals (BIP-67):
            {"ticket": "BIP-7",         #   the facts did not determine the
             "reason": str,             #   outcome, so nothing was written.
             "detail": str}, ...],      #   Recipients are NOT here; the
                                        #   future delivery slice owns how
                                        #   they get resolved.
        "conflicts": ["BIP-7", ...],    # Closes+Refs same ticket: demoted to
                                        #   advance AND recorded (Scope A 110-112)
        "no_ticket": {                  # EVENT-level (BIP-67): the event named
            "reason": str,              #   NO ticket at all, so there is none
            "detail": str}}}            #   to key it by. One object, not a list.

    {"coalesced_to": <delivery_id>}     # RESERVED to the inbox seam (BIP-56).
                                        # THE alias discriminator; written only
                                        # by inbox.record_observation and the
                                        # alias finalization in _resolve_alias,
                                        # never through this constructor.

Readers of ``result`` (census: Vex, 2026-08-13 — this is an EXTERNAL surface,
not internal bookkeeping): ``inbox.is_alias``/the ALIAS return (coalesced_to),
``_advance``'s per-refusal record writes (unverified; the moved-accumulator
and its ``add_moved`` helper are DELETED with the board mutations — while every
write is refused, ``moved`` is always empty and only the constructor writes
it), ``_resolve_alias``'s holder
spread (whole dict), the completion default, migration 0128's backfill, and —
the external part — the webhook endpoint's duplicate-delivery responses spread
the stored result into the HTTP body. One shape everywhere, deliberately: the
response mirrors the durable record. Rows stored before this contract keep
their legacy flat keys (``ignored_review``/``ignored_near_misses``/
``unscoped_repo`` at top level) and are served as stored; the shape is
versioned by the presence of the ``"ignored"`` object. ``moved`` is top-level
and always present in both generations.
"""

__all__ = [
    "COALESCED_KEY",
    "build",
    "merge_ignored",
    "cross_project_entry",
    "unverified_entry",
]

# Reserved to the inbox seam (see module docstring). Exists so the reservation
# has a referencable name, not so anyone else may write it.
COALESCED_KEY = "coalesced_to"


def build(
    moved=(), *, review=None, near_misses=(), unscoped_repo=None, cross_project=(),
    unverified=(), no_ticket=None, conflicts=(),
):
    """A fresh result. ``moved`` always present; diagnostics only when real."""
    result = {"moved": sorted(set(moved))}
    return merge_ignored(
        result,
        review=review,
        near_misses=near_misses,
        unscoped_repo=unscoped_repo,
        cross_project=cross_project,
        unverified=unverified,
        no_ticket=no_ticket,
        conflicts=conflicts,
    )


def merge_ignored(
    result,
    *,
    review=None,
    near_misses=(),
    unscoped_repo=None,
    cross_project=(),
    unverified=(),
    no_ticket=None,
    conflicts=(),
):
    """Merge diagnostics into ``result`` under the one namespaced key,
    preserving ``moved`` and any diagnostics already present."""
    out = dict(result or {})
    out.setdefault("moved", [])
    ignored = dict(out.get("ignored") or {})
    if review is not None:
        ignored["review"] = review
    if near_misses:
        ignored["near_misses"] = list(near_misses)
    if unscoped_repo is not None:
        ignored["unscoped_repo"] = unscoped_repo
    if cross_project:
        ignored["cross_project"] = list(ignored.get("cross_project") or []) + [
            dict(entry) for entry in cross_project
        ]
    if unverified:
        # DEDUPE BY (ticket, reason) — a retry re-processes refs it already
        # recorded, so plain append yields [A, A, B] in the durable result and
        # duplicates A in the reply (Morrow). Distinct REASONS for one ticket
        # are kept: they are different facts about it, not a repeat.
        merged = list(ignored.get("unverified") or []) + [dict(e) for e in unverified]
        seen, deduped = set(), []
        for entry in merged:
            key = (entry.get("ticket"), entry.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        ignored["unverified"] = deduped
    if conflicts:
        # Scope A 110-112: a Closes+Refs same-ticket pair DEMOTES to advance
        # and the conflict is RECORDED — the demotion is a datum, not just an
        # outcome, or the under-move is silent. ONE conflict per ticket, first
        # order preserved (Morrow): two commits each conflicting on BIP-7 are
        # one fact about one ticket, and a retry must not multiply it.
        merged = list(ignored.get("conflicts") or [])
        for key in conflicts:
            if key not in merged:
                merged.append(key)
        ignored["conflicts"] = merged
    if no_ticket is not None:
        # EVENT-level, so it is a single object rather than a list keyed by
        # ticket: this event named none, and there is no ticket to key it by.
        ignored["no_ticket"] = dict(no_ticket)
    if ignored:
        out["ignored"] = ignored
    return out


def cross_project_entry(ticket: str, repo: str, reason: str) -> dict:
    """One durable scope-guard rejection: which ref, from where, and why —
    the record the spec requires beyond process logs (§M2 scope guard)."""
    return {"ticket": ticket, "repo": repo, "reason": reason}


def unverified_entry(refusal) -> dict:
    """One durable write-boundary refusal (BIP-67).

    The bridge did not write because the facts did not determine the outcome.
    Recorded here rather than only logged for the same reason the scope guard
    is: a refusal that exists only in process logs cannot be handed back to the
    person who has to act on it, and under Scope A the asking is the more
    valuable half of the service.

    **WHAT CONSUMES IT.** Today, the pull-request reply — and only where a pull
    request exists, so a refusal from a push is durable and reaches nobody. The
    notification slice was cut from this release, so this is the durable input a
    FUTURE delivery slice will read; nothing consumes it in this release.

    ``detail`` is a sentence for a human; ``reason`` is a stable code so a
    caller can group or route without parsing prose. Recipients are deliberately
    absent — see :class:`~plane.bridge.write_boundary.Refusal`.
    """
    return {"ticket": refusal.ticket, "reason": refusal.reason, "detail": refusal.detail}
