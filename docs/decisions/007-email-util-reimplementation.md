# 007 — Email plain-text utility: reimplementation and history excision

**Status:** accepted · **Decided by:** the Biplane project owner · **Date:** 2026-07-30

## Context

One file in the inherited tree — `apps/api/plane/utils/email.py` — carried a
`LicenseRef-Plane-Commercial` header ("unauthorized use or distribution is
prohibited") inside a repository whose only license is AGPL-3.0 with no
carve-out. Upstream distributes that same file publicly in their AGPL-labeled
repository, which suggests a mis-stamped header, but that is upstream's
ambiguity to own, not ours to interpret on their behalf.

## Decision

1. The commercially-headered file is **excluded from everything we publish** —
   removed from the tip and excised from all published git history, so no
   reachable ref or object contains it.
2. It is replaced by an **independent reimplementation** written from the
   function's public interface and behavior contract (one function:
   HTML email → plain text). The author had seen the original; this is an
   interface-based rewrite, **not** a two-team clean-room process, and we say
   so rather than borrow the stronger term.
3. The replacement is licensed **AGPL-3.0-only** and carries
   `Copyright (c) 2026 The Biplane Authors` — the fork-authorship convention
   used by Forgejo ("The Forgejo Authors") and OpenTofu ("The OpenTofu
   Authors"). Inherited files keep Plane's copyright line unchanged; only
   fully fork-authored content carries the Biplane line alone.

## Verification

A reviewer ran the original and the replacement side by side in the running
stack against all twelve shipped email templates (bound by checksum to the
reviewed commit). The contract held, and the replacement additionally fixed a
live defect: the original emitted entity-escaped (`&amp;`) password-reset URLs
into the text/plain body, breaking the documented copy-paste path. The
contract is pinned by `apps/api/plane/tests/unit/utils/test_email.py`.

## Compliance position

The AGPL-3.0 fully licenses this fork: modification and redistribution are the
grant, and the fork meets the license's conditions (root LICENSE retained, fork
and modifications disclosed in the README, all code kept AGPL, and the
application UI links its own source repository, satisfying the network-source
clause). The only file that ever claimed to sit *outside* that grant is the one
this decision removes from the tip and from all published history.

## Risk accepted

What remains is the possibility of a dispute over the replacement utility,
whose author had seen the original. The project owner accepts this as
negligible: the function's interface and behavior are dictated by its purpose,
and interfaces are not protectable expression (*Google v. Oracle*, 2021). The
worst realistic outcome is replacing one 42-line utility again.
