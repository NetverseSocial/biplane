#!/usr/bin/env bash
# BIP-42 / M5.4: apply one explicit Biplane release on a self-host deployment.
#
# This is the implementation, not a wrapper around a server-side apply state
# machine. The current backend image owns bounded exact-tag metadata fetching;
# this host command owns backup, digest pulls, migration admission, the atomic
# config commit, service recreation, readback, and rollback reporting.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../release/release-version.sh
source "$SCRIPT_DIR/../release/release-version.sh"

SELFHOST_DIR="${BIPLANE_SELFHOST_DIR:-$SCRIPT_DIR}"
ENV_FILE="$SELFHOST_DIR/.env"
BACKUP_ROOT="$SELFHOST_DIR/.biplane-backups"
LOCK_FILE="$SELFHOST_DIR/.biplane-update.lock"
SERVICES=(web space admin api worker beat-worker)
PIN_KEYS=(
  BIPLANE_BACKEND_IMAGE
  BIPLANE_WEB_IMAGE
  BIPLANE_ADMIN_IMAGE
  BIPLANE_SPACE_IMAGE
  BIPLANE_APPLIED_RELEASE
)

die() {
  printf 'Biplane update refused: %s\n' "$*" >&2
  exit 1
}

# Baseline diagnostics go to STDERR, NEVER stdout. capture_running_snapshot and
# service_image_snapshot both emit their JSON on stdout into a command
# substitution, so a reason printed to stdout would be spliced into the snapshot
# and corrupt it silently. Do not "tidy" these into echo/printf-to-stdout.
#
# The WORDING constraint that used to live here is DELETED, not extended, as
# its author instructed: it asked these messages to avoid backup / pull /
# migrat / probe / verification and capitalised Recreate|Starting|Started|
# Stopping, because the gauge matched every log line and read error text as
# progress. `stage` below is the sentinel that replaces it — the recogniser
# now reads only marked lines, so operator text is free to say what is true.
snap_fail() {
  printf 'baseline mismatch: %s\n' "$*" >&2
  return 1
}

# The ONLY progress claim this script makes (BIP-72). The UI gauge advances on
# these lines and on nothing else, so ordinary output and error text can no
# longer move it — previously any line containing "migrat" did, including this
# script's own "migration did not complete" failure message, and a sweep of
# main found eighteen such messages, most of them failure and recovery lines.
#
# Emitted on stdout, whole-line, before the work it announces begins. Adding a
# stage here is safe in both directions: a UI that does not know the name
# ignores it, and a UI newer than this script simply never advances.
stage() {
  printf '__BIPLANE_STAGE__ %s\n' "$1"
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' is unavailable"
}

env_value() {
  local key="$1" file="${2:-$ENV_FILE}" count
  count="$(grep -cE "^${key}=" "$file" || true)"
  [ "$count" -le 1 ] || die "$file contains duplicate $key entries"
  [ "$count" -eq 1 ] || return 1
  sed -n "s/^${key}=//p" "$file"
}

assert_env_target() {
  [ -f "$ENV_FILE" ] || die "$ENV_FILE is absent"
  [ ! -L "$ENV_FILE" ] || die "$ENV_FILE must be a regular file, not a symlink"
  [ -r "$ENV_FILE" ] && [ -w "$ENV_FILE" ] || die "$ENV_FILE must be readable and writable"
  for key in "${PIN_KEYS[@]}"; do
    local count
    count="$(grep -cE "^${key}=" "$ENV_FILE" || true)"
    [ "$count" -le 1 ] || die "$ENV_FILE contains duplicate $key entries"
  done
}

