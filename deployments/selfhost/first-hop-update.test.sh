#!/usr/bin/env bash
# Executable BIP-42 first-hop contract. Docker/health edges are stubbed; the real
# first-hop-update.sh (which sources apply-update.sh), its jq parsing, config
# rendering, atomic rename, backup and rollback paths execute unchanged. Falsifies
# the deployment-update-rowan.md controls: managed/legacy mutual exclusion incl.
# the transitional third state, seed-mismatch-before-backup, resolver env/mount
# isolation (asserted against the logged docker invocation), unknown legacy
# identity, sole pre-backup seed pull, target completeness, migration admission,
# post-adoption refusal, and the resolver command-start control.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/first-hop-update.sh"
APPLY="$HERE/apply-update.sh"
RELEASE_SHIM="$HERE/../release/release-version.sh"
RELEASE_AUTH_DIR="$HERE/../../apps/api/plane/license/utils"
SEED_OK="registry.test/biplane-backend@sha256:1111111111111111111111111111111111111111111111111111111111111111"
SEED_BAD="registry.test/biplane-backend@sha256:9999999999999999999999999999999999999999999999999999999999999999"
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$PASS" "$1"; }
no() { FAIL=$((FAIL + 1)); printf 'not ok - %s\n' "$1" >&2; }

# mode: legacy (tag pins, no applied) | managed (digest pins, applied==version) |
# unstamped (digest pins, NO applied — the acceptance #2 third state)
new_fixture() {
  local mode="${1:-legacy}"
  FIXTURE="$(mktemp -d)"
  mkdir -p "$FIXTURE/bin" "$FIXTURE/deployments/selfhost" \
    "$FIXTURE/deployments/release" "$FIXTURE/apps/api/plane/license/utils"
  cp "$SCRIPT" "$FIXTURE/deployments/selfhost/first-hop-update.sh"
  cp "$APPLY" "$FIXTURE/deployments/selfhost/apply-update.sh"
  cp "$RELEASE_SHIM" "$FIXTURE/deployments/release/release-version.sh"
  cp "$RELEASE_AUTH_DIR/release_version.sh" \
    "$RELEASE_AUTH_DIR/release_version.datum" \
    "$FIXTURE/apps/api/plane/license/utils/"
  : > "$FIXTURE/deployments/selfhost/docker-compose.yml"
  : > "$FIXTURE/deployments/selfhost/docker-compose.override.yml"
  # Decoy live credentials that MUST NOT reach the ephemeral resolver.
  {
    printf 'WEB_URL=http://biplane.test\n'
    printf 'REDIS_URL=redis://real-redis:6379/0\n'
    printf 'DATABASE_URL=postgresql://plane:livesecret@plane-db/plane\n'
    printf 'BIPLANE_FORGEJO_URL=https://forge.local:3000\n'
    printf 'BIPLANE_FORGEJO_REPO=example/biplane\n'
    printf 'BIPLANE_FORGEJO_RELEASE_TOKEN=forgetoken\n'
    if [ "$mode" = legacy ]; then
      printf 'BIPLANE_BACKEND_IMAGE=registry.test/old-backend:8ca1fa6\n'
      printf 'BIPLANE_WEB_IMAGE=registry.test/old-web:8ca1fa6\n'
      printf 'BIPLANE_ADMIN_IMAGE=registry.test/old-admin:8ca1fa6\n'
      printf 'BIPLANE_SPACE_IMAGE=registry.test/old-space:8ca1fa6\n'
    else
      printf 'BIPLANE_BACKEND_IMAGE=registry.test/old-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'
      printf 'BIPLANE_WEB_IMAGE=registry.test/old-web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n'
      printf 'BIPLANE_ADMIN_IMAGE=registry.test/old-admin@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n'
      printf 'BIPLANE_SPACE_IMAGE=registry.test/old-space@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\n'
    fi
    [ "$mode" = managed ] && printf 'BIPLANE_APPLIED_RELEASE=v2.0.0\n'
  } > "$FIXTURE/deployments/selfhost/.env"

  STUB_LOG="$FIXTURE/docker.log"; : > "$STUB_LOG"
  export STUB_LOG
  cat > "$FIXTURE/bin/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%q ' "$@" >> "$STUB_LOG"; printf '\n' >> "$STUB_LOG"
args=" $* "
if [[ "$args" == *"help biplane_update_metadata"* ]]; then
  if [[ "$args" == *" compose "* ]]; then
    [ "${STUB_RUNNING_RESOLVER:-1}" = 1 ]   # is_managed resolver-present fact
  else
    [ "${STUB_RESOLVER_NOSTART:-0}" = 0 ]    # ephemeral command-start control
  fi
elif [[ "$args" == *" biplane_update_metadata "* ]]; then
  level="${STUB_LEVEL:-code}"
  tag="${STUB_META_TAG:-${!#}}"
  backend_digest="${STUB_META_BACKEND_DIGEST:-sha256:1111111111111111111111111111111111111111111111111111111111111111}"
  cat <<JSON
{"source":"forgejo","release":{"tag":"$tag","commit_sha":"1234567890abcdef1234567890abcdef12345678","level":"$level","images":[{"image":"registry.test/biplane-backend","digest":"$backend_digest"},{"image":"registry.test/biplane-web","digest":"sha256:2222222222222222222222222222222222222222222222222222222222222222"},{"image":"registry.test/biplane-admin","digest":"sha256:3333333333333333333333333333333333333333333333333333333333333333"},{"image":"registry.test/biplane-space","digest":"sha256:4444444444444444444444444444444444444444444444444444444444444444"}]}}
JSON
elif [[ "$args" == *" image inspect "* ]]; then
  printf 'BIPLANE_BUILD=1234567\nBIPLANE_VERSION=v2.0.0\n'
elif [[ "$args" == *" run --rm --entrypoint sh "* ]]; then
  [ "${STUB_DISPLAY_BUILD_MISMATCH:-0}" = 0 ]
elif [[ "$args" == *" ps -q "* ]]; then
  service="${!#}"; printf 'cid-%s\n' "$service"
elif [[ "$1" = inspect && "$args" == *" cid-"* ]]; then
  service="${!#}"; service="${service#cid-}"
  case "$service" in
    web) key=BIPLANE_WEB_IMAGE ;; admin) key=BIPLANE_ADMIN_IMAGE ;;
    space) key=BIPLANE_SPACE_IMAGE ;; *) key=BIPLANE_BACKEND_IMAGE ;;
  esac
  ref="$(sed -n "s/^${key}=//p" "$BIPLANE_SELFHOST_DIR/.env")"
  up_count=0; [ ! -f "$STUB_UP_COUNT_FILE" ] || read -r up_count < "$STUB_UP_COUNT_FILE"
  if [ "${STUB_ROLLBACK_DRIFT:-0}" != 0 ] && [ "$up_count" -ge 2 ]; then
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
  printf '%s\037%s\037%s' plane plane plane
