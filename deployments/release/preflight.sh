#!/usr/bin/env bash
# biplane release preflight (BIP-40): RUN EVERY RELEASE GATE, IN ORDER, FROM THE
# PATH A HUMAN ACTUALLY WALKS.
#
# WHY THIS EXISTS. The gate scripts beside this one — validate-tag,
# parse-changelog, derive-level, enforce-level — were correct, self-tested, and
# called by exactly ONE thing: .github/workflows/release.yml. That workflow has
# never executed and, with zero registered runners, cannot. Outside it the
# scripts appear only in the README, as a table of what they do (Vex, 2026-08-15).
#
# So the entire release gate was enforced by a caller that does not run, while
# the README described it as a live guarantee. That is the same defect class as
# the release note claiming every bridge write shipped off — a control read from
# the design and asserted as behaviour — one layer up, in the document that
# tells a human how to release. A mechanism with no production caller is not a
# mechanism; the rule existed everywhere except in the product.
#
# This script is the missing caller. It does no building and no publishing: it
# refuses, or it prints the two facts the release needs (the notes and the level).
# Run it FIRST and the gate is real; skip it and you are releasing by memory.
#
#   preflight.sh <tag> <outdir>
#     writes <outdir>/release-notes.md and <outdir>/level
#     non-zero exit = DO NOT RELEASE, with the reason on stderr
#
# Env:
#   FORGEJO_API   base API url, e.g. http://forge.example.com:3000/api/v1   (required
#                 unless RELEASE_TAGS_FILE is set)
#   FORGEJO_REPO  owner/repo, e.g. example/biplane                       (as above)
#   FORGEJO_TOKEN read token for the releases listing                (as above)
#   RELEASE_TAGS_FILE  published stable tags, one per line — used by the
#                 self-test, and by an operator who has the listing already.
#   MAIN_REF      protected branch ref to prove ancestry against (default
#                 origin/main).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="${1:?usage: preflight.sh <tag> <outdir>}"
OUT="${2:?usage: preflight.sh <tag> <outdir>}"
MAIN_REF="${MAIN_REF:-origin/main}"
LIMIT=1000

mkdir -p "$OUT"
say() { printf '%s\n' "$*" >&2; }
die() { printf 'PREFLIGHT REFUSED: %s\n' "$*" >&2; exit 1; }

# --- 1. the tag is a comparable stable semver -------------------------------
# A tag the update check can never compare publishes a release that forces
# UNKNOWN forever (Rowan RC 3412). Refuse it before it exists.
"$HERE/validate-tag.sh" "$TAG" >/dev/null || die "tag $TAG is not a comparable stable semver"
say "ok  - tag $TAG is a comparable stable semver"

# --- 2. the tagged commit is on protected main ------------------------------
# Prove ancestry BEFORE anything builds or publishes, so a tag pushed on an
# off-main tree cannot get a legitimate artifact issued against it.
# --verify --quiet, NOT a bare rev-parse with stderr redirected: on an
# unresolvable name a bare rev-parse ECHOES THAT NAME ON STDOUT and then exits
# non-zero, so the fallback appends to it and SHA becomes two lines. The
# resulting refusal names a "commit" that is the tag string followed by a real
# sha, which reads like a repository problem rather than a quoting one. Caught
# by this script's own positive control.
#
# Preflight normally runs BEFORE the tag exists — that is the point of a
# preflight — so an absent tag falls back to HEAD and is not an error.
if SHA="$(git rev-parse --verify --quiet "${TAG}^{commit}")"; then
  say "ok  - $TAG already exists; checking the tagged commit"
else
  SHA="$(git rev-parse --verify HEAD)"
  say "ok  - $TAG does not exist yet; checking HEAD as the prospective tag commit"
fi
git fetch --no-tags -q origin main 2>/dev/null || say "warn- could not fetch origin main; comparing against local $MAIN_REF"
git merge-base --is-ancestor "$SHA" "$MAIN_REF" \
  || die "$TAG ($SHA) is not an ancestor of $MAIN_REF"
say "ok  - $TAG ($SHA) is on protected main"

# --- 3. exactly one changelog entry, with an explicit level -----------------
# Also where the FILL-AT-TAG refusal fires: an entry drafted before the tag
# existed must be finished against the tagged tree, and this is the call that
# makes that refusal REACHABLE rather than notional.
if ! body="$("$HERE/parse-changelog.sh" CHANGELOG.md "$TAG" 2>"$OUT/.cl.err")"; then
  die "$(tail -1 "$OUT/.cl.err")"