write_pinned_env() {
  local source="$1" destination="$2" release="$3" backend="$4" web="$5" admin="$6" space="$7"
  awk \
    -v backend="$backend" -v web="$web" -v admin="$admin" -v space="$space" -v release="$release" '
      BEGIN {
        value["BIPLANE_BACKEND_IMAGE"] = backend
        value["BIPLANE_WEB_IMAGE"] = web
        value["BIPLANE_ADMIN_IMAGE"] = admin
        value["BIPLANE_SPACE_IMAGE"] = space
        value["BIPLANE_APPLIED_RELEASE"] = release
      }
      /^[A-Za-z_][A-Za-z0-9_]*=/ {
        key = $0
        sub(/=.*/, "", key)
        if (key in value) {
          seen[key]++
          if (seen[key] > 1) exit 42
          print key "=" value[key]
          next
        }
      }
      { print }
      END {
        order[1] = "BIPLANE_BACKEND_IMAGE"
        order[2] = "BIPLANE_WEB_IMAGE"
        order[3] = "BIPLANE_ADMIN_IMAGE"
        order[4] = "BIPLANE_SPACE_IMAGE"
        order[5] = "BIPLANE_APPLIED_RELEASE"
        for (i = 1; i <= 5; i++) if (!seen[order[i]]) print order[i] "=" value[order[i]]
      }
    ' "$source" > "$destination" || die "could not render the replacement deployment config"
}

atomic_replace() {
  local target="$1" replacement="$2" temporary target_device temporary_device
  ATOMIC_REPLACE_OUTCOME=pre_rename
  if [ -L "$target" ]; then
    printf 'pin target %s became a symlink before commit\n' "$target" >&2
    return 1
  fi
  temporary="$(mktemp "$(dirname "$target")/.biplane-pin.XXXXXX")" || return 1
  trap 'rm -f "${temporary:-}"' RETURN
  cat "$replacement" > "$temporary" || return 1
  chmod --reference="$target" "$temporary" || return 1
  target_device="$(stat -c '%d' "$target")" || return 1
  temporary_device="$(stat -c '%d' "$temporary")" || return 1
  if [ "$target_device" != "$temporary_device" ]; then
    printf 'pin replacement is not on the target filesystem\n' >&2
    return 1
  fi
  sync -f "$temporary" || return 1
  mv -f "$temporary" "$target" || return 1
  ATOMIC_REPLACE_OUTCOME=renamed_not_durable
  if ! sync -f "$(dirname "$target")"; then
    printf 'directory sync failed after pin rename; visible config is an uncertain commit\n' >&2
    return 1
  fi
  ATOMIC_REPLACE_OUTCOME=durable
  trap - RETURN
}

image_ref_for() {
  local metadata="$1" basename="$2"
  jq -er --arg basename "$basename" '
    [.release.images[] | select((.image | split("/")[-1]) == $basename)]
    | if length == 1 then .[0].image + "@" + .[0].digest else error("image set mismatch") end
  ' <<<"$metadata"
}

compose() {
  docker compose --project-directory "$SELFHOST_DIR" --env-file "$ENV_FILE" \
    -f "$SELFHOST_DIR/docker-compose.yml" -f "$SELFHOST_DIR/docker-compose.override.yml" "$@"
}

compose_with_env() {
  local env_file="$1"
  shift
  docker compose --project-directory "$SELFHOST_DIR" --env-file "$env_file" \
    -f "$SELFHOST_DIR/docker-compose.yml" -f "$SELFHOST_DIR/docker-compose.override.yml" "$@"
}

fetch_metadata() {
  local tag="$1"
  compose run --rm --no-deps -T --entrypoint python api \
    manage.py biplane_update_metadata "$tag"
}

database_target() {
  compose exec -T api python -c '
import json, os, sys
from urllib.parse import unquote, urlsplit
url = urlsplit(os.environ.get("DATABASE_URL", ""))
want = {
    "host": os.environ.get("PGHOST", ""),
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ.get("POSTGRES_USER", ""),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
    "database": os.environ.get("POSTGRES_DB", ""),
}

actual = {
    "host": url.hostname or "",
    "port": url.port or 5432,
    "user": unquote(url.username or ""),
    "password": unquote(url.password or ""),
    "database": unquote(url.path.lstrip("/")),
}
if url.scheme not in {"postgres", "postgresql"} or url.query or url.fragment:
    sys.exit("DATABASE_URL is not one plain PostgreSQL target")
if want["host"] != "plane-db" or actual != want:
    sys.exit("DATABASE_URL does not name the bundled plane-db target exactly")
print(json.dumps({k: actual[k] for k in ("host", "port", "user", "database")}, sort_keys=True))
'
}