elif [[ "$args" == *" exec -T plane-db sh "* ]]; then
  printf 'PGDUMP'
elif [[ "$args" == *" exec -T plane-db pg_restore --list "* ]]; then
  cat >/dev/null
elif [[ "$args" == *" exec -T api python "* ]]; then
  printf '{"database":"plane","host":"plane-db","port":5432,"user":"plane"}\n'
elif [[ "$args" == *" exec -T api sh "*'$DATABASE_URL'* ]]; then
  printf 'postgresql://plane:plane@plane-db/plane'
elif [[ "$args" == *" config --format json "* ]]; then
  env_file=""; previous=""
  for argument in "$@"; do [ "$previous" != --env-file ] || env_file="$argument"; previous="$argument"; done
  [ -n "$env_file" ]
  backend="$(sed -n 's/^BIPLANE_BACKEND_IMAGE=//p' "$env_file")"
  web="$(sed -n 's/^BIPLANE_WEB_IMAGE=//p' "$env_file")"
  admin="$(sed -n 's/^BIPLANE_ADMIN_IMAGE=//p' "$env_file")"
  space="$(sed -n 's/^BIPLANE_SPACE_IMAGE=//p' "$env_file")"
  cat <<JSON
{"services":{"web":{"image":"$web"},"admin":{"image":"$admin"},"space":{"image":"$space"},"api":{"image":"$backend","environment":{"DATABASE_URL":"postgresql://plane:plane@plane-db/plane","PGHOST":"plane-db","POSTGRES_PORT":"5432","POSTGRES_USER":"plane","POSTGRES_PASSWORD":"plane","POSTGRES_DB":"plane"}},"worker":{"image":"$backend"},"beat-worker":{"image":"$backend"},"migrator":{"image":"$backend","environment":{"DATABASE_URL":"postgresql://plane:plane@plane-db/plane","PGHOST":"plane-db","POSTGRES_PORT":"5432","POSTGRES_USER":"plane","POSTGRES_PASSWORD":"plane","POSTGRES_DB":"plane"}},"plane-db":{"environment":{"POSTGRES_USER":"plane","POSTGRES_PASSWORD":"plane","POSTGRES_DB":"plane"}}}}
JSON
elif [[ "$args" == *" config "* ]]; then
  printf 'services: {}\n'
