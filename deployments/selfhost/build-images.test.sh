#!/usr/bin/env bash
# Executable bar for build-images.sh (Morrow RC 3208).
#
# The claim being pinned is "the build shown in the UI identifies the running
# image". That rests on four behaviours, each checked here by RUNNING the
# script against a stub `docker` — no images are built, so this is fast and
# needs no daemon:
#
#   1. clean tree      -> build id is the commit, no -dirty suffix
#   2. dirty tree      -> refuses (non-zero), unless BIPLANE_ALLOW_DIRTY=1,
#                         which marks the id <sha>-dirty
#   3. dual arg        -> BOTH web and admin get --build-arg VITE_BIPLANE_BUILD
#   4. tag equality    -> the tag and the baked build id are the same value
#
#   ./deployments/selfhost/build-images.test.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/deployments/selfhost/build-images.sh"
PASS=0
FAIL=0

ok() { echo "  ok   - $1"; PASS=$((PASS + 1)); }
no() { echo "  FAIL - $1"; FAIL=$((FAIL + 1)); }

# A scratch clone so a dirty checkout of the real repo cannot affect the run.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone -q --no-hardlinks "$ROOT" "$WORK/repo" 2>/dev/null
cd "$WORK/repo"

# Test the WORKING-TREE script, not the committed one. The clone exists only
# to give the dirty-tree cases a scratch checkout; without this copy the suite
# would exercise HEAD's script and every uncommitted change — including a
# deliberate mutation — would be invisible. Committing it here keeps the clone
# clean so the clean-tree assertions stay meaningful.
cp "$ROOT/deployments/selfhost/build-images.sh" deployments/selfhost/build-images.sh
git add deployments/selfhost/build-images.sh
git -c user.email=test@biplane.invalid -c user.name=test commit -q -m "script under test" 2>/dev/null || true

# Stub docker: record the argv of every build, do nothing else.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
# Record BUILD and TAG both: a stub that logs only `build` cannot observe the
# alias step, so deleting the whole tag loop stayed green (Morrow).
case "${1:-}" in
  build | tag) echo "$*" >> "$DOCKER_LOG" ;;
esac
exit 0
STUB
chmod +x "$WORK/bin/docker"
export PATH="$WORK/bin:$PATH"

SHA="$(git rev-parse --short HEAD)"

# --- 1. clean tree ---------------------------------------------------------
export DOCKER_LOG="$WORK/clean.log"; : > "$DOCKER_LOG"
if bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1; then
  ok "clean tree builds"
else
  no "clean tree should build"
fi
grep -q -- "VITE_BIPLANE_BUILD=$SHA " "$DOCKER_LOG" && ok "clean build id is the commit" \
  || no "clean build id should be $SHA"
grep -q -- "-dirty" "$DOCKER_LOG" && no "clean build must not be marked dirty" \
  || ok "clean build carries no -dirty suffix"

# --- 3. dual arg -----------------------------------------------------------
web_arg=$(grep -c -- "Dockerfile.web" "$DOCKER_LOG")
web_with_build=$(grep -- "Dockerfile.web" "$DOCKER_LOG" | grep -c -- "VITE_BIPLANE_BUILD=$SHA")
adm_with_build=$(grep -- "Dockerfile.admin" "$DOCKER_LOG" | grep -c -- "VITE_BIPLANE_BUILD=$SHA")
[ "$web_arg" -ge 1 ] && [ "$web_with_build" -ge 1 ] && ok "web build receives the build arg" \
  || no "web build must receive VITE_BIPLANE_BUILD"
[ "$adm_with_build" -ge 1 ] && ok "admin build receives the build arg" \
  || no "admin build must receive VITE_BIPLANE_BUILD"

# The BACKEND must get it too (BIP-36, Morrow RC 3271). Without this the
# Instance.biplane_installed_build column existed and no repo-supported build
# could ever fill it — the same false green this harness exists to prevent for
# the UI, repeated one layer down.
api_with_build=$(grep -- "Dockerfile.api" "$DOCKER_LOG" | grep -c -- "BIPLANE_BUILD=$SHA")
[ "$api_with_build" -ge 1 ] && ok "backend build receives the build arg" \
  || no "backend build must receive BIPLANE_BUILD"
# ONE id across all four images: backend and frontends cannot disagree.
api_id=$(grep -- "Dockerfile.api" "$DOCKER_LOG" | grep -oE "[^V]BIPLANE_BUILD=[^ ]+" | head -1 | cut -d= -f2)
web_id=$(grep -- "Dockerfile.web" "$DOCKER_LOG" | grep -oE "VITE_BIPLANE_BUILD=[^ ]+" | head -1 | cut -d= -f2)
[ -n "$api_id" ] && [ "$api_id" = "$web_id" ] && ok "backend and web share one build id" \
  || no "backend id '$api_id' must equal web id '$web_id'"

# --- 4. tag equality -------------------------------------------------------
# Every tag written must carry the same id that was baked in.
bad_tags=$(grep "^build " "$DOCKER_LOG" | grep -oE "biplane-[a-z]+:[^ ]+" | grep -vc ":pi5-$SHA$")
[ "$bad_tags" -eq 0 ] && ok "image tags equal the baked build id (pi5-$SHA)" \
  || no "tags disagree with the baked id: $(grep -oE 'biplane-[a-z]+:[^ ]+' "$DOCKER_LOG" | sort -u | tr '\n' ' ')"

# --- 2. dirty tree ---------------------------------------------------------
echo "scratch" >> README.md   # make the tree dirty
export DOCKER_LOG="$WORK/dirty.log"; : > "$DOCKER_LOG"
if bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1; then
  no "dirty tree must be refused by default"
else
  ok "dirty tree is refused by default"
