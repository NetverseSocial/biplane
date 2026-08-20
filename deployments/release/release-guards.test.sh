#!/usr/bin/env bash
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# The orchestrator's apply guards, exercised against a REAL applier.
#
# release.sh once reported a release as successful when nothing had been
# applied: an HTTP error was exit 0 with an error body, so a 409 "an apply is
# already running" printed the refusal and the script walked on to judge this
# release by an EARLIER run's exit code (Sable 3996). The service side of that
# shape is covered in apply-service/wrapper.test.sh; this is the CONSUMING side,
# which is where the wrong conclusion was actually drawn.
#
# Three properties, each of which failed differently before the fix:
#   1. an HTTP error must not read as success        (the op helper dies)
#   2. a refused apply must not be treated as started (`started` absent)
#   3. an unchanged last-result must not be read as this run's outcome
#
# WHAT THIS SUITE VERIFIES, AND WHAT IT DOES NOT (Sable 4011, Sia 4013).
#
# This suite verifies that the APPLIER can support the orchestrator's guards,
# and re-implements the orchestrator sequence inline to do so. It does NOT
# execute release.sh — no test does; checked rather than assumed, three files in
# deployments/ mention it and none run it. So if a guard is removed from
# release.sh, nothing here reds, with ONE deliberate exception: property 4 also
# pins the complete identity comparison as a source read, so deleting that
# specific guard does red this suite.
#
# THE GAP IS NOT INHERENT, and the earlier draft of this header said it was —
# which would have been a claim that quietly retires the fix. Running release.sh
# END TO END does require performing a real release. Its GUARDS do not: the
# identity block is four lines of pure logic over a JSON string with no side
# effects. Extracted into one sourceable helper that release.sh and this suite
# both source, the test would exercise the real production code with no release
# happening. That is the executable-boundary pattern, and it is filed in
# docs/future-changes.md rather than left to be remembered.
#
#   RELEASE_GUARDS_URL=http://127.0.0.1:7699 RELEASE_GUARDS_TOKEN=... \
#     bash release-guards.test.sh
#
# Needs an applier reachable at that URL with a tree that passes its own checks.

set -uo pipefail
URL="${RELEASE_GUARDS_URL:?set RELEASE_GUARDS_URL}"
TOKEN="${RELEASE_GUARDS_TOKEN:?set RELEASE_GUARDS_TOKEN}"
FAIL=0
# Clean up on EVERY exit path. Previously the lock was removed only where the
# script reached the end, so a failure left a stale lock behind and the NEXT run
# saw a busy applier for no reason — a test that damages what it tests (Sia 4006).
cleanup() {
  [ -n "${BUSY:-}" ] && kill "$BUSY" 2>/dev/null
  [ -n "${LOCK:-}" ] && rm -f "$LOCK"
  # a planted last-result must never outlive the run that planted it
  [ -n "${RESULT_FILE:-}" ] && [ -f "$RESULT_FILE.bak" ] && mv "$RESULT_FILE.bak" "$RESULT_FILE"
  return 0
}
trap cleanup EXIT INT TERM
is() { if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "not ok - $1 (want $2, got $3)"; FAIL=$((FAIL+1)); fi; }

# The helper under test, copied in shape from release.sh: the point is that an
# HTTP >= 400 must terminate rather than return an error body to a caller who
# might print it and continue.
op() {
  local method="$1" path="$2" body="${3:-}" out code
  if [ -n "$body" ]; then
    out="$(curl -sS -w '\n%{http_code}' -X "$method" -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' -d "$body" "$URL$path")" || return 9
  else
    out="$(curl -sS -w '\n%{http_code}' -X "$method" -H "Authorization: Bearer $TOKEN" "$URL$path")" || return 9
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  [ "$code" -lt 400 ] 2>/dev/null || return 8
  printf '%s' "$out"
}

echo "=== 1. an HTTP error must not read as success ==="
op GET /status >/dev/null 2>&1; is "a good call succeeds" 0 "$?"
TOKEN_SAVE="$TOKEN"; TOKEN="wrong-token"
op GET /status >/dev/null 2>&1; is "a 401 makes the helper FAIL, not return a body" 8 "$?"
TOKEN="$TOKEN_SAVE"
op GET /op/nope >/dev/null 2>&1; is "a 404 makes the helper FAIL" 8 "$?"

echo
echo "=== 2. a refused apply must not be treated as started ==="
# make the applier busy so a valid tag gets a genuine 409
sleep 45 & BUSY=$!
# NOTE: no apostrophes in a ${VAR:?message} default — inside the braces a bare
# single quote opens a string bash never closes, and the parse error surfaces
# many lines later at whatever `if` it swallowed.
LOCK="${RELEASE_GUARDS_LOCK:?set RELEASE_GUARDS_LOCK to the apply.lock path}"
printf '%s' "$BUSY" > "$LOCK"

# This is EXACTLY the sequence release.sh runs. Before the fix, `started` was
# never checked and the script proceeded to grade an earlier run.
# Assert the 409 ITSELF, not merely the absence of `started`. If the lock trick
# silently fails, the applier is not busy and this POST starts a REAL apply —
# and "no started field" would then be the wrong reason to pass, while the test
# had just triggered the thing it exists to avoid (Sia 4006).
busy_code="$(curl -sS -o /tmp/rg-busy.json -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tag":"v1.2.6"}' "$URL/apply")"
is "a busy applier answers 409 (not 202 — which would mean we STARTED one)" 409 "$busy_code"
started="$(jq -r '.started // empty' < /tmp/rg-busy.json 2>/dev/null)"
is "and carries no 'started' value for a caller to key on" "" "$started"
if [ "$busy_code" = "409" ] && [ -z "$started" ]; then
  echo "ok   - the orchestrator would REFUSE here (it dies on empty started)"
