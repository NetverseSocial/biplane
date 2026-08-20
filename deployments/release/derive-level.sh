#!/usr/bin/env bash
# biplane release pipeline (BIP-40): derive the MINIMUM release level from the
# diff itself (doc 7b5234e §M5.1, Morrow 3305). The operator-run preflight gate
# enforces it (see deployments/release/README.md): a hand-declared level lower
# than the derived minimum REFUSES the release — a signed but mistaken `code`
# label must be impossible to publish, not merely inadvisable.
#
#   derive-level.sh <prev-ref> <ref>     -> prints code|data|full
#
# Rules (doc-normative):
#   full  — lockfile/dependency, Docker/base/runtime or packaging changes
#   data  — schema migrations
#   code  — everything else
# Order of precedence: full > data > code (the maximum wins).
set -euo pipefail

PREV="${1:?usage: derive-level.sh <prev-ref> <ref>}"
REF="${2:?usage: derive-level.sh <prev-ref> <ref>}"

changed="$(git diff --name-only "${PREV}..${REF}")"

level="code"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in
    # --- full: dependencies, runtime, packaging -------------------------
    pnpm-lock.yaml|package.json|*/package.json) level="full" ;;
    # ONE PATTERN FOR THE WHOLE REQUIREMENTS FAMILY, not a list of the members
    # that existed when this was written. It used to be
    # `apps/api/requirements/*|apps/api/requirements.txt`, which covered the
    # DIRECTORY and the legacy file — and silently missed
    # `apps/api/requirements.lock`, the hash-locked file the API Dockerfile
    # actually installs from (BIP-48 added it after this case was written).
    # A lock-only change — a hash refresh, an added hash for an unchanged
    # version, or a hand-edited lock, which is precisely the compromised-index
    # substitution the hashes exist to stop — therefore derived `code` and
    # skipped this gate. Both halves were individually correct; the file was
    # simply in neither (Vex, found by running this case statement rather than
    # reading it).
    apps/api/requirements*) level="full" ;;
    *Dockerfile*|*docker-compose*) level="full" ;;
    deployments/*) level="full" ;;
    .github/workflows/*) level="full" ;;
    turbo.json|pnpm-workspace.yaml|setup.sh) level="full" ;;
    # --- data: schema migrations ---------------------------------------
    apps/api/plane/db/migrations/*|apps/api/plane/*/migrations/*)
      [ "$level" = "full" ] || level="data" ;;
  esac
done <<< "$changed"

echo "$level"
