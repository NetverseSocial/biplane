#!/usr/bin/env bash
# Build the forked Biplane images with a build identifier baked in (BIP-30).
#
# WHY THIS FILE EXISTS (Morrow, PR 27 review): the Dockerfiles accept
# VITE_BIPLANE_BUILD, but until now nothing committed SUPPLIED it — every
# repo-supported build defaulted to "dev", and the claim that the screen
# matches the deployed image rested on an unversioned script on one operator's
# machine. That is not a reviewable artifact, so the caller lives here.
#
# The build id is the commit being built. Both the images' TAGS and the value
# compiled into the frontends come from the same variable, so "screen == tag"
# holds by construction rather than by discipline.
#
#   ./deployments/selfhost/build-images.sh                 # tag pi5-<sha>
#   BIPLANE_IMAGE_TAG=latest ./deployments/selfhost/build-images.sh
#   BIPLANE_ALLOW_DIRTY=1 ./deployments/selfhost/build-images.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

BUILD_ID="$(git rev-parse --short HEAD)"

# A dirty tree means the images would NOT match the commit they claim. Refuse
# by default; an operator who genuinely wants a scratch build says so, and the
# id is marked so the screen never lies about it.
# MODIFIED *and* UNTRACKED files both count (Morrow RC 3210). Docker copies the
# working tree, not the commit, so an untracked .ts under apps/ is a build
# INPUT: accepting it would bake unreviewed code under a clean commit's id.
tree_differs() {
  git diff-index --quiet HEAD -- 2>/dev/null || return 0
  [ -z "$(git ls-files --others --exclude-standard -- apps packages 2>/dev/null)" ] || return 0
  return 1
}

if tree_differs; then
  if [ "${BIPLANE_ALLOW_DIRTY:-0}" = "1" ]; then
    BUILD_ID="${BUILD_ID}-dirty"
    echo "WARNING: working tree differs from HEAD; building as ${BUILD_ID}" >&2
  else
    echo "ERROR: working tree differs from HEAD — the build id would not identify these images." >&2
    git diff-index --name-only HEAD -- 2>/dev/null | sed 's/^/       modified:  /' >&2
    git ls-files --others --exclude-standard -- apps packages 2>/dev/null | sed 's/^/       untracked: /' >&2
    echo "       Commit, stash, or set BIPLANE_ALLOW_DIRTY=1 to build as <sha>-dirty." >&2
    exit 1
  fi
fi

# The PRIMARY tag is ALWAYS the build id — that is the entire promise: the tag
# names the code inside. A caller-supplied name can only be an ADDITIONAL alias
# (RC 3210: BIPLANE_IMAGE_TAG=latest used to bake one id while tagging another).
TAG="pi5-${BUILD_ID}"
ALIAS="${BIPLANE_IMAGE_ALIAS:-}"
if [ -n "${BIPLANE_IMAGE_TAG:-}" ]; then
  echo "NOTE: BIPLANE_IMAGE_TAG no longer REPLACES the build tag (that broke the" >&2
  echo "      tag-identifies-the-build guarantee); '${BIPLANE_IMAGE_TAG}' is added as an alias." >&2
  ALIAS="${BIPLANE_IMAGE_TAG}"
fi

echo "Building Biplane images: build id ${BUILD_ID}, tag ${TAG}"

# The RELEASE version reaches the web bundles too — the sidebar's version
# line reads it. Empty on a dev build, so the UI says "dev" rather than
# borrowing upstream's base version as if it were ours (John, 2026-08-16:
# "Version: v1.3.1" on a v1.1.0 install read as "not deployed" three times).
docker build --build-arg VITE_BIPLANE_BUILD="${BUILD_ID}" \
  --build-arg VITE_BIPLANE_VERSION="${BIPLANE_RELEASE_TAG:-}" \
  -f apps/web/Dockerfile.web -t "biplane-web:${TAG}" .
docker build --build-arg VITE_BIPLANE_BUILD="${BUILD_ID}" \
  --build-arg VITE_BIPLANE_VERSION="${BIPLANE_RELEASE_TAG:-}" \
  -f apps/admin/Dockerfile.admin -t "biplane-admin:${TAG}" .
docker build -f apps/space/Dockerfile.space -t "biplane-space:${TAG}" .
# The BACKEND gets the id too (BIP-36, Morrow RC 3271). Until now only the
# frontends did, so `Instance.biplane_installed_build` could never be populated
# by any repo-supported build — the field existed and nothing could fill it,
# which is the same false green this script was written to close for the UI.
# Same BUILD_ID, so backend and frontends cannot disagree, and a -dirty build
# says so on both.
# BIPLANE_RELEASE_TAG is set ONLY by the release pipeline (the tag being
# released); interactive/dev builds leave it empty and the backend bakes an
# empty BIPLANE_VERSION => the update check reports an honest UNKNOWN
# rather than comparing a commit id as a version (RC 3392 #2).
# OUR changelog ships in the backend image (Settings → Updates renders it).
# The api build context is apps/api, so stage a copy inside it for the build
# and remove it after — the repo copy at the root stays authoritative.
cp CHANGELOG.md apps/api/CHANGELOG.md
trap 'rm -f apps/api/CHANGELOG.md' EXIT
docker build --build-arg BIPLANE_BUILD="${BUILD_ID}" \
  --build-arg BIPLANE_VERSION="${BIPLANE_RELEASE_TAG:-}" \
  -f apps/api/Dockerfile.api -t "biplane-backend:${TAG}" apps/api
rm -f apps/api/CHANGELOG.md

if [ -n "$ALIAS" ]; then
  for img in web admin space backend; do
    docker tag "biplane-${img}:${TAG}" "biplane-${img}:${ALIAS}"
  done
  echo "Also tagged alias: ${ALIAS} (the build tag ${TAG} remains authoritative)"
fi

echo
echo "Built:"
for img in web admin space backend; do
  docker images --format '  {{.Repository}}:{{.Tag}} {{.ID}}' | grep -E "biplane-${img}:${TAG} " || true
done
echo
echo "The web and admin UIs, and the backend Instance record, will report build ${BUILD_ID}."
echo "Point BIPLANE_*_IMAGE in your .env at tag ${TAG}, then recreate the services."