else
  echo "not ok - the orchestrator would proceed"; FAIL=$((FAIL+1))
fi

echo
echo "=== 3. an unchanged last-result must not be read as this run's outcome ==="
before="$(op GET /status)" || { echo "not ok - could not read status"; FAIL=$((FAIL+1)); }
baseline="$(jq -r '.last_result.finished_at // "none"' <<<"$before")"
after="$(op GET /status)"
now="$(jq -r '.last_result.finished_at // "none"' <<<"$after")"
is "with no run in between, finished_at is unchanged" "$baseline" "$now"
# release.sh dies when these are equal. NOTE the limit, stated rather than
# implied: this asserts an EQUIVALENT comparison of our own, not the one
# release.sh makes — the file tests shapes, as its header says of op(). The
# pin in property 4 is what ties a comparison to production (Sia 4012).
[ "$now" = "$baseline" ] && echo "ok   - the orchestrator would REFUSE (outcome UNKNOWN, not assumed)" \
                         || { echo "not ok - baseline comparison did not detect the unchanged result"; FAIL=$((FAIL+1)); }

kill $BUSY 2>/dev/null; rm -f "$LOCK"; BUSY=""

echo
echo "=== 4. a CHANGED last-result is not necessarily THIS run's ==="
# Properties 1-3 are behavioural against a real applier; this one must be too.
# An earlier version asserted (a) a jq `// "none"` value is non-empty — a
# tautology that printed ok even when the applier exposed no identity at all —
# and (b) a source grep for the check in release.sh. Sia 4009 proved (b)
# insufficient by the mutation that actually happens: keep the ran_tag
# assignment, delete only the comparison-and-die block, and the grep still
# passes. That is what a later "simplification" looks like.
#
# So: plant a finished result for a DIFFERENT run — fresh finished_at, exit 0 —
# and run the exact sequence release.sh runs. This produces the combination
# properties 1-3 never do: baseline CHANGED, exit_code 0, WRONG RUN. Without the
# identity check that reads as this release succeeding.
RESULT_FILE="$(dirname "$LOCK")/last-result.json"
[ -f "$RESULT_FILE" ] && cp "$RESULT_FILE" "$RESULT_FILE.bak" 2>/dev/null

printf '{"tag":"v0.0.0-baseline","exit_code":0,"finished_at":1.0}' > "$RESULT_FILE"
baseline="$(op GET /status | jq -r '.last_result.finished_at // "none"')"

# another run finishes — successfully — between our poll and our read
printf '{"tag":"v0.0.0-other","exit_code":0,"finished_at":2.0}' > "$RESULT_FILE"
status="$(op GET /status)"
now="$(jq -r '.last_result.finished_at // "none"' <<<"$status")"
code="$(jq -r '.last_result.exit_code // "unknown"' <<<"$status")"
ran_tag="$(jq -r '.last_result.tag // "none"' <<<"$status")"

is "the decoy looks like success to the property-3 guard (changed)" "true" \
   "$([ "$now" != "$baseline" ] && echo true || echo false)"
is "and to an exit-code check" "0" "$code"
# the ONLY thing that distinguishes it is identity
if [ "$ran_tag" != "v1.2.6" ]; then
  echo "ok   - identity refuses it: last_result is for '$ran_tag', not the tag we applied"
else
  echo "not ok - a different run's success was accepted as ours"; FAIL=$((FAIL+1))
fi

if [ -f "$RESULT_FILE.bak" ]; then mv "$RESULT_FILE.bak" "$RESULT_FILE"; else rm -f "$RESULT_FILE"; fi

# The demonstration above proves the PROPERTY is real. It does not prove the
# ORCHESTRATOR has it: with no executable reference to release.sh, deleting the
# comparison there leaves every property here green (Sia 4012). The previous
# version had a weak tie — a grep for `last_result.tag`, which caught
# delete-everything and missed delete-only-the-comparison — and replacing it
# with a stronger demonstration removed the tie altogether.
#
# So pin the COMPLETE expression, alongside the demo rather than instead of it:
# the demo shows the property is real, the pin shows production implements it.
# Presence of an identifier is not a guard; the whole comparison is.
# (Still a source read, with every limit of one. The structural answer is a
# single sourceable helper both call — the executable-boundary pattern — which
# is a follow-up ticket, not this PR.)
RELEASE_SH="$(dirname "$0")/release.sh"
if grep -qF '[ "$ran_tag" = "$TAG" ]' "$RELEASE_SH"; then
  echo "ok   - release.sh pins the whole comparison, not just the field name"
else
  echo "not ok - release.sh does not compare ran_tag to TAG — the property is"
  echo "         demonstrated here but NOT implemented in the orchestrator"
  FAIL=$((FAIL+1))
fi

echo
[ "$FAIL" -eq 0 ] && echo "all release-guard tests passed" || { echo "FAILED: $FAIL"; exit 1; }
