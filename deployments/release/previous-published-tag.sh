#!/usr/bin/env bash
# biplane release pipeline (BIP-40): the level baseline is the MAX published
# stable release by SEMVER, and the current release MUST be strictly greater
# than it — a MONOTONIC stable channel (Morrow RC 3467, then the next layer).
#
# Two defects this closes, one shape (baseline computed against the wrong point,
# under-deriving the level so a `code` label can skip a migration):
#   - a refused/build-failed tag stays reachable and `git describe` would pick it
#     (fixed by reading the release API — a refused tag never became a release);
#   - a baseline chosen by PUBLICATION ORDER rather than VERSION order, and no
#     monotonic guard, so an out-of-order or re-released older line is accepted.
#
#   previous-published-tag.sh <current-tag>
#     reads published stable-release tags on stdin (order irrelevant);
#     prints the MAX published stable tag and exits 0 when current > max;
#     prints nothing and exits 0 when there is no prior published stable
#       (first release; the caller falls back to the repo root);
#     exits 1 (prints nothing) when current is NOT strictly greater than the max
#       published stable — a re-release or out-of-order publication, refused.
#
# ORDERING IS NOT DEFINED HERE. Both the grammar and the comparator come from
# the one release-version authority, shared with the consumer.
#
# This file previously carried its own comparator, and it was wrong twice in the
# same shape — a bound nobody declared:
#   - `awk %d` clamped at INT32_MAX, so 2147483647 compared EQUAL to
#     2147483648: a false refusal that fails closed and therefore looks like
#     the guard working;
#   - the length-prefixed key that replaced it used `%03d`, which is ordered
#     only while a component has <=999 digits. Morrow's executed witness: a
#     prior major of 999 nines against a current major of 1 followed by 999
#     zeroes — both inside the published grammar — sorted backwards, so a
#     larger release was refused as non-monotonic (RC 3487).
# The defect moved from value width to length width because the fix each time
# was a new bespoke prefix. The authority states a FINITE accepted domain
# instead, and refuses anything outside it rather than mis-ordering it.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/release-version.sh"

CUR="${1:?usage: previous-published-tag.sh <current-tag>}"
release_version_valid "$CUR" \
  || { echo "previous-published-tag: current tag '$CUR' is outside the accepted release grammar (up to ${RV_MAX_DIGITS} digits per component, no leading zeros; first refused $(release_version_first_refused))" >&2; exit 2; }

max=""
while IFS= read -r tag; do
  [ -n "$tag" ] || continue
  # Ignore lines the shared grammar does not accept — the SAME predicate the
  # consumer applies, so the producer cannot select a baseline the consumer
  # could not compare.
  release_version_valid "$tag" || continue
  if [ -z "$max" ] || release_version_gt "$tag" "$max"; then max="$tag"; fi
done

[ -n "$max" ] || exit 0  # no prior published stable -> caller falls back to repo root

if release_version_gt "$CUR" "$max"; then
  printf '%s\n' "$max"
else
  echo "previous-published-tag: $CUR is not greater than the max published stable release $max (non-monotonic: re-release or out-of-order)" >&2
  exit 1
fi
