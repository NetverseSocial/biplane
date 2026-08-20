#!/usr/bin/env bash
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# ONE COMMAND for a Biplane release, replacing ~20 hand-run steps of which
# several required a human sitting at a Docker-capable shell.
#
#   deployments/release/release.sh v1.2.6
#
# WHY THIS EXISTS. Docker group membership is root-equivalent, so no agent
# holds it; that is correct and it is what closed the escalation that got an
# agent removed on 2026-08-17. The cost was that every Docker-requiring step of
# a release became a human copy-paste, and a release is about twenty of them.
# Twenty pastes is not security either — it is a human used as an access
# control, and humans approve what they are tired of reading. So the Docker
# steps go through the applier's operator operations, which hold the capability
# behind a fixed, audited, no-passthrough surface, and this script sequences
# them.
#
# EVERY STEP FAILS CLOSED. A release that half-happened is worse than one that
# did not start: this script refuses rather than continues, and says which gate
# stopped it. It NEVER passes --force to anything, and it never invents a
# value it failed to read.
#
# WHAT IT DELIBERATELY DOES NOT DO: build. That the build runs on a
# target-architecture machine is a logistics fact and would be a weak reason on
# its own (Sable). The real reason is that the build is the step whose OUTPUT an
# operator must inspect — what was compiled, from which commit, with which
# version baked in. Automating it behind an approval prompt converts inspection
# into a click, which is precisely the failure this script exists to remove
# everywhere else: a human kept in the loop as a formality approves what they
# are tired of reading. So step 2 prints the exact commands and stops.

set -euo pipefail

TAG="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${FORGEJO_API:=http://forge.example.com:3000/api/v1}"
: "${FORGEJO_REPO:=example/biplane}"
: "${REGISTRY_OWNER:=biplane}"
: "${APPLY_SERVICE_URL:=}"      # e.g. http://forge.example.com:7671 — operator operations
: "${APPLY_SERVICE_TOKEN:=}"
: "${WORK:=/tmp/biplane-release-$$}"