elif [[ "$args" == *" ps --status running --services "* ]]; then
  stopped=0; [ ! -f "$STUB_STOP_FILE" ] || read -r stopped < "$STUB_STOP_FILE"
  [ "$stopped" != 0 ] || printf 'web\nspace\nadmin\napi\nworker\nbeat-worker\n'
elif [[ "$args" == *" exec -T api sh "*"%s %s"* ]]; then
  printf '1234567 v2.0.0\n'
elif [[ "$args" == *" up -d "* ]]; then
  count=0; [ ! -f "$STUB_UP_COUNT_FILE" ] || read -r count < "$STUB_UP_COUNT_FILE"
  printf '%s\n' "$((count + 1))" > "$STUB_UP_COUNT_FILE"
  printf '0\n' > "$STUB_STOP_FILE"
elif [[ "$args" == *" stop "* ]]; then
  printf '1\n' > "$STUB_STOP_FILE"
elif [[ "$1" = pull ]] || [[ "$args" == *" kill "* ]]; then
  :
else
  printf 'unexpected docker invocation: %s\n' "$*" >&2; exit 90
fi
STUB
  cat > "$FIXTURE/bin/curl" <<'STUB'
#!/usr/bin/env bash
[ "${STUB_HEALTH_FAIL:-0}" = 0 ] || exit 1
printf '1234567\n'
STUB
  cat > "$FIXTURE/bin/sync" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  # getent ahostsv4 <host> -> canned addresses. STUB_GETENT_ADDRS overrides
  # (space-separated); an empty value simulates an unresolvable origin.
  cat > "$FIXTURE/bin/getent" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" = ahostsv4 ] || exit 0
addrs="${STUB_GETENT_ADDRS-10.0.0.9}"
for a in $addrs; do printf '%s STREAM %s\n' "$a" "${2:-}"; done
STUB
  STUB_UP_COUNT_FILE="$FIXTURE/up.count"; STUB_STOP_FILE="$FIXTURE/stopped"
  export STUB_UP_COUNT_FILE STUB_STOP_FILE
  chmod +x "$FIXTURE/bin/docker" "$FIXTURE/bin/curl" "$FIXTURE/bin/sync" \
    "$FIXTURE/bin/getent" "$FIXTURE/deployments/selfhost/first-hop-update.sh"
  export BIPLANE_SELFHOST_DIR="$FIXTURE/deployments/selfhost"
}

cleanup_fixture() {
  rm -rf "$FIXTURE"
  unset STUB_LEVEL STUB_PLAN STUB_MIGRATE_FAIL STUB_HEALTH_FAIL STUB_META_TAG \
    STUB_META_BACKEND_DIGEST STUB_RUNNING_RESOLVER STUB_RESOLVER_NOSTART \
    STUB_DISPLAY_BUILD_MISMATCH STUB_ROLLBACK_DRIFT STUB_GETENT_ADDRS STUB_UP_COUNT_FILE STUB_STOP_FILE \
    BIPLANE_SELFHOST_DIR STUB_LOG 2>/dev/null || true
}

# Run first-hop-update.sh under the stubbed PATH; capture exit + combined output.
run_fh() {
  local tag="$1" seed="$2"
  ( PATH="$FIXTURE/bin:$PATH" \
    "$FIXTURE/deployments/selfhost/first-hop-update.sh" "$tag" "$seed" ) >"$FIXTURE/out" 2>&1
}

