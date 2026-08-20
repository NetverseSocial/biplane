#!/usr/bin/env bash
# Executable BIP-42 host-orchestration contract. The Docker and health edges
# are stubbed; the real apply-update.sh, jq parsing, config rendering and atomic
# rename paths execute unchanged.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/apply-update.sh"
RELEASE_SHIM="$HERE/../release/release-version.sh"
RELEASE_AUTH_DIR="$HERE/../../apps/api/plane/license/utils"
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$PASS" "$1"; }
no() { FAIL=$((FAIL + 1)); printf 'not ok - %s\n' "$1" >&2; }

new_fixture() {
  FIXTURE="$(mktemp -d)"
  mkdir -p "$FIXTURE/bin" "$FIXTURE/deployments/selfhost" \
    "$FIXTURE/deployments/release" "$FIXTURE/apps/api/plane/license/utils"
  cp "$SCRIPT" "$FIXTURE/deployments/selfhost/apply-update.sh"
  cp "$RELEASE_SHIM" "$FIXTURE/deployments/release/release-version.sh"
  cp "$RELEASE_AUTH_DIR/release_version.sh" \
    "$RELEASE_AUTH_DIR/release_version.datum" \
    "$FIXTURE/apps/api/plane/license/utils/"
  : > "$FIXTURE/deployments/selfhost/docker-compose.yml"
  : > "$FIXTURE/deployments/selfhost/docker-compose.override.yml"
  cat > "$FIXTURE/deployments/selfhost/.env" <<'EOF'
APP_RELEASE=v1.3.1
BIPLANE_APPLIED_RELEASE=v1.0.0
BIPLANE_BACKEND_IMAGE=registry.test/old-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
BIPLANE_WEB_IMAGE=registry.test/old-web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
BIPLANE_ADMIN_IMAGE=registry.test/old-admin@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
BIPLANE_SPACE_IMAGE=registry.test/old-space@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
WEB_URL=http://biplane.test
EOF
  cp "$FIXTURE/deployments/selfhost/.env" "$FIXTURE/original.env"
  STUB_LOG="$FIXTURE/docker.log"
  export STUB_LOG
  cat > "$FIXTURE/bin/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$STUB_LOG"; printf '\n' >> "$STUB_LOG"
args=" $* "
if [[ "$args" == *" biplane_update_metadata "* ]]; then
  level="${STUB_LEVEL:-code}"
  tag="${!#}"
  cat <<JSON
{"source":"forgejo","release":{"tag":"$tag","commit_sha":"1234567890abcdef1234567890abcdef12345678","level":"$level","images":[{"image":"registry.test/biplane-backend","digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111"},{"image":"registry.test/biplane-web","digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222"},{"image":"registry.test/biplane-admin","digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333"},{"image":"registry.test/biplane-space","digest":"sha256:4444444444444444444444444444444444444444444444444444444444444444"}]}}
JSON
elif [[ "$args" == *" image inspect "* ]]; then
  if [ "${STUB_SHORT_BUILD:-0}" = 0 ]; then
    printf 'BIPLANE_BUILD=1234567\nBIPLANE_VERSION=v2.0.0\n'
  else
    printf 'BIPLANE_BUILD=1\nBIPLANE_VERSION=v2.0.0\n'
  fi
elif [[ "$args" == *" run --rm --entrypoint sh "* ]]; then
  [ "${STUB_DISPLAY_BUILD_MISMATCH:-0}" = 0 ]
elif [[ "$args" == *" ps -q "* ]]; then
  service="${!#}"
  printf 'cid-%s\n' "$service"
  if [ "${STUB_SECOND_REPLICA:-}" = "$service" ]; then
    printf 'cid-%s-2\n' "$service"
  fi
elif [[ "$1" = inspect && "$args" == *" cid-"* ]]; then
  service="${!#}"; service="${service#cid-}"
  base_service="${service%-2}"
  case "$base_service" in
    web) key=BIPLANE_WEB_IMAGE ;;
    admin) key=BIPLANE_ADMIN_IMAGE ;;
    space) key=BIPLANE_SPACE_IMAGE ;;
    *) key=BIPLANE_BACKEND_IMAGE ;;
  esac
  ref="$(sed -n "s/^${key}=//p" "$BIPLANE_SELFHOST_DIR/.env")"
  if [ "${STUB_ACTIVE_DRIFT:-}" = "$service" ]; then
    ref="registry.test/drifted-$service@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  fi
  if [ "${STUB_SECOND_REPLICA_DRIFT:-0}" != 0 ] && [[ "$service" == *-2 ]]; then
    ref="registry.test/drifted-replica@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  fi
  up_count=0
  [ ! -f "$STUB_UP_COUNT_FILE" ] || read -r up_count < "$STUB_UP_COUNT_FILE"
  if [ "${STUB_ROLLBACK_DRIFT:-0}" != 0 ] && [ "$up_count" -ge 2 ] \
    && [ "$(sed -n 's/^BIPLANE_APPLIED_RELEASE=//p' "$BIPLANE_SELFHOST_DIR/.env")" = v1.0.0 ]; then
    ref="registry.test/drifted-rollback@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  fi
  image_id="sha256:$(printf '%s' "$ref" | sha256sum | cut -d' ' -f1)"
  printf '%s %s\n' "$ref" "$image_id"
