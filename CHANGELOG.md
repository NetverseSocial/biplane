# Changelog

All notable changes to Biplane are recorded here. A release without a
changelog entry is an invalid release (BIP-40): the release procedure — the
manual, operator-run sequence in `deployments/release/README.md` — reads the
entry for the tag as the release notes and refuses to publish without one.

Each entry declares its release **level** — `code`, `data`, or `full` — which
the preflight gate (`deployments/release/preflight.sh`, run as a step of that
procedure) independently DERIVES from the diff and ENFORCES: a declared level
below the diff-derived minimum refuses the release (a migration forces at
least `data`; a dependency, base-image, runtime or packaging change forces
`full`).

<!--
Entry template. THIS BLOCK LIVES ABOVE [Unreleased] ON PURPOSE. The release
notes are extracted as every line after an entry's heading up to the next
"## " heading, so anything parked at the BOTTOM of this file is published as
part of the last entry — which is exactly what happened to the first draft of
v1.1.0. Above the first heading, it can never be swallowed.

## [vX.Y.Z] - YYYY-MM-DD  (level: code|data|full)

### Added / Changed / Fixed / Security
- ...
-->

## [Unreleased]

## [v1.2.10] - 2026-08-19 (level: code)

Both fixes come from watching the v1.2.9 button apply on the production board —
the first update anyone observed end to end with the repaired gauge. The pull
marks fired and 100% held; these are the two defects that survived the watching.

### Fixed

- **"Update status unavailable" no longer appears after an apply.** Every apply
  recreates the api container, which empties the status cache. The safety net —
  a post-apply re-check inside the applier — has 60 seconds, and a cold Django
  on small hardware takes longer than that, so the net failed exactly when it
  was needed and the page declared ignorance for up to an hour.

  The status endpoint now answers from the durable columns when they hold both
  sides of the comparison: the running version and the last verified latest are
  in the database, and their comparison is pure. The timestamp shown is the
  columns' own — when the latest value was actually verified, never a replayed
  freshness. Deployments the columns cannot answer for (never checked, or no
  declared running version) degrade exactly as before: unknown, with the reason.

- **A silent bar now says why it is silent.** After the pull marks, the apply
  restarts the six services — including the api that the page polls through. On
  small hardware that is minutes of failed polls, which the page rode out as a
  bar frozen at the last pull mark, indistinguishable from a hang, before
  jumping straight to 100%.

  The page cannot see through a restarting server, and pretending otherwise is
  how progress displays lie. Instead it now states the observation: when status
  polls are failing during an apply, a line under the bar says the server is not
  answering, that this is expected while services restart, and that the bar
  resumes when it returns. Derived from a real failed poll plus the last real
  stage mark — not a timer, not an animation, per the v1.2.8 reasoning.

## [v1.2.9] - 2026-08-19 (level: code)

### Fixed

- **"Current version" now reports what the SERVER is running.** It was read from
  `import.meta.env.VITE_BIPLANE_VERSION`, which is compiled into the JavaScript
  bundle when the image is built. A tab holding an older bundle therefore
  reported the version it was built from, indefinitely and without hedging,
  while the "Last update check" line directly beneath it refreshed from the API.
  One line moved and the other did not, and the one a reader trusts is the wrong
  one.

  It now reads `running_release` from the update-status payload — the
  deployment's own `biplane_installed_version`, the same field the update check
  classifies against — and falls back to the compiled-in value only when the
  server has not answered, because a missing answer is not evidence about the
  bundle either way.

  When the two disagree the page says so rather than silently picking a winner:
  *"This page is still running the vX interface (build Y). Reload to load vZ."*
  That is the one moment the compiled-in value is worth showing, because it
  describes the interface you are actually looking at rather than the deployment.

  Found the hard way: after v1.2.8 was applied and verified by image digest, the
  page kept reporting v1.2.7 — confidently, unhedged, and wrong. A version
  display that cannot be trusted during an upgrade is worse than no version
  display, because it is consulted precisely when it is lying.