verify_database_wiring() {
  local rendered runtime_url plane_db_runtime key expected actual
  rendered="$(compose config --format json)" || return 1
  runtime_url="$(compose exec -T api sh -c 'printf "%s" "$DATABASE_URL"')" || return 1
  [ -n "$runtime_url" ] || return 1
  [ "$(jq -r '.services.api.environment.DATABASE_URL' <<<"$rendered")" = "$runtime_url" ] || return 1
  [ "$(jq -r '.services.migrator.environment.DATABASE_URL' <<<"$rendered")" = "$runtime_url" ] || return 1
  for key in PGHOST POSTGRES_PORT POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do
    [ "$(jq -r --arg key "$key" '.services.api.environment[$key]' <<<"$rendered")" = \
      "$(jq -r --arg key "$key" '.services.migrator.environment[$key]' <<<"$rendered")" ] || return 1
  done
  plane_db_runtime="$(compose exec -T plane-db sh -c \
    'printf "%s\037%s\037%s" "$POSTGRES_USER" "$POSTGRES_PASSWORD" "$POSTGRES_DB"' \
    biplane-db-proof)" || return 1
  for key in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do
    expected="$(jq -r --arg key "$key" '.services.api.environment[$key]' <<<"$rendered")"
    [ "$(jq -r --arg key "$key" '.services["plane-db"].environment[$key]' <<<"$rendered")" = "$expected" ] || return 1
  done
  IFS=$'\037' read -r -a actual <<<"$plane_db_runtime"
  [ "${#actual[@]}" -eq 3 ] || return 1
  [ "${actual[0]}" = "$(jq -r '.services["plane-db"].environment.POSTGRES_USER' <<<"$rendered")" ] || return 1
  [ "${actual[1]}" = "$(jq -r '.services["plane-db"].environment.POSTGRES_PASSWORD' <<<"$rendered")" ] || return 1
  [ "${actual[2]}" = "$(jq -r '.services["plane-db"].environment.POSTGRES_DB' <<<"$rendered")" ] || return 1
}

service_image_snapshot() {
  local service="$1" expected="$2" container pair ref image_id count=0 result='[]'
  while IFS= read -r container; do
    [ -n "$container" ] || continue
    pair="$(docker inspect --format '{{.Config.Image}} {{.Image}}' "$container")" || \
      { snap_fail "could not inspect the running $service container $container"; return 1; }
    read -r ref image_id <<<"$pair"
    # Split from the image-id check so each names itself. A running ref that is
    # a TAG where the pin is a digest is the hot-swap fingerprint: someone
    # replaced one service by hand and .env still pins the released digest.
    [ "$ref" = "$expected" ] || \
      { snap_fail "running $service image is '$ref' but the persisted pin is '$expected'"; return 1; }
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || \
      { snap_fail "running $service container $container reports a malformed image id '$image_id'"; return 1; }
    result="$(jq -c --arg ref "$ref" --arg id "$image_id" '. + [{ref:$ref, image_id:$id}]' <<<"$result")"
    count=$((count + 1))
  done < <(compose ps -q "$service")
  [ "$count" -gt 0 ] || { snap_fail "no running container for service $service"; return 1; }
  jq -cS 'sort_by(.ref, .image_id)' <<<"$result"
}