elif [[ "$args" == *" manage.py migrate --plan "* ]]; then
  if [ "${STUB_PLAN:-empty}" = empty ]; then
    printf 'Planned operations:\n  No planned migration operations.\n'
  else
    printf 'Planned operations:\n  db.9999_example\n'
  fi
elif [[ "$args" == *" manage.py migrate --noinput "* ]]; then
  [ "${STUB_MIGRATE_FAIL:-0}" = 0 ]
elif [[ "$args" == *" exec -T plane-db sh "*"biplane-db-proof"* ]]; then
  printf '%s\037%s\037%s' "${STUB_PLANE_DB_USER:-plane}" \
    "${STUB_PLANE_DB_PASSWORD:-plane}" "${STUB_PLANE_DB_NAME:-plane}"
elif [[ "$args" == *" exec -T plane-db sh "* ]]; then
  printf 'PGDUMP'
elif [[ "$args" == *" exec -T plane-db pg_restore --list "* ]]; then
  cat >/dev/null
elif [[ "$args" == *" exec -T api python "* ]]; then
  if [ "${STUB_DATABASE_MISMATCH:-0}" = 0 ]; then
    printf '{"database":"plane","host":"plane-db","port":5432,"user":"plane"}\n'
  else
    exit 1
  fi
elif [[ "$args" == *" exec -T api sh "*'$DATABASE_URL'* ]]; then
  printf 'postgresql://plane:plane@plane-db/plane'