This is also the first `code`-level release since the update gauge was repaired
in v1.2.8, so applying it with the button is the first opportunity to observe,
rather than assert, that the bar moves during the image pull, reaches 100%, and
holds before the reload. v1.2.8 could not show that — it was `full`, and a
`full` release has no button.

## [v1.2.8] - 2026-08-18 (level: full)

**APPLY BY HAND** — this changes the host-side updater, which correctly derives
`full`: the automated path cannot verify changes living outside the images it
pulls.

Note the consequence, because it is not obvious: everything below is about what
you see *during a one-click update*, and this release is not one the button can
apply. Hand-applying v1.2.8 puts the new updater and UI in place; the first time
you will actually watch any of it is the next `code`-level release, applied with
the button.

### Fixed

- **The bar moves during the pull.** Pulling four images was the longest silent
  stretch of an apply — measured at ~95 seconds on the production board, during
  which nothing moved and the operator could not tell working from wedged. The
  updater now emits a mark after each image *completes*, turning one silent
  stage into four steps.

  Deliberately not a spinner and not a timer: an animation is driven by nothing
  and keeps moving through a hung updater, a dead updater and a network
  partition, and a clock-driven heartbeat outlives the work it claims to
  describe. Progress derived from work completed can do neither. A single large
  layer stalling remains invisible — the updater genuinely knows nothing during
  it — and the display says nothing about liveness rather than guessing.

- **The bar reaches 100%.** Stages topped out at 95% and the page jumped
  straight to the success text, so the completion was never visible. The
  finished state now renders and is held before the reload.

- **A successful update no longer looks broken.** The board kept its previous
  idea of the latest release, saw itself running something newer than anything
  it knew about, could not classify that, and honestly reported "Update status
  unavailable" — for up to an hour. The updater now refreshes the check
  immediately on success, using the pure check rather than the scheduled task:
  the task also carries the auto-apply hook, which records its one attempt per
  release *before* sending a request that this very apply would refuse, and
  that record is permanent by design.


## [v1.2.7] - 2026-08-18 (level: code)

Records what v1.2.6 shipped but did not describe.

### Added

- **Once the service is installed, a release no longer needs a human at a
  Docker-capable shell — and installing it does.** The service ships as code in
  this repo and is put in place BY HAND on the deploy host, once: token file,
  environment, systemd unit (`deployments/selfhost/apply-service/README.md`).
  None of that happens by applying a release, so an operator upgrading to this
  version does not thereby get one-command releases; they get the code that
  makes them possible after a one-time manual install. The applier
  now exposes a fixed set of operator operations — publish images to the
  registry (returning digests read back FROM the registry), trigger an update
  check, and report running images against the configured pins — alongside a
  release script that sequences the whole procedure and fails closed at every
  gate. This shipped in v1.2.6 and its entry did not mention it.

  Docker group membership is root-equivalent, so no agent holds it. The
  capability lives in one reviewed service that accepts a fixed set of named
  operations and nothing else: fixed argument vectors rather than shell
  strings, per-argument allowlists, no passthrough, and a route table that
  refuses anything unnamed before doing work. The service also refuses to
  start if the files it executes are writable by the callers it defends
  against — a privileged service running code its callers can edit is not a
  boundary.


## [v1.2.6] - 2026-08-18 (level: full)

**APPLY BY HAND** — the release changes the host-side applier
(`deployments/`), which correctly derives `full`: the automated path cannot
verify changes that live outside the images it pulls. Note the consequence,
because it is not obvious: the release that repairs the progress gauge is
itself not one the button can install. The gauge becomes visible on the NEXT
code-level release, which is the first one the button applies with both the
new applier and the new UI in place.

### Fixed

