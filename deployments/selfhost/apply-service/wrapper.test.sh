#!/usr/bin/env bash
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# Exercise the RUNNING SERVICE over HTTP — not its functions in-process.
#
# Proving the operations an automated path performs is NOT proving the path.
# The in-process suites (operator-ops.test.py, operator-tree.test.py) import the
# module and call functions; they cannot see anything that lives in the wrapper:
# the auth gate, the route table, JSON parsing, the status codes a caller
# actually receives, or whether refusals reach the journal. Every one of those
# is where a real caller meets this service, so every one of them is tested here
# by curl against a listening process.
#
#   bash wrapper.test.sh            # needs docker for board-status, and a host
#                                   # where the deployment tree passes the check
#
# THE FIXTURE TREE MUST BE COMPLETE, and this is not a detail. validate-tag.sh
# sources release-version.sh, which reads
# apps/api/plane/license/utils/release_version.{sh,datum}. With either missing,
# EVERY tag is refused — so "an invalid tag is refused" passes while proving
# nothing, and the 409 path is unreachable because validation fails first. A
# fixture that is merely incomplete produces green tests that assert nothing,
# which is worse than a red one.
#
# It starts the service on a throwaway port with a throwaway token, runs the
# checks, and stops it.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${WRAPPER_TEST_PORT:-7699}"
TREE="${WRAPPER_TEST_TREE:-}"
FAIL=0

[ -n "$TREE" ] || { echo "set WRAPPER_TEST_TREE to a deployment tree that passes the trusted-tree check" >&2; exit 2; }

TOK="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf '%s' "$TOK" > "$TREE/state/tok"; chmod 600 "$TREE/state/tok"
LOG="$(mktemp)"

APPLY_SERVICE_REPO="$TREE/repo" APPLY_SERVICE_COMPOSE_DIR="$TREE" \
APPLY_SERVICE_STATE_DIR="$TREE/state" APPLY_SERVICE_TOKEN_FILE="$TREE/state/tok" \
APPLY_SERVICE_PORT="$PORT" APPLY_SERVICE_BIND=127.0.0.1 \
  python3 "$HERE/apply-service.py" > "$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
sleep 3
grep -q listening "$LOG" || { echo "not ok - the service did not start"; tail -5 "$LOG"; exit 1; }

B="http://127.0.0.1:$PORT"
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
is() { # is <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "not ok - $1 (want $2, got $3)"; FAIL=$((FAIL+1)); fi
}

# --- the auth gate. Not reachable from an in-process test at all.
is "no token is rejected"     401 "$(code $B/status)"
is "a wrong token is rejected" 401 "$(code -H 'Authorization: Bearer wrong' $B/status)"
is "the right token is accepted" 200 "$(code -H "Authorization: Bearer $TOK" $B/status)"

A=(-H "Authorization: Bearer $TOK")

# --- the route table is an allowlist: everything unnamed is 404 BEFORE work
is "GET  /op/board-status is served"      200 "$(code "${A[@]}" $B/op/board-status)"
is "a path-traversal suffix is not served" 404 "$(code "${A[@]}" "$B/op/board-status/../../etc")"
is "GET  an unknown /op path 404s"        404 "$(code "${A[@]}" $B/op/nope)"
is "POST an unknown /op path 404s"        404 "$(code -X POST "${A[@]}" -d '{}' $B/op/nope)"

# --- caller input over the wire, which is the only place it actually arrives
for payload in \
  '{"tag":"v1.2.6","images":[{"kind":"web; rm -rf /","build":"69117ec"}]}' \
  '{"tag":"v1.2.6","images":[{"kind":"web","build":"$(id)"}]}' \
  '{"tag":"v1.2.6","images":[{"kind":"web","build":"`id`"}]}' \
  '{"tag":"v1.2.6 && curl evil","images":[{"kind":"web","build":"69117ec"}]}' \
  '{"tag":"v1.2.6","images":[{"kind":"web","build":"69117ec\nrm -rf /"}]}' \
  '{"tag":"v1.2.6","images":[{"kind":"web","build":"--privileged"}]}' ; do
  is "refused over HTTP: ${payload:0:46}" 422 "$(code -X POST "${A[@]}" -d "$payload" $B/op/push-images)"
done
is "malformed JSON is refused"  400 "$(code -X POST "${A[@]}" -d 'not json' $B/op/push-images)"
is "an invalid apply tag is refused" 422 "$(code -X POST "${A[@]}" -d '{"tag":"latest"}' $B/apply)"

# --- the 409 path. release.sh mishandled exactly this shape (it printed the
# --- refusal and walked on to judge an earlier run's result), so the state that
# --- produces it must be reachable in a test rather than only in production.
sleep 300 & BUSY=$!
printf '%s' "$BUSY" > "$TREE/state/apply.lock"
is "a second apply while one is running is refused" 409 \
   "$(code -X POST "${A[@]}" -d '{"tag":"v1.2.6"}' $B/apply)"
busy_body="$(curl -s -X POST "${A[@]}" -d '{"tag":"v1.2.6"}' $B/apply)"
case "$busy_body" in
  *"already running"*) echo "ok   - the 409 says WHY, so a caller can distinguish it" ;;
  *) echo "not ok - the 409 body does not name the cause: $busy_body"; FAIL=$((FAIL+1)) ;;
esac
# and it must NOT have started anything
case "$busy_body" in
  *'"started"'*) echo "not ok - a refused apply reported 'started'"; FAIL=$((FAIL+1)) ;;
  *) echo "ok   - a refused apply reports no 'started' field for a caller to key on" ;;
esac
kill $BUSY 2>/dev/null; rm -f "$TREE/state/apply.lock"

# --- board-status must project fields, never pass a docker blob through
bs="$(curl -s "${A[@]}" $B/op/board-status)"
leak="$(grep -ciE 'POSTGRES_PASSWORD|SECRET_KEY|Config\.Env|RABBITMQ_DEFAULT_PASS' <<<"$bs")"
is "board-status leaks no container environment" 0 "$leak"

# --- a privileged action must never be silent, and refusals are the half a
# --- service most easily goes blind to
recs="$(grep -c '^OPERATOR ' "$LOG")"
refs="$(grep -c '"phase": "refused"' "$LOG")"
[ "$recs" -gt 0 ] && echo "ok   - operations are recorded to the journal ($recs)" || { echo "not ok - nothing recorded"; FAIL=$((FAIL+1)); }
[ "$refs" -gt 0 ] && echo "ok   - REFUSALS are recorded, not only successes ($refs)" || { echo "not ok - refusals unrecorded"; FAIL=$((FAIL+1)); }

echo
[ "$FAIL" -eq 0 ] && echo "all wrapper tests passed" || { echo "FAILED: $FAIL"; exit 1; }
