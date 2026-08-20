#!/usr/bin/env bash
# Hook-level tests for .husky/pre-commit (Rowan RC 3416).
#
# The checker's own table proves what the checker decides; it cannot see the
# hook's EXIT SEMANTICS, which is where the real risk lives. Two properties,
# and the second was broken by the commit that added the advisory:
#
#   1. the advisory can never block — including when it is missing (127) or
#      non-executable (126), both reachable by a bad checkout;
#   2. lint-staged failure STILL blocks.
#
# Runs the actual .husky/pre-commit with pnpm stubbed.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0 fail=0

# lint_rc advisory_state -> RC
run() {
  local lint_rc="$1" advisory="$2" work
  work="$(mktemp -d)"
  mkdir -p "$work/bin" "$work/.husky" "$work/.githooks"
  printf '#!/bin/sh\nexit %s\n' "$lint_rc" > "$work/bin/pnpm"
  chmod +x "$work/bin/pnpm"
  cp "$ROOT/.husky/pre-commit" "$work/.husky/pre-commit"
  case "$advisory" in
    present)        cp "$ROOT/.githooks/check-commit-identity.sh" "$work/.githooks/"
                    chmod +x "$work/.githooks/check-commit-identity.sh" ;;
    non_executable) cp "$ROOT/.githooks/check-commit-identity.sh" "$work/.githooks/"
                    chmod -x "$work/.githooks/check-commit-identity.sh" ;;
    missing)        ;;
  esac
  ( cd "$work" && PATH="$work/bin:$PATH" \
      GIT_AUTHOR_NAME=X GIT_AUTHOR_EMAIL=x@biplane.invalid \
      GIT_COMMITTER_NAME=X GIT_COMMITTER_EMAIL=x@biplane.invalid \
      sh .husky/pre-commit ) >/dev/null 2>&1
  local rc=$?
  rm -rf "$work"
  return $rc
}

check() { # want_block lint_rc advisory label
  local want="$1" got
  if run "$2" "$3"; then got=allows; else got=blocks; fi
  if [ "$got" = "$want" ]; then
    printf '  ok    %-6s  %s\n' "$want" "$4"; pass=$((pass+1))
  else
    printf '  FAIL  want %s got %s: %s\n' "$want" "$got" "$4"; fail=$((fail+1))
  fi
}

echo "husky pre-commit exit semantics"
check allows 0 present        "clean: lint passes, advisory warns"
check allows 0 missing        "advisory MISSING must not block (bare call exits 127)"
check allows 0 non_executable "advisory NON-EXECUTABLE must not block (bare call exits 126)"
check blocks 1 present        "lint-staged FAILS: must still block"
check blocks 1 missing        "lint-staged fails + advisory missing: still blocks"
check blocks 1 non_executable "lint-staged fails + advisory non-exec: still blocks"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