capture_running_snapshot() {
  local backend="$1" web="$2" admin="$3" space="$4"
  local service expected service_images images='{}' running build version db configured web_url
  for service in "${SERVICES[@]}"; do
    case "$service" in
      web) expected="$web" ;;
      admin) expected="$admin" ;;
      space) expected="$space" ;;
      *) expected="$backend" ;;
    esac
    # service_image_snapshot has already named the specific mismatch.
    service_images="$(service_image_snapshot "$service" "$expected")" || return 1
    images="$(jq -c --arg service "$service" --argjson values "$service_images" \
      '. + {($service): $values}' <<<"$images")"
  done
  running="$(compose exec -T api sh -c 'printf "%s %s\n" "$BIPLANE_BUILD" "$BIPLANE_VERSION"')" || \
    { snap_fail "could not read BIPLANE_BUILD/BIPLANE_VERSION from the running api container"; return 1; }
  read -r build version <<<"$running"
  [[ "$build" =~ ^[0-9a-f]{7,40}$ ]] || \
    { snap_fail "the running api reports build '$build', which is not a commit id"; return 1; }
  release_version_valid "$version" || \
    { snap_fail "the running api reports version '$version', which is outside the release version grammar"; return 1; }
  configured="$(env_value BIPLANE_APPLIED_RELEASE || true)"
  [ -z "$configured" ] || [ "$configured" = "$version" ] || \
    { snap_fail "BIPLANE_APPLIED_RELEASE is '$configured' but the running api reports '$version'"; return 1; }
  web_url="$(env_value WEB_URL || true)"
  [ -n "$web_url" ] || { snap_fail "WEB_URL is not set in $ENV_FILE"; return 1; }
  # CATEGORY ONLY for the two database checks, deliberately: they compare
  # POSTGRES_PASSWORD and whole DATABASE_URLs, so naming observed/expected here
  # would print connection material into an operator-visible log. Naming WHICH
  # check fired is already strictly more than the single message gave before.
  verify_database_wiring || \
    { snap_fail "the running services do not agree on one database target (values withheld)"; return 1; }
  db="$(database_target)" || \
    { snap_fail "the running api's DATABASE_URL does not name the bundled plane-db target (values withheld)"; return 1; }
  jq -cn --arg release "$version" --arg build "$build" --argjson database "$db" \
    --argjson images "$images" '{release:$release, build:$build, database:$database, images:$images}'
}

# Freshly recreated services need a warmup window (gunicorn takes ~30-60s to
# answer after `compose up`); a single curl during that window fails a healthy
# apply and triggers a needless rollback — which then fails ITS verify in the
# same window (2026-08-17, the first live one-click apply). Poll to success
# instead; the timeout still bounds a genuinely dead service.
# The probe is /api/instances/ because /api/health/ does not exist on this
# deployment (the api registers health_check at path "", so the proxied
# /api/health/ is 404 even fully warm — measured on the healthy board).
# All post-recreate endpoint checks poll under ONE shared deadline
# (BIPLANE_APPLY_HEALTH_TIMEOUT, default 180s) and print why each attempt
# failed. Both properties are from the first live applies (2026-08-17): the
# per-probe timeouts stacked to ~19 minutes worst-case, and a silent verify
# rolled back three healthy deployments without ever naming the failing
# check.
# There is deliberately NO scrape of the served HTML for the build id here.
# served_build_present tried that and could never pass: the deployed web and
# admin apps reference their bundles as <link rel="modulepreload" href=...>
# chains (the web root lists 32+, and the id lives in lazy chunks not present
# in either page's preload list — measured 2026-08-17 after it rolled back a
# completed, healthy v1.2.3 apply). The identity guarantee it reached for is
# already held more strongly upstream: every service is verified running its
# exact pinned digest (service_image_snapshot), the api reports the expected
# build and version from its own environment, and these 200s prove the proxy
# reaches the recreated containers — the old ones no longer exist.
verify_served_endpoints() {
  local web_url="$1" timeout="${BIPLANE_APPLY_HEALTH_TIMEOUT:-180}"
  local deadline=$((SECONDS + timeout))
  until probe_served_once "$web_url"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      printf 'endpoint verification did not pass within %ss\n' "$timeout" >&2
      return 1
    fi
    sleep 5
  done
}

probe_served_once() {
  local web_url="$1" url rc
  for url in "${web_url%/}/api/instances/" "${web_url%/}/" "${web_url%/}/admin/"; do
    rc=0
    curl --fail --silent --max-time 10 "$url" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      # rc is curl's exit code: 22 = HTTP error status, 7 = connection
      # refused, 28 = timed out — "warming" and "dead" must not print the
      # same line (Vex, review 3954).
      printf 'probe not ready: %s (curl exit %s)\n' "$url" "$rc" >&2
      return 1
    fi
  done
}

