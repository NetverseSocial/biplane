#!/usr/bin/env bash
#
# ADVISORY warning when a commit is about to be made under a fleet-internal
# identity instead of @biplane.invalid.
#
# THIS IS NOT A GATE, AND MUST NOT BECOME ONE.
#
# The guarantee that an internal identity does not reach a published artifact
# is the MERGE GATE — two reviewers at an exact head. It has caught this twice
# (Rowan RC 3372, and again on PR #53) before anything reached main. This
# script exists only to save those review rounds, which makes it an
# affordance rather than a control.
#
# That distinction is load-bearing, because husky owns core.hooksPath on every
# clone that runs `pnpm install`, so anything invoked from .husky/pre-commit
# runs for EVERYONE — including an outside self-hoster whose email is quite
# properly not @biplane.invalid. A refusal here would reject their perfectly
# good commit; a warning costs them one line of output. If someone later
# "hardens" this into a refusal, they will have turned a fleet convenience
# into a defect that blocks contributions.
#
# WHY THE FLEET NEEDS THE WARNING AT ALL: the agent compose stanzas set
# GIT_AUTHOR_* and GIT_COMMITTER_*, and environment beats `git config` and
# even `git -c`. An agent can set its identity, read it back correctly, and
# still commit as someone else. `git var` is the only thing that reports what
# git will actually write. See docs/agents/commit-identity.md for the
# host-side fix that retires the need for this entirely.
#
# Exits 0 unconditionally. Never blocks a commit.
set -uo pipefail

want='biplane.invalid'
warned=0

for role in AUTHOR COMMITTER; do
  ident="$(git var "GIT_${role}_IDENT" 2>/dev/null)" || continue
  # `git var` renders `Name <email> <timestamp> <tz>`, and git forbids `<` and
  # `>` inside a name, so the email is between the last `<` and the next `>`.
  # Domain compared for EQUALITY after the last `@` — a substring test matched
  # the token in a display name and any longer domain (Rowan RC 3389).
  email="${ident##*<}"; email="${email%%>*}"
  domain="${email##*@}"
  local_part="${email%@*}"
  if [ "$domain" != "$want" ] || [ -z "$local_part" ] || [ "$local_part" = "$email" ]; then
    printf 'commit-identity: %s is <%s>, not <name>@%s\n' \
      "$(echo "$role" | tr '[:upper:]' '[:lower:]')" "$email" "$want" >&2
    warned=1
  fi
done

if [ "$warned" -eq 1 ]; then
  cat >&2 <<MSG
  If you are a fleet agent, the container is injecting the wrong identity:
    unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
    git config user.name "<Your Name>"; git config user.email "<you>@${want}"
  Verify with \`git var GIT_AUTHOR_IDENT\`, not \`git config user.email\`.
  If you are an outside contributor, ignore this — your identity is correct
  and nothing here blocks you. See docs/agents/commit-identity.md.
MSG
fi
exit 0
