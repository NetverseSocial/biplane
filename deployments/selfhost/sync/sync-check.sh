#!/usr/bin/env bash
# biplane-sync: upstream-release watcher for the Biplane fork.
# Deterministic pipeline: detect new Plane CE release -> merge candidate branch ->
# build every forked image -> report via a Forgejo/Gitea issue. Humans/agents enter
# only on failure. Designed to run in a container (see Dockerfile); host cron just
# invokes the container.
#
# Fail-closed rules:
#   - a failed push or failed report NEVER advances the handled-state file;
#   - "READY" is only reported after ALL forked images build;
#   - the status drop never claims "deployed" — this watcher never merges or deploys.
set -u -o pipefail

# --- configuration (env-driven; defaults suit the container image) -------
BASE="${SYNC_BASE:-/data}"                 # persistent volume: state, logs, work clone
FORGEJO="${FORGEJO_URL:-http://localhost:3000}"
REPO="${FORK_REPO:-}"                      # REQUIRED: owner/name of the fork on your Forgejo
BRANCH="${FORK_BRANCH:-main}"              # the fork's integration branch
ASSIGNEE="${SYNC_ASSIGNEE:-}"              # optional Forgejo user to assign issues to
UPSTREAM_REPO="${UPSTREAM_REPO:-makeplane/plane}"
UPSTREAM="https://github.com/${UPSTREAM_REPO}.git"
BUILD_TIMEOUT="${BUILD_TIMEOUT:-7200}"     # ceiling PER IMAGE build, seconds
STATUS_FILE="${STATUS_FILE:-}"             # optional JSON status drop (for dashboards)
GIT_IDENT_EMAIL="${SYNC_GIT_EMAIL:-sync@noreply.biplane.dev}"   # non-deliverable

[ -n "$REPO" ] || { echo "FORK_REPO is required (owner/name, e.g. your-org/biplane)"; exit 1; }
case "$REPO" in */*) ;; *) echo "FORK_REPO must be owner/name, got: $REPO"; exit 1;; esac

WORK="$BASE/work/plane"
STATE="$BASE/state"                        # last tag handled (fully reported)
LOG="$BASE/last-run.log"
TOKEN="${FORGEJO_TOKEN:-$(cat "${TOKEN_FILE:-$BASE/forgejo.token}" 2>/dev/null || true)}"
[ -z "$TOKEN" ] && { echo "no Forgejo token (FORGEJO_TOKEN or ${TOKEN_FILE:-$BASE/forgejo.token})"; exit 1; }

# Base dir must exist BEFORE the lock fd opens — with an absent SYNC_BASE the old
# order made exec/flock fail and the '|| exit 0' read as a clean no-op run.
mkdir -p "$BASE" || { echo "cannot create SYNC_BASE=$BASE"; exit 1; }
exec 9>"$BASE/.lock" || { echo "cannot open lock in SYNC_BASE=$BASE"; exit 1; }
flock -n 9 || exit 0                                # never overlap
exec > "$LOG" 2>&1
echo "=== biplane-sync $(date -Is) ==="

# Token plumbing that never reaches argv:
#   - git: GIT_CONFIG_* carries the auth header URL-SCOPED to the Forgejo host only
#     (an unscoped http.extraHeader would send the token to github.com on the
#     upstream fetch — witnessed with GIT_CURL_VERBOSE both directions);
#   - curl: header file on disk (0600), passed by name.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="http.${FORGEJO%/}/.extraHeader"
export GIT_CONFIG_VALUE_0="Authorization: token $TOKEN"
HDRF="$BASE/.authhdr"
umask 077
printf 'Authorization: token %s\n' "$TOKEN" > "$HDRF"

report() { # $1 = title, $2 = body -> returns non-zero if Forgejo did not accept it
  local assignees="[]"
  [ -n "$ASSIGNEE" ] && assignees="[\"$ASSIGNEE\"]"
  if jq -n --arg t "$1" --arg b "$2" --argjson a "$assignees" '{title:$t, body:$b, assignees:$a}' \
      | curl -s --fail-with-body -X POST -H @"$HDRF" -H "Content-Type: application/json" \
          "$FORGEJO/api/v1/repos/$REPO/issues" -d @- >/dev/null; then
    echo "reported: $1"
  else
    echo "REPORT FAILED (state NOT advanced): $1"
    return 1
  fi
}

mark_handled() { # $1 = tag — call ONLY after the outcome was durably reported
  echo "$1" > "$STATE"
}

write_status() { # $1 = note
  [ -z "$STATUS_FILE" ] && return 0
  jq -n --arg c "$(date -Is)" --arg u "${TAG:-unknown}" \
        --arg h "$(cat "$STATE" 2>/dev/null || echo none)" --arg n "${1:-ok}" \
        '{checked:$c, latest_upstream:$u, last_handled:$h, note:$n}' > "$STATUS_FILE.tmp" 2>/dev/null \
    && mv "$STATUS_FILE.tmp" "$STATUS_FILE" 2>/dev/null || true
}

# 1. latest upstream release — conditional request; 304s are free of rate limits
ETAGF="$BASE/.etag"; RELF="$BASE/.latest-release.json"
COND=""; [ -s "$ETAGF" ] && COND="If-None-Match: $(cat "$ETAGF")"
CODE=$(curl -s -D "$BASE/.hdrs" -o "$RELF.new" -w "%{http_code}" ${COND:+-H "$COND"} \
  "https://api.github.com/repos/$UPSTREAM_REPO/releases/latest")
if [ "$CODE" = "304" ]; then
  echo "no release change (304)"
  TAG=$(jq -r '.tag_name // empty' "$RELF" 2>/dev/null)
  write_status "no change (304)"; exit 0
fi
[ "$CODE" != "200" ] && { echo "release check HTTP $CODE"; write_status "check failed HTTP $CODE"; exit 0; }
mv "$RELF.new" "$RELF"
grep -i "^etag:" "$BASE/.hdrs" | sed "s/^[Ee][Tt][Aa][Gg]: *//" | tr -d "\r" > "$ETAGF"
TAG=$(jq -r '.tag_name // empty' "$RELF")
[ -z "$TAG" ] && { echo "could not parse release"; write_status "parse failed"; exit 0; }
echo "upstream latest: $TAG"