verify_snapshot() {
  local snapshot="$1" service expected current stored running build version db
  for service in "${SERVICES[@]}"; do
    expected="$(jq -r --arg service "$service" '.images[$service][0].ref' <<<"$snapshot")"
    current="$(service_image_snapshot "$service" "$expected")" || return 1
    stored="$(jq -cS --arg service "$service" '.images[$service]' <<<"$snapshot")"
    [ "$current" = "$stored" ] || return 1
  done
  running="$(compose exec -T api sh -c 'printf "%s %s\n" "$BIPLANE_BUILD" "$BIPLANE_VERSION"')" || return 1
  read -r build version <<<"$running"
  [ "$build" = "$(jq -r .build <<<"$snapshot")" ] || return 1
  [ "$version" = "$(jq -r .release <<<"$snapshot")" ] || return 1
  db="$(database_target)" || return 1
  [ "$(jq -cS . <<<"$db")" = "$(jq -cS .database <<<"$snapshot")" ] || return 1
  local web_url
  web_url="$(env_value WEB_URL || true)"
  [ -n "$web_url" ] || return 1
  verify_served_endpoints "$web_url"
}

create_backup() {
  local directory="$1" snapshot="$2" metadata="$3"
  mkdir -m 0700 "$directory"
  cp --preserve=mode,timestamps "$ENV_FILE" "$directory/config.env"
  compose config > "$directory/compose.rendered.yaml"
  printf '%s\n' "$snapshot" > "$directory/deployment-snapshot.json"
  printf '%s\n' "$metadata" > "$directory/release.json"
  # PGPASSWORD + -h localhost: the plane-db image requires a password even
  # locally, so a bare pg_dump fails "no password supplied" and the apply aborts
  # at its first backup. Same form the deploy host's own restic-backup uses.
  compose exec -T plane-db sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -w -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$directory/database.dump"
  [ -s "$directory/database.dump" ] || die "database backup is empty"
  compose exec -T plane-db pg_restore --list < "$directory/database.dump" >/dev/null
  (cd "$directory" && sha256sum config.env compose.rendered.yaml deployment-snapshot.json release.json database.dump > SHA256SUMS)
}

verify_backup() {
  local directory="$1"
  (cd "$directory" && sha256sum --check --strict SHA256SUMS >/dev/null)
}

inspect_release_image() {
  local backend="$1" tag="$2" commit="$3" image_env build version
  image_env="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$backend")"
  version="$(sed -n 's/^BIPLANE_VERSION=//p' <<<"$image_env")"
  build="$(sed -n 's/^BIPLANE_BUILD=//p' <<<"$image_env")"
  [ "$version" = "$tag" ] || die "backend image declares BIPLANE_VERSION=$version, expected $tag"
  [[ "$build" =~ ^[0-9a-f]{7,40}$ ]] && [[ "$commit" == "$build"* ]] || \
    die "backend image build id '$build' does not match release commit $commit"
  printf '%s\n' "$build"
}

inspect_display_build() {
  local image="$1" build="$2" root="$3"
  docker run --rm --entrypoint sh "$image" -c \
    'grep -R -F -q -- "$1" "$2"' sh "$build" "$root" || \
    die "$image does not contain the backend build id $build in its displayed bundle"
}

migration_plan() {
  local backend="$1"
  BIPLANE_BACKEND_IMAGE="$backend" compose run --rm --no-deps -T \
    --entrypoint python migrator manage.py migrate --plan
}

run_migrations() {
  local backend="$1"
  BIPLANE_BACKEND_IMAGE="$backend" compose run --rm --no-deps -T \
    --entrypoint python migrator manage.py migrate --noinput
}

recreate_application() {
  compose up -d --force-recreate --no-deps "${SERVICES[@]}"
}

stop_application() {
  local running service
  if ! compose stop "${SERVICES[@]}"; then
    compose kill "${SERVICES[@]}" || return 1
  fi
  running="$(compose ps --status running --services)" || return 1
  for service in "${SERVICES[@]}"; do
    ! grep -qx "$service" <<<"$running" || return 1
  done
}