fi
[ ! -s "$DOCKER_LOG" ] && ok "refusal happens before any build runs" \
  || no "refusal must precede docker build"

export DOCKER_LOG="$WORK/override.log"; : > "$DOCKER_LOG"
if BIPLANE_ALLOW_DIRTY=1 bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1; then
  ok "BIPLANE_ALLOW_DIRTY=1 permits the build"
else
  no "BIPLANE_ALLOW_DIRTY=1 should permit the build"
fi
grep -q -- "VITE_BIPLANE_BUILD=$SHA-dirty " "$DOCKER_LOG" && ok "override marks the id <sha>-dirty" \
  || no "override must mark the id -dirty"
grep -qE -- "[^V]BIPLANE_BUILD=$SHA-dirty " "$DOCKER_LOG" && ok "backend id is marked -dirty too" \
  || no "backend BIPLANE_BUILD must be $SHA-dirty on a dirty build"
bad_dirty=$(grep "^build " "$DOCKER_LOG" | grep -oE "biplane-[a-z]+:[^ ]+" | grep -vc ":pi5-$SHA-dirty$")
[ "$bad_dirty" -eq 0 ] && ok "dirty tags equal the dirty build id" \
  || no "dirty tags must equal the dirty id"

# --- 5. untracked input is NOT clean (RC 3210) ---------------------------
git checkout -q -- README.md 2>/dev/null; git clean -qfd 2>/dev/null
mkdir -p apps/web/core/scratch
echo "export const x = 1;" > apps/web/core/scratch/untracked-input.ts
export DOCKER_LOG="$WORK/untracked.log"; : > "$DOCKER_LOG"
if bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1; then
  no "an untracked build input must be refused (Docker copies the tree, not the commit)"
else
  ok "untracked build input is refused"
fi
[ ! -s "$DOCKER_LOG" ] && ok "untracked refusal precedes any build" || no "untracked refusal must precede docker build"
export DOCKER_LOG="$WORK/untracked-override.log"; : > "$DOCKER_LOG"
BIPLANE_ALLOW_DIRTY=1 bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1
grep -q -- "VITE_BIPLANE_BUILD=$SHA-dirty " "$DOCKER_LOG" && ok "untracked + override marks the id -dirty" \
  || no "untracked override must mark -dirty"
rm -rf apps/web/core/scratch

# --- 6. an alias never replaces the build tag, and IS applied (RC 3210 + the
#        false-green Morrow found: the stub logged only `build`, so deleting
#        the whole docker-tag loop stayed green) --------------------------
export DOCKER_LOG="$WORK/alias.log"; : > "$DOCKER_LOG"
BIPLANE_IMAGE_TAG=latest bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1
built_with_latest=$(grep "^build " "$DOCKER_LOG" | grep -cE "biplane-[a-z]+:latest")
built_with_sha=$(grep "^build " "$DOCKER_LOG" | grep -cE "biplane-[a-z]+:pi5-$SHA")
[ "$built_with_latest" -eq 0 ] && ok "an alias never REPLACES the build tag in docker build" \
  || no "alias must not replace the build tag (found $built_with_latest)"
[ "$built_with_sha" -ge 4 ] && ok "every image is still built under the build id" \
  || no "images must be built under pi5-$SHA (saw $built_with_sha)"
grep -q -- "VITE_BIPLANE_BUILD=$SHA " "$DOCKER_LOG" && ok "baked id matches the build tag under an alias" \
  || no "baked id must match the build tag"

# The alias must actually be APPLIED, and each image must be tagged from
# ITSELF. Morrow: a regex of the shape "biplane-[a-z]+:pi5-X biplane-[a-z]+:y"
# does not bind source to target, so mutating every alias target to
# biplane-web stayed green. Assert the four EXACT pairs by name.
assert_alias_pairs() {
  local log="$1" id="$2" alias_name="$3" label="$4"
  local missing="" img
  for img in web admin space backend; do
    grep -qxF "tag biplane-${img}:pi5-${id} biplane-${img}:${alias_name}" "$log" || missing="$missing $img"
  done
  local calls; calls=$(grep -c "^tag " "$log")
  if [ -z "$missing" ] && [ "$calls" -eq 4 ]; then
    ok "$label: each image tagged from its OWN build-id image (4 exact pairs)"
  else
    no "$label: wrong alias pairs (missing:${missing:- none}, tag calls: $calls)"
  fi
}
assert_alias_pairs "$DOCKER_LOG" "$SHA" latest "clean"

# --- 6b. the same contract under the dirty override (RC 3216) ------------
echo "scratch" >> README.md
export DOCKER_LOG="$WORK/alias-dirty.log"; : > "$DOCKER_LOG"
BIPLANE_ALLOW_DIRTY=1 BIPLANE_IMAGE_TAG=latest bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1
assert_alias_pairs "$DOCKER_LOG" "$SHA-dirty" latest "dirty override"
dirty_built_alias=$(grep "^build " "$DOCKER_LOG" | grep -cE "biplane-[a-z]+:latest")
[ "$dirty_built_alias" -eq 0 ] && ok "dirty override: alias still never replaces the build tag" \
  || no "dirty override: alias must not replace the build tag"
git checkout -q -- README.md 2>/dev/null; git clean -qfd 2>/dev/null

# --- 7. no alias requested -> no tag calls at all -------------------------
export DOCKER_LOG="$WORK/noalias.log"; : > "$DOCKER_LOG"
bash "$WORK/repo/deployments/selfhost/build-images.sh" >/dev/null 2>&1
[ "$(grep -c "^tag " "$DOCKER_LOG")" -eq 0 ] && ok "no alias requested, no tag calls" \
  || no "no alias should mean no docker tag calls"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
