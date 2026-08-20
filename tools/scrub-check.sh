#!/usr/bin/env bash
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# Scrub gate: refuse to let this repository ship to a public host while it
# carries secrets, PII, or internal-infrastructure references — in the TREE or
# anywhere in HISTORY. A public push publishes every commit ever made, not the
# checkout you happen to be looking at: a credential deleted last month still
# ships in the clone.
#
#   tools/scrub-check.sh                      full gate: tree + all history
#   tools/scrub-check.sh --baseline-from <remote>   full gate; the script asks
#       the PUBLISH TARGET what it currently serves (git ls-remote <remote>
#       main) and scopes the denylist HISTORY layer to commits not reachable
#       from that. Primary form: the gate measures the baseline in the same
#       breath as it uses it, so no caller can hand it a value (Vex 4058 — a
#       caller-supplied internal-main sha would otherwise exempt the entire
#       internal lineage and end on the tool's strongest claim).
#   tools/scrub-check.sh --baseline <sha>     offline form of the same scope,
#       for when the remote is unreachable at gate time. The sha must be an
#       ANCESTOR of HEAD — by construction that accepts the squash's public
#       base (its parent) and refuses internal main (whose TREE the squash
#       carries, but never the commit as a parent). The operator owns the
#       claim that <sha> is actually published; the verdict names the
#       exemption so that claim is visible, not silent.
#       Neither form ever affects the TREE layer or the gitleaks layer.
#       Material reachable from the baseline is already published, so
#       republishing it adds no new exposure; findings there are grandfathered
#       and fixed forward (enumerated record: one unscoped run, kept — see the
#       runbook's triage rule).
#   tools/scrub-check.sh --tree-only          fast pass over the checkout ONLY —
#                                             a working aid, NOT a publish gate,
#                                             and it says so in its output
#
# EXIT CODES — a caller must treat anything but 0 as "do not publish":
#   0  clean (within the declared scope)
#   1  findings
#   2  the scan itself FAILED — neither clean nor findings. The distinction
#      exists because a broken scan that reported "clean" would be the exact
#      defect this gate exists to prevent (Vex 4048: the history layer
#      previously converted its own errors into "clean" via `|| true`).
#
# Two layers, one report:
#   1. gitleaks' stock rules — keys, tokens, high-entropy strings. Adopted, not
#      rebuilt: secrets detection is prior art (install: apt-get install
#      gitleaks; Ubuntu archive, distro-signed).
#   2. a PROJECT DENYLIST (tools/scrub-denylist.txt) of the leakage no generic
#      scanner can know: internal hostnames, LAN addresses, private org names,
#      real email domains, home directory paths. One regex per line, `#`
#      comments allowed. Additions need only a line, not a code change.
#
# The FULL run fails closed: any finding, either layer, exits 1; any scan
# error exits 2. There is no --force and no allowlist flag; a finding that is
# genuinely fine is excluded by narrowing the denylist line, or by a reasoned
# .gitleaksignore fingerprint, in a REVIEWED commit — so every exception has
# an author and a reason in history.
#
# The procedure that drives this gate is docs/operations/publish-runbook.md —
# the script names its caller and the caller names the script, so neither can
# be found without the other.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DENYLIST="$REPO_ROOT/tools/scrub-denylist.txt"
TREE_ONLY=""
BASELINE=""
BASELINE_SRC=""
ERRF="$(mktemp)"
trap 'rm -f "$ERRF"' EXIT
while [ $# -gt 0 ]; do
  case "$1" in
    --tree-only) TREE_ONLY="--tree-only"; shift ;;
    --baseline)
      [ -n "${2:-}" ] || { printf 'scrub-check: --baseline requires a value\n' >&2; exit 2; }
      BASELINE="$2"; BASELINE_SRC="caller-supplied"; shift 2 ;;
    --baseline-from)
      [ -n "${2:-}" ] || { printf 'scrub-check: --baseline-from requires a remote\n' >&2; exit 2; }
      # Measure the publish target in the same breath as the scan. A remote
      # that cannot answer is exit 2: the scan could not establish its scope.
      BASELINE="$(git ls-remote "$2" main 2>"$ERRF" | cut -f1)" \
        || { printf 'scrub-check: SCAN FAILED — could not read main from %s: %s\n' "$2" "$(head -c 200 "$ERRF")" >&2; exit 2; }
      [ -n "$BASELINE" ] || { printf 'scrub-check: SCAN FAILED — remote %s reported no main branch\n' "$2" >&2; exit 2; }
      BASELINE_SRC="measured from $2"
      shift 2 ;;
    *) printf 'scrub-check: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

