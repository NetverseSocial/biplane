#!/usr/bin/env bash
# Deterministic tests for sync-check.sh using PATH-stubbed git/docker/curl.
# Covers: happy path (valid docker tags, all four images), failed push,
# Forgejo 500 on report, merge conflict, build failure — and asserts the
# token never appears in any stubbed process's argv.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/sync-check.sh"
FAILURES=0

make_stubs() { # $1 = stub dir, $2 = argv log
  local bin="$1" alog="$2"
  mkdir -p "$bin"

  cat > "$bin/git" <<'EOF'
#!/usr/bin/env bash
echo "git $*" >> "$STUB_ARGV_LOG"
echo "gitenvkey ${GIT_CONFIG_KEY_0:-none}" >> "$STUB_ARGV_LOG"
echo "gitenvkey ${GIT_CONFIG_KEY_0:-none}" >> "$STUB_ARGV_LOG"
args=("$@"); sub=""; i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    -C|-c) i=$((i+2)); continue;;
    -*) i=$((i+1)); continue;;
    *) sub="$a"; break;;
  esac
done
case "$sub" in
  clone) last="${args[${#args[@]}-1]}"; mkdir -p "$last/.git"; exit 0;;
  rev-parse) exit 1;;                       # tag not present locally -> not merged
  merge) for a in "$@"; do [ "$a" = "--abort" ] && exit 0; done
         [ "${STUB_MERGE:-ok}" = "fail" ] && exit 1 || exit 0;;
  diff) echo "apps/web/conflicted-file.tsx"; exit 0;;
  push) [ "${STUB_PUSH:-ok}" = "fail" ] && exit 1 || exit 0;;
  *) exit 0;;
esac
EOF

  cat > "$bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >> "$STUB_ARGV_LOG"