elif [[ "$args" == *" config --format json "* ]]; then
  env_file=""
  previous=""
  for argument in "$@"; do
    [ "$previous" != --env-file ] || env_file="$argument"
    previous="$argument"
  done
  [ -n "$env_file" ]
  backend="$(sed -n 's/^BIPLANE_BACKEND_IMAGE=//p' "$env_file")"
  web="$(sed -n 's/^BIPLANE_WEB_IMAGE=//p' "$env_file")"
  admin="$(sed -n 's/^BIPLANE_ADMIN_IMAGE=//p' "$env_file")"
  space="$(sed -n 's/^BIPLANE_SPACE_IMAGE=//p' "$env_file")"
  migrator_database_url=postgresql://plane:plane@plane-db/plane
  [ "${STUB_MIGRATOR_DB_MISMATCH:-0}" = 0 ] || migrator_database_url=postgresql://plane:plane@external-db/plane
  plane_db_user="${STUB_PLANE_DB_USER:-plane}"
  plane_db_password="${STUB_PLANE_DB_PASSWORD:-plane}"
  plane_db_name="${STUB_PLANE_DB_NAME:-plane}"
  if [ "${STUB_RENDER_MISMATCH:-0}" != 0 ] && [[ "$env_file" == *.env.biplane-render.* ]]; then
    web="registry.test/biplane-web@sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
  fi
  cat <<JSON
{"services":{"web":{"image":"$web"},"admin":{"image":"$admin"},"space":{"image":"$space"},"api":{"image":"$backend","environment":{"DATABASE_URL":"postgresql://plane:plane@plane-db/plane","PGHOST":"plane-db","POSTGRES_PORT":"5432","POSTGRES_USER":"plane","POSTGRES_PASSWORD":"plane","POSTGRES_DB":"plane"}},"worker":{"image":"$backend"},"beat-worker":{"image":"$backend"},"migrator":{"image":"$backend","environment":{"DATABASE_URL":"$migrator_database_url","PGHOST":"plane-db","POSTGRES_PORT":"5432","POSTGRES_USER":"plane","POSTGRES_PASSWORD":"plane","POSTGRES_DB":"plane"}},"plane-db":{"environment":{"POSTGRES_USER":"$plane_db_user","POSTGRES_PASSWORD":"$plane_db_password","POSTGRES_DB":"$plane_db_name"}}}}
JSON
elif [[ "$args" == *" compose "*" config "* ]]; then
  printf 'services: {}\n'
elif [[ "$args" == *" ps --status running --services "* ]]; then
  stopped=0
  [ ! -f "$STUB_STOP_FILE" ] || read -r stopped < "$STUB_STOP_FILE"
  if [ "$stopped" = 0 ]; then
    printf 'web\nspace\nadmin\napi\nworker\nbeat-worker\n'
  fi
elif [[ "$args" == *" exec -T api sh "*"%s %s"* ]]; then
  release="$(sed -n 's/^BIPLANE_APPLIED_RELEASE=//p' "$BIPLANE_SELFHOST_DIR/.env")"
  if [ "$release" = v2.0.0 ]; then
    printf '1234567 v2.0.0\n'
  else
    printf '7654321 %s\n' "${STUB_CURRENT_RELEASE:-$release}"
  fi
elif [[ "$args" == *" exec -T api sh "* ]]; then
  printf '%s\n' "${STUB_CURRENT_RELEASE:-v1.0.0}"
elif [[ "$args" == *" up -d "* ]]; then
  count=0
  [ ! -f "$STUB_UP_COUNT_FILE" ] || read -r count < "$STUB_UP_COUNT_FILE"
  printf '%s\n' "$((count + 1))" > "$STUB_UP_COUNT_FILE"
  printf '0\n' > "$STUB_STOP_FILE"
  if [ "${STUB_CORRUPT_BACKUP:-0}" != 0 ]; then
    backup_config="$(find "$BIPLANE_SELFHOST_DIR/.biplane-backups" -name config.env -print -quit)"
    printf '# corrupted after backup\n' >> "$backup_config"
  fi
elif [[ "$args" == *" stop web "* ]]; then
  printf '1\n' > "$STUB_STOP_FILE"
elif [[ "$1" = pull ]] || [[ "$args" == *" kill web "* ]]; then
  :
else
  printf 'unexpected docker invocation: %s\n' "$*" >&2
  exit 90
fi
STUB
# apply-update.sh discards curl's stdout and reads only its exit status
# (probe_served_once), so the stub produces no body. STUB_HEALTH_CURL_RC picks
# which failure the probe has to name: 7 refused, 22 HTTP status, 28 timed out.
cat > "$FIXTURE/bin/curl" <<'STUB'
#!/usr/bin/env bash
[ "${STUB_HEALTH_FAIL:-0}" = 0 ] || exit "${STUB_HEALTH_CURL_RC:-7}"
STUB
  cat > "$FIXTURE/bin/sync" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