- **The gauge was invisible, not missing.** The theme declares
  `--color-*: initial` and defines no `custom-*` token, so every
  `bg-custom-*` / `text-custom-*` / `border-custom-*` utility compiled to
  nothing. The progress bar had correct geometry and no colour at any
  percentage, and — the part an operator actually hit — **the active
  "Update to vX" button was white text with a white border on a white
  page.** The control you must click to start an update could not be seen.
  33 dead classes mapped to live semantic tokens, plus a guard, derived
  premise-first, that fails if the theme ever defines those tokens again.

- **Only the applier's own marks move the gauge.** The recogniser matched
  any log line containing `pull`, `migrat`, `backup`, `probe`,
  `verification` or a capitalised `Recreate|Starting|Started|Stopping` —
  and the shipped applier already emits "migration did not complete", so a
  failure message drove the bar to "Running migrations 55%". Because
  advance is monotonic, that misread then **latched** for the rest of the
  run, straight through the failure and the rollback. Eighteen emitted
  messages could move the bar, most of them failure and recovery lines, so
  the gauge lied hardest exactly when an operator most needed it. The
  applier now emits explicit stage marks and the recogniser matches only
  those, whole-line anchored so an error quoting a mark still says nothing.

- **A refusal names WHICH check failed.** "running services, persisted
  pins, release/build identity, or database target do not form one proven
  baseline" covered roughly thirty distinct states across six services and
  told the operator nothing about which one fired. Each now reports its
  specific mismatch — naming the service and both refs where those are not
  secrets, and the check that fired where they are.

### Changed

- **An unreported stage shows no bar at all, rather than a number.** A
  release installed by an older applier reports no stages, so the gauge
  renders "progress not reported" with **no bar element whatsoever** —
  earlier it dropped only the fill and kept the track, which is visually a
  0% bar and reads as stalled-at-zero, precisely what this claim promises it
  is not. Rolling back leaves the forward scale entirely and is terminal — a
  rollback is not a later stage of success, and no subsequent mark can put
  the bar back.

- **Both unwind paths are marked, and one deliberately is not.** The failure
  that unwinds after the config commit now reports itself as rolling back.
  The migration-failure branch deliberately does NOT, because it leaves the
  prior configuration active and refuses to describe itself as a rollback —
  marking it would be a false claim rather than a missing one.

- **One shared progress module.** The stage logic was byte-duplicated in
  both apps; it now lives once in `@plane/utils`.

## [v1.2.5] - 2026-08-18 (level: code)

The button's own release, with v1.2.4's gauge on screen.

### Changed

- **The "Update" button is always visible.** When the instance is up to
  date, Settings → Update previously rendered no action at all; now it shows
  a disabled, grayed-out "Update" button (and likewise when the update status
  is unavailable), so the control is always present and its state is legible
  at a glance. The active one-click button is unchanged when a newer release
  exists.

## [v1.2.4] - 2026-08-17 (level: full)

**APPLY BY HAND** — the release includes installer (deployments/) changes,
which correctly derive `full`: the automated path cannot verify changes
that live outside the images it pulls. The demo release: everything the
first button-install showed John, fixed the same day. The next code-level
release is the button's again — with this release's gauge on screen.

### Fixed

- **No second login.** The updates endpoints now accept the board session
  of an instance admin (who you are: the board's own session store; what
  you may do: unchanged instance-admin gate). Settings → Updates and the
  help-menu Update row work directly from the board — no hourly
  admin-site login, no gray bounce page. The tradeoff is recorded as
  decision 012.
- **A real progress gauge.** The apply button now shows the run's actual
  stages — backing up, pulling images, migrating, restarting, verifying —
  with percent, read from the applier's own log. Never a timer.
- **The page finishes what it starts.** On success it reloads itself once;
  the operator never refreshes by hand. On failure the verdict shows even
  when the run fails faster than the first status poll, and a previous
  run's result can never masquerade as this one's.
- **The changelog pane scrolls visibly** (macOS overlay scrollbars hid it).
- **Host-side (lands with the deployment checkout, not the images): a
  leading-zero apply timeout is refused up front** instead of misread as
  octal (08/09 were worse: a fatal arithmetic abort mid-apply).

