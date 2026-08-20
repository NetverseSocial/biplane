#!/usr/bin/env bash
# Test harness for the BIP-40 release pipeline scripts, in the
# build-images.test.sh idiom: real invocations, violating cases first-class.
# Run from anywhere: cd's to its own directory. Requires: git, jq.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); echo "  ok   - $*"; }
no() { FAIL=$((FAIL+1)); echo "  FAIL - $*"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# --- fixture: a scratch git repo with classified commits --------------------
R="$TMP/repo"; mkdir -p "$R"; cd "$R" || exit 1
git init -q -b main; git config user.email t@t.invalid; git config user.name t
mkdir -p apps/api/plane/db/migrations apps/api/requirements deployments/selfhost
echo base > f.txt; git add -A; git commit -qm base; git tag v0
echo code > apps/api/plane/views.py; git add -A; git commit -qm code; git tag c1
echo mig > apps/api/plane/db/migrations/0999_x.py; git add -A; git commit -qm mig; git tag c2
echo dep > apps/api/requirements/base.txt; git add -A; git commit -qm dep; git tag c3
# The hash-locked file the API image installs from. A lock-only change must
# derive `full`: it is a dependency change even when no source pin moved.
echo lock > apps/api/requirements.lock; git add -A; git commit -qm lock; git tag c4

# --- 1. derive-level --------------------------------------------------------
[ "$("$HERE/derive-level.sh" v0 c1)" = "code" ] && ok "code-only diff derives code" || no "code diff must derive code"
[ "$("$HERE/derive-level.sh" c1 c2)" = "data" ] && ok "migration derives data" || no "migration must derive data"
[ "$("$HERE/derive-level.sh" c2 c3)" = "full" ] && ok "dependency change derives full" || no "deps must derive full"
[ "$("$HERE/derive-level.sh" v0 c3)" = "full" ] && ok "mixed diff takes the maximum (full)" || no "mixed must take max"
# The lock file is the one the image installs from, and it was in NEITHER of
# the old dependency patterns — a lock-only change derived `code` and skipped
# the gate. This is the witness for that: it fails if the pattern narrows back.
[ "$("$HERE/derive-level.sh" c3 c4)" = "full" ] && ok "lock-only change derives full" || no "requirements.lock must derive full"

# --- 2. enforce-level -------------------------------------------------------
"$HERE/enforce-level.sh" code data >/dev/null 2>&1 && no "under-label code<data must be REFUSED" || ok "under-label code<data refused"
"$HERE/enforce-level.sh" full data >/dev/null 2>&1 && ok "over-label full>=data accepted" || no "full>=data must be accepted"
"$HERE/enforce-level.sh" data data >/dev/null 2>&1 && ok "exact label accepted" || no "exact label must be accepted"
"$HERE/enforce-level.sh" bogus data >/dev/null 2>&1 && no "unknown level must not pass" || ok "unknown level rejected"

# --- 3. make-release-metadata: happy path + schema validation ---------------
cd "$TMP" || exit 1
cat > images.json <<'EOF'
[{"image":"ghcr.io/x/biplane-backend","digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
 {"image":"ghcr.io/x/biplane-web","digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
 {"image":"ghcr.io/x/biplane-admin","digest":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
 {"image":"ghcr.io/x/biplane-space","digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}]
EOF
COMMIT="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"$HERE/make-release-metadata.sh" --tag v1.2.3 --commit "$COMMIT" --level data --images images.json --out "$TMP/rm" >/dev/null 2>&1
RJ="$TMP/rm/release.json"
[ -s "$RJ" ] && [ "$(jq -r .schema_version "$RJ")" = "1" ] && [ "$(jq -r .tag "$RJ")" = "v1.2.3" ] \
  && [ "$(jq -r .commit_sha "$RJ")" = "$COMMIT" ] && [ "$(jq -r .level "$RJ")" = "data" ] \
  && [ "$(jq -r '.images | length' "$RJ")" = "4" ] \
  && ok "release.json carries schema_version/tag/commit/level/images" || no "release.json fields wrong"
# NO signature / bundle / key assets anywhere in the output (the whole point of the rewrite)
[ -z "$(find "$TMP/rm" \( -name '*.sig' -o -name '*bundle*' -o -name '*.key' \) -print -quit)" ] \
  && ok "no signature/bundle/key assets produced" || no "signing artifacts must not exist"
# invalid level refused
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level urgent --images images.json --out "$TMP/rm2" >/dev/null 2>&1 \
  && no "invalid level must be refused" || ok "invalid level refused"
# short commit refused
"$HERE/make-release-metadata.sh" --tag v1 --commit SHORTSHA --level code --images images.json --out "$TMP/rm3" >/dev/null 2>&1 \
  && no "short commit sha must be refused" || ok "non-40-hex commit refused"
# non-64-hex image digest refused
echo '[{"image":"ghcr.io/x/y","digest":"sha256:x"}]' > bad-images.json
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level code --images bad-images.json --out "$TMP/rm4" >/dev/null 2>&1 \
  && no "non-64-hex image digest must be refused" || ok "non-64-hex image digest refused"
# empty images array refused
echo '[]' > empty-images.json
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level code --images empty-images.json --out "$TMP/rm5" >/dev/null 2>&1 \
  && no "empty images array must be refused" || ok "empty images array refused"
# missing image field refused
echo '[{"digest":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}]' > noimg.json
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level code --images noimg.json --out "$TMP/rm6" >/dev/null 2>&1 \
  && no "image entry without an image field must be refused" || ok "image entry missing image field refused"
# exact service set (Morrow RC 3406/3438): missing / extra / duplicate refused
jq '[.[0]]' images.json > backend-only.json
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level code --images backend-only.json --out "$TMP/rm7" >/dev/null 2>&1 \
  && no "backend-only (missing services) must be refused" || ok "missing-service set refused"
jq '. + [{image:"ghcr.io/x/biplane-extra",digest:"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}]' images.json > extra-svc.json
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level code --images extra-svc.json --out "$TMP/rm8" >/dev/null 2>&1 \
  && no "an extra service must be refused" || ok "extra-service set refused"
jq '. + [.[0]]' images.json > dup-svc.json
"$HERE/make-release-metadata.sh" --tag v1 --commit "$COMMIT" --level code --images dup-svc.json --out "$TMP/rm9" >/dev/null 2>&1 \
  && no "a duplicate service must be refused" || ok "duplicate-service set refused"

# --- 4. changelog exact-entry parse -----------------------------------------
cat > CL.md <<'CLEOF'
# Changelog
## [Unreleased]
### Added
- wip
CLEOF
"$HERE/parse-changelog.sh" CL.md v1.0.0 >/dev/null 2>&1 \
  && no "no entry for the tag must FAIL (not default to code)" || ok "missing changelog entry fails the release"
cat >> CL.md <<'CLEOF'

## [v1.0.0] - 2026-08-11  (level: data)
### Added
- the thing
CLEOF
if body=$("$HERE/parse-changelog.sh" CL.md v1.0.0 2>"$TMP/cl.err"); then
  echo "$body" | grep -q "the thing" && grep -q "level=data" "$TMP/cl.err" \
    && ok "valid entry parses body + explicit level" || no "entry body/level wrong"
else no "valid changelog entry must parse"; fi
# entry with no level is refused
printf '\n## [v2.0.0] - 2026-08-11\n### Added\n- no level\n' >> CL.md
"$HERE/parse-changelog.sh" CL.md v2.0.0 >/dev/null 2>&1 \
  && no "entry without explicit level must FAIL" || ok "level-less entry refused"

# an UNFILLED placeholder must fail the release rather than reach operators.
# The positive control below is the load-bearing half: a guard that refused
# everything would satisfy the negative on its own.
printf '\n## [v3.0.0] - 2026-08-11  (level: code)\n### Added\n- FILL-AT-TAG migration count\n' >> CL.md
"$HERE/parse-changelog.sh" CL.md v3.0.0 >/dev/null 2>&1 \
  && no "an entry carrying FILL-AT-TAG must FAIL" || ok "unfilled placeholder refused"
printf '\n## [v4.0.0] - 2026-08-11  (level: code)\n### Added\n- five migrations\n' >> CL.md
"$HERE/parse-changelog.sh" CL.md v4.0.0 >/dev/null 2>&1 \
  && ok "a FILLED entry still publishes" || no "the placeholder guard must not refuse a finished entry"

# --- 5. registry digest exact-record parse ----------------------------------
cat > push.log <<'PLEOF'
The push refers to repository [ghcr.io/x/biplane-backend]
deadbeef: Pushed sha256:1111111111111111111111111111111111111111111111111111111111111111
v1.0.0: digest: sha256:abababababababababababababababababababababababababababababababab size: 1234
PLEOF
d=$("$HERE/parse-push-digest.sh" push.log 2>/dev/null) || d=""
[ "$d" = "sha256:abababababababababababababababababababababababababababababababab" ] \
  && ok "digest parsed from the exact record, ignoring an earlier misleading sha" \
  || no "must parse the final digest record, got '$d'"
# ambiguous (two digest records) fails closed
printf 'v1: digest: sha256:%s size: 1\nv2: digest: sha256:%s size: 1\n' "$(printf a%.0s $(seq 64))" "$(printf b%.0s $(seq 64))" > push2.log
"$HERE/parse-push-digest.sh" push2.log >/dev/null 2>&1 \
  && no "two digest records must fail closed" || ok "ambiguous push digest refused"

# --- 6. validate-tag: comparable stable release tags only (RC 3412) --------
"$HERE/validate-tag.sh" v1.2.3 >/dev/null 2>&1 && ok "v1.2.3 accepted" || no "v1.2.3 must be accepted"
"$HERE/validate-tag.sh" v10.20.30 >/dev/null 2>&1 && ok "v10.20.30 accepted" || no "v10.20.30 must be accepted"
"$HERE/validate-tag.sh" vgarbage >/dev/null 2>&1 && no "vgarbage must be refused" || ok "vgarbage refused"
"$HERE/validate-tag.sh" v1.2 >/dev/null 2>&1 && no "two-part v1.2 must be refused" || ok "two-part v1.2 refused"
"$HERE/validate-tag.sh" v1.2.3-rc1 >/dev/null 2>&1 && no "prerelease must be refused" || ok "prerelease v1.2.3-rc1 refused"
"$HERE/validate-tag.sh" 1.2.3 >/dev/null 2>&1 && no "missing-v prefix must be refused" || ok "missing-v 1.2.3 refused"
"$HERE/validate-tag.sh" "" >/dev/null 2>&1 && no "empty tag must be refused" || ok "empty tag refused"

# --- 7. previous-published-tag: MAX published stable + monotonic (RC 3467 + next layer) --
[ "$(printf 'v1.1.0\nv2.0.0\nv1.5.0\n' | "$HERE/previous-published-tag.sh" v3.0.0)" = "v2.0.0" ] \
  && ok "PREV is the MAX published stable, regardless of publication order" || no "must pick max version"
# The FINITE accepted domain, stated by the authority and pinned here so the
# contract, the helper and the suite say the same thing (Morrow RC 3487).
#
# The old INT32 cases are gone deliberately. They asserted correct ORDERING of
# 10-digit components, which required an unbounded comparator — and every
# unbounded comparator we wrote had an undeclared bound instead (%d clamped at
# INT32_MAX; the %03d length key clamped at 999 digits). The domain is now
# bounded on purpose, so those values are REFUSED rather than ordered, and a
# value outside the grammar can never be mis-ordered because it never enters.
. "$HERE/release-version.sh"
UPPER="$(release_version_upper_accepted)"; FIRST_REFUSED="$(release_version_first_refused)"
"$HERE/validate-tag.sh" "$UPPER" >/dev/null 2>&1 \
  && ok "upper accepted boundary $UPPER is accepted" || no "upper accepted boundary must be accepted"
"$HERE/validate-tag.sh" "$FIRST_REFUSED" >/dev/null 2>&1 \
  && no "first refused $FIRST_REFUSED must be refused" || ok "first refused $FIRST_REFUSED is refused"
"$HERE/previous-published-tag.sh" "$FIRST_REFUSED" </dev/null >/dev/null 2>&1 \
  && no "selection must refuse a current tag outside the domain" || ok "selection refuses a current tag outside the domain"
# Ordering INSIDE the domain, at its widest — the property the INT32 cases were
# really protecting, expressed against the boundary that actually exists.
NINES="${UPPER#v}"; NINES="${NINES%%.*}"
[ "$(printf 'v%s.0.0\n' "$((10#0${NINES:0:8}))" | "$HERE/previous-published-tag.sh" "v${NINES}.0.0")" = "v${NINES:0:8}.0.0" ] \
  && ok "orders correctly at the widest accepted component" || no "must order at the widest accepted component"
# Producer and consumer must agree on leading zeros — they had already drifted
# (Vex, executed cross-PR table: v1.01.0 accepted here, refused by the consumer,
# which forces UNKNOWN forever once published).
for z in v1.01.0 v01.1.0 v1.1.00; do
  "$HERE/validate-tag.sh" "$z" >/dev/null 2>&1 \
    && no "leading-zero tag $z must be refused (consumer cannot compare it)" \
    || ok "leading-zero tag $z refused, matching the consumer"
done
# multi-digit component ordering (v1.2.9 < v1.2.10), which %010d happened to get right but is worth pinning
[ "$(printf 'v1.2.9\n' | "$HERE/previous-published-tag.sh" v1.2.10)" = "v1.2.9" ] \
  && ok "orders v1.2.9 < v1.2.10 (multi-digit component)" || no "must order multi-digit components"
printf 'v2.0.0\nv1.5.0\n' | "$HERE/previous-published-tag.sh" v1.3.0 >/dev/null 2>&1 \
  && no "out-of-order (current < max) must be refused" || ok "out-of-order release refused (non-monotonic)"
printf 'v2.0.0\nv1.5.0\n' | "$HERE/previous-published-tag.sh" v2.0.0 >/dev/null 2>&1 \
  && no "re-release of a published version must be refused" || ok "re-release refused (non-monotonic)"
[ "$(printf 'v2.0.0\nv1.5.0\n' | "$HERE/previous-published-tag.sh" v2.1.0)" = "v2.0.0" ] \
  && ok "current > max -> PREV is the max published stable" || no "must return max"
[ -z "$(printf '' | "$HERE/previous-published-tag.sh" v1.0.0)" ] \
  && ok "no prior published stable -> empty (fall back to root)" || no "must be empty when none"
[ "$(printf 'vgarbage\nv1.2.0\n' | "$HERE/previous-published-tag.sh" v1.3.0)" = "v1.2.0" ] \
  && ok "non-semver/refused line ignored in the max computation" || no "non-semver must be ignored"

# --- 9. preflight: the gate must actually be REACHABLE from the manual path --
# Vex's finding: every gate below was enforced only by a workflow that has never
# run and cannot. These cases assert that preflight.sh CALLS them — a guard with
# no caller is not a guard, and that is what these prove is fixed.
PF="$TMP/pf"; mkdir -p "$PF/repo"; cd "$PF/repo"
git init -q .; git config user.email a@b.invalid; git config user.name t
mkdir -p deployments/release; cp "$HERE"/*.sh deployments/release/
# validate-tag.sh sources the semver rules from the API tree, not from this
# directory — the ONE shared definition, so the gate and the consumer can never
# disagree. The fixture must carry it or preflight refuses every tag and the
# positive control below fails for the fixture's reason rather than the code's.
mkdir -p apps/api/plane/license/utils
cp -R "$HERE/../../apps/api/plane/license/utils/." apps/api/plane/license/utils/
cat > CHANGELOG.md <<'PFEOF'
# Changelog

## [Unreleased]

## [v1.0.0] - 2026-08-11  (level: full)
### Added
- a real entry
PFEOF
git add -A >/dev/null; git commit -qm "seed"
git branch -f main HEAD; git remote add origin . 2>/dev/null || true
git update-ref refs/remotes/origin/main HEAD
: > "$PF/none.txt"
export RELEASE_TAGS_FILE="$PF/none.txt" MAIN_REF=refs/remotes/origin/main

if RELEASE_TAGS_FILE="$PF/none.txt" bash deployments/release/preflight.sh v1.0.0 "$PF/out" >/dev/null 2>&1; then
  ok "preflight passes a well-formed release and emits notes + level"
  [ -s "$PF/out/release-notes.md" ] && grep -q '^full$' "$PF/out/level" \
    && ok "preflight writes the notes body and the declared level" \
    || no "preflight must emit notes + level"
else no "preflight must pass a well-formed release"; fi

# THE ONE THAT MATTERS: an unfinished entry must stop the release HERE, in the
# path a human walks — not only inside a script nobody calls.
sed -i 's/- a real entry/- FILL-AT-TAG migration count/' CHANGELOG.md
git commit -aqm "unfinished"; git branch -f main HEAD; git update-ref refs/remotes/origin/main HEAD
RELEASE_TAGS_FILE="$PF/none.txt" bash deployments/release/preflight.sh v1.0.0 "$PF/out2" >/dev/null 2>&1 \
  && no "preflight must REFUSE an entry still carrying FILL-AT-TAG" \
  || ok "preflight refuses an unfinished changelog entry"

# a bad tag is refused before anything else happens
RELEASE_TAGS_FILE="$PF/none.txt" bash deployments/release/preflight.sh v1.2 "$PF/out3" >/dev/null 2>&1 \
  && no "preflight must refuse a non-comparable tag" || ok "preflight refuses a non-comparable tag"

# --- 10. the NON-EMPTY baseline path (Vex RC 3773) ---------------------------
# Every case above uses an EMPTY releases listing, so PREV is always empty and
# only the root-baseline branch runs. The non-empty baseline — the whole point
# of deriving from the last PUBLISHED release, Morrow RC 3467 — had zero
# coverage, and it was broken: the API returns a tag NAME that need not exist
# locally, and an unresolvable one reached git as `v1.0.0..<sha>` and exited 128
# with no refusal line. Both arms, because a step that refused every baseline
# would satisfy the negative alone.
sed -i 's/- FILL-AT-TAG migration count/- a real entry again/' CHANGELOG.md
git commit -aqm "finished"; git branch -f main HEAD; git update-ref refs/remotes/origin/main HEAD
printf 'v0.9.0\n' > "$PF/have.txt"
git tag v0.9.0 HEAD                     # the baseline EXISTS locally
if RELEASE_TAGS_FILE="$PF/have.txt" bash deployments/release/preflight.sh v1.0.0 "$PF/out4" >"$PF/o4" 2>&1; then
  grep -q "baseline is the max published stable, v0.9.0" "$PF/o4" \
    && ok "preflight derives from a RESOLVABLE published baseline" \
    || no "preflight must baseline from the published tag, not the root"
else no "preflight must pass with a resolvable published baseline: $(tail -2 "$PF/o4")"; fi

git tag -d v0.9.0 >/dev/null           # the baseline is now UNRESOLVABLE
RELEASE_TAGS_FILE="$PF/have.txt" bash deployments/release/preflight.sh v1.0.0 "$PF/out5" >"$PF/o5" 2>&1 \
  && no "preflight must refuse an unresolvable baseline tag" \
  || ok "preflight refuses an unresolvable baseline tag"
grep -q "PREFLIGHT REFUSED" "$PF/o5" \
  && ok "the unresolvable baseline refusal NAMES ITSELF (not a raw git error)" \
  || no "a refusal must name its reason, got: $(tail -2 "$PF/o5")"
cd "$TMP"

echo; echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