case "$1" in
  build)
    tag=""; prev=""
    for a in "$@"; do [ "$prev" = "-t" ] && tag="$a"; prev="$a"; done
    ref_tag="${tag#*:}"
    case "$ref_tag" in */*) echo "invalid reference format" >&2; exit 125;; esac
    case "$tag" in biplane-${STUB_BUILD_FAIL_APP:-none}:*) echo "boom" >&2; exit 1;; esac
    echo "sha256:stub"; exit 0;;
  images) exit 0;;
  *) exit 0;;
esac
EOF

  cat > "$bin/curl" <<'EOF'
#!/usr/bin/env bash
echo "curl $*" >> "$STUB_ARGV_LOG"
out=""; hdr=""; w=""; post=0; prev=""
for a in "$@"; do
  case "$prev" in -o) out="$a";; -D) hdr="$a";; -w) w="$a";; esac
  [ "$a" = "-X" ] && post=1
  prev="$a"
done
if [ $post -eq 1 ]; then    # report() -> Forgejo issue POST
  cat >/dev/null            # drain stdin (-d @-)
  [ "${STUB_REPORT:-ok}" = "fail" ] && exit 22 || exit 0
fi
# GitHub latest-release GET
[ -n "$hdr" ] && printf 'etag: "stub-etag"\r\n' > "$hdr"
[ -n "$out" ] && printf '{"tag_name":"v9.9.9"}' > "$out"
[ -n "$w" ] && printf '200'
exit 0
EOF
  chmod +x "$bin/git" "$bin/docker" "$bin/curl"
}

run_case() { # $1 = name; env STUB_* preset by caller; returns via globals BASE_DIR/RC
  local name="$1"
  BASE_DIR="$(mktemp -d)"
  export STUB_ARGV_LOG="$BASE_DIR/argv.log"; : > "$STUB_ARGV_LOG"
  make_stubs "$BASE_DIR/bin" "$STUB_ARGV_LOG"
  printf 'sekrit-token-123\n' > "$BASE_DIR/forgejo.token"
  PATH="$BASE_DIR/bin:$PATH" SYNC_BASE="$BASE_DIR" FORK_REPO="example-org/biplane" \
    FORGEJO_URL="http://forgejo.example" bash "$SCRIPT"
  RC=$?
}

check() { # $1 = case, $2 = description, $3 = shell condition
  if eval "$3"; then echo "PASS [$1] $2"; else echo "FAIL [$1] $2"; FAILURES=$((FAILURES+1)); fi
}

# ---- case 1: happy path --------------------------------------------------
unset STUB_MERGE STUB_PUSH STUB_REPORT STUB_BUILD_FAIL_APP
run_case happy
check happy "exit 0" "[ $RC -eq 0 ]"
check happy "state advanced to v9.9.9" "[ \"\$(cat "$BASE_DIR/state" 2>/dev/null)\" = v9.9.9 ]"
check happy "READY reported" "grep -q 'reported: .*READY' '$BASE_DIR/last-run.log'"
check happy "4 images built" "[ \"\$(grep -c '^docker build ' '$STUB_ARGV_LOG')\" = 4 ]"
check happy "docker tags have no slash" "! grep '^docker build ' '$STUB_ARGV_LOG' | grep -oE 'biplane-[a-z]+:[^ ]+' | grep -q '/'"
check happy "tag uses sync- prefix" "grep -q 'biplane-web:sync-v9.9.9' '$STUB_ARGV_LOG'"
check happy "token never in argv" "! grep -q 'sekrit-token-123' '$STUB_ARGV_LOG'"
check happy "git auth header key is host-scoped" "grep -q 'gitenvkey http.http://forgejo.example/.extraHeader' '$STUB_ARGV_LOG'"
check happy "no unscoped extraHeader key" "! grep -qE 'gitenvkey http.extraHeader\$' '$STUB_ARGV_LOG'"
check happy "backend builds with apps/api context" "grep '^docker build ' '$STUB_ARGV_LOG' | grep 'biplane-backend:' | grep -q ' apps/api\$'"
rm -rf "$BASE_DIR"

# ---- case 2: push failure -> state NOT advanced, nonzero exit -------------
export STUB_PUSH=fail
run_case push-fail
check push-fail "exit nonzero" "[ $RC -ne 0 ]"
check push-fail "state NOT advanced" "[ ! -s '$BASE_DIR/state' ]"
check push-fail "push failure logged" "grep -q 'PUSH FAILED' '$BASE_DIR/last-run.log'"
unset STUB_PUSH; rm -rf "$BASE_DIR"

# ---- case 3: Forgejo 500 on READY report -> state NOT advanced ------------
export STUB_REPORT=fail
run_case report-fail
check report-fail "state NOT advanced" "[ ! -s '$BASE_DIR/state' ]"
check report-fail "report failure visible" "grep -q 'REPORT FAILED (state NOT advanced)' '$BASE_DIR/last-run.log'"
unset STUB_REPORT; rm -rf "$BASE_DIR"

# ---- case 4: merge conflict -> reported, state advanced -------------------
export STUB_MERGE=fail
run_case conflict
check conflict "conflict reported" "grep -q 'reported: .*MERGE CONFLICT' '$BASE_DIR/last-run.log'"
check conflict "state advanced after durable report" "[ \"\$(cat "$BASE_DIR/state" 2>/dev/null)\" = v9.9.9 ]"
check conflict "no docker build attempted" "! grep -q '^docker build ' '$STUB_ARGV_LOG'"
unset STUB_MERGE; rm -rf "$BASE_DIR"

# ---- case 5: build failure (space) -> reported with app name --------------
export STUB_BUILD_FAIL_APP=space
run_case build-fail
check build-fail "BUILD FAILED (space) reported" "grep -q 'reported: .*BUILD FAILED (space)' '$BASE_DIR/last-run.log'"
check build-fail "state advanced after durable report" "[ \"\$(cat "$BASE_DIR/state" 2>/dev/null)\" = v9.9.9 ]"
check build-fail "web built before space failed" "grep -q 'biplane-web:sync-v9.9.9' '$STUB_ARGV_LOG'"
unset STUB_BUILD_FAIL_APP; rm -rf "$BASE_DIR"

# ---- case 6: absent SYNC_BASE -> created, run proceeds to completion -------
unset STUB_MERGE STUB_PUSH STUB_REPORT STUB_BUILD_FAIL_APP
ROOT_DIR="$(mktemp -d)"
export STUB_ARGV_LOG="$ROOT_DIR/argv.log"; : > "$STUB_ARGV_LOG"
make_stubs "$ROOT_DIR/bin" "$STUB_ARGV_LOG"
ABSENT="$ROOT_DIR/not/yet/created"
PATH="$ROOT_DIR/bin:$PATH" SYNC_BASE="$ABSENT" FORGEJO_TOKEN="sekrit-token-123" \
  FORK_REPO="example-org/biplane" FORGEJO_URL="http://forgejo.example" bash "$SCRIPT"
RC=$?
check absent-base "exit 0 with freshly created base" "[ $RC -eq 0 ]"
check absent-base "base dir was created" "[ -d '$ABSENT' ]"
check absent-base "run did real work (state advanced)" "[ \"\$(cat "$ABSENT/state" 2>/dev/null)\" = v9.9.9 ]"
rm -rf "$ROOT_DIR"

# ---- case 7: uncreatable SYNC_BASE -> loud nonzero, not silent success -----
OUT=$(SYNC_BASE="/proc/definitely-not-creatable/x" FORGEJO_TOKEN="t" FORK_REPO="example-org/biplane" bash "$SCRIPT" 2>&1); RC=$?
check bad-base "exit nonzero" "[ $RC -ne 0 ]"
check bad-base "names SYNC_BASE in the error" "echo \"\$OUT\" | grep -q 'cannot create SYNC_BASE'"

# ---- case 8: missing FORK_REPO -> refuses to run ---------------------------
BASE_DIR="$(mktemp -d)"; export STUB_ARGV_LOG="$BASE_DIR/argv.log"; : > "$STUB_ARGV_LOG"
make_stubs "$BASE_DIR/bin" "$STUB_ARGV_LOG"
printf 'sekrit-token-123\n' > "$BASE_DIR/forgejo.token"
OUT=$(PATH="$BASE_DIR/bin:$PATH" SYNC_BASE="$BASE_DIR" bash "$SCRIPT" 2>&1); RC=$?
check no-repo "exit nonzero without FORK_REPO" "[ $RC -ne 0 ]"
check no-repo "message names FORK_REPO" "echo \"\$OUT\" | grep -q 'FORK_REPO is required'"
rm -rf "$BASE_DIR"

echo
if [ "$FAILURES" -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "$FAILURES FAILURES"; exit 1; fi