## [v1.2.3] - 2026-08-17 (level: code)

The first release installed by the Update button.

### Added

- The future-changes record gains the single-port install plan (the applier
  moves to a file socket so cloud installs need one web port and no firewall
  steps), and the open question of why the shared web client sends no
  session — one instance or a class.

### Changed

- This changelog, visible in Settings → Updates, now includes this entry —
  which you are reading on a board that updated itself.

## [v1.2.2] - 2026-08-16 (level: full)

**APPLY BY HAND** — the compose file changed (the applier keys reach the api
and worker), which correctly derives `full`. No migrations. The next release
is the button's.

### Fixed

- **Settings → Updates authenticates.** The page called the server through a
  client that attached no session in the board app, locking out the very
  admin it was built for — with an error message blaming him. It now uses
  the board's own authenticated client, and every server refusal reaches
  the screen in the server's own words.
- **Automatic updates can actually fire.** The hourly check runs on the
  worker service, which never received the applier's address and token —
  auto mode would have logged "no applier configured" forever. Both
  services now carry the keys, with the reason stated in the compose.

### Added

- The applier's install guide gains a **Firewall** section (a host process
  is not a published port; the one rule that restores reachability without
  widening trust) and the bearing-ref install note.

## [v1.2.1] - 2026-08-16 (level: full)

**APPLY BY HAND once more** — level derives `full` because the backend image
now carries our changelog (a Dockerfile change). The one-click path's first
real exercise moves to the next `code`/`data` release; everything it needs
ships here.

No migrations. All changes are UI and update-machinery.

### Added

