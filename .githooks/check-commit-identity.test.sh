#!/usr/bin/env bash
# Table test for check-commit-identity.sh.
#
# Asserts two things, and the second is the one that matters most:
#   * it WARNS on a fleet-internal identity;
#   * it NEVER BLOCKS — every row must exit 0, including the warning rows.
#     A refusal here would reject an outside contributor's perfectly good
#     commit, because husky runs this for everyone who has ever run
#     `pnpm install`. See the script header.
#
# Every "warn" row below except the first two is a counterexample an earlier
# version accepted (Rowan RC 3389): the token smuggled into the DISPLAY NAME,
# a LONGER domain that merely starts with the allowed one, the allowed domain
# as the LOCAL part, and an empty local part. That version searched the whole
# rendered identity for a substring.
#
# Takes HOOK_UNDER_TEST so an older revision can be run against this table.
set -uo pipefail

CHECK="${HOOK_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check-commit-identity.sh}"
[ -x "$CHECK" ] || { echo "not executable: $CHECK" >&2; exit 2; }

pass=0 fail=0

run() { # name email -> sets RC and OUT to the CHECK's output only
  local repo; repo="$(mktemp -d)"
  # Set the repo up quietly and OUTSIDE the capture: anything the shell itself
  # writes to stderr (a locale warning, for one) would otherwise be read as
  # the check having spoken. That bug made every row look like a warning.
  ( cd "$repo" && git init -q . && : > f && git add f ) >/dev/null 2>&1
  OUT="$(
    cd "$repo" || exit 2
    GIT_AUTHOR_NAME="$1" GIT_AUTHOR_EMAIL="$2" \
    GIT_COMMITTER_NAME="$1" GIT_COMMITTER_EMAIL="$2" \
      "$CHECK" 2>&1 1>/dev/null
  )"
  RC=$?
  rm -rf "$repo"
}

check() { # expected(warn|quiet) name email
  local want="$1" name="$2" email="$3" got
  run "$name" "$email"
  # Match the check's OWN marker, not "any stderr": the interpreter itself
  # can write to stderr (a locale warning here), and reading that as the
  # check having spoken made every row look like a warning.
  if printf '%s' "$OUT" | grep -q "^commit-identity:"; then got=warn; else got=quiet; fi
  if [ "$got" = "$want" ] && [ "$RC" -eq 0 ]; then
    printf '  ok    %-5s exit=0  %s <%s>\n' "$want" "$name" "$email"; pass=$((pass+1))
  else
    printf '  FAIL  want %s exit=0, got %s exit=%s: %s <%s>\n' "$want" "$got" "$RC" "$name" "$email"; fail=$((fail+1))
  fi
}

echo "commit-identity advisory"
check quiet "Vex"                  "vex@biplane.invalid"
check quiet "Vex Opus"             "someone.else@biplane.invalid"
check warn  "Vex Opus"             "vex@fleet.example"
check warn  "Vex @biplane.invalid" "vex@fleet.example"
check warn  "Vex"                  "vex@biplane.invalid.example"
check warn  "Vex"                  "biplane.invalid@evil.com"
check warn  "Vex"                  "vex@notbiplane.invalid"
check warn  "Vex"                  "vex@BIPLANE.INVALID"
check warn  "Vex"                  "@biplane.invalid"
check warn  "Vex"                  "biplane.invalid"
# An outside contributor: warned, but NEVER blocked. exit=0 is the assertion.
check warn  "Outside Contributor"  "someone@example.com"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
