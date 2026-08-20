#!/usr/bin/env bash
# Shell-side controls. The CASES live in release-version-corpus.tsv, which the
# Python parity test reads too — so a domain change that reaches only one
# language turns one of the two red rather than shipping as a divergence.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/release-version.sh"
_corpus="$here/../../apps/api/plane/license/utils/release_version_corpus.tsv"
_auth="$here/../../apps/api/plane/license/utils/release_version.sh"
pass=0; fail=0

# FAIL CLOSED ON A MISSING CORPUS. Vex, RC 3502: with either TSV absent this
# harness exited 0 having silently dropped 39 cases — a test that passes
# because it never ran what it claims. The counts below are the real guard:
# every non-comment row must be EXECUTED, not merely readable.
_require_corpus() {
  # NOTE: this runs inside $( ), so `exit` here would only kill the SUBSHELL —
  # the harness would print FATAL and then fail later for an unrelated reason,
  # which is the same fails-for-the-wrong-reason trap. It RETURNS non-zero and
  # the caller exits.
  [ -r "$1" ] || { echo "FATAL: corpus unreadable: $1" >&2; return 2; }
  [ -s "$1" ] || { echo "FATAL: corpus empty: $1" >&2; return 2; }
  # The counter and the loop must agree on what a comment IS. grep tolerating
  # leading whitespace while `case "$verdict" in ''|'#'*)` does not made a
  # space-indented comment a comment to one and an unknown verdict to the other
  # (Vex, RC 3505 — found by attacking the class, not a mechanism I named).
  # COMMENTS ARE COLUMN-0 ONLY, in both, stated rather than implied.
  local rows; rows="$(grep -cvE '^(#|$)' "$1")"
  [ "$rows" -gt 0 ] || { echo "FATAL: corpus has no cases: $1" >&2; return 2; }
  printf '%s' "$rows"
}
_ran_corpus=0
_ran_matrix=0
_corpus_rows="$(_require_corpus "$_corpus")" || exit 2
ok()  { pass=$((pass+1)); }
bad() { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
while IFS=$'\t' read -r verdict a b; do
  case "$verdict" in ''|'#'*) continue ;; esac
  case "$verdict" in
    accept) release_version_valid "$a" && ok || bad "accept $(printf '%q' "$a")" ;;
    refuse) release_version_valid "$a" && bad "REFUSE $(printf '%q' "$a")" || ok ;;
    gt)     release_version_gt "$a" "$b" && ok || bad "$a > $b" ;;
    ngt)    release_version_gt "$a" "$b" && bad "!($a > $b)" || ok ;;
    *)      bad "unknown verdict $verdict" ;;
  esac
  _ran_corpus=$((_ran_corpus + 1))
done < "$_corpus"

# Shell-only properties, not expressible in the shared corpus.
release_version_valid "v1.2.3
v9.9.9" && bad "a validator must validate the VALUE, not a LINE" || ok
[ "$RV_MAX_DIGITS" = "$(cat "$_rv_datum_file")" ] \
  && ok || bad "width is not the datum's complete bytes"
# Derived edges are COMPUTED, never stored in the datum.
grep -qiE 'upper_accepted|first_refused' "$_rv_datum_file" \
  && bad "datum stores a derived value" || ok
[ "$(release_version_upper_accepted)" = "v999999999.999999999.999999999" ] && ok || bad "upper accepted"
[ "$(release_version_first_refused)" = "v1000000000.0.0" ] && ok || bad "first refused"
release_version_valid "$(release_version_upper_accepted)" && ok || bad "upper accepted must validate"
release_version_valid "$(release_version_first_refused)" && bad "first refused must NOT validate" || ok
case "$RV_PATTERN" in *"{0,$((RV_MAX_DIGITS - 1))}"*) ok ;; *) bad "pattern bound not derived from the datum" ;; esac

# CONVERSION-KILL CONTROLS (Rowan RC 3491, Morrow RC 3490 numeric half).
# Behavioural, not a grep: if any numeric conversion survived, a digit string
# longer than any integer type would come back changed.
_huge="$(printf '9%.0s' $(seq 1 40))"
[ "$(_rv_pad "$_huge")" = "$_huge" ] && ok || bad "padding altered a 40-digit string — a numeric conversion survived"
[ "$(_rv_pad 7)" = "000000007" ] && ok || bad "padding does not zero-fill to the datum width"
# Textual: no %d-family conversion anywhere outside comments.
grep -vE '^[[:space:]]*#' "$_auth" | grep -qE '%[0-9*]*d|10#' \
  && bad "a %d-family conversion or base-10 coercion is present" || ok