count_file="${STUB_SYNC_COUNT_FILE:?}"
count=0
[ ! -f "$count_file" ] || read -r count < "$count_file"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
[ "$count" != "${STUB_SYNC_FAIL_AT:-0}" ]
STUB
  # Health-fail arms must not poll a 180s deadline; 0 = one attempt.
  export BIPLANE_APPLY_HEALTH_TIMEOUT=0
  STUB_SYNC_COUNT_FILE="$FIXTURE/sync.count"
  STUB_UP_COUNT_FILE="$FIXTURE/up.count"
  STUB_STOP_FILE="$FIXTURE/stopped"
  export STUB_SYNC_COUNT_FILE STUB_UP_COUNT_FILE STUB_STOP_FILE
  chmod +x "$FIXTURE/bin/docker" "$FIXTURE/bin/curl" "$FIXTURE/bin/sync" "$FIXTURE/deployments/selfhost/apply-update.sh"
}

cleanup_fixture() {
  rm -rf "$FIXTURE"
  unset STUB_LEVEL STUB_PLAN STUB_MIGRATE_FAIL STUB_HEALTH_FAIL STUB_HEALTH_CURL_RC STUB_RENDER_MISMATCH
  unset STUB_CURRENT_RELEASE STUB_DISPLAY_BUILD_MISMATCH
  unset STUB_SHORT_BUILD
  unset STUB_SYNC_FAIL_AT STUB_SYNC_COUNT_FILE
  unset STUB_DATABASE_MISMATCH
  unset STUB_MIGRATOR_DB_MISMATCH
  unset STUB_PLANE_DB_USER STUB_PLANE_DB_PASSWORD STUB_PLANE_DB_NAME
  unset STUB_ACTIVE_DRIFT STUB_ROLLBACK_DRIFT STUB_UP_COUNT_FILE
  unset STUB_SECOND_REPLICA STUB_SECOND_REPLICA_DRIFT
  unset STUB_STOP_FILE
  unset STUB_CORRUPT_BACKUP
}

run_apply() {
  run_apply_tag v2.0.0
}

run_apply_tag() {
  PATH="$FIXTURE/bin:$PATH" BIPLANE_SELFHOST_DIR="$FIXTURE/deployments/selfhost" \
    "$FIXTURE/deployments/selfhost/apply-update.sh" "$1"
}

new_fixture
if run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q '^BIPLANE_APPLIED_RELEASE=v2.0.0$' "$FIXTURE/deployments/selfhost/.env" \
  && grep -q '^BIPLANE_BACKEND_IMAGE=registry.test/biplane-backend@sha256:1111' "$FIXTURE/deployments/selfhost/.env" \
  && grep -q 'biplane_update_metadata v2.0.0' "$STUB_LOG" \
  && [ "$(grep -c '^pull ' "$STUB_LOG")" -eq 4 ]; then
  ok 'explicit-tag code apply commits the exact four digest pins'
else
  cat "$FIXTURE/err" >&2; no 'explicit-tag code apply'
fi
cleanup_fixture

# BIP-42 (7of9, from the v1.1.0 manual apply): the backup pg_dump MUST
# authenticate. plane-db requires a password even for a local connection, so a
# bare `pg_dump` fails "no password supplied" and the apply aborts at its very
# first backup — the silent-shaped failure the manual run hit. The stub logs
# every docker invocation, so gate the PGPASSWORD form (it precedes pg_dump on
# the same exec line). Without the fix in apply-update.sh this assertion is RED.
new_fixture
if run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -Eq 'PGPASSWORD.*pg_dump' "$STUB_LOG"; then
  ok 'backup pg_dump authenticates with PGPASSWORD (plane-db needs a password even locally)'
else
  cat "$FIXTURE/err" >&2; no 'backup pg_dump PGPASSWORD form'
fi
cleanup_fixture

new_fixture
sed -i '/^WEB_URL=/d' "$FIXTURE/deployments/selfhost/.env"
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && ! grep -q '^pull ' "$STUB_LOG" \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'missing health origin refuses before backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'baseline health-origin ownership'
fi
cleanup_fixture

