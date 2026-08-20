#!/usr/bin/env bash
# biplane release pipeline (BIP-40): refuse an under-labeled release.
#   enforce-level.sh <declared> <derived>   -> exit 0 ok, exit 1 refuse
# The operator-run preflight gate (deployments/release/README.md) derives the
# minimum from the diff (derive-level.sh) and the declared level comes from the
# changelog entry; declaring LOWER than derived refuses the release
# (doc 7b5234e: impossible to publish, not inadvisable).
set -euo pipefail
rank() { case "$1" in code) echo 0 ;; data) echo 1 ;; full) echo 2 ;; *) echo "enforce-level: unknown level '$1'" >&2; exit 2 ;; esac; }
declared_rank=$(rank "${1:?declared}") ; derived_rank=$(rank "${2:?derived}")
if [ "$declared_rank" -lt "$derived_rank" ]; then
  echo "REFUSED: declared level '$1' is below the diff-derived minimum '$2'" >&2
  exit 1
fi
echo "level ok: declared '$1' >= derived minimum '$2'"