# The ONLY arithmetic is the derived pattern bound, never a version value.
[ "$(grep -vE '^[[:space:]]*#' "$_auth" | grep -c '\$((')" = 1 ] \
  && ok || bad "arithmetic appears somewhere other than the derived bound"

# THE SHIM MUST FORWARD, and the CLI must answer — the parity test in Python
# invokes exactly this entry point, so if it breaks, that test silently stops
# comparing anything (Rowan RC 3493).
bash "$_auth" valid v1.2.3 && ok || bad "CLI: valid v1.2.3"
bash "$_auth" valid v01.2.3 && bad "CLI: v01.2.3 must be refused" || ok
bash "$_auth" gt v1.2.4 v1.2.3 && ok || bad "CLI: gt"
bash "$_auth" 2>/dev/null; [ $? = 64 ] && ok || bad "CLI: usage exit"

# IMMUTABILITY CONTROLS (Rowan RC 3494). The authority must not be widenable by
# assignment after sourcing — the same defect as the datum-path override, one
# layer in. Run in a child shell because a readonly assignment aborts the shell
# that attempts it, which is the behaviour we want and cannot assert inline.
# A readonly assignment ABORTS the attempting shell, which is the behaviour we
# want: the control asserts the attempt never yields a widened verdict.
_widen_attempt() { bash -c '. "$1" 2>/dev/null; RV_PATTERN=".*"; release_version_valid "v1.2.3;x" && echo WIDENED' _ "$1" 2>/dev/null; }
[ -z "$(_widen_attempt "$_auth")" ] && ok || bad "grammar widenable by assignment (authority)"
[ -z "$(_widen_attempt "$here/release-version.sh")" ] && ok || bad "grammar widenable by assignment (shim)"
_width_attempt() { bash -c '. "$1" 2>/dev/null; RV_MAX_DIGITS=1; release_version_key v1.2.3' _ "$1" 2>/dev/null; }
[ -z "$(_width_attempt "$_auth")" ] && ok || bad "width mutable by assignment"
# ...and the value is still right when nobody tampers.
[ "$(release_version_key v1.2.3)" = "000000001.000000002.000000003" ] && ok || bad "key under normal use"

# SHARED MALFORMED-DATUM MATRIX (Morrow RC 3496). The corpus varies version
# VALUES; nothing varied the AUTHORITY DATUM, which is why two readers drifted
# on it undetected. The Python adapter runs the same file.
_dm="$here/../../apps/api/plane/license/utils/release_version_datum_matrix.tsv"
_dm_rows="$(_require_corpus "$_dm")" || exit 2
while IFS=$'\t' read -r _v _bytes _note; do
  case "$_v" in ''|'#'*) continue ;; esac
  _tmp="$(mktemp)"; printf "$_bytes" > "$_tmp"
  if _rv_read_datum "$_tmp" >/dev/null 2>&1; then _got=accept; else _got=refuse; fi
  [ "$_got" = "$_v" ] && ok || bad "datum matrix: expected $_v for $(printf %q "$_bytes"), got $_got"
  rm -f "$_tmp"
  _ran_matrix=$((_ran_matrix + 1))
done < "$_dm"

# EXECUTED == DECLARED. Readable is not enough: a corpus can be present and
# still not run (a bad IFS, an early break, a renamed column). These two lines
# are what actually caught the missing-corpus defect.
[ "$_ran_corpus" = "$_corpus_rows" ] && ok \
  || bad "version corpus: executed $_ran_corpus of $_corpus_rows declared rows"
[ "$_ran_matrix" = "$_dm_rows" ] && ok \
  || bad "datum matrix: executed $_ran_matrix of $_dm_rows declared rows"
# The datum reader must say WHY it refused, not merely that it did.
_diag="$(_rv_read_datum /nonexistent/datum 2>&1; _rv_fail "probe" 2>&1)"
case "$_diag" in *release-version*) ok ;; *) bad "datum failure carries no diagnostic: $_diag" ;; esac

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
