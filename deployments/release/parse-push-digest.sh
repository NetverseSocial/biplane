#!/usr/bin/env bash
# biplane release pipeline (BIP-40): extract the ONE authoritative image digest
# from `docker push` output, then require the caller to confirm it by reading
# the digest back FROM the registry (Morrow RC 3336). The old code trusted the
# first sha256-looking token in stdout — an attacker-influenced layer line or a
# log prefix could smuggle a different digest before the real one.
#
#   parse-push-digest.sh <push-output-file>
#     stdout: the single sha256:<64hex> from the final "digest:" record
#     non-zero: zero or more-than-one digest records (ambiguous → fail closed)
#
# Docker/buildx writes the pushed manifest digest as: "<ref>: digest: sha256:.. size: .."
# We parse THAT record, not any sha256 substring.
set -euo pipefail
OUT="${1:?usage: parse-push-digest.sh <push-output-file>}"
[ -f "$OUT" ] || { echo "push-digest: $OUT not found" >&2; exit 1; }

# only lines with the explicit "digest: sha256:<64hex>" record
digests=$(grep -oE 'digest: sha256:[0-9a-f]{64}' "$OUT" | awk '{print $2}' | sort -u)
n=$(printf '%s\n' "$digests" | grep -c . || true)
[ "$n" -eq 1 ] || { echo "push-digest: expected exactly ONE digest record, found $n" >&2; exit 1; }
printf '%s\n' "$digests"