backup_dir_count() { find "$BIPLANE_SELFHOST_DIR/.biplane-backups" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' '; }
target_pull_count() { grep -c "^pull registry.test/biplane-\(web\|admin\|space\)" "$STUB_LOG" 2>/dev/null || true; }

# ---- 1. mutual exclusion: managed refuses, no mutation ----
new_fixture managed
STUB_LEVEL=code
if ! run_fh v2.0.0 "$SEED_OK" && grep -q 'already managed' "$FIXTURE/out" \
   && [ "$(backup_dir_count)" = 0 ] && [ "$(target_pull_count)" = 0 ]; then
  ok 'managed deployment refuses first hop with no backup or target pull'
else
  no 'managed refusal'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 2. transitional third state (digest pins, no applied): first hop PROCEEDS ----
new_fixture unstamped
STUB_LEVEL=code
if run_fh v2.0.0 "$SEED_OK" && ! grep -q 'already managed' "$FIXTURE/out"; then
  ok 'digest-pinned-but-unstamped state is handled by first hop, not refused (acceptance #2)'
else
  no 'transitional third state'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 3. legacy happy path adopts the managed state ----
new_fixture legacy
STUB_LEVEL=code
if run_fh v2.0.0 "$SEED_OK" && grep -q 'first hop succeeded' "$FIXTURE/out" \
   && grep -q '^BIPLANE_APPLIED_RELEASE=v2.0.0$' "$BIPLANE_SELFHOST_DIR/.env" \
   && grep -q '^BIPLANE_BACKEND_IMAGE=registry.test/biplane-backend@sha256:1111' "$BIPLANE_SELFHOST_DIR/.env"; then
  ok 'legacy first hop resolves, activates, and adopts managed pins'
else
  no 'legacy happy path'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 4. seed mismatch is refused BEFORE backup or target pull ----
new_fixture legacy
STUB_LEVEL=code
if ! run_fh v2.0.0 "$SEED_BAD" && grep -q 'does not match the operator seed' "$FIXTURE/out" \
   && [ "$(backup_dir_count)" = 0 ] && [ "$(target_pull_count)" = 0 ]; then
  ok 'seed-digest mismatch refuses before backup and before any target pull'
else
  no 'seed binding'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 5. resolver isolation: the ephemeral run carries no socket/mount/live cred ----
new_fixture legacy
STUB_LEVEL=code
run_fh v2.0.0 "$SEED_OK" || true
resolver_line="$(grep 'biplane_update_metadata v2.0.0' "$STUB_LOG" | grep -- '--entrypoint python' | grep -v ' compose ' | head -1)"
if [ -n "$resolver_line" ] \
   && grep -q 'REDIS_URL=redis://127.0.0.1:1/0' <<<"$resolver_line" \
   && grep -q 'add-host forge.local:10.0.0.9' <<<"$resolver_line" \
   && ! grep -q -- '-v ' <<<"$resolver_line" \
   && ! grep -q 'docker.sock' <<<"$resolver_line" \
   && ! grep -q 'real-redis' <<<"$resolver_line" \
   && ! grep -q 'livesecret' <<<"$resolver_line"; then
  ok 'ephemeral resolver: narrow --add-host origin, no mount, no socket, no live credential'
else
  no 'resolver isolation'; printf '%s\n' "$resolver_line" >&2
fi
cleanup_fixture

# ---- 5b. ambiguity is refused ONLY on the .local pin path ----
new_fixture legacy
export STUB_LEVEL=code STUB_GETENT_ADDRS="10.0.0.9 10.0.0.10"
if ! run_fh v2.0.0 "$SEED_OK" && grep -q 'ambiguous host resolution' "$FIXTURE/out" \
   && [ "$(backup_dir_count)" = 0 ]; then
  ok 'an mDNS (.local) origin resolving to multiple host addresses is refused before backup'
else
  no 'ambiguous .local refusal'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 5c. an ordinary DNS origin (multi-A) proceeds over bridge DNS, no pin ----
new_fixture legacy
export STUB_LEVEL=code STUB_GETENT_ADDRS="93.184.216.34 93.184.216.35 93.184.216.36 93.184.216.37"
sed -i 's#^BIPLANE_FORGEJO_URL=.*#BIPLANE_FORGEJO_URL=https://forge.example.com#' "$BIPLANE_SELFHOST_DIR/.env"
rline=""
if run_fh v2.0.0 "$SEED_OK" && grep -q 'first hop succeeded' "$FIXTURE/out"; then
  rline="$(grep 'biplane_update_metadata v2.0.0' "$STUB_LOG" | grep -- '--entrypoint python' | grep -v ' compose ' | head -1)"
