# Future changes — recorded, not scheduled

Improvements noted during review that are **not required by any spec** and do
not belong on the board (John's ruling, 2026-08-16: the board is spec work
only). Each entry names where the full detail lives. Nothing here blocks
anything; delete an entry when it lands or stops mattering.


## Extract the orchestrator's guards behind a sourceable boundary

**Tracked as BIP-73** (`aaccb36d-74d9-4a70-8eb0-051e741d95c2`) — that ticket is the
source of truth; this is a pointer so the work is discoverable from the repo and
cannot be lost with a merged PR body.

`deployments/release/release.sh` is ~242 lines that **no test executes**; its guards
are read-verified only. Running the orchestrator end to end does require a real
release — but its guards do not: the identity check is four lines of pure logic over
a JSON string with no side effects.

Extracting it applies a convention this repo **already uses so that tests can reach
production logic directly**. Measured: 7 files reference `release-version.sh` and 4
are tests — `release-version.test.sh` and `release-pipeline.test.sh` source it, while
`apply-update.test.sh` and `first-hop-update.test.sh` copy it into their fixtures.
Do this before adding a fourth guard to `release.sh`, or the read-verified surface
keeps growing.

## Apply path — test-strength hardenings (from the update-button reviews)

1. **Applier harness race arm: use the socket racer.** The simultaneous-apply
   arm is ~5/8-sensitive measured (never false-red). Vex's replacement —
   connect both sockets before writing either request — measured 5/5 on both
   mutant and fix: the internal evidence record (bip99-simultaneous-apply-racer, Vex 2026-08-14).
2. **Pin the GET-path non-JSON 502.** The guard exists on both verbs
   (`apply_client.request_status`), tested on POST only — a tidy could strip
   the status path's handling with nothing red.
3. **Harness `req()` shares one body file.** Fine while race arms read only
   status codes; a liar if anyone ever asserts on race response bodies.
4. **Migration 0010 was rewritten in place** (column → attempts table) while
   unmerged. Anyone who applied the column form locally: `migrate license
0009` then forward — invisible to `--nomigrations` runs (Vex, 3844).

## Version surfaces (from the version-line reviews)

5. **Restore translated tokens** for "on Plane" / "dev build" — dropping
   `useTranslation` hardcoded English (Sable, 3809; recorded on the closed
   version-line ticket).
6. **Unify the upstream-version source**: web reads `packageJson.version`,
   admin reads `instance.current_version` — two sources that can disagree
   (pre-existing; same record).

## Update flow

7. **Auto-apply completion notice.** The automatic mode logs its verdicts;
   an admin-visible "an automatic update ran at <time>" surface (banner
   variant or notification) would close the loop without email.
8. **Burn-on-refusal refinement** was argued and deliberately NOT taken:
   clearing the once-per-tag guard on UNREACHABLE reintroduces the hourly
   loop when a timeout means "applying right now" (Sable 3826 / Vex 3833,
   reasoning in the merge commit for `0a8c38ae`). Revisit only with a signal
   that distinguishes never-arrived from accepted-slowly.

## Pre-existing lint debt (main, not owned by any recent PR)

9. `release_source.py` F401 (`FetchRefused`), `test_release_source.py`
   E402/F811, `test_bounded_fetch.py` F401 — and root `pnpm check:types` is
   red on `@plane/utils` (3 errors in `reset-submit.test.ts`), aborting app
   typechecks (Vex, measured at base `4f54555b`).

## Records

10. **Per-ticket credits roll** (John, 2026-08-16): one assembled view of a
    ticket's full chain — authored, reviewed, merged, deployed, each with the
    person and moment. The records all exist (commit authors, PR seats and
    merger, board activity, the service ledger's decider-with-automation-
    marked rows); this is a read over them, not new state.

## Deploy tooling (from the Pi5 applier pre-stage)

11. **apply-service.py could export `BIPLANE_SELFHOST_DIR=$COMPOSE_DIR`**
    itself so flattened prod layouts work without the operator knowing to
    set it in the unit (today it is bridged in the systemd unit on Pi5).

12. **Pi5 deployment code and applier state move to a shared, group-accessible
    path** (John, 2026-08-17). Today `~7of9/biplane-prod` and
    `~7of9/.local/state/biplane-apply/` are readable by one agent only: when
    the first one-click apply refused (jq preflight), nobody but 7of9 could
    read the log. Move the repo, compose dir and applier state to something
    like `/opt/biplane` owned by a dev group all agents belong to, so any
    agent can diagnose and operate. Pairs with #11 (env export) and #13
    (socket) — one install layout, group-readable, one web port.

## Single-port install (John, 2026-08-16 — priority for the next cycle)

13. **The applier moves from a TCP port to a file socket.** Small companies
    installing on their own cloud server will not manage a second port; the
    install story must be: one web port, done. The helper listens on a unix
    socket shared into the api and worker containers, the board talks HTTP
    over it, the bearer token stays, and firewall steps stop existing.
    Sized: basic ~an afternoon; hardened through review ~1–2 days. Platform
    note: solid on Linux hosts (cloud, Pi); Mac-hosted dev containers are
    the only historically flaky case and do not run production.

