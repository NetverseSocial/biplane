# How Biplane gets from an edit to the running board

Written 2026-08-18 because none of this was written down. It lived in
conversations and in one agent's workspace, and when that agent was removed the
knowledge went with them — which is how an operator ends up unable to answer
"what happens when I click Update?" about their own system. If you are reading
this to find out how a change reaches production, you are in the right file.

Plain language on purpose. It assumes you know what a file and a server are,
and nothing else.

---

## The one-paragraph version

Biplane runs as a set of programs in containers. A container runs an **image** —
a frozen snapshot of the built software. Changing the source does nothing on its
own: the image has to be **rebuilt**, **published**, and then the running
containers have to be **pointed at the new image**. The Update button in the UI
is the last of those three steps, done for you. The first two are the release.

---

## The pieces, and where they live

| Piece | Where | What it is |
|---|---|---|
| Source | the project's Forgejo remote | the code |
| Build machine | the Mac, `~/biplane/build` | where images are built (~4 min) |
| Dev board | a test host `:8912` | a throwaway Biplane for trying changes |
| Registry | Forgejo on Pi5, `localhost:3000` | where published images are stored |
| Production | the deploy host, `/opt/biplane` | the real board, `:3001` |

Production's **data** — the database, uploads — lives in Docker volumes, NOT in
`/opt/biplane`. This is why moving that directory is safe and why no code change
can lose your work items. (Safe to *move* — not safe to *delete*: the pre-update
database backups live in `.biplane-backups/` inside it, and they are what a
failed migration is recovered from.) The volumes are named `biplane-prod_*` and nothing in
this document ever touches them.

---

## The flow

```
edit → review → merge → build → publish → apply → verify
```

### 1. Edit and preview

Change the code in a clone. To *see* a UI change there are two ways:

- **Dev server** (fast): run the app in dev mode, edits appear in the browser
  immediately, no build. Right for iterating on how something looks.
- **Built image on the dev board** (slower, truer): build and run it on
  the dev board `:8912`. Right before shipping, because it tests the real artifact.

> **Do not hot-swap a single image into a board to preview it.** Swapping only
> `web` while the rest stays on the previous release leaves the deployment in a
> state where what is running disagrees with what is pinned. The applier checks
> exactly that agreement and will refuse the next update until the baseline is
> restored. This happened on 2026-08-18 and cost an evening.

### 2. Review and merge

Two approving reviews at the current commit, author does not merge their own.
Pushing a new commit **resets** the approvals — they are recorded against a
specific commit, so a re-push means re-review.

### 3. Build

On the Mac, in the `builder` tmux session:

```bash
cd ~/biplane/build
git fetch --all && git checkout <merge-commit>
git ls-remote --tags --refs -q origin | sed 's#.*refs/tags/##' > /tmp/tags.txt
RELEASE_TAGS_FILE=/tmp/tags.txt bash deployments/release/preflight.sh v1.2.5 /tmp/release
BIPLANE_RELEASE_TAG=v1.2.5 bash deployments/selfhost/build-images.sh
```

`preflight.sh` is a gate, not a formality: it refuses a tag that is not on
protected `main`, refuses a release with no changelog entry, and derives the
release **level** from the diff — `code` (one-click applicable), `data`
(migrations), or `full` (dependency/runtime/installer changes, hand-applied).
A declared level below the derived one refuses the release.

Building produces four images tagged `pi5-<short commit>`: backend, web, admin,
space. **Every change means a rebuild** — an image is a snapshot, so there is no
such thing as editing the code of a running container.

### 4. Publish

Move the images to Pi5 and push them into the registry, then create the release:

```bash
docker save biplane-{backend,web,admin,space}:pi5-<sha> | ssh <deploy-host> docker load
# on Pi5: docker tag + docker push each, then read the digest back FROM the
# registry — push output is a claim, the registry is the fact
```

Then assemble `release.json` (`make-release-metadata.sh`), tag the merge commit,
create the Forgejo release, attach `release.json`, and **download it back and
diff it byte-for-byte**. A publish you have not read back is a hope, not a
release.

### 5. Apply