fi
if [ -n "$rline" ] && ! grep -q -- '--add-host' <<<"$rline"; then
  ok 'an ordinary multi-address DNS origin proceeds over bridge DNS with no --add-host'
else
  no 'ordinary DNS no-pin'; printf '%s\n' "$rline" >&2; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 6. unknown legacy identity: snapshot records version/build unknown ----
new_fixture legacy
STUB_LEVEL=code
run_fh v2.0.0 "$SEED_OK" || true
snap="$(find "$BIPLANE_SELFHOST_DIR/.biplane-backups" -name deployment-snapshot.json -print -quit 2>/dev/null)"
if [ -n "$snap" ] && [ "$(jq -r .release "$snap")" = unknown ] && [ "$(jq -r .build "$snap")" = unknown ]; then
  ok 'legacy baseline records installed release/build as unknown, never inferred'
else
  no 'unknown legacy identity'; [ -n "$snap" ] && cat "$snap" >&2
fi
cleanup_fixture

# ---- 7. the seed pull is the SOLE pre-backup pull ----
new_fixture legacy
STUB_LEVEL=code
run_fh v2.0.0 "$SEED_OK" || true
# Line numbers in the docker log: the seed pull, the pg_dump (backup), the target pulls.
seed_pull_ln="$(grep -n "^pull registry.test/biplane-backend@sha256:1111" "$STUB_LOG" | head -1 | cut -d: -f1)"
dump_ln="$(grep -n 'exec -T plane-db sh' "$STUB_LOG" | head -1 | cut -d: -f1)"
web_pull_ln="$(grep -n '^pull registry.test/biplane-web@' "$STUB_LOG" | head -1 | cut -d: -f1)"
if [ -n "$seed_pull_ln" ] && [ -n "$dump_ln" ] && [ -n "$web_pull_ln" ] \
   && [ "$seed_pull_ln" -lt "$dump_ln" ] && [ "$dump_ln" -lt "$web_pull_ln" ]; then
  ok 'seed pull precedes backup; every target pull follows the verified backup'
else
  no 'sole pre-backup pull ordering'; printf 'seed=%s dump=%s web=%s\n' "$seed_pull_ln" "$dump_ln" "$web_pull_ln" >&2
fi
cleanup_fixture

# ---- 8. target completeness: a tag mismatch in the record refuses ----
new_fixture legacy
export STUB_LEVEL=code STUB_META_TAG=v9.9.9
if ! run_fh v2.0.0 "$SEED_OK" && grep -qi 'returned v9.9.9 for requested v2.0.0\|incomplete release identity' "$FIXTURE/out"; then
  ok 'a release record whose tag differs from the request is refused'
else
  no 'target completeness (tag mismatch)'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 9a. migration admission: code release with pending migrations refuses ----
new_fixture legacy
export STUB_LEVEL=code STUB_PLAN=nonempty
if ! run_fh v2.0.0 "$SEED_OK" && grep -q 'pending migrations' "$FIXTURE/out"; then
  ok 'a code release reporting pending migrations is refused'
else
  no 'migration admission (code+pending)'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 9b. migration admission: a full release is refused ----
new_fixture legacy
export STUB_LEVEL=full
if ! run_fh v2.0.0 "$SEED_OK" && grep -qi 'level full' "$FIXTURE/out"; then
  ok 'a level-full release is refused (manual full/runtime path)'
else
  no 'migration admission (full)'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 10. post-adoption refusal: activated but managed predicate unmet ----
new_fixture legacy
export STUB_LEVEL=code STUB_RUNNING_RESOLVER=0   # after activation, resolver still not provable
if ! run_fh v2.0.0 "$SEED_OK" && grep -q 'does not satisfy the managed predicate' "$FIXTURE/out"; then
  ok 'activation without an adopted managed predicate reports recovery-required'
else
  no 'post-adoption refusal'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 11. command-start control: resolver that cannot boot is named distinctly ----
