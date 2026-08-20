#!/usr/bin/env bash
# biplane release pipeline (BIP-40): extract EXACTLY the one changelog entry for
# a release tag, with its explicit level (Morrow RC 3336). The old gate
# defaulted a missing level to `code` and published the whole file — a
# code-only tag could publish with no real entry even though the file calls
# that invalid. This fails closed instead.
#
#   parse-changelog.sh <CHANGELOG.md> <tag>
#     stdout: the entry body (published as the release notes)
#     stderr + non-zero exit: missing / duplicate / level-less entry
#     also prints "level=<code|data|full>" as the LAST stderr line for the caller
set -euo pipefail

FILE="${1:?usage: parse-changelog.sh <CHANGELOG.md> <tag>}"
TAG="${2:?usage: parse-changelog.sh <CHANGELOG.md> <tag>}"
[ -f "$FILE" ] || { echo "changelog: $FILE not found" >&2; exit 1; }

# A release entry heading: "## [<tag>] - <date>  (level: <code|data|full>)".
# The tag must match EXACTLY (not [Unreleased], not a different version).
esc_tag=$(printf '%s' "$TAG" | sed 's/[].[^$*\/]/\\&/g')
heading_re="^## \[${esc_tag}\]"

count=$(grep -cE "$heading_re" "$FILE" || true)
[ "$count" -eq 1 ] || { echo "changelog: expected exactly ONE entry for $TAG, found $count" >&2; exit 1; }

# the heading line, and its level (REQUIRED — no default)
heading=$(grep -nE "$heading_re" "$FILE" | head -1)
lineno=${heading%%:*}
level=$(printf '%s' "$heading" | grep -oiE '\(level: *(code|data|full) *\)' | grep -oiE 'code|data|full' | tr 'A-Z' 'a-z' || true)
[ -n "$level" ] || { echo "changelog: entry for $TAG has no explicit (level: code|data|full)" >&2; exit 1; }

# body = lines after the heading up to (but not including) the next "## " heading
body=$(awk -v start="$lineno" 'NR>start { if ($0 ~ /^## /) exit; print }' "$FILE")
[ -n "$(printf '%s' "$body" | tr -d '[:space:]')" ] || { echo "changelog: entry for $TAG has an empty body" >&2; exit 1; }

# An entry is DRAFTED before the tag exists and finished against the tagged
# tree, so some values — migration counts, dates — cannot be correct until the
# moment of the tag. Marking those with FILL-AT-TAG and refusing to publish
# while a marker survives makes forgetting IMPOSSIBLE instead of merely
# discouraged (Vex RC 3772).
#
# The failure this closes is not hypothetical: this same extractor was
# publishing the changelog's own entry TEMPLATE as part of the release notes,
# because a thing meant for authors sat where the notes are read from. An
# unfilled placeholder is that defect wearing different clothes, and it would
# reach operators rather than reviewers.
if printf '%s\n' "$body" | grep -qF 'FILL-AT-TAG'; then
  echo "changelog: entry for $TAG still carries a FILL-AT-TAG marker — finish it against the tagged tree before publishing" >&2
  exit 1
fi

printf '%s\n' "$body"
echo "level=$level" >&2