verify_running_release() {
  local tag="$1" expected_build="$2" backend="$3" web="$4" admin="$5" space="$6"
  local running build version services service expected web_url
  stage verify
  services="$(compose ps --status running --services)"
  for service in "${SERVICES[@]}"; do
    grep -qx "$service" <<<"$services" || return 1
    case "$service" in
      web) expected="$web" ;;
      admin) expected="$admin" ;;
      space) expected="$space" ;;
      *) expected="$backend" ;;
    esac
    service_image_snapshot "$service" "$expected" >/dev/null || return 1
  done
  running="$(compose exec -T api sh -c 'printf "%s %s\n" "$BIPLANE_BUILD" "$BIPLANE_VERSION"')"
  read -r build version <<<"$running"
  [ "$version" = "$tag" ] || return 1
  [ "$build" = "$expected_build" ] || return 1
  web_url="$(env_value WEB_URL || true)"
  [ -n "$web_url" ] || return 1
  verify_served_endpoints "$web_url"
}


verify_rendered_identity() {
  local env_file="$1" backend="$2" web="$3" admin="$4" space="$5" rendered
  rendered="$(compose_with_env "$env_file" config --format json)" || return 1
  jq -e \
    --arg backend "$backend" --arg web "$web" --arg admin "$admin" \
    --arg space "$space" '
      .services.web.image == $web and
      .services.admin.image == $admin and
      .services.space.image == $space and
      .services.api.image == $backend and
      .services.worker.image == $backend and
      .services["beat-worker"].image == $backend and
      .services.migrator.image == $backend
    ' <<<"$rendered" >/dev/null
}