new_fixture legacy
export STUB_LEVEL=code STUB_RESOLVER_NOSTART=1
if ! run_fh v2.0.0 "$SEED_OK" && grep -q 'command did not start' "$FIXTURE/out" \
   && [ "$(backup_dir_count)" = 0 ]; then
  ok 'a seed image whose resolver cannot initialize is refused before backup'
else
  no 'command-start control'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 12. rollback restores the legacy bytes and proves them by local image id ----
new_fixture legacy
export STUB_LEVEL=code STUB_HEALTH_FAIL=1 STUB_ROLLBACK_DRIFT=0
if ! run_fh v2.0.0 "$SEED_OK" \
   && grep -q 'Prior legacy release restored and proven by local image id' "$FIXTURE/out" \
   && grep -q '^BIPLANE_BACKEND_IMAGE=registry.test/old-backend:8ca1fa6$' "$BIPLANE_SELFHOST_DIR/.env"; then
  ok 'failed activation rolls back and proves the legacy bytes restored by local image id'
else
  no 'rollback restore success'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# ---- 13. a drifted local image id after snapshot makes restoration unprovable ----
new_fixture legacy
export STUB_LEVEL=code STUB_HEALTH_FAIL=1 STUB_ROLLBACK_DRIFT=1
if ! run_fh v2.0.0 "$SEED_OK" \
   && grep -q 'could not be proven restored' "$FIXTURE/out" \
   && ! grep -q 'proven by local image id' "$FIXTURE/out"; then
  ok 'a changed local image id after snapshot forces recovery-required, not a false restore claim'
else
  no 'rollback drift refusal'; cat "$FIXTURE/out" >&2
fi
cleanup_fixture

# THE REFUSAL MUST NAME SOMETHING THAT EXISTS. Both apply scripts send a
# level-full operator to a document, and for the whole life of this feature that
# document did not exist anywhere in the repository — the phrase appeared ONLY
# in the two die messages pointing at it. A refusal that names a missing file is
# a dead end at the exact moment someone needs a route, so the reference is a
# fact to check rather than a promise to keep.
#
# It bites here rather than theoretically: the FIRST release of any deployment
# derives `full` (no prior release means the pipeline baselines from the
# repository root), so this refusal is the first thing a new operator meets.
_doc="$(cd "$(dirname "$0")" && pwd)/MANUAL-FULL-UPGRADE.md"
if [ -s "$_doc" ]; then
  ok 'the level-full refusal points at a document that exists'
else
  no "level-full refusal names a missing document: $_doc"
fi
for _script in first-hop-update.sh apply-update.sh; do
  if grep -q 'MANUAL-FULL-UPGRADE.md' "$(dirname "$0")/$_script"; then
    ok "$_script sends a full release to the manual path by name"
  else
    no "$_script does not name the manual path"
  fi
done

# THE DOC'S SERVICE LIST MUST EQUAL THE SCRIPT'S. Four images back SIX services
# — BIPLANE_BACKEND_IMAGE runs api, worker and beat-worker — so a manual
# operator recreating only the four "image-shaped" services leaves two
# background workers on the OLD backend against the NEW schema. I wrote exactly
# that mistake into the first draft of the document, and caught it only by
# reading SERVICES in apply-update.sh.
#
# "Keep the two lists in step" is a request; this is the check. It compares the
# SETS, so reordering either list is fine and dropping or adding a service is not.
_d="$(cd "$(dirname "$0")" && pwd)"
_script_services="$(sed -n 's/^SERVICES=(\(.*\))$/\1/p' "$_d/apply-update.sh" | tr ' ' '\n' | sort | tr '\n' ' ')"
_doc_services="$(sed -n 's/^  web space admin api worker beat-worker$/&/p' "$_d/MANUAL-FULL-UPGRADE.md" | tr -s ' ' '\n' | sed '/^$/d' | sort | tr '\n' ' ')"
if [ -n "$_doc_services" ] && [ "$_script_services" = "$_doc_services" ]; then
  ok 'the manual document recreates exactly the services apply-update.sh does'
else
  no "manual doc service list [$_doc_services] != apply-update.sh SERVICES [$_script_services]"
fi

printf '1..%d\n' "$((PASS + FAIL))"
[ "$FAIL" -eq 0 ]