# The validation lives in main(), before the backup and the config commit, and
# this case exists to keep it there: verify_served_endpoints runs only after
# recreate_application has swapped services, so dying inside it would exit past
# stop/rollback (Vex, review 3954). Moving the check back down leaves the
# refusal message identical, so the assertions below are all about position --
# the env file untouched, $STUB_LOG never created (docker never invoked at all),
# no backup directory.
new_fixture
export BIPLANE_APPLY_HEALTH_TIMEOUT=abc
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'BIPLANE_APPLY_HEALTH_TIMEOUT must be a whole number' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && [ ! -s "$STUB_LOG" ] \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'non-numeric health timeout refuses before backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'health-timeout validation ordering'
fi
cleanup_fixture

new_fixture
export BIPLANE_APPLY_HEALTH_TIMEOUT=010
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'without leading zeros' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && [ ! -s "$STUB_LOG" ] \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'leading-zero health timeout refuses before backup or pull (octal trap)'
else
  cat "$FIXTURE/err" >&2; no 'leading-zero health-timeout refusal'
fi
cleanup_fixture

new_fixture
export STUB_PLANE_DB_USER=backup-user
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG" \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'plane-db-only user override refuses before backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'plane-db runtime/rendered user binding'
fi
cleanup_fixture

new_fixture
export STUB_PLANE_DB_NAME=backup-db
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG" \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'plane-db-only database override refuses before backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'plane-db runtime/rendered database binding'
fi
cleanup_fixture

new_fixture
export STUB_DATABASE_MISMATCH=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG" \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'external database target refuses before backup, pull or mutation'
else
  cat "$FIXTURE/err" >&2; no 'backup-versus-migration database target binding'
fi
cleanup_fixture

new_fixture
export STUB_MIGRATOR_DB_MISMATCH=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG"; then
  ok 'migrator database target mismatch refuses before backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'api-versus-migrator database wiring'
fi
cleanup_fixture

new_fixture
export STUB_ACTIVE_DRIFT=worker
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG"; then
  ok 'same-version active image drift refuses before backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'active-versus-persisted image baseline binding'
fi
cleanup_fixture

new_fixture
# 7of9's Pi5 case (2026-08-18): ONE service hot-swapped to a preview image while
# .env still pins the released digest. The apply refused correctly — but the
# single sentence "…do not form one proven baseline" covers ~30 distinct states
# across six services, so it cost a whole diagnosis cycle to learn WHICH.
# Two properties are pinned here, and both are regression traps:
#   1. the refusal NAMES the service and BOTH refs (revert to generic -> red);
#   2. the reason goes to STDERR, never stdout — capture_running_snapshot emits
#      its JSON on stdout into a command substitution, so a reason printed there
#      is spliced into the snapshot and corrupts it silently ("tidying"
#      snap_fail into echo -> red).
export STUB_ACTIVE_DRIFT=web
web_pin="$(sed -n 's/^BIPLANE_WEB_IMAGE=//p' "$FIXTURE/deployments/selfhost/.env")"
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q "running web image is 'registry.test/drifted-web@sha256:" "$FIXTURE/err" \
  && grep -qF "but the persisted pin is '$web_pin'" "$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && ! grep -q 'baseline mismatch' "$FIXTURE/out" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG"; then
  ok 'a baseline mismatch names the service and both refs, on stderr only'
else
  cat "$FIXTURE/err" >&2; no 'specific-cause reporting for a baseline mismatch'
fi
cleanup_fixture

new_fixture
export STUB_SECOND_REPLICA=worker STUB_SECOND_REPLICA_DRIFT=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && ! grep -q '^pull ' "$STUB_LOG"; then
  ok 'drift in a second active replica refuses the baseline'
else
  cat "$FIXTURE/err" >&2; no 'all-replica active identity binding'
fi
cleanup_fixture

new_fixture
export STUB_CURRENT_RELEASE=v1.5.0
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'do not form one proven baseline' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG" \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'stale configured baseline refuses before backup, pull or mutation'
else
  cat "$FIXTURE/err" >&2; no 'configured-versus-running baseline binding'