die() { printf '\nRELEASE REFUSED at %s: %s\n' "${STEP:-startup}" "$*" >&2; exit 1; }
step() { STEP="$1"; printf '\n=== %s ===\n' "$1"; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command '$1' is unavailable"; }

[ -n "$TAG" ] || die "usage: release.sh vMAJOR.MINOR.PATCH"
need jq; need git; need curl
mkdir -p "$WORK"

# The operator operations are what remove the human from the Docker steps. If
# they are not configured, say exactly which steps that costs rather than
# failing later with a confusing error.
op() {  # op <METHOD> <path> [json]
  [ -n "$APPLY_SERVICE_URL" ] && [ -n "$APPLY_SERVICE_TOKEN" ] || \
    die "APPLY_SERVICE_URL/TOKEN are unset, so the Docker-requiring steps cannot run here.
     Either set them, or run this release the manual way — deployments/release/README.md."
  local method="$1" path="$2" body="${3:-}" out code
  # An HTTP error must not look like success. `curl -sS` alone exits 0 on a 4xx
  # and prints the error body, so every caller would have to remember to check
  # the CONTENT — and one that forgets succeeds silently (Sable 3996). Making it
  # structural here means a call site cannot opt out by forgetting.
  if [ -n "$body" ]; then
    out="$(curl -sS -w '\n%{http_code}' -X "$method" -H "Authorization: Bearer $APPLY_SERVICE_TOKEN" \
      -H 'Content-Type: application/json' -d "$body" "$APPLY_SERVICE_URL$path")" || \
      die "operator call $method $path could not be made"
  else
    out="$(curl -sS -w '\n%{http_code}' -X "$method" -H "Authorization: Bearer $APPLY_SERVICE_TOKEN" \
      "$APPLY_SERVICE_URL$path")" || die "operator call $method $path could not be made"
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  [ "$code" -lt 400 ] 2>/dev/null || die "operator call $method $path returned HTTP $code: $(head -c 300 <<<"$out")"
  printf '%s' "$out"
}

# ---------------------------------------------------------------- 1. preflight
step "1/8 preflight — every gate, in order, refusing rather than warning"
FORGEJO_TOKEN="${FORGEJO_TOKEN:-}"
[ -n "$FORGEJO_TOKEN" ] || die "FORGEJO_TOKEN is unset"
FORGEJO_API="$FORGEJO_API" FORGEJO_REPO="$FORGEJO_REPO" FORGEJO_TOKEN="$FORGEJO_TOKEN" \
  bash "$SCRIPT_DIR/preflight.sh" "$TAG" "$WORK" || die "preflight refused $TAG"
LEVEL="$(cat "$WORK/level")"
COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
echo "level=$LEVEL commit=$COMMIT"
[ "$LEVEL" != "full" ] || die "$TAG derives level 'full' — it changes the runtime itself and takes
     the hand-applied path: deployments/selfhost/MANUAL-FULL-UPGRADE.md"

# -------------------------------------------------------------------- 2. build
step "2/8 build — on a machine of the target architecture"
BUILD="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
if ! docker image inspect "biplane-backend:pi5-$BUILD" >/dev/null 2>&1; then
  cat <<BUILDHELP
The four images for build $BUILD are not present here. Build them on the
target-architecture host, then re-run this script:

  cd ~/biplane/build && git fetch --all && git checkout $COMMIT
  BIPLANE_RELEASE_TAG=$TAG bash deployments/selfhost/build-images.sh
  docker save biplane-{backend,web,admin,space}:pi5-$BUILD | ssh pi5 docker load

BUILDHELP
  die "images for build $BUILD are not available to this host"
fi

# ------------------------------------------------------------------- 3. publish
step "3/8 publish images — digests read back FROM THE REGISTRY, not from stdout"
IMAGES_REQ="$(jq -cn --arg tag "$TAG" --arg b "$BUILD" \
  '{tag:$tag, images:[ "backend","web","admin","space" | {kind:., build:$b} ]}')"
PUSHED="$(op POST /op/push-images "$IMAGES_REQ")" || die "push-images failed"
jq -e '.pushed | length == 4' <<<"$PUSHED" >/dev/null 2>&1 || \
  die "push-images did not return four digests: $(head -c 300 <<<"$PUSHED")"
jq -c '.pushed' <<<"$PUSHED" > "$WORK/images.json"
jq -r '.pushed[] | "  \(.image) \(.digest)"' <<<"$PUSHED"

# ------------------------------------------------------------------ 4. metadata
step "4/8 release metadata"
bash "$SCRIPT_DIR/make-release-metadata.sh" --tag "$TAG" --commit "$COMMIT" \
  --level "$LEVEL" --images "$WORK/images.json" --out "$WORK" || die "metadata assembly failed"

# ----------------------------------------------------------------------- 5. tag
step "5/8 tag the exact commit"
if git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  existing="$(git -C "$REPO_ROOT" rev-list -n1 "$TAG")"
  [ "$existing" = "$COMMIT" ] || die "tag $TAG already exists at $existing, not $COMMIT"
  echo "tag already present at the right commit"
else
  git -C "$REPO_ROOT" tag "$TAG" "$COMMIT"
  git -C "$REPO_ROOT" push origin "$TAG"
fi

# ------------------------------------------------------------------- 6. release
step "6/8 publish the release and READ IT BACK"
notes="$(awk -v t="$TAG" '$0 ~ "^## \\["t"\\]" {f=1;next} f && /^## /{exit} f' "$REPO_ROOT/CHANGELOG.md")"
[ -n "$notes" ] || die "no changelog entry for $TAG"
rid="$(curl -sS -X POST -H "Authorization: token $FORGEJO_TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg t "$TAG" --arg c "$COMMIT" --arg b "$notes" \
        '{tag_name:$t,target_commitish:$c,name:$t,body:$b}')" \
  "$FORGEJO_API/repos/$FORGEJO_REPO/releases" | jq -r '.id // empty')"
[ -n "$rid" ] || rid="$(curl -sS -H "Authorization: token $FORGEJO_TOKEN" \
  "$FORGEJO_API/repos/$FORGEJO_REPO/releases/tags/$TAG" | jq -r '.id // empty')"
[ -n "$rid" ] || die "could not create or find the release for $TAG"
curl -sS -X POST -H "Authorization: token $FORGEJO_TOKEN" \
  -F "attachment=@$WORK/release.json;filename=release.json" \
  "$FORGEJO_API/repos/$FORGEJO_REPO/releases/$rid/assets?name=release.json" >/dev/null

# A publish you have not read back is a hope, not a release.
url="$(curl -sS -H "Authorization: token $FORGEJO_TOKEN" \
  "$FORGEJO_API/repos/$FORGEJO_REPO/releases/$rid/assets" \
  | jq -r '.[] | select(.name=="release.json") | .browser_download_url')"
url="${url/forge.example.com:3000/forge.example.com:3000}"
curl -sSfL -H "Authorization: token $FORGEJO_TOKEN" "$url" -o "$WORK/readback.json" || \
  die "could not read the published release.json back"
cmp -s "$WORK/release.json" "$WORK/readback.json" || \
  die "published release.json differs from what was assembled — refusing to continue"
echo "readback byte-identical"

# --------------------------------------------------------------------- 7. apply
step "7/8 make the board notice, then apply"
op POST /op/trigger-update-check '' | jq -r '"  update check -> \(.state // .error)"'

# BASELINE BEFORE THE CLICK. Without it this step reports the verdict of
# whatever ran LAST — so a refused apply (409 "already running" is reachable)
# would be judged by an EARLIER run's exit code and reported as this release
# succeeding. That is precisely the defect the UI fixed tonight with the
# applier's own finished_at; the orchestrator had it in shell with no baseline
# (Sable 3996). Two conditions, because they fail differently: `started` catches
# "it never began", a CHANGED finished_at catches "it began, but what I am
# reading is not its result".
before="$(op GET /status)"
baseline="$(jq -r '.last_result.finished_at // "none"' <<<"$before")"

