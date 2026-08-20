#!/usr/bin/env bash
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# Harness for the narrow privileged applier. Same shape as the repo's other
# *.test.sh files: a sandbox repo with stub scripts, then requests against a
# live instance of the service, asserting on OBSERVED responses and the stub's
# own records — never on the service's logging.
#
# The stubs record their argv, so every "the service delegated X" claim is
# proved by the stub's record, not assumed from a status code.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0; N=0
ok()   { N=$((N+1)); PASS=$((PASS+1)); echo "ok $N - $1"; }
fail() { N=$((N+1)); FAIL=$((FAIL+1)); echo "not ok $N - $1"; }
check() { # check <desc> <cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else fail "$desc"; fi
}

SANDBOX="$(mktemp -d)"
trap 'kill "${SVC_PID:-0}" 2>/dev/null || true; rm -rf "$SANDBOX"' EXIT

# ── sandbox: a fake repo whose scripts RECORD their invocation ──────────────
REPO="$SANDBOX/repo"; COMPOSE="$SANDBOX/prod"; STATE="$SANDBOX/state"
mkdir -p "$REPO/deployments/release" "$REPO/deployments/selfhost" "$COMPOSE" "$STATE"

cat > "$REPO/deployments/release/validate-tag.sh" << 'EOF'
#!/usr/bin/env bash
echo "$@" >> "$(dirname "$0")/validate-tag.calls"
case "$1" in v[0-9]*.[0-9]*.[0-9]*) exit 0 ;; *) echo "refused: $1" >&2; exit 1 ;; esac
EOF
cat > "$REPO/deployments/selfhost/apply-update.sh" << 'EOF'
#!/usr/bin/env bash
echo "$@" >> "$(dirname "$0")/apply-update.calls"
echo "stub apply ran for $1"
sleep "${STUB_APPLY_SLEEP:-0}"
exit "${STUB_APPLY_EXIT:-0}"
EOF
chmod +x "$REPO"/deployments/*/*.sh

TOKEN_FILE="$SANDBOX/token"; printf 'test-token-123\n' > "$TOKEN_FILE"
PORT=$(( (RANDOM % 2000) + 47000 ))

APPLY_SERVICE_REPO="$REPO" APPLY_SERVICE_COMPOSE_DIR="$COMPOSE" \
APPLY_SERVICE_TOKEN_FILE="$TOKEN_FILE" APPLY_SERVICE_STATE_DIR="$STATE" \
APPLY_SERVICE_PORT="$PORT" \
  python3 "$HERE/apply-service.py" > "$SANDBOX/svc.log" 2>&1 &
SVC_PID=$!

for _ in $(seq 1 50); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/status" && break
  sleep 0.1
done

AUTH=(-H "Authorization: Bearer test-token-123")
req() { curl -s -o "$SANDBOX/body" -w '%{http_code}' "$@"; }

# ── the token is the gate, both verbs ───────────────────────────────────────
[ "$(req "http://127.0.0.1:$PORT/status")" = 401 ] \
  && ok "status without token is refused 401" || fail "status without token is refused 401"
[ "$(req -X POST -d '{"tag":"v1.2.0"}' "http://127.0.0.1:$PORT/apply")" = 401 ] \
  && ok "apply without token is refused 401" || fail "apply without token is refused 401"
[ ! -s "$REPO/deployments/selfhost/apply-update.calls" ] \
  && ok "a refused request never reached apply-update.sh (stub record empty)" \
  || fail "a refused request never reached apply-update.sh (stub record empty)"

# ── tag grammar is DELEGATED, and refusal carries the authority's words ─────
[ "$(req "${AUTH[@]}" -X POST -d '{"tag":"vgarbage"}' "http://127.0.0.1:$PORT/apply")" = 422 ] \
  && ok "invalid tag refused 422" || fail "invalid tag refused 422"
grep -q '^vgarbage$' "$REPO/deployments/release/validate-tag.calls" \
  && ok "the verdict came from validate-tag.sh (its record shows the call)" \
  || fail "the verdict came from validate-tag.sh (its record shows the call)"
[ ! -s "$REPO/deployments/selfhost/apply-update.calls" ] \
  && ok "a refused tag never started an apply" || fail "a refused tag never started an apply"

# ── a valid tag starts the wrapped command, exactly once, with the tag ──────
[ "$(req "${AUTH[@]}" -X POST -d '{"tag":"v1.2.0"}' "http://127.0.0.1:$PORT/apply")" = 202 ] \
  && ok "valid tag accepted 202" || fail "valid tag accepted 202"
sleep 0.5
[ "$(cat "$REPO/deployments/selfhost/apply-update.calls" 2>/dev/null)" = "v1.2.0" ] \
  && ok "apply-update.sh ran once with exactly the tag" \
  || fail "apply-update.sh ran once with exactly the tag"

# ── status reports the finished run's real exit code ────────────────────────
[ "$(req "${AUTH[@]}" "http://127.0.0.1:$PORT/status")" = 200 ] \
  && ok "status readable with token" || fail "status readable with token"
grep -q '"exit_code": 0' "$STATE/last-result.json" \
  && ok "last-result records exit 0" || fail "last-result records exit 0"

# ── a FAILING apply's exit code reaches the durable record ──────────────────
# (Sable RC 3798 #3: the reaper is where a failed apply becomes visible to an
#  operator, and it was only ever asserted at exit 0.)
sed -i 's/^exit .*/exit 7/' "$REPO/deployments/selfhost/apply-update.sh"
req "${AUTH[@]}" -X POST -d '{"tag":"v1.2.1"}' "http://127.0.0.1:$PORT/apply" > /dev/null
for _ in $(seq 1 30); do grep -q '"tag": "v1.2.1"' "$STATE/last-result.json" 2>/dev/null && break; sleep 0.1; done
grep -q '"exit_code": 7' "$STATE/last-result.json" \
  && ok "a failing apply records its real exit code (7)" \
  || fail "a failing apply records its real exit code (7)"