fi
cleanup_fixture

new_fixture
export STUB_DISPLAY_BUILD_MISMATCH=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'does not contain the backend build id' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env"; then
  ok 'frontend bundle build mismatch refuses before config commit'
else
  cat "$FIXTURE/err" >&2; no 'displayed-build identity binding'
fi
cleanup_fixture

new_fixture
export STUB_SYNC_FAIL_AT=2
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && grep -q 'directory sync failed after pin rename' "$FIXTURE/err" \
  && grep -q 'outcome=renamed_not_durable' "$FIXTURE/err" \
  && grep -q 'Prior pins restored and the unchanged running services still match' "$FIXTURE/err" \
  && [ "$(cat "$FIXTURE/sync.count")" -eq 4 ]; then
  ok 'post-rename sync failure restores old pins and reports the uncertain commit'
else
  cat "$FIXTURE/err" >&2; no 'post-rename pin-commit failure recovery'
fi
cleanup_fixture

new_fixture
export STUB_SYNC_FAIL_AT=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && grep -q 'outcome=pre_rename' "$FIXTURE/err"; then
  ok 'pre-rename pin failure is distinguished and leaves old pins intact'
else
  cat "$FIXTURE/err" >&2; no 'pre-rename pin-commit outcome'
fi
cleanup_fixture

new_fixture
export STUB_SHORT_BUILD=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q "backend image build id '1' does not match" "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env"; then
  ok 'short build prefix cannot satisfy release or displayed-build identity'
else
  cat "$FIXTURE/err" >&2; no 'minimum build identity width'
fi
cleanup_fixture

new_fixture
export STUB_LEVEL=full
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && ! grep -q '^pull ' "$STUB_LOG" \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'full release refuses before backup, pull or config mutation'
else
  no 'full release fail-closed ordering'
fi
cleanup_fixture

new_fixture
export STUB_PLAN=nonempty
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'level code.*pending migrations' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env"; then
  ok 'code release with a migration plan refuses with old pins intact'
else
  cat "$FIXTURE/err" >&2; no 'code-level migration admission guard'
fi
cleanup_fixture

new_fixture
export STUB_RENDER_MISMATCH=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'rendered Compose graph does not bind' "$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env"; then
  ok 'rendered service/digest mismatch refuses before config commit'
else
  cat "$FIXTURE/err" >&2; no 'rendered digest-set binding'
fi
cleanup_fixture

new_fixture
export STUB_HEALTH_FAIL=1 STUB_SYNC_FAIL_AT=3
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'saved pins could not be restored (outcome=pre_rename)' "$FIXTURE/err" \
  && grep -q '^BIPLANE_APPLIED_RELEASE=v2.0.0$' "$FIXTURE/deployments/selfhost/.env"; then
  ok 'activation rollback pin-write failure reports recovery_required'
else
  cat "$FIXTURE/err" >&2; no 'activation rollback pin-write outcome'
fi
cleanup_fixture

new_fixture
export STUB_HEALTH_FAIL=1 STUB_CORRUPT_BACKUP=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'backup failed checksum verification' "$FIXTURE/err" \
  && grep -q '^BIPLANE_APPLIED_RELEASE=v2.0.0$' "$FIXTURE/deployments/selfhost/.env" \
  && [ "$(grep -c ' stop web ' "$STUB_LOG")" -eq 1 ]; then
  ok 'corrupt saved config refuses rollback and reports recovery_required'
else
  cat "$FIXTURE/err" >&2; no 'rollback backup checksum enforcement'
fi
cleanup_fixture

new_fixture
export STUB_HEALTH_FAIL=1 STUB_ROLLBACK_DRIFT=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'RECOVERY REQUIRED' "$FIXTURE/err" \
  && ! grep -q 'Prior release pins and services restored' "$FIXTURE/err"; then
  ok 'code rollback never claims restoration when old snapshot readback fails'
else
  cat "$FIXTURE/err" >&2; no 'old-snapshot rollback verification'
