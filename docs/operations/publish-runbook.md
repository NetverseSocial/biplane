# Publishing this repository to a public mirror

The procedure that drives `tools/scrub-check.sh`. The gate is a script; this
is its caller — written down because a gate with no documented caller does not
fail closed, it fails UNRUN (this repository has been there before, with a
release workflow that no runner ever executed).

## The shape: squash, then shared lineage

History from before the first public cutover stays on the internal remote
only. The public lineage begins at a **squash commit** whose tree is
byte-identical to the internal main it snapshots — internal history carried
author identities and infrastructure references that a public push would
publish irrevocably. After the cutover, internal development continues on top
of the squash commit, both remotes share one lineage, and every future push
is a plain fast-forward carrying full per-commit history.

What this costs and preserves, stated plainly: the pre-cutover batch is one
coarse public commit (the changelog carries its story); the internal remote
keeps every original commit, tag, and release permanently on an archive
branch — blame, bisect, and reproduction of old builds are untouched there.

## Procedure

Preconditions: every PR meant to ride in the snapshot is merged; the
relocated-docs change (for anything the scrub deleted) has a home.

1. **Squash commit.** From a clone at the internal main to publish (sha `M`),
   with the public mirror's current main as parent (sha `P`):

   ```sh
   S=$(GIT_AUTHOR_NAME=<name> GIT_AUTHOR_EMAIL=<name>@biplane.invalid \
       GIT_COMMITTER_NAME=<name> GIT_COMMITTER_EMAIL=<name>@biplane.invalid \
       git commit-tree "M^{tree}" -p "$P" -F <message-file>)
   ```

   Parent `P` makes the push a fast-forward; `commit-tree M^{tree}` makes the
   published tree byte-identical to internal main **by construction**.

2. **Verify the construction:** `git diff --stat "$S" "$M"` must print nothing.

3. **Gate the public lineage.** In a fresh clone containing ONLY `S`’s lineage
   (public base + `S`), run:

   ```sh
   tools/scrub-check.sh --baseline-from <public-remote>
   ```

   The gate itself asks the publish target what it currently serves, in the
   same breath as the scan — no operator-supplied value exists to be wrong
   (the raw `--baseline <sha>` form is for an unreachable remote only, and
   requires the sha to be an ancestor of HEAD). Material reachable from it is
   already published, so republishing it adds no new exposure; the denylist
   HISTORY layer scans only the new commits and prints the exemption as a
   count. The baseline never affects the tree layer or the gitleaks layer.

   Exception mechanics, measured 2026-08-19 on gitleaks 8.16.0 (Ubuntu):
   full-git findings fingerprint as `commit:file:rule:line`; `--no-git` as
   `file:rule:line`; the forms do not cross-suppress. Fingerprints for
   findings introduced by OLD, stable commits converge; findings introduced
   by the squash commit itself are structurally unfingerprintable (adding the
   exception file changes the sha that the fingerprint names). For those, use
   gitleaks’ inline `# gitleaks:allow` annotation at the source line with the
   reason in the comment — it travels with the content and is immune to sha
   churn. Either way the step CLOSES on the assertion: **the full run exits
   0** — an exception that silently suppresses nothing is invisible to anyone
   who adds it without re-running.

   Triage rule: findings fingerprinted in the OLD public base are already
   public (grandfather, fix forward); findings introduced at `S` are blockers
   — fix at the source, never except.

4. **Push:** `git push <public-remote> "$S":main` — fast-forward, over the
   repo-scoped deploy key. Optionally tag the release at `S`; the SHA
   necessarily differs from the internal tag of the same name — say so in the
   release notes if tagged.

5. **Anonymous verify:** fresh unauthenticated clone of the public mirror;
   run the scrub against it; read the rendered README and CHANGELOG.

6. **Cutover (internal remote, coordinated and announced — never solo):**
   archive the old lineage (`git branch <archive-name> "$M"` — this keeps
   every tag reachable, nothing is garbage-collected), then reset the internal
   main to `S`. Every clone re-bases onto the new main; announce with exact
   commands. From here the two remotes share lineage.

7. **Identities:** all committers use `<name>@biplane.invalid`
   (see `docs/agents/commit-identity.md`); the pre-commit hook warns, the
   merge gate and this procedure enforce.

## Standing rule

`tools/scrub-check.sh` runs in full — never `--tree-only`, which says so in
its own output — before **every** public push, and its exit is the verdict:
0 publish, 1 findings, 2 the scan itself failed (neither clean nor findings;
treat as do-not-publish and fix the scan).