main() {
  [ "$#" -eq 1 ] || die "usage: $0 <vMAJOR.MINOR.PATCH>"
  local tag="$1"
  release_version_valid "$tag" || die "tag is outside the accepted release version grammar"

  for command in docker jq curl awk flock sha256sum stat sync timeout; do need "$command"; done
  # Validated here, before the backup and config commit: verify_served_endpoints
  # runs only after services are swapped, and dying there would exit without
  # stop/rollback (Vex, review 3954).
  # No leading zeros: bash arithmetic reads 010 as octal 8, silently — the
  # same quietly-wrong family this validation exists to refuse (Sable).
  [[ "${BIPLANE_APPLY_HEALTH_TIMEOUT:-180}" =~ ^(0|[1-9][0-9]*)$ ]] || \
    die "BIPLANE_APPLY_HEALTH_TIMEOUT must be a whole number of seconds without leading zeros, got '${BIPLANE_APPLY_HEALTH_TIMEOUT}'"
  assert_env_target
  cd "$SELFHOST_DIR"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another Biplane update is already running"

  local metadata source level commit selected current backend web admin space build
  local old_backend old_web old_admin old_space snapshot
  metadata="$(fetch_metadata "$tag")" || die "exact release metadata fetch failed"
  jq -e --arg tag "$tag" '
    type == "object" and keys == ["release", "source"] and
    (.source == "forgejo" or .source == "github") and
    .release.tag == $tag and
    (.release.level == "code" or .release.level == "data" or .release.level == "full") and
    (.release.commit_sha | test("^[0-9a-f]{40}$")) and
    (.release.images | type == "array" and length == 4)
  ' <<<"$metadata" >/dev/null || die "metadata helper returned an incomplete release identity"
  source="$(jq -r .source <<<"$metadata")"
  level="$(jq -r .release.level <<<"$metadata")"
  commit="$(jq -r .release.commit_sha <<<"$metadata")"
  selected="$(jq -r .release.tag <<<"$metadata")"
  [ "$selected" = "$tag" ] || die "release source returned $selected for requested $tag"
  [ "$level" != "full" ] || die "$tag is level full; apply it by hand — see deployments/selfhost/MANUAL-FULL-UPGRADE.md"

  old_backend="$(env_value BIPLANE_BACKEND_IMAGE || true)"
  old_web="$(env_value BIPLANE_WEB_IMAGE || true)"
  old_admin="$(env_value BIPLANE_ADMIN_IMAGE || true)"
  old_space="$(env_value BIPLANE_SPACE_IMAGE || true)"
  for image in "$old_backend" "$old_web" "$old_admin" "$old_space"; do
    [[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || \
      die "persisted image pins are incomplete or not registry digests; establish a proven baseline manually"
  done
  verify_rendered_identity "$ENV_FILE" "$old_backend" "$old_web" "$old_admin" "$old_space" || \
    die "persisted pins do not match the rendered Compose graph"
  snapshot="$(capture_running_snapshot "$old_backend" "$old_web" "$old_admin" "$old_space")" || \
    die "running services, persisted pins, release/build identity, or database target do not form one proven baseline; the specific mismatch is named above"
  current="$(jq -r .release <<<"$snapshot")"
  if release_version_gt "$tag" "$current"; then
    :
  else
    local comparison_status="$?"
    case "$comparison_status" in
      1) die "requested release $tag is not newer than running release $current" ;;
      *) die "running release $current is not comparable" ;;
    esac
  fi

  backend="$(image_ref_for "$metadata" biplane-backend)"
  web="$(image_ref_for "$metadata" biplane-web)"
  admin="$(image_ref_for "$metadata" biplane-admin)"
  space="$(image_ref_for "$metadata" biplane-space)"

  local stamp backup_dir rendered_env plan migration_started=0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="$BACKUP_ROOT/${stamp}-${tag}"
  mkdir -p "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  stage backup
  create_backup "$backup_dir" "$snapshot" "$metadata"

  stage pull
  # Mark after each image COMPLETES, not before it starts: progress derived from
  # work finished cannot outlive the work, which a timer-driven heartbeat can.
  # Four images is ~95s of otherwise silent bar on the production board.
  for pair in "backend:$backend" "web:$web" "admin:$admin" "space:$space"; do
    docker pull "${pair#*:}"
    stage "pull-${pair%%:*}"
  done
  build="$(inspect_release_image "$backend" "$tag" "$commit")"
  inspect_display_build "$web" "$build" /usr/share/nginx/html
  inspect_display_build "$admin" "$build" /usr/share/nginx/html/admin

  plan="$(migration_plan "$backend")" || die "new-image migration plan failed"
  if [ "$level" = "code" ] && ! grep -q 'No planned migration operations' <<<"$plan"; then
    die "release is level code but the new image reports pending migrations"
  fi

  rendered_env="$(mktemp "$SELFHOST_DIR/.env.biplane-render.XXXXXX")"
  trap 'rm -f "${rendered_env:-}"' EXIT
  write_pinned_env "$ENV_FILE" "$rendered_env" "$tag" "$backend" "$web" "$admin" "$space"
  verify_rendered_identity "$rendered_env" "$backend" "$web" "$admin" "$space" || \
    die "rendered Compose graph does not bind every executing service to the selected digest set"

  if [ "$level" = "data" ]; then
    migration_started=1
    stage migrate
    stop_application || die "could not quiesce every mutation-serving application service before migration"
    if ! run_migrations "$backend"; then
      printf 'Biplane update failed: migration did not complete.\n' >&2
      printf 'Prior config remains active at %s; database backup: %s/database.dump\n' "$ENV_FILE" "$backup_dir" >&2
      printf 'Operator action is required before restarting services; do not describe this as an automatic rollback.\n' >&2
      exit 1
    fi
  fi

  if ! atomic_replace "$ENV_FILE" "$rendered_env"; then
    local failed_commit_outcome="$ATOMIC_REPLACE_OUTCOME"
    # Also an unwind (Sable 3991): the pins are being restored, so the gauge
    # must leave the forward scale here too. NOT marked on the migration-failure
    # branch above, deliberately — that one says the prior config REMAINS active
    # and refuses to call itself a rollback, so marking it would be a false
    # claim rather than a missing one.
    stage rollback
    printf 'Biplane update failed while committing image pins (outcome=%s); restoring the saved config.\n' \
      "$failed_commit_outcome" >&2
    if ! verify_backup "$backup_dir"; then
      printf 'RECOVERY REQUIRED: the pre-update backup failed checksum verification; refusing automatic pin restoration. Inspect %s.\n' \
        "$backup_dir" >&2
      exit 1
    fi
    if ! atomic_replace "$ENV_FILE" "$backup_dir/config.env"; then
      printf 'RECOVERY REQUIRED: pin commit and pin restoration both failed. Inspect %s and %s/config.env before recreating services.\n' \
        "$ENV_FILE" "$backup_dir" >&2
      exit 1
    fi
    if [ "$migration_started" -eq 1 ]; then
      printf 'RECOVERY REQUIRED: prior pins are restored but the data migration completed. Reconcile the database before restarting services.\n' >&2
      printf 'Database backup: %s/database.dump\n' "$backup_dir" >&2
    elif verify_snapshot "$snapshot"; then
      printf 'Prior pins restored and the unchanged running services still match the captured snapshot. Backup: %s\n' "$backup_dir" >&2
    else
      printf 'RECOVERY REQUIRED: prior pins are restored but the unchanged running services no longer match the captured snapshot. Backup: %s\n' \
        "$backup_dir" >&2
    fi
    exit 1
  fi
  stage restart
  if recreate_application && verify_running_release "$tag" "$build" "$backend" "$web" "$admin" "$space"; then
    rm -f "$rendered_env"
    trap - EXIT
    # Re-check immediately. Without this the board keeps its OLD idea of "latest",
  # sees itself running something newer than anything it knows about, cannot
  # classify that, and honestly reports "Update status unavailable" — so the
  # moment after a success is the moment it looks broken, and it stays that way
  # until the next hourly check. Never fatal: a failed re-check must not turn a
  # successful apply into a failed one.
  # IMPORT FROM services, NOT bgtasks (Sable 4026). The bgtasks wrapper is the
  # check PLUS the auto-apply hook, and that hook writes its once-per-tag
  # attempt record BEFORE sending the request. Called from here it would create
  # the record and then fire a request GUARANTEED to 409 — because this script
  # is the apply holding the lock — burning the tag's only attempt on a request
  # that could never succeed. Auto-apply never retries a tag, so an automation
  # the operator switched on would silently become manual. services.update_check
  # is the pure check: it refreshes the banner and cannot start anything.
  #
  # timeout is local ON PURPOSE. The network half is already bounded two modules
  # away (bounded_fetch's wall-clock deadline), but a guarantee that far from the
  # call site is invisible here — and `compose exec` itself is unbounded. A hang
  # would also strand the UI at 95%: the 100% state is synthesised on finish, so
  # it is unreachable while this blocks, leaving the operator staring at an
  # incomplete bar after a SUCCESSFUL apply.
  timeout 60 compose exec -T api python manage.py shell -c \
    'from plane.license.services.update_check import run_update_check; run_update_check()' \
    >/dev/null 2>&1 || printf 'note: post-apply update re-check did not run; the banner may lag until the next hourly check\n' >&2
  printf 'Biplane update succeeded: %s (%s, %s)\n' "$tag" "$level" "$source"
    printf 'Backup retained at %s\n' "$backup_dir"
    return 0
  fi

  stage rollback
  printf 'Biplane update failed after config commit; stopping application services before restoring prior image pins.\n' >&2
  if ! stop_application; then
    printf 'RECOVERY REQUIRED: failed activation could not be fully stopped; mutation-serving state remains unproven.\n' >&2
  fi
  if ! verify_backup "$backup_dir"; then
    printf 'RECOVERY REQUIRED: the pre-update backup failed checksum verification; refusing automatic pin restoration. Inspect %s.\n' \
      "$backup_dir" >&2
    exit 1
  fi
  if ! atomic_replace "$ENV_FILE" "$backup_dir/config.env"; then
    printf 'RECOVERY REQUIRED: saved pins could not be restored (outcome=%s). Inspect %s and %s/config.env before recreating services.\n' \
      "$ATOMIC_REPLACE_OUTCOME" "$ENV_FILE" "$backup_dir" >&2
    return 1
  fi
  if [ "$migration_started" -eq 0 ] && recreate_application && verify_snapshot "$snapshot"; then
    printf 'Prior release pins and services restored. Backup: %s\n' "$backup_dir" >&2
  else
    printf 'RECOVERY REQUIRED: prior pins are restored but a data migration was attempted or service rollback failed.\n' >&2
    printf 'Database backup: %s/database.dump. Restore/reconcile it explicitly before starting the old release.\n' "$backup_dir" >&2
  fi
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