fi
cleanup_fixture

new_fixture
export STUB_LEVEL=data STUB_PLAN=nonempty STUB_HEALTH_FAIL=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'recovery' < <(tr '[:upper:]' '[:lower:]' < "$FIXTURE/err") \
  && [ "$(grep -c ' stop web ' "$STUB_LOG")" -eq 2 ] \
  && [ "$(cat "$FIXTURE/up.count")" -eq 1 ]; then
  ok 'partial data activation is stopped before returning recovery_required'
else
  cat "$FIXTURE/err" >&2; no 'partial data activation stop'
fi
cleanup_fixture

new_fixture
export STUB_HEALTH_FAIL=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && [ "$(grep -c ' up -d ' "$STUB_LOG")" -eq 2 ]; then
  ok 'post-commit health failure atomically restores prior pins and services'
else
  cat "$FIXTURE/err" >&2; no 'post-commit rollback'
fi
cleanup_fixture

# "warming" and "dead" must not print the same line: the probe reports curl's
# exit code, so an operator reading a rollback can tell a refused port from an
# HTTP error (Vex, review 3954; live arms 7/22/28 on the board).
new_fixture
export STUB_HEALTH_FAIL=1 STUB_HEALTH_CURL_RC=22
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'probe not ready: http://biplane.test/api/instances/ (curl exit 22)' "$FIXTURE/err" \
  && ! grep -q '(curl exit 7)' "$FIXTURE/err"; then
  ok 'a failed probe names curl exit 22, not just the url'
else
  cat "$FIXTURE/err" >&2; no 'probe failure cause reporting'
fi
cleanup_fixture

new_fixture
export STUB_LEVEL=data STUB_PLAN=nonempty STUB_MIGRATE_FAIL=1
if ! run_apply >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && cmp -s "$FIXTURE/original.env" "$FIXTURE/deployments/selfhost/.env" \
  && grep -q 'Operator action is required' "$FIXTURE/err" \
  && ! grep -q ' up -d ' "$STUB_LOG"; then
  ok 'migration failure retains old pins and names operator recovery'
else
  cat "$FIXTURE/err" >&2; no 'migration failure state'
fi
cleanup_fixture

# The apply command, not only the authority functions, owns the selection gate.
for candidate in v1.0.0 v0.9.9; do
  new_fixture
  if ! run_apply_tag "$candidate" >"$FIXTURE/out" 2>"$FIXTURE/err" \
    && grep -q "requested release $candidate is not newer than running release v1.0.0" "$FIXTURE/err" \
    && ! grep -q '^pull ' "$STUB_LOG" \
    && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
    ok "apply refuses non-newer release $candidate before backup or pull"
  else
    cat "$FIXTURE/err" >&2; no "apply-flow release ordering for $candidate"
  fi
  cleanup_fixture
done

new_fixture
if ! run_apply_tag v01.2.3 >"$FIXTURE/out" 2>"$FIXTURE/err" \
  && grep -q 'outside the accepted release version grammar' "$FIXTURE/err" \
  && [ ! -s "$STUB_LOG" ] \
  && [ ! -d "$FIXTURE/deployments/selfhost/.biplane-backups" ]; then
  ok 'apply refuses incomparable tag before transport, backup or pull'
else
  cat "$FIXTURE/err" >&2; no 'apply-flow release grammar'
fi
cleanup_fixture

# Pure selection/version controls execute the shared authority without Docker.
# shellcheck source=apply-update.sh
source "$SCRIPT"
if release_version_gt v2.0.0 v1.9.9 \
  && ! release_version_gt v2.0.0 v2.0.0 \
  && ! release_version_gt v2.0.0 v2.0.1 \
  && ! release_version_valid latest \
  && ! release_version_valid v01.2.3; then
  ok 'only a strictly newer explicit stable tag is admissible'
else
  no 'semantic version admission'
fi

printf '1..%d\n' "$((PASS + FAIL))"
[ "$FAIL" -eq 0 ]
