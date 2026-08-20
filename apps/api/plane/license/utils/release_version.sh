#!/usr/bin/env bash
# THE release version authority, shell side (BIP-40).
#
# THE DOMAIN IS ONE DATUM, PACKAGE-CARRIED. Both languages read
# apps/api/plane/license/utils/release_version.datum — inside the Python
# package, because the API image copies only apps/api. One key, one line, one
# strict pattern, so the shell and Python parsers cannot differ in strictness
# (Morrow RC 3492: sed accepted malformed JSON that Python refused).
#
# THE PATH IS POLICY, NOT A PARAMETER. An earlier revision honoured a
# RELEASE_VERSION_DATUM override, which makes the authority caller-mutable —
# the opposite of an authority.
#
# Nothing derived is stored. The upper accepted value and the first refused
# value are COMPUTED here and asserted by the harness; an earlier revision
# carried them in the datum, which was a second encoding of the same fact
# inside the file that exists to remove second encodings.
# Re-source guard: readonly below would abort a caller that sources twice.
if [ "${_RELEASE_VERSION_LOADED-}" = "1" ]; then return 0 2>/dev/null || true; fi
_RELEASE_VERSION_LOADED=1

_rv_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_rv_datum_file="$_rv_here/release_version.datum"

_rv_fail() { echo "release-version: $1" >&2; return 1; }

# THE DATUM IS ONE CANONICAL COMPLETE VALUE — not a key/value line, because two
# readers of a line format drifted: Python stripped whitespace and int()-
# normalised while shell matched raw text and evaluated it arithmetically, so
# " MAX_COMPONENT_DIGITS=9" and "...=09" were accepted by one and refused by the
# other, and both ignored trailing garbage (Morrow, RC 3492/3496).
#
# Both readers now validate the COMPLETE BYTES under the same constraints:
# canonical decimal, 1-2 digits, no leading zero, no whitespace, nothing else.
# Exposed as a function taking a path so the shared malformed-datum matrix can
# exercise it; the AUTHORITY still reads only its own fixed path.
_rv_read_datum() {
  # THE SHELL VARIABLE IS NOT THE BYTE BOUNDARY. Command substitution strips
  # ALL trailing newlines and drops NUL, so "9\n\n" and "9\0" reached the
  # checks as a clean "9" and were accepted here while Python's fullmatch
  # refused them (Morrow, RC 3497). The bytes are validated as bytes, via od,
  # before anything becomes a variable.
  local f="$1" bytes n
  [ -f "$f" ] && [ -r "$f" ] || return 1
  bytes="$(od -An -v -tu1 < "$f" | tr -s ' \n' ' ')"
  set -- $bytes
  n=$#
  # 1-2 digit bytes, then AT MOST one LF, and nothing else at all.
  case "$n" in 1|2|3) ;; *) return 1 ;; esac
  [ "$1" -ge 49 ] && [ "$1" -le 57 ] || return 1        # [1-9], no leading zero
  if [ "$n" -ge 2 ]; then
    if [ "$2" -eq 10 ]; then
      [ "$n" -eq 2 ] || return 1                          # LF must be last
    else
      [ "$2" -ge 48 ] && [ "$2" -le 57 ] || return 1     # second digit
      [ "$n" -eq 2 ] || [ "$3" -eq 10 ] || return 1      # only an LF may follow
    fi
  fi
  # Emit the validated digit bytes themselves — never a re-read of the file.
  if [ "$n" -ge 2 ] && [ "$2" -ne 10 ]; then
    printf "\\$(printf '%03o' "$1")\\$(printf '%03o' "$2")"
  else
    printf "\\$(printf '%03o' "$1")"
  fi
}
RV_MAX_DIGITS="$(_rv_read_datum "$_rv_datum_file")" \
  || { _rv_fail "datum is not a single canonical width: $_rv_datum_file"; return 1 2>/dev/null || exit 1; }

_rv_component="(0|[1-9][0-9]{0,$((RV_MAX_DIGITS - 1))})"
RV_PATTERN="^v${_rv_component}\.${_rv_component}\.${_rv_component}$"

# THE AUTHORITY IS NOT CALLER-MUTABLE. Sourcing it must not leave a caller able
# to widen the grammar by assignment: RV_PATTERN=.* accepted "v1.2.3;x" and
# RV_MAX_DIGITS=1 changed every key (Rowan, RC 3494) — the same defect as the
# datum-path override, one layer in. The re-source guard above is what lets
# these be readonly without a second `source` aborting the caller.
readonly RV_MAX_DIGITS RV_PATTERN

release_version_valid() {
  # A NEWLINE IS REJECTED FIRST: grep -E anchors ^ and $ PER LINE, so a value
  # containing one passes a line-anchored test while being a different string.
  case "${1-}" in *"
"*) return 1 ;; esac
  printf '%s' "${1-}" | grep -qE "$RV_PATTERN"
}

# Zero-pad by STRING. A %d-family conversion would reintroduce an integer bound
# in the one file whose purpose is not having one.
_rv_pad() { local s="$1"; while [ "${#s}" -lt "$RV_MAX_DIGITS" ]; do s="0$s"; done; printf '%s' "$s"; }

release_version_key() {
  release_version_valid "$1" || { echo "release-version: '${1-}' is outside the accepted release grammar" >&2; return 2; }
  local rest="${1#v}" a b c
  a="${rest%%.*}"; rest="${rest#*.}"; b="${rest%%.*}"; c="${rest#*.}"
  printf '%s.%s.%s\n' "$(_rv_pad "$a")" "$(_rv_pad "$b")" "$(_rv_pad "$c")"
}

release_version_gt() {
  local x y; x="$(release_version_key "$1")" || return 2; y="$(release_version_key "$2")" || return 2
  [ "$x" \> "$y" ]
}

# Computed, never stored.
release_version_upper_accepted() { local n; n="$(_rv_pad "")"; n="${n//0/9}"; printf 'v%s.%s.%s\n' "$n" "$n" "$n"; }
release_version_first_refused()  { local n; n="1$(_rv_pad "")"; printf 'v%s.0.0\n' "$n"; }

# CLI so another language can obtain THIS implementation's verdict rather than
# reimplementing it. `valid <v>` -> exit 0/1 ; `gt <a> <b>` -> exit 0/1.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1-}" in
    valid) release_version_valid "${2-}" ;;
    gt)    release_version_gt "${2-}" "${3-}" ;;
    *)     echo "usage: release_version.sh valid <v> | gt <a> <b>" >&2; exit 64 ;;
  esac
fi