die()  { printf 'scrub-check: SCAN FAILED — %s\n' "$*" >&2; exit 2; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command '$1' is unavailable (apt-get install $1)"; }

need git; need gitleaks; need grep; need xargs
[ -f "$DENYLIST" ] || die "denylist missing: $DENYLIST"
cd "$REPO_ROOT"

findings=0

echo "=== 1/2 gitleaks (stock rules) ==="
if [ "$TREE_ONLY" = "--tree-only" ]; then
  gitleaks detect --source . --no-git --redact -v || findings=1
else
  # Full history: every blob in every commit. This is what a push publishes.
  gitleaks detect --source . --redact -v || findings=1
fi

echo
echo "=== 1b/2 inline gitleaks:allow annotations (derived every run — never a hand list) ==="
# An allow-comment suppresses by POSITION and survives a change to the VALUE,
# so its defense is visibility: enumerate every annotation where the reviewer
# already looks, and refuse any marker carrying no reason (Vex 4058).
set +e
ALLOWS="$(git grep -n "gitleaks:allow" -- . ":(exclude)tools/scrub-check.sh" ":(exclude)docs/operations/publish-runbook.md" 2>"$ERRF")"
rc=$?
set -e
[ "$rc" -le 1 ] || die "allow-annotation scan failed (git grep exit $rc): $(head -c 200 "$ERRF")"
if [ -n "$ALLOWS" ]; then
  printf '%s\n' "$ALLOWS" | sed 's/^/  /'
  bare="$(printf '%s\n' "$ALLOWS" | grep -E 'gitleaks:allow[[:space:]]*$' || true)"
  if [ -n "$bare" ]; then
    printf 'BARE gitleaks:allow (no reason after the marker) — refused:\n%s\n' "$(printf '%s\n' "$bare" | sed 's/^/  /')"
    findings=1
  fi
else
  echo "  none"
fi

echo
echo "=== 2/2 project denylist ==="
# Strip comments/blanks; every remaining line is one extended regex.
PATTERNS="$(grep -vE '^\s*(#|$)' "$DENYLIST" || true)"
[ -n "$PATTERNS" ] || die "denylist has no active patterns — refusing to pass on an empty check"

# --- tree ---
tree_hits=0
while IFS= read -r pat; do
  # The denylist file itself legitimately contains every pattern; exclude it.
  # git grep: 0 = matches, 1 = none, >1 = ERROR — and an error is neither.
  set +e
  hits="$(git grep -nIE "$pat" -- . ":(exclude)tools/scrub-denylist.txt" 2>"$ERRF")"
  rc=$?
  set -e
  [ "$rc" -le 1 ] || die "tree scan failed for '$pat' (git grep exit $rc): $(head -c 200 "$ERRF")"
  if [ -n "$hits" ]; then
    printf 'TREE  %s\n%s\n' "$pat" "$(printf '%s\n' "$hits" | sed 's/^/  /')"
    tree_hits=1
  fi
done <<< "$PATTERNS"
[ "$tree_hits" = "0" ] && echo "tree: clean"

# --- history ---
hist_hits=0
if [ "$TREE_ONLY" != "--tree-only" ]; then
  # Baseline scoping: exempt commits the public remote already serves. The
  # exemption is printed as a count so the scope is a stated fact, not a
  # silent narrowing; a baseline that exempts everything prints "0 new" —
  # loudly true when a push adds no commits, loudly suspicious otherwise.
  REV_LIMIT=""
  if [ -n "$BASELINE" ]; then
    git cat-file -e "$BASELINE^{commit}" 2>/dev/null || die "baseline $BASELINE is not a commit in this repository"
    # The baseline must be an ANCESTOR of what is being published. By the
    # squash's construction (commit-tree M^{tree} -p P) this accepts P and
    # refuses internal main M: S carries M's tree but never M as a parent —
    # so the one value that would exempt the entire internal lineage cannot
    # pass (Vex 4058, construction argument Sable 4059).
    git merge-base --is-ancestor "$BASELINE" HEAD 2>/dev/null \
      || die "baseline $BASELINE is not an ancestor of HEAD — it cannot describe what this push builds on"
    total=$(git rev-list --all --count)
    BASE_NEW=$(git rev-list --all --count ^"$BASELINE")
    BASE_EXEMPT=$((total-BASE_NEW))
    echo "baseline $BASELINE ($BASELINE_SRC): scanning $BASE_NEW new commit(s); $BASE_EXEMPT exempted as already published"
    REV_LIMIT="^$BASELINE"
  fi
  while IFS= read -r pat; do
    # Blobs reachable from ANY ref (--all), matching the message scan's scope:
    # a secret on a side branch or unreachable-from-HEAD tag still publishes
    # if that ref is ever pushed. Batched through xargs so a repository large
    # enough to exceed ARG_MAX makes the gate SLOWER, never quieter — the
    # unbatched form died E2BIG past a few thousand commits, and `|| true`
    # then reported the failure as "clean" (Vex 4048). The inner wrapper maps
    # git grep's no-match (1) to 0 so only REAL errors surface; xargs then
    # propagates any nonzero, and pipefail carries it out of the pipeline.
    set +e
    blob_hits="$(git rev-list --all $REV_LIMIT \
      | xargs -r -n 256 sh -c 'git grep -lIE "$0" "$@" -- ":(exclude)tools/scrub-denylist.txt"; rc=$?; [ "$rc" -le 1 ] && exit 0; exit "$rc"' "$pat" 2>"$ERRF" \
      | sort -u)"
    rc=$?
    set -e
    [ "$rc" -eq 0 ] || die "history blob scan failed for '$pat' (exit $rc): $(head -c 200 "$ERRF")"
    set +e
    msg_hits="$(git log --all $REV_LIMIT -E --grep="$pat" --format='%h %s' 2>"$ERRF")"
    rc=$?
    set -e
    [ "$rc" -eq 0 ] || die "history message scan failed for '$pat' (exit $rc): $(head -c 200 "$ERRF")"
    if [ -n "$blob_hits$msg_hits" ]; then
      printf 'HISTORY  %s\n' "$pat"
      [ -n "$blob_hits" ] && printf '  commits with matching blobs (first 20 of %s):\n%s\n' \
        "$(printf '%s\n' "$blob_hits" | wc -l)" "$(printf '%s\n' "$blob_hits" | head -20 | sed 's/^/    /')"
      [ -n "$msg_hits" ]  && printf '  commit messages:\n%s\n' "$(printf '%s\n' "$msg_hits" | head -10 | sed 's/^/    /')"
      hist_hits=1
    fi
  done <<< "$PATTERNS"
  [ "$hist_hits" = "0" ] && echo "history: clean"
fi

echo
if [ "$findings" = "0" ] && [ "$tree_hits" = "0" ] && [ "$hist_hits" = "0" ]; then
  if [ "$TREE_ONLY" = "--tree-only" ]; then
    echo "TREE CLEAN — history was NOT examined. This is not a publish verdict;"
    echo "run without --tree-only before any public push."
  elif [ -n "$BASELINE" ]; then
    echo "SCRUB CLEAN for the $BASE_NEW commit(s) this push adds — $BASE_EXEMPT exempted as"
    echo "already published at $BASELINE ($BASELINE_SRC). Tree and secrets layers were not scoped."
    echo "(These checks cannot see: images/binaries content, meaning-level PII, or anything"
    echo " a pattern does not cover. A human read of the diff-to-public is still owed.)"
  else
    echo "SCRUB CLEAN — safe to publish as far as these checks can see."
    echo "(They cannot see: images/binaries content, meaning-level PII, or anything"
    echo " a pattern does not cover. A human read of the diff-to-public is still owed.)"
  fi
  exit 0
fi
echo "SCRUB FINDINGS — do NOT publish. Fix in a reviewed PR (tree) or triage the"
echo "history hits; there is deliberately no override flag."
exit 1
