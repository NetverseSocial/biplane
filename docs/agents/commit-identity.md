# Commit identity in agent containers

Agent commits in this repo should be authored and committed as
`<name>@biplane.invalid`, not under a fleet-internal address.

**The guarantee is the merge gate, not this hook.** Two reviewers at an exact
head is what has actually caught a wrong identity — twice, before it reached
main (Rowan RC 3372, and again on PR #53). `.githooks/check-commit-identity.sh`
runs from `.husky/pre-commit` and only **warns**. It exists to save those review
rounds. It is an affordance, and it must not be hardened into a refusal: husky
owns `core.hooksPath` on every clone that has run `pnpm install`, so a refusal
would reject an outside contributor's perfectly good commit.

## The bug it warns about

The agent compose stanzas set four variables per agent:

```yaml
GIT_AUTHOR_NAME: <Name>
GIT_AUTHOR_EMAIL: <name>@internal.example      # a real, internal domain
GIT_COMMITTER_NAME: <Name>
GIT_COMMITTER_EMAIL: <name>@internal.example
```

**Environment beats `git config`, and it beats `git -c user.email=...` too.** An
agent can set its identity, read it back correctly, and still commit as someone
else. Nothing in git's normal settings reveals it — only inspecting the commit
afterwards, which is what reviewers were doing by hand.

Two approaches that do **not** work, so nobody re-derives them:

- **`~/.bashrc`** — non-interactive shells never read it, and tool calls run in
  non-interactive shells. That is the path most commits actually take.
- **`git config --global`** — `~/.gitconfig` is a bind-mounted file and git
  writes config via rename, so it fails with "Device or resource busy".

A third, tried and withdrawn: a hook of our own under `.githooks/` enabled with
`git config core.hooksPath`. Husky sets `core.hooksPath=.husky/_` from
`"prepare": "husky"`, so **every `pnpm install` silently reset it** and the
guard died without a sound — the exact failure shape it existed to prevent.
Anything hook-shaped in this repo belongs in `.husky/`, which already owns that
setting (Sia).

## The fix, host side — this is the real one

Delete those four lines from every agent's stanza. Git then falls back to
per-repository config, which is per-directory by nature and can hold a
different identity here than elsewhere — something a single environment value
structurally cannot do. An agent with no identity configured gets a loud
refusal rather than a commit under the wrong name.

## The fix, container side — per clone

```bash
unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
git config user.name  "<Your Name>"
git config user.email "<you>@biplane.invalid"
git var GIT_AUTHOR_IDENT            # verify HERE
```

`unset` rather than `export`: with the variables gone, per-repository config
decides, so this clone can carry a different identity from your other work.
Verify with `git var`, never `git config` — config reports what is *set*,
`git var` reports what git will actually *write*, and this whole class of bug
lives in the gap between the two.

## Publication: the gate that now exists

This repository publishes to a public mirror. Two controls stand between an
internal identity and that mirror, and neither is this hook:

- **the merge gate** — two reviewers at an exact head, which caught internal
  identities twice before anything reached main;
- **`tools/scrub-check.sh`** — run before every public push, it refuses on any
  internal address in the tree or in the history being pushed. There is no
  override flag. The procedure that drives it is
  `docs/operations/publish-runbook.md`.

History from before the public cutover stays on the internal remote only; the
public lineage begins at a squash commit that carries no per-commit identities
from that era.

**When publication is configured for this repo — a push mirror, a public
remote, open-sourcing — that is the moment a real gate is needed, and it
belongs wherever publication is configured, not in a commit hook. The policy
has to cover every repo rather than this one.** Recorded with its trigger so it
can come back, rather than being re-derived after the fact.

## The guard's own history, kept because it is the instructive part

The first version searched the whole rendered `git var` identity for the
substring `@biplane.invalid`. Rowan broke it two ways (RC 3389), and both were
accepted commits rather than theory:

| identity | first version | now |
|---|---|---|
| `Vex @biplane.invalid <vex@fleet.example>` | **accepted** | warns |
| `Vex <vex@biplane.invalid.example>` | **accepted** | warns |
| `Vex <@biplane.invalid>` | **accepted** | warns |
| `Vex <biplane.invalid@evil.com>` | refused | warns |
| `Vex <vex@notbiplane.invalid>` | refused | warns |
| `Outside Contributor <someone@example.com>` | refused | **warns, never blocks** |

The token in a display name passes a substring test, and so does any longer
domain beginning with the allowed one. The check now parses the email field —
the text between the last `<` and the next `>`, unambiguous because git forbids
both characters inside a name — takes the domain after the last `@`, and
compares it for **equality**.

`.githooks/check-commit-identity.test.sh` carries every row and asserts
`exit 0` on all of them, including the warning rows: **never blocking is the
property most worth pinning.** It takes `HOOK_UNDER_TEST` so an older revision
can be run against the same table.

The general shape, worth more than the instance: **a check that decides
accept-or-refuse earns an adversarial pass before it propagates, not after.**
This one was written, verified on the happy path plus one obvious failure, and
handed to three agents to adopt before anyone tried to defeat it.