started="$(op POST /op/apply "$(jq -cn --arg t "$TAG" '{tag:$t}')" | jq -r '.started // empty')"
[ -n "$started" ] || die "the applier did not start a run for $TAG — it refused, and this release did NOT apply"
echo "  apply -> started $started"

# A single dropped packet must not abandon a run that is still going: `op` dies
# on failure, and the apply CONTINUES on the host regardless, so the operator
# would see a refusal for a release that may be succeeding (Sable 3997). Tolerate
# consecutive blips, but never silently — and never treat "cannot see it" as
# "finished".
misses=0
for _ in $(seq 1 60); do
  sleep 10
  # Keep the error rather than discarding it: it carries the HTTP code, and
  # without it an expired token, a 500 and a genuine network drop are
  # indistinguishable — after which "lost contact" is a specific claim the
  # evidence no longer supports (Sable 3998).
  if ! status="$(op GET /status 2>"$WORK/poll.err")"; then
    misses=$((misses+1))
    last_err="$(tr -d '\n' < "$WORK/poll.err" | tail -c 200)"
    echo "  (status poll failed ${misses}/3: ${last_err:-no detail} — the apply continues on the host)" >&2
    [ "$misses" -lt 3 ] || die "three consecutive status polls failed; last error: ${last_err:-none}.
     The apply may still be running — do NOT re-run this script; check the host."
    continue
  fi
  misses=0
  [ "$(jq -r '.running // false' <<<"$status")" = "true" ] || break
done
[ "$(jq -r '.running // false' <<<"$status")" = "false" ] || \
  die "the apply is still running after 10 minutes — refusing to report an outcome I have not seen"
now="$(jq -r '.last_result.finished_at // "none"' <<<"$status")"
[ "$now" != "$baseline" ] || \
  die "the applier's last result has not changed since before the click ($baseline) —
     whatever is there belongs to an earlier run, so this release's outcome is UNKNOWN"

# CHANGED IS NOT MINE. A moved finished_at proves that A run finished, not that
# THIS run did: the mutex prevents overlap but not sequential interleaving, so an
# auto-apply, a second operator or a hand-run landing between the poll breaking
# on running==false and this read would hand us someone else's verdict — the same
# reachability class as the 409 that motivated these guards. The applier already
# records the identity; we simply were not reading it (Sia 4006). This is the
# identity half of what the UI learned tonight: applyRunVerdict keys on WHICH RUN,
# and "something changed" is not identity.
ran_tag="$(jq -r '.last_result.tag // "none"' <<<"$status")"
[ "$ran_tag" = "$TAG" ] || \
  die "the applier's last result is for '$ran_tag', not '$TAG' — another run finished
     between the poll and this read, so this release's outcome is UNKNOWN. Check the
     host before re-running; do not assume this release failed."
code="$(jq -r '.last_result.exit_code // "unknown"' <<<"$status")"
[ "$code" = "0" ] || die "the apply exited $code — see the applier log"

# --------------------------------------------------------------------- 8. verify
step "8/8 verify — the running pins must be the digests we published"
served="$(op GET /op/board-status)"
jq -r '.pins | to_entries[] | "  pinned \(.key)=\(.value)"' <<<"$served"
# A gate that cannot refuse is not a gate. We hold both sides — the digests we
# pushed and the pins the deployment reports — so compare them rather than
# printing them next to each other and calling it verification (Sable 3996).
# Compare against what is RUNNING, not only what is pinned. A pin is an
# intention; `services[].image` from docker ps is the fact, and the gap between
# them is exactly the running-versus-pinned divergence that broke Pi5 tonight and
# that the applier's own baseline check refuses on (Sable 3997). Checking pins
# alone would call a hot-swapped deployment verified.
running_images="$(jq -r '.services[].image' <<<"$served")"
pinned_values="$(jq -r '.pins[]' <<<"$served")"
missing=0
while read -r digest; do
  grep -qF -- "$digest" <<<"$pinned_values" || {
    echo "  NOT PINNED:  $digest" >&2; missing=$((missing+1)); }
  grep -qF -- "$digest" <<<"$running_images" || {
    echo "  NOT RUNNING: $digest" >&2; missing=$((missing+1)); }
done < <(jq -r '.[].digest' "$WORK/images.json")
[ "$missing" -eq 0 ] || die "$missing digest check(s) failed — the release published, but the
     board is not pinned to it and/or is not running it. NOT PINNED means the
     config did not commit; NOT RUNNING means the containers were not replaced,
     which is the divergence the next apply will refuse on."
echo "  all four published digests are both pinned AND running"
echo
echo "RELEASED $TAG ($LEVEL) at $COMMIT"
echo "  metadata: $WORK/release.json"
echo "Confirm the served version in the UI before calling it done — a pin is an"
echo "intention and the served version is the fact."