The board checks for updates hourly. To check immediately, run the update-check
task. Then **Settings → Update** shows the new version with an active button;
clicking it runs the apply: back up → pull the new images by digest → run
migrations if any → repoint the containers → verify → reload. A progress gauge shows the
stages, read from the applier's own log: the applier emits explicit stage
markers and the display matches only those (BIP-72).

That fix is **merged but not yet deployed** — a board still running an older
release matches *any* log line, so error text moves the bar. That is what made
the v1.2.5 evening confusing: the applier's own failure message contains
"migrat", so the gauge announced "Running migrations 55%" on the line reporting
the failure, and because the bar only moves forward it stayed there through the
rollback. If your board predates that release, distrust the gauge on a failing
apply and read the log.

**What happens on failure depends on whether a migration had started, and the
difference matters more than anything else on this page.**

*Before any migration* (every `code` release, and a `data` release that fails
early): the apply restores the previous pins and brings the board back up by
itself. Designed behaviour, not an error — the board stays on the previous
version, running.

*After a migration has been attempted*: there is **no automatic rollback, and
you should not call it one** — the applier says so in those words. Automatic
recreation is gated off once a migration starts, so the run ends in
`RECOVERY REQUIRED`, **the services are left stopped**, and the database needs an
operator to reconcile it against the pre-update backup in `.biplane-backups/`.
The board is **down** until someone acts.

That is also designed, and the reason is worth saying out loud: staying down
beats restarting the old code against a half-migrated database. A stopped board
is recoverable; silent corruption is not.

`full`-level releases refuse the button on purpose and take
`deployments/selfhost/MANUAL-FULL-UPGRADE.md` instead.

### 6. Verify

Served version and build in `GET /api/instances/`; every page 200; running image
IDs equal the digests in `release.json`. Compare bytes, not tags.

---

## Who can do what, and why it is annoying

Every step above that touches Docker needs Docker, and **on Linux, membership in
the `docker` group is equivalent to root** — a container can mount the whole
filesystem. That is not a quirk; it is the reason an agent was removed for using
it to make unrequested root changes to Pi5 on 2026-08-17.

So Docker on Pi5 is held by one person, and the `dev` group grants only the
ability to **edit** the deployment's config files — not to run Docker. The
consequence is real and should not be pretended away: today a release requires a
human at a Docker-capable shell for several steps.

The fix is not to hand Docker back out. It is a **narrow operator service**: a
small reviewed program that holds the Docker capability and exposes a fixed set
of named operations (publish these images, run the update check, report running
versus pinned) to token-holding callers, with no passthrough and no shell. Then
a release is one orchestrated command and the dangerous capability stays in one
audited place. That work is in progress; until it lands, the manual steps above
are the procedure.

A precondition that is easy to miss and fatal to get wrong: **a privileged
service must not execute code from a directory its callers can write.** If it
does, it is not a boundary — it is a remote-code-execution endpoint with good
manners, and no amount of argument validation helps.

---

## When it goes wrong

| Symptom | Cause | What to do |
|---|---|---|
| "update refused: ... do not form one proven baseline" | what is running disagrees with what is pinned (usually a hot-swapped image) | restore the pinned images, then re-apply |
| Update button missing entirely | you are up to date, or looking at the admin panel rather than the board's Settings → Update | check the version line |
| Apply fails, version unchanged, board **up** | it failed before any migration and restored itself | nothing to do; read the log if you want the cause |
| Apply fails and the board is **down**, log says `RECOVERY REQUIRED` | a migration had started, so recovery is **not** automatic | **read the apply log first.** Reconcile the database against `.biplane-backups/` before restarting anything |
| Board 502 right after a change | the app container is restarting | **wait a full 60s** and re-check before diagnosing — the API takes ~30-60s to warm, and treating the optimistic half as the whole window is what caused a needless rollback on the first live apply |

Rollback of a whole deployment: the images are content-addressed and the data is
in named volumes, so returning to a previous **code** release means pointing the
pins back and recreating the containers, and nothing about that touches data.
A release that ran **migrations** is not reversible this way — the schema moved,
and going back means restoring the database from the pre-update backup. Check the
release's level before assuming a rollback is cheap.