LAST=$(cat "$STATE" 2>/dev/null || echo "")
[ "$TAG" = "$LAST" ] && { echo "already handled $TAG — nothing to do"; write_status "current"; exit 0; }

# 2. working clone (auth via GIT_CONFIG_* env, never argv/URL)
if [ ! -d "$WORK/.git" ]; then
  git clone --quiet "$FORGEJO/$REPO.git" "$WORK" \
    || { report "[sync] clone failed" "could not clone $REPO; see the sync container's $LOG" || true; exit 1; }
  git -C "$WORK" remote add upstream "$UPSTREAM"
fi
cd "$WORK"
git fetch --quiet origin && git fetch --quiet --tags upstream \
  || { report "[sync] fetch failed" "git fetch failed; see the sync container's $LOG" || true; exit 1; }

# 3. already merged? then just record and exit
if TAGC=$(git rev-parse -q --verify "$TAG^{commit}") && git merge-base --is-ancestor "$TAGC" "origin/$BRANCH"; then
  mark_handled "$TAG"; echo "$TAG already in $BRANCH — state updated"; exit 0
fi

# 4. merge candidate
BR="sync/$TAG"
IMG_TAG="sync-${TAG//\//-}"           # docker tags cannot contain '/'
git checkout -q -B "$BR" "origin/$BRANCH"
if ! git -c user.name=biplane-sync -c user.email="$GIT_IDENT_EMAIL" merge --no-edit "$TAG"; then
  CONFLICTS=$(git diff --name-only --diff-filter=U | head -40)
  git merge --abort
  if report "[sync] Plane $TAG: MERGE CONFLICT — human/agent needed" \
"Automatic merge of upstream $TAG into $BRANCH hit conflicts.

Conflicting files:
\`\`\`
$CONFLICTS
\`\`\`
Recreate with: \`git checkout -B sync/$TAG origin/$BRANCH && git merge $TAG\` in a clone. Full log: sync container $LOG"; then
    mark_handled "$TAG"
  fi
  write_status "merge conflict on $TAG"
  exit 0
fi

if ! git push -q origin "$BR"; then
  echo "PUSH FAILED for $BR — state NOT advanced, will retry next run"
  report "[sync] Plane $TAG: candidate push FAILED" \
"Merge of $TAG was clean but pushing branch \`$BR\` to $REPO failed. State was not advanced; the next run will retry. Full log: sync container $LOG" || true
  write_status "push failed on $TAG"
  exit 1
fi
echo "pushed $BR"

# 5. candidate image builds — ALL forked deployables, or it is not READY
BUILDS=(
  "web:apps/web/Dockerfile.web:."
  "space:apps/space/Dockerfile.space:."
  "admin:apps/admin/Dockerfile.admin:."
  "backend:apps/api/Dockerfile.api:apps/api"
)
: > "$BASE/build-id.txt"          # truncate across runs (appended per image below)
FAILED_APP=""
for entry in "${BUILDS[@]}"; do
  app="${entry%%:*}"; rest="${entry#*:}"; dockerfile="${rest%%:*}"; ctx="${rest#*:}"
  echo "building biplane-$app:$IMG_TAG"
  if ! timeout "$BUILD_TIMEOUT" nice -n 15 docker build -q -f "$dockerfile" \
       -t "biplane-$app:$IMG_TAG" "$ctx" >> "$BASE/build-id.txt" 2> "$BASE/build-err.txt"; then
    FAILED_APP="$app"
    break
  fi
done
docker builder prune -f >/dev/null 2>&1 || true

if [ -z "$FAILED_APP" ]; then
  # prune older sync-* candidate images, keep the current tag
  for app in web space admin backend; do
    docker images "biplane-$app" --format "{{.Tag}}" | grep "^sync-" | grep -v "^$IMG_TAG$" \
      | xargs -r -I{} docker rmi "biplane-$app:{}" 2>/dev/null || true
  done
  if report "[sync] Plane $TAG: candidate READY — merge clean, all fork images built" \
"Upstream $TAG merged cleanly; web, space, admin, and backend candidate images built as \`biplane-<app>:$IMG_TAG\`.

Branch \`$BR\` is pushed. Next: human-reviewed PR of $BR into $BRANCH (author does not merge), then rebuild/deploy per usual. Nothing has been merged or deployed by this watcher."; then
    mark_handled "$TAG"
  fi
  write_status "candidate ready for $TAG"
else
  ERRTAIL=$(tail -c 2500 "$BASE/build-err.txt")
  if report "[sync] Plane $TAG: BUILD FAILED ($FAILED_APP) — human/agent needed" \
"Merge was clean but the \`$FAILED_APP\` candidate build failed.

Last of the build log:
\`\`\`
$ERRTAIL
\`\`\`
Branch \`$BR\` is pushed for diagnosis. Full log: sync container $LOG"; then
    mark_handled "$TAG"
  fi
  write_status "build failed ($FAILED_APP) on $TAG"
fi