14. **Why the shared package client sends no session in the board app** —
    Vex's class question (review 3934): the measured fix rewired one page,
    but both clients extend an APIService with identical withCredentials
    construction, so if the cause lives in the shared package bundle, every
    board-app page calling @plane/services for an authenticated endpoint has
    the same defect. One instance of a possible class; someone should look
    once the dust settles.

## Host prerequisites (from the first one-click apply, 2026-08-17)

15. **The update check verifies host prerequisites and the UI says so before
    offering the button.** The first click refused cleanly at apply-update.sh's
    `jq` preflight — jq was never on the Pi, and every manual apply had used
    python3, so the wrapper's own requirement was the one unexercised step.
    Fix the class: (a) the applier's /status (or the hourly check) runs the
    script's required-command list against the host and the Updates page shows
    "missing on host: jq" instead of a live button; (b) prefer dropping the
    jq dependency from apply-update.sh entirely — python3 is already
    guaranteed by the applier itself. A farm staging replica of the
    registry+applier chain (John's point) is the test-bed that exercises the
    WHOLE wrapper, not just its operations, before a real click.

16. **Apply progress needs a spinner — better, a gauge** (John, 2026-08-17,
    watching the first live click). "Updating to v1.2.3…" is a static line;
    the applier's /status already reports the run's log tail, so the page can
    poll it and show a real stage gauge (backup → pull → migrate → restart →
    verify) instead of leaving the operator to wonder if anything is moving.
    A real PERCENT is achievable (John asked): weight the stages, and inside
    the long pole — the image pull — use Docker's per-layer byte progress
    (docker pull --quiet=false / registry manifest sizes give total bytes);
    migrations report N-of-M. Stage weights + byte percent ≈ honest gauge
    within a few percent; never a fake timer.

17. **wait_for_health says nothing on timeout** (Vex, review 3944, non-blocking
    on #119): a 180s poll failure prints no reason — 404, refused, 502, DNS all
    look identical, and the next event is a rollback. Capture and echo the last
    HTTP status/curl error (snippet in the review). Same class as tonight: a
    probe failing invisibly. Also: `BIPLANE_APPLY_HEALTH_TIMEOUT` should be
    documented in env.example — the slow-host escape hatch is currently
    discoverable only by reading the script.

18. **served_build_present is still single-shot — poll the correctness check
    directly** (Vex, review 3946, the one they'd act on). After the warmups,
    the build-presence check runs once: a page fetch plus up to 32 JS asset
    fetches, each one hiccup = rollback, against a proxy serving for ~30s.
    Same class as the original defect, one step later. Fix is smaller than
    what exists: wrap served_build_present itself in the until-loop; the /
    and /admin/ warmups become redundant. Fold in ONE shared deadline across
    the verify tail — today BIPLANE_APPLY_HEALTH_TIMEOUT is per-probe, so a
    dead deployment costs ~19 min through apply+rollback (Sable, 3947,
    computed from the defaults, source-traced not executed — and it holds
    only while BIPLANE_APPLY_HEALTH_TIMEOUT is unset; called the current
    shape a fine trade). Preserve
    the load-bearing trailing slash on /admin/: Caddy redir /admin → /admin/
    301 satisfies curl --fail, so the slashless probe proves only that the
    proxy is up (Sable, 3947). Deliberately deferred past the 2026-08-17
    re-click to keep two fresh approval seats on #119.

19. **apply-service's reaper is a daemon thread** (Sable, 3947, pre-existing):
    an applier restart mid-run leaves the detached apply-update.sh running
    with last-result.json never written — the board shows a run that never
    concludes. Longer verify budgets widen the window. Reap via a persisted
    pid + startup sweep, or a non-daemon reaper.

## Updates surface (from the v1.2.4 reviews, 2026-08-17 afternoon)

20. **The apply POST should return the new run's identity** (Sable, 3962):
    today the UI baselines the applier's prior finished_at and detects THIS
    run by difference; returning a run id (or start stamp) from the POST
    lets the client match instead of guess, deleting the three-state
    baseline machinery. An applier + endpoint + UI change. No run identity
    exists today — the lock holds only a pid, for liveness — so one must be
    minted at start, returned in the 202, and recorded in last_result
    (Sable, 3966: a pid is not a substitute — OS-reused, and an applier
    internal we would be publishing into a client contract).

21. **apply-progress-state is duplicated byte-for-byte, and only the admin
    copy has a suite** (Sable, 3963): "a rule that lives only as a test
    protects only the file that has the test" (Vex's formulation, 12319,
    introduced as theirs in 3963) — the untested twin is Settings→Updates,
    the demoed surface. One copy in a shared package, imported twice.
    Sable's 3963 call: fix before the next change touches either file.

22. **request() never resets stage/sawRunning per click** (Sable, 3963,
    latent): monotonicity currently spans the component lifetime, not the
    run. Unreachable today — every terminal outcome renders a message, not
    the button — but the moment anyone adds "Try again", a second click
    starts at the previous high-water mark and a stale sawRunning lets the
    degraded baseline path fire against the previous run's result. Two
    lines in request(), plus the reset the docstring already promises.
