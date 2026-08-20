#!/usr/bin/env bash
# biplane release pipeline (BIP-40 / M5.1): reject a release tag the update
# check (M5.2) could never compare. A tag outside the accepted release grammar
# publishes a release that forces UNKNOWN forever (Rowan RC 3412: `vgarbage`
# executed exit 0 at the producer while the consumer can only compare semver).
# Validate at the producer — the party that can refuse the tag before the
# release exists. Prereleases are excluded from the stable channel by policy,
# so a `-rc`/`-beta` suffix is refused here too.
#
# THE GRAMMAR IS NOT DEFINED HERE. It is one authority, shared with the
# consumer, and this script obtains its verdict rather than restating it.
#
# It used to restate it, as `^v[0-9]+\.[0-9]+\.[0-9]+$`, and that is precisely
# how the guarantee broke: the consumer tightened to refuse leading zeros and
# this pattern did not move with it, so `v1.01.0` was ACCEPTED by the producer
# and REFUSED by the consumer (Vex, executed cross-PR table on #55). Tag it and
# publish it — today via the manual README procedure — and every deployment
# classifies UNKNOWN permanently — the exact
# failure the header above says this file exists to prevent, reintroduced by
# the file itself. Two independently-maintained copies of one rule had already
# drifted once; a third round of patching the copy was the wrong answer
# (Morrow RC 3487: stop taking rounds around "plain semver" and use the vetted
# comparator already in the toolchain).
#
#   validate-tag.sh <tag>   -> exit 0 if a comparable stable release tag, else 1
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/release-version.sh"

TAG="${1:?usage: validate-tag.sh <tag>}"

if ! release_version_valid "$TAG"; then
  # State the FINITE domain, computed from the same datum the check used, so an
  # operator reads the real boundary instead of inferring one from a regex.
  echo "validate-tag: '$TAG' is not a comparable stable release tag." >&2
  echo "  accepted: v<major>.<minor>.<patch>, up to ${RV_MAX_DIGITS} digits per component, no leading zeros" >&2
  echo "  upper accepted: $(release_version_upper_accepted)" >&2
  echo "  first refused:  $(release_version_first_refused)" >&2
  exit 1
fi

echo "tag ok: $TAG"