- **Settings → Updates** (the owner's design): a page under your profile
  settings showing the current version and when it was last checked; a
  newer-release section with the action when one exists; a real
  **Automatic updates** switch (stored in instance settings, with the
  deployment variable as a force-on); the **update-server choice** —
  Current server, GitHub, Biplane.dev (visible, coming soon), or Other with
  your own server's address; and our changelog, scrollable, served from the
  running image.
- **Custom update servers work end to end** (forks welcome): saving the
  address is the whole authorization; the check reads it first, and updates
  install from it **exclusively** — never a fallback, so a fork tracking
  upstream versions can never install a different project's release under a
  colliding tag. The exact-tag convention (`<url>/tags/<tag>`, same JSON
  shape as the forges) is satisfiable by a static file host. The field
  states its trust boundary: any instance admin can set it.
- The ? menu gains a plain **Update** row opening the page; the version line
  stays at the menu's bottom.

### Changed

- The top-bar **Biplane · online** light is a plain indicator — name and
  dot, no box, nothing that invites a click.

## [v1.2.0] - 2026-08-16 (level: full)

**APPLY THIS RELEASE BY HAND**, same as v1.1.0 and for the same reason: the
level derives `full` (both frontend Dockerfiles and the selfhost compose
changed), and the automated apply refuses `full` by design. Follow
`deployments/selfhost/MANUAL-FULL-UPGRADE.md`. The Update button ships IN
this release, so the button applies future `code`/`data` releases — not
this one, which merely delivers it.

Two additive migrations (board operation ledger; auto-apply attempts).
No column is dropped or rewritten.

### Added

- **In-UI updates**: an Update button beside the update banner (admin) and in
  the board sidebar's version pulldown — one click asks the host applier to
  install a flagged `code`/`data` release, with progress and the run's real
  outcome shown. A narrow privileged applier service on the deploy host wraps
  the reviewed apply command and adds no logic to it; full-level releases are
  always refused toward the manual path.
- **Automatic update mode** (`BIPLANE_APPLY_AUTO=1`, off by default): when the
  hourly check flags a `code`/`data` release, the server requests the apply —
  at most once per tag, recorded durably before the request, so a failed
  attempt never retries itself hourly.
- **The board service**: a server-owned audited door for ticket transitions —
  actor verified, accepted decisions replay-safe under an operation key,
  mutation + audit + outcome committed atomically. Automation credits the
  person or agent whose completed act triggered it, marked as automatic —
  machinery gets no name in the record. Declared remainder, visible as a
  strict xfail in the suite: the primary app write route is not yet routed
  through it.
- **Convergence census**: an executable, declared-bounds inventory of the
  ticket-state write sites it can see; routed sites, recorded exclusions,
  and outstanding convergence targets are named explicitly.

### Fixed

- **Version surfaces tell OUR version.** Four places (web sidebar, admin
  sidebar, What's-new pill, edition-badge tooltip) led with upstream's base
  version or a stale constant; all now show the baked release
  (`Biplane v1.2.0 (build)`), dev builds say so, and the lying constant is
  deleted. Build-arg pins are counted per frontend image so a dropped bake
  cannot ship silently.
- **Selfhost**: `pg_dump`/`pg_restore` authenticate (plane-db requires a
  password even locally — this also broke the automated apply's own backup);
  `extra_hosts` host-gateway mapping so in-container callers can reach
  host-side URLs like the Forgejo base (`SELFHOST_HOST_ALIAS`).

### Security

- The applier's trust boundary is stated where it is configured: with a
  non-loopback bind the bearer token is the only gate to "run the apply on
  this host" — bind stays loopback by default so exposure is an operator's
  explicit choice.

## [v1.1.0] - 2026-08-15 (level: full)

First release published to the private Forgejo instance, and the first release
of the git-bridge work.

**APPLY THIS RELEASE BY HAND. Both automated apply commands refuse it, on
purpose.** `first-hop-update.sh` and `apply-update.sh` decline any `full`
release, because `full` means the runtime itself changed — a base image, a
pinned dependency, the lock file, packaging — and they cannot verify the part of
the change that lives outside the images they pull. The refusal is correct and
stays. Follow `deployments/selfhost/MANUAL-FULL-UPGRADE.md`, which mirrors the
automated path's guarantees step for step: back up before anything is pulled,
verify that backup by checksum before relying on it, pull by digest and check
the images declare the version and build they claim, plan the migrations before
running them, write the pins atomically, and read the running version back
rather than trusting that a container started.

**And this first release measures `full`.** With no prior published release
the pipeline baselines from the repository ROOT, and root..HEAD for this
repository contains packaging, Docker and requirements changes — so v1.1.0
derives `full` as a measured fact of this history. (Stated as a measurement,
not a law: a root-baselined diff excludes files exactly as they stood in the
root commit, so a repository whose packaging never changed after its first
commit could derive lower. Ours did not.) The automated path becomes usable
from the second release onward.

**Level `full`, not `data`.** There is no prior release or tag on this instance,
so the pipeline derives the level from the repository ROOT rather than from a
previous release: every migration path changed after root counts, plus
deployment, Docker, requirements and workflow changes. `full` is what the diff
says, and a declared level below the derived minimum refuses the release.

**Two different migration counts are both correct, and confusing them is how
this gets misread.** The pipeline's count is against the root baseline, because
there is nothing earlier to compare to. What an OPERATOR of the existing Pi5
deployment actually applies is the far smaller set added since the build they
are running. Neither number is wrong; they answer different questions.
An operator of the existing Pi5 deployment applies **three** migrations —
0128 (delivery semantic key), 0129 (description backfill), 0130 (poll cursor) —
all additive; no column is dropped or rewritten. The pipeline's root-baseline
diff touches **nine** migration paths, which with the packaging and Docker
changes is what derives `full`. _(Measured on the prospective merge tree,
2026-08-15; re-verified against the exact tagged commit as the final gate —
the FILL marker that used to sit here is why an unfinished entry could not
publish.)_

The migrations are additive — no column is dropped or rewritten, so existing
ticket data is untouched.

### Added

- **GitHub polling transport** (BIP-46): the bridge reads push and merged-pull
  activity from repositories it cannot receive webhooks from. Lossless by
  construction — every observation is durably stored before the boundary moves,
  a retention-window gap fails closed and names the operator recovery, and a
  concurrent worker's advance is reported as a stale cursor rather than as lost
  history. Author-versus-merger attribution is carried as provider id plus
  login, never a login alone.
- **Release channel** (BIP-40 / M5.1): the manual, operator-run procedure in
  `deployments/release/README.md` is the producer — it digest-pins the images
  and publishes a release carrying unsigned metadata — tag, commit, level,
  resolved image digests — with the changelog entry as its notes. The preflight
  and metadata scripts under `deployments/release/` are that procedure's gates
  and helpers: they refuse an unready release and assemble the metadata, and do
  not themselves pin or publish anything. Publication CI was deleted: it had no
  runner and never executed, so the manual procedure is the authoritative path,
  and the preflight script exists precisely so the gates run from the path a
  human actually walks. Image digests are the executable identity; no signed
  manifest or key material.
- **Update path** (BIP-42 / M5.4): an exact-tag apply command that backs up
  first, pulls by digest, admits migrations only when the release level says so,
  commits pins atomically and reports rollback. Plus a first-hop command for
  moving a legacy deployment onto the managed path.
- **Version identity** (BIP-43 / M4): installed build and installed release
  version are separate fields, and an unreachable or incomparable check reports
  UNKNOWN rather than "up to date".

### Changed

- **THE BRIDGE NO LONGER MOVES TICKETS. It tells you instead.** This is the
  largest behaviour change in the release and the one to read before deploying.

  Until now, `Closes BIP-12` in a pull-request body moved that ticket to Done
  when the pull request merged. It no longer does. **No board write happens in
  this release** — no ticket state transition, no `completed_at`, no activity
  row, no backlink — not on a merge, not on a push, and not on a
  changes-requested review. (Stated narrowly on purpose: the bridge still
  writes its own durable delivery record — that is its record, not a board
  write. An earlier draft said "no bridge write at all", which was the
  design-read-as-runtime overclaim this entry has already been corrected for
  once.) Every declined action is refused with a durably recorded reason;
  whether that reason also reaches the pull request as a comment is a separate,
  credential-gated capability stated once, in its own item below.

  **Two of those three are permanent; one is waiting on facts.** Worth being
  exact, because "not yet" and "never" are different promises:
  - **A push will never move a ticket again.** It is neither a merge nor an
    approval, so nothing it carries can determine an outcome. No future field
    changes this.
  - **A changes-requested review will never send a ticket back automatically.**
    That behaviour is withdrawn outright rather than postponed — a review
    requesting changes cannot satisfy the conditions for a write, so no amount
    of waiting makes it qualify. The reviewer or the ticket's owner moves it.
  - **Completion on a merge is the one that is waiting**, and on facts rather
    than on a decision. See below.

  **Why completion is refused rather than narrowed.** Roles move tickets; the
  bridge is a tool and holds no authority of its own. It may complete a ticket
  only where facts it verified for itself determine the outcome: the merge, the
  approving reviews, the ticket's named reviewers, and the ticket naming the
  pull request. Author-supplied text SELECTS which ticket an event concerns and
  never authorises anything. Two of those four have nowhere to be recorded at
  all — a ticket cannot name its reviewers, and cannot name a pull request — so
  the condition cannot be evaluated, and an unevaluable condition is a refusal
  by the rule's own terms.

  **There is no switch, deliberately.** Every refusal is derived from a named
  missing fact or a withdrawn rule, never from a setting. And the completion
  refusal in this release is unconditional in the source: the decision accepts
  none of those facts as inputs, because none of them exist to be read.
  Enabling completion is a future CODE change that must carry the fields, the
  reads, the decision and the write together through its own review — nothing
  in this release begins acting when data appears. Where a rule was withdrawn,
  there was never anything to turn on at all. This project has already paid
  for the alternative: an earlier draft of this very entry claimed bridge writes
  shipped off, read out of the specification rather than the source, and it was
  false — there was no such switch anywhere (caught by Morrow, and
  independently by Vex, who went looking for it and established it did not
  exist).

  **What an operator should expect.** Set a forge credential and the bridge will
  read your repository, recognise directives under the anchored grammar below —
  NOT exactly as before; the narrowing is real and measured — and then decline
  to act, recording which ticket each event selected and why it was not moved.
  Tickets move when a person moves them, in whichever direction the work
  actually went; there is no forward-only rule on the board.

- **Refusals are recorded durably; the pull-request reply activates only with
  its own credential.** The CODE can answer on the pull request — the typo'd
  ticket, the malformed near-miss line, the no-ticket merge — but THIS
  DEPLOYMENT ships with `FORGEJO_BRIDGE_WRITE_TOKEN` absent (measured on the
  box, ruled fail-closed rather than minted under deadline), so v1.1.0 records
  every refusal and posts nothing until a least-privilege comment token is
  provisioned as a separate, deliberate step. Stating otherwise would claim a
  mechanism whose only credential is blank.
- **The in-app notification half is NOT in this release, deliberately.** A
  first implementation was built and cut in cold review the same night: done
  right it needs a real outbox — DB-enforced once-only delivery, starvation-free
  paging, coordinates that cannot rebind through config changes, and a policy
  for repeating the ask — which is a reviewed slice of its own. How that slice
  determines WHO to ask is deliberately left undesigned here: the cut
  implementation's answer was one of the things found unsafe, and pre-deciding
  its replacement in a release note would repeat the mistake. Two honest
  consequences named in the spec: nothing re-asks yet ("keeps notifying" is
  policy, not behaviour), and a mistaken ticket reference in a PUSH is recorded
  durably but reaches no person — a push has no pull request to answer on.
- **One owner for directive selection** (BIP-54): the matcher and the
  keyword-to-class map lived in two files with two spellings, and the runtime
  used the copy. The compatibility grammar is deleted outright; this release
  ships ONE canonical anchored grammar reading one keyword-to-class map.
- **Directive grammar** (BIP-64, BIP-54): a directive is an ANCHORED TRAILER —
  a line that IS the directive (`Closes BIP-N` and its keyword family), standing
  alone in a commit message or pull-request body. A directive quoted in a code
  span or fence is prose about a directive rather than one; a mid-line reference
  is prose; benign empty markup is not extra content. The narrowing against the
  old mid-line matcher was measured on this repository's history before it
  shipped (Vex): of 523 documents, 108 directive selections are kept and 25 are
  lost — 11 parenthetical, 5 invalid multi-ticket lines the old grammar silently
  PARTIALLY acted on (refusing whole is the improvement, not the regression),
  4 mid-line prose, 3 code-span, 2 ignored-context — and 11 formerly-invisible
  lines now surface as recorded near misses. One repository's history is a
  sample, not a proof; the numbers say what this narrowing costs HERE.
  **Pull-request titles are inert.** The title nomination arm is deleted
  outright — a directive in a title selects nothing, whatever its spelling or
  rendering. Measured cost of the removal before it was made: **0 of 89
  pull-request titles in this repository's history nominate a ticket**, and that
  zero is structural rather than lucky, because the anchored form requires the
  entire title to BE the directive (Vex). An earlier draft of this entry
  disclosed the arm as still live; that was true when written and closed before
  the tag.
- **Scope guard** (BIP-38): bridge authority is keyed by provider instance and
  stable repository id to project UUIDs — display paths are never identity.

### Fixed

- Tickets read as empty through the API because `description_stripped` was not
  exposed (BIP-59).
- Search dropped unknown `entities` values silently instead of rejecting them,
  and did not validate `query_type` or `count` (BIP-58, BIP-62).

### Security

- Every external base image is pinned by manifest-list digest, and Python
  dependencies are hash-locked and enforced at install (BIP-48).