fi
DECLARED="$(grep '^level=' "$OUT/.cl.err" | tail -1 | cut -d= -f2)"
[ -n "$DECLARED" ] || die "the changelog entry for $TAG produced no level"
printf '%s\n' "$body" > "$OUT/release-notes.md"
say "ok  - changelog entry parsed, declared level '$DECLARED'"

# --- 4. baseline = the last SUCCESSFULLY PUBLISHED release ------------------
# NOT any reachable tag: a refused or build-failed tag still sits in the repo,
# and git describe would pick it and silently narrow the diff window,
# under-deriving the level (Morrow RC 3467). A truncated listing cannot prove
# the max published stable was seen, so refuse at the cap rather than baseline
# against a partial set (RC 3482).
if [ -n "${RELEASE_TAGS_FILE:-}" ]; then
  TAGS="$(cat "$RELEASE_TAGS_FILE")"
else
  : "${FORGEJO_API:?set FORGEJO_API or RELEASE_TAGS_FILE}"
  : "${FORGEJO_REPO:?set FORGEJO_REPO or RELEASE_TAGS_FILE}"
  : "${FORGEJO_TOKEN:?set FORGEJO_TOKEN or RELEASE_TAGS_FILE}"
  # Forgejo, not gh: the release listing authority is the instance we publish to.
  TAGS="$(curl -sfS -H "Authorization: token $FORGEJO_TOKEN" \
      "$FORGEJO_API/repos/$FORGEJO_REPO/releases?limit=$LIMIT&draft=false&pre-release=false" \
      | python3 -c 'import sys,json
for r in json.load(sys.stdin):
    if not r.get("draft") and not r.get("prerelease"):
        print(r["tag_name"])')" \
    || die "could not list published releases"
fi
COUNT="$(printf '%s\n' "$TAGS" | grep -c . || true)"
[ "$COUNT" -lt "$LIMIT" ] \
  || die "release listing returned $COUNT == limit $LIMIT — cannot prove the max published stable was seen"

if ! PREV="$(printf '%s\n' "$TAGS" | "$HERE/previous-published-tag.sh" "$TAG")"; then
  die "$TAG is not greater than the max published stable release (non-monotonic)"
fi
if [ -z "$PREV" ]; then
  PREV="$(git rev-list --max-parents=0 HEAD | tail -1)"
  say "ok  - no prior published stable; baselining from the repository root"
else
  # THE BASELINE IS A TAG NAME FROM THE RELEASES API; IT NEED NOT EXIST LOCALLY.
  # Step 2 fetches --no-tags, and a fresh clone or a CI-style checkout may carry
  # no tags at all. Handing an unresolvable name straight to derive-level makes
  # git exit 128 with "ambiguous argument 'v1.0.0..<sha>'" and NO refusal line —
  # breaking this script's own contract that a non-zero exit names its reason,
  # and handing the operator a git-internals message that reads like a corrupt
  # repository. Same failure SHAPE as the rev-parse defect above, one step later
  # (Vex RC 3773).
  #
  # It fails CLOSED either way, so nothing can publish wrongly. What is fixed
  # here is that it now REFUSES BY NAME, and tries the one recovery that works.
  if ! git rev-parse --verify --quiet "${PREV}^{commit}" >/dev/null; then
    say "warn- baseline tag $PREV is not present locally; fetching it"
    git fetch --quiet origin "refs/tags/${PREV}:refs/tags/${PREV}" 2>/dev/null || true
  fi
  git rev-parse --verify --quiet "${PREV}^{commit}" >/dev/null \
    || die "the last published release is tagged $PREV, but that tag cannot be resolved in this checkout — fetch it before releasing (git fetch origin refs/tags/${PREV}:refs/tags/${PREV}); refusing rather than baselining against the wrong point"
  say "ok  - baseline is the max published stable, $PREV"
fi

# --- 5. the declared level must not be below the derived minimum ------------
DERIVED="$("$HERE/derive-level.sh" "$PREV" "$SHA")"
"$HERE/enforce-level.sh" "$DECLARED" "$DERIVED" >/dev/null \
  || die "declared level '$DECLARED' is below the derived minimum '$DERIVED'"
printf '%s\n' "$DECLARED" > "$OUT/level"
say "ok  - level '$DECLARED' >= derived minimum '$DERIVED'"

rm -f "$OUT/.cl.err"
say "PREFLIGHT PASSED — notes in $OUT/release-notes.md, level in $OUT/level"