sed -i 's/^exit 7$/exit "${STUB_APPLY_EXIT:-0}"/' "$REPO/deployments/selfhost/apply-update.sh"

# ── concurrency: one apply at a time (sequential arm) ───────────────────────
: > "$REPO/deployments/selfhost/apply-update.calls"
# NOTE: the stub reads its sleep from its own file, so make the long run by
# patching the stub itself —
# exporting an env var through the service is not possible.
sed -i 's/sleep "${STUB_APPLY_SLEEP:-0}"/sleep 3/' "$REPO/deployments/selfhost/apply-update.sh"
req "${AUTH[@]}" -X POST -d '{"tag":"v1.3.0"}' "http://127.0.0.1:$PORT/apply" > /dev/null
sleep 0.3
[ "$(req "${AUTH[@]}" -X POST -d '{"tag":"v1.4.0"}' "http://127.0.0.1:$PORT/apply")" = 409 ] \
  && ok "second apply while one runs is refused 409" \
  || fail "second apply while one runs is refused 409"
# Settle until the FIRST apply's record appears, so the negative below cannot
# pass merely because the recorder had not written yet (Sable RC 3798, note).
for _ in $(seq 1 30); do grep -q 'v1.3.0' "$REPO/deployments/selfhost/apply-update.calls" 2>/dev/null && break; sleep 0.1; done
grep -q 'v1.3.0' "$REPO/deployments/selfhost/apply-update.calls" \
  && ok "the first apply's own record is present (recorder proven live)" \
  || fail "the first apply's own record is present (recorder proven live)"
! grep -q 'v1.4.0' "$REPO/deployments/selfhost/apply-update.calls" \
  && ok "the refused second apply never reached the command" \
  || fail "the refused second apply never reached the command"

# ── concurrency: TWO SIMULTANEOUS posts (the arm Vex witnessed racing) ──────
# No sleep between requests: both in flight at once. Exactly one 202, one
# 409, and exactly ONE invocation recorded. Before the check-and-start mutex
# this failed with two 202s and two invocations (RC 3801, executed).
for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$PORT/status" "${AUTH[@]}"; grep -q '"running": false' <(curl -s "${AUTH[@]}" "http://127.0.0.1:$PORT/status") && break; sleep 0.2; done
: > "$REPO/deployments/selfhost/apply-update.calls"
req "${AUTH[@]}" -X POST -d '{"tag":"v3.0.0"}' "http://127.0.0.1:$PORT/apply" > "$SANDBOX/race-a" &
A=$!
req "${AUTH[@]}" -X POST -d '{"tag":"v3.0.1"}' "http://127.0.0.1:$PORT/apply" > "$SANDBOX/race-b" &
B=$!
wait "$A" "$B"
codes="$(cat "$SANDBOX/race-a") $(cat "$SANDBOX/race-b")"
sleep 1
case "$codes" in
  "202 409"|"409 202") ok "simultaneous applies: exactly one 202 and one 409 (got: $codes)" ;;
  *) fail "simultaneous applies: exactly one 202 and one 409 (got: $codes)" ;;
esac
[ "$(wc -l < "$REPO/deployments/selfhost/apply-update.calls")" = 1 ] \
  && ok "exactly one invocation recorded under simultaneous posts" \
  || fail "exactly one invocation recorded under simultaneous posts ($(wc -l < "$REPO/deployments/selfhost/apply-update.calls") lines)"

echo "1..$N"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
