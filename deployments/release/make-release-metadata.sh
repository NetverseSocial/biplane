#!/usr/bin/env bash
# biplane release pipeline (BIP-40 / M5.1): assemble the UNSIGNED release
# metadata the update check (M5.2) reads. No signature, no keys, no bundles —
# image digests are the executable identity (Scope-A rewrite 2026-08-12, §M5).
# Fails closed on anything the consumer would reject.
#
#   make-release-metadata.sh --tag <tag> --commit <40hex> \
#                            --level <code|data|full> --images <images.json> \
#                            --out <dir>            -> writes <dir>/release.json
#
# release.json schema (the producer->consumer contract):
#   { schema_version: 1, tag, commit_sha, level, images: [{image, digest}] }
set -euo pipefail
TAG=""; COMMIT=""; LEVEL=""; IMAGES=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tag)     TAG="$2";     shift 2 ;;
    --commit)  COMMIT="$2";  shift 2 ;;
    --level)   LEVEL="$2";   shift 2 ;;
    --images)  IMAGES="$2";  shift 2 ;;
    --out)     OUT="$2";     shift 2 ;;
    *) echo "make-release-metadata: unknown arg '$1'" >&2; exit 2 ;;
  esac
done
[ -n "$TAG" ]    || { echo "make-release-metadata: --tag required" >&2; exit 2; }
[ -n "$OUT" ]    || { echo "make-release-metadata: --out required" >&2; exit 2; }
[ -n "$IMAGES" ] && [ -f "$IMAGES" ] || { echo "make-release-metadata: --images <file> required" >&2; exit 2; }

# level: exactly one of the three (decides how the operator applies the update)
case "$LEVEL" in code|data|full) ;; *) echo "make-release-metadata: level must be code|data|full, got '$LEVEL'" >&2; exit 1 ;; esac
# commit: full 40-hex only — a short sha is an ambiguous identity
printf '%s' "$COMMIT" | grep -qE '^[0-9a-f]{40}$' || { echo "make-release-metadata: commit must be 40-hex, got '$COMMIT'" >&2; exit 1; }
# images: non-empty array of {image:non-empty, digest:sha256:<64hex>}; else fail closed
jq -e 'type=="array" and length>0 and all(.[]; (.image|type=="string" and length>0) and (.digest|type=="string" and test("^sha256:[0-9a-f]{64}$")))' "$IMAGES" >/dev/null \
  || { echo "make-release-metadata: images.json must be a non-empty array of {image, sha256:<64hex> digest}" >&2; exit 1; }

# The apply path pulls EXACTLY these four executing services by digest; a
# missing, extra, or duplicated image publishes a release that cannot be applied
# (Morrow RC 3406/3438). Enforce the exact set at the producer boundary, keyed on
# the image basename (registry/owner prefix ignored).
_want="$(printf '%s\n' biplane-backend biplane-web biplane-admin biplane-space | sort)"
_got="$(jq -r '.[].image' "$IMAGES" | sed 's#.*/##' | sort)"
if [ "$_got" != "$_want" ]; then
  echo "make-release-metadata: images must be exactly biplane-{backend,web,admin,space} once each; got:" >&2
  printf '%s\n' "$_got" | sed 's/^/  /' >&2
  exit 1
fi

mkdir -p "$OUT"
jq -n --arg tag "$TAG" --arg commit "$COMMIT" --arg level "$LEVEL" --slurpfile images "$IMAGES" \
  '{schema_version:1, tag:$tag, commit_sha:$commit, level:$level, images:$images[0]}' > "$OUT/release.json"
echo "wrote $OUT/release.json"
