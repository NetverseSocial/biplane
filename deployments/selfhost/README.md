# Biplane self-host

Run the Biplane **web/CE stack** (13 containers: web, space, admin, live, api, worker,
beat-worker, migrator + postgres, valkey, rabbitmq, minio, proxy) with Docker Compose.

Scope, honestly: `web`, `space`, `admin`, and the backend (`api`/`worker`/`beat-worker`/
`migrator`) run **forked Biplane images**. `live` and `proxy` intentionally run upstream
Plane images (unforked). The git bridge is **built into the backend image** and needs
configuring — see [Git bridge setup](#git-bridge-setup) below. The separately distributed
agent services (ledger, write-path, idP-lite) are **not** part of this compose.

## Files

- `docker-compose.yml` — upstream Plane CE compose, **byte-for-byte** the official
  release asset:
  source: `https://github.com/makeplane/plane/releases/download/v1.3.1/docker-compose.yml`
  sha256: `d4cefab6a281a07495713ffac4bdeec206476bff8718d30f9ea67076c7a415f0`
  Do not edit; replace wholesale (and update this provenance note) when syncing a release.
- `docker-compose.override.yml` — the Biplane deltas: swaps in the forked images.
  Image names come from `.env`.
- `env.example` — copy to `.env`; fill every CHANGEME; keep the marked groups in sync
  (domain/port ↔ WEB_URL/CORS; passwords ↔ the URLs that embed them).
- `sync/` — optional upstream-release watcher (containerized); see `sync/README.md`.
- `apply-update.sh` — the reviewed one-command Biplane release apply path; see
  [Apply a Biplane update](#apply-a-biplane-update).

## Install

**1. Build (or pull) the forked images** — the override has no `build:` stanza, so the
images must exist before `up`. Build on a machine matching your deploy architecture:

```bash
./deployments/selfhost/build-images.sh          # tags pi5-<commit>
BIPLANE_IMAGE_TAG=latest ./deployments/selfhost/build-images.sh
```

Use the script rather than raw `docker build`: it bakes the commit into the web and
admin bundles (`VITE_BIPLANE_BUILD`) and uses that same id as the image tag.

**What the build id proves, and what it does not.** The id in the UI identifies the
**source revision** the bundle was built from. It is *not* proof that the artifact
bytes are unchanged: every Docker tag is mutable, including this one — re-running the
script on the same commit rebuilds and overwrites `pi5-<commit>`, and a registry tag
can be re-pushed by anyone with access.

Two different digests, for two different questions — do not mix them:

```bash
# WHICH LOCAL IMAGE IS THIS HOST RUNNING?  (local image/config ID)
docker inspect --format '{{.Image}}' <container>     # the image ID the container runs
docker image inspect --format '{{.Id}}' <image:tag>  # same ID, from the image side

# WHAT DO I PIN A DEPLOYMENT TO?  (registry manifest digest — RepoDigest)
docker image inspect --format '{{index .RepoDigests 0}}' <image:tag>
```

`.Id` is the **local** content ID of the image config. It is evidence about this host
and **cannot be used in `image@sha256:…`** — a pull by that value will not resolve. The
value `image@sha256:…` needs is the **RepoDigest**, the registry's manifest digest,
which exists only once the image has been pushed to (or pulled from) a registry. Pin
deployments to a RepoDigest when you need a guarantee about the exact bytes; use `.Id`
or `{{.Image}}` only to state what a given host is currently running.

- **`pi5-<commit>` is the authoritative, commit-derived tag.** It is written on every
  build and is the only tag the script treats as naming the code inside. Deploy from
  it — or from its digest — when you need to know what is running.
- **An alias is a mutable convenience pointer.** `BIPLANE_IMAGE_ALIAS=latest` (or the
  legacy `BIPLANE_IMAGE_TAG`, accepted with a notice) additionally tags each image from
  its own `pi5-<commit>` image. An alias is expected to move between builds; it never
  replaces the commit-derived tag at build time.
- **Do not overwrite a published tag.** Once `pi5-<commit>` has been pushed anywhere
  others pull from, treat it as spent: rebuild from a new commit rather than
  re-pointing it, or consumers silently get different bytes under the same name.
- It refuses to build when the working tree differs from HEAD — **including untracked
  files under `apps/` or `packages/`**, because Docker copies the working tree, not the
  commit. `BIPLANE_ALLOW_DIRTY=1` overrides and marks the id `<sha>-dirty`.

Push to your registry and point the `BIPLANE_*_IMAGE` variables at it, or
`docker save | docker load` for air-gapped hosts.

**2. Configure:**

```bash
cd deployments/selfhost
cp env.example .env
$EDITOR .env          # every CHANGEME, APP_DOMAIN, LISTEN_HTTP_PORT (+ the keep-in-sync pairs)
```

**3. Start and verify:**

```bash
docker compose config >/dev/null   # render check: catches missing/mismatched env early
docker compose up -d
docker compose logs -f migrator api   # first boot runs migrations
```

Then open `http://<APP_DOMAIN>:<LISTEN_HTTP_PORT>` — the first visit walks you through
creating the instance admin ("god-mode" setup).

## Apply a Biplane update

Run the host command from a checkout that contains the self-host deployment:

```bash
./deployments/selfhost/apply-update.sh v2.4.1
```

The tag is mandatory and must be exactly `vMAJOR.MINOR.PATCH`. The command never
selects `latest` and never chooses from a release listing: it asks the configured
Forgejo source, then the public GitHub mirror, for that exact tag. This removes
listing order, pagination and concurrent-publication ambiguity from the apply
decision. The current backend image performs both bounded metadata fetches under
the same origin/redirect/credential rules as the update banner.

Host prerequisites are Docker with Compose v2, `bash`, `curl`, `jq`, `awk`,
`flock`, `sha256sum`, `stat`, and GNU `sync`. The deployment must already be
running a release image that contains the `biplane_update_metadata` management
command. A development or pre-pipeline image with no comparable release tag is
refused; establish the first release manually rather than asking the updater to
guess its baseline.

The command then:

1. validates the strict `release.json` identity: requested tag, full commit,
   level, and exactly one backend/web/admin/space image with a registry digest;
2. refuses `full` releases before backup or pull — dependency, runtime and image
   changes use the manual rebuild path;
3. stores `.env`, rendered Compose configuration, a custom-format PostgreSQL
   dump, its `pg_restore --list` proof, the running deployment snapshot, and
   checksums under
   `.biplane-backups/<UTC>-<tag>/`;
4. pulls every new image as `image@sha256:...`, verifies the backend's baked
   release tag and commit-derived build id, verifies that same build id is
   present in both displayed frontend bundles, and inspects the new image's
   migration plan;
5. refuses a `code` release when that plan is non-empty. A producer label is
   not permission to run handling the consumer cannot satisfy;
6. for `data`, stops mutation-serving application services and runs migrations
   from the new digest-pinned backend image;
7. writes the four image pins and `BIPLANE_APPLIED_RELEASE` into `.env` through
   a same-directory, fsynced temporary file and atomic rename, recreates the six
   Biplane services, then reads back their active digest references, service
   state, the baked version/build id, the served web/admin build id, and
   `/api/health/`.

No archive is downloaded or extracted. There is no release-signing key, trust
table, Docker socket inside the application, or server apply-run state machine.
The host command is the implementation; a future narrow privileged service may
invoke it without becoming a second updater.

Before treating the installed version as an upgrade baseline, the command
captures one deployment snapshot. Every active Biplane container must use the
digest reference rendered from the four persisted pins; the API's bounded build
id and release version must agree with the persisted release; and the API's
`DATABASE_URL` must resolve exactly to the bundled `plane-db` credentials and
database that the backup command uses. A stale pin, same-version image drift, or
external database therefore refuses before backup, pull, or mutation. External
PostgreSQL apply is deliberately unsupported until one reviewed backup path can
address that target directly.

On a failure before the `.env` commit, the old pins remain byte-identical. On a
code-level failure after commit, the command stops the failed activation,
checksum-verifies and atomically restores the saved `.env`, recreates the old
services, and claims restoration only after their full captured snapshot and
health read back. A data migration is different: failed or partial new services
are stopped and prior pins are restored, but the command does **not** call that
a complete rollback or restore the database automatically. It prints the exact
backup path and leaves database reconciliation/restoration to the operator. See
[`update/FAILURE-STATES.md`](update/FAILURE-STATES.md) for every terminal state.

## Git bridge setup

The bridge runs inside the backend (api/worker/beat-worker) — there is nothing extra to
deploy. It stays **inert until you configure it**: with no `FORGEJO_WEBHOOK_SECRET` set,
the endpoint rejects every delivery, so an unconfigured install is safe by default.

Set these in `.env`:

| Variable | Required | Purpose |
|---|---|---|
| `FORGEJO_WEBHOOK_SECRET` | yes | Forgejo/Gitea's shared secret, **minimum 16 characters**; their deliveries are HMAC-SHA256 verified against it. Unset or shorter than 16 and the bridge refuses every Forgejo/Gitea delivery. Each forge personality has its OWN credential — this one no longer opens any other door. |
| `FORGEJO_INSTANCE_ID` | yes* | A **stable id for THIS Forgejo instance** — it namespaces semantic event keys so two Forgejo servers cannot collide (ADR 010). *Required whenever `FORGEJO_WEBHOOK_SECRET` is set*: an enabled forge with no instance id **fails startup** (and refuses deliveries at 500). It must be **unique per forge** and **stable** — changing it after deliveries have arrived orphans the old namespace (a namespace migration, not a config edit). |
| `FORGEJO_BRIDGE_REPO_MAP` | yes | **JSON object** mapping each repo to the **projects whose work items a directive in it may name** (BIP-38 scope guard — nothing moves, so the guard is on selection, where it keeps its full force: an out-of-scope ref is rejected before it is ever looked up). Keys are `"<provider INSTANCE id>:<stable repository id>"` — the prefix is your configured `*_INSTANCE_ID` value (**not** the forge family name; read it from the live env, never assume `forgejo`), the number is the immutable repo id your forge shows in its API/UI; values are non-empty **lists of stable project UUIDs**. With `FORGEJO_INSTANCE_ID=pi5-forgejo`: `{"pi5-forgejo:42":["9c1f4e2a-…"],"gitlab:1337":["4a2b17c0-…","77de91b5-…"]}`. A display path is mutable — a rename plus path reuse would hand the scope to a different repository — so the immutable id is the boundary; the instance prefix keeps two same-family instances sharing a repo number isolated, and a key under the wrong prefix grants nothing (silently-inert apart from a loud wrong-prefix log warning). A bare `"myorg/myrepo"` path key is a **legacy migration convenience, Forgejo only**: it still works but logs a loud warning on every use, and never grants any other provider. A **workspace-slug value is the retired pre-BIP-38 schema** and is refused as a config defect — it was the live cross-project mover the guard closes; list the project UUIDs explicitly. A ref to any project outside the list is rejected and durably recorded in the delivery result. A repo that isn't listed is ignored. Leaving the map unset while a secret *is* set is treated as a misconfiguration (503), not as "off"; use `{}` to scope nothing deliberately. |
| `FORGEJO_BASE_URL` | no | Your Forgejo URL, e.g. `http://forgejo:3000`. Only needed so the bridge can resolve pushes that exceeded Forgejo's webhook commit limit (default 15). |
| `FORGEJO_BRIDGE_API_TOKEN` | no | Read-only Forgejo token, same purpose. Without both, a truncated push is deferred and retried rather than partially applied. |
| `FORGEJO_BRIDGE_WRITE_TOKEN` | no | **Separate** token with comment access on the mapped repositories. Lets the bridge reply on a pull request explaining why it did not move a ticket. The read token above cannot do this and must not be widened to; unset means the bridge stays silent. |
| `BRIDGE_ALLOW_UNSIGNED_BODY_FORGES` | no | Set to `1` to accept forges whose signature does not cover the request body (e.g. GitLab's static token). That is a weaker guarantee — it proves the sender knew the secret, nothing about the bytes that arrived — so it is off by default and refused with a logged error until you opt in deliberately. |
| `GITHUB_WEBHOOK_SECRET` | no | GitHub's own HMAC secret (min 16 chars). Unset = the GitHub door is closed. **Must differ from every other forge credential.** |
| `GITLAB_WEBHOOK_TOKEN` | no | GitLab's secret token (min 16 chars). Unset = the GitLab door is closed. GitLab **echoes this value verbatim** on every delivery while the other forges use their credentials as HMAC keys — so if this token equals an HMAC secret, observing one GitLab delivery would hand out signing power for the body-bound forges. The bridge refuses GitLab deliveries (with a logged error) until the values differ. |
| `GITHUB_INSTANCE_ID` | yes* | Stable per-instance namespace for GitHub (ADR 010). *Required when `GITHUB_WEBHOOK_SECRET` is set.* Unique per forge; must differ from every other `*_INSTANCE_ID`. |
| `GITLAB_INSTANCE_ID` | yes* | Stable per-instance namespace for GitLab (ADR 010). *Required when `GITLAB_WEBHOOK_TOKEN` is set.* Unique per forge; must differ from every other `*_INSTANCE_ID`. |

**Apply the config before activating the webhook.** These are read at process start, so
running containers keep whatever they booted with — editing `.env` alone leaves the
endpoint rejecting every delivery. Recreate the three services that run the bridge:

```bash
docker compose up -d --force-recreate api worker beat-worker
```

Verify it took, rather than assuming:

```bash
docker compose exec api sh -lc 'echo "${FORGEJO_WEBHOOK_SECRET:+secret set}"; echo "$FORGEJO_BRIDGE_REPO_MAP"'
```

That must print `secret set` and your repo map. If either is empty the recreate didn't
pick up `.env` — fix that before touching Forgejo, or your first deliveries will be
rejected and you'll be debugging the webhook instead of the config.

Then in Forgejo, add a webhook per mapped repo:

- **Target URL** — your `WEB_URL` plus `/api/public/git-bridge/forgejo/`. Use the exact
  `WEB_URL` value from `.env`, including scheme and any non-default port: on a stock
  install that is `http://localhost/api/public/git-bridge/forgejo/`, and if you changed
  `LISTEN_HTTP_PORT` it belongs here too (e.g. `http://localhost:8080/api/...`).
  **Forgejo must be able to reach that URL from where Forgejo runs** — `localhost` only
  works if Forgejo is on this same host. Otherwise use the address Forgejo can resolve
  (and keep `APP_DOMAIN`/`WEB_URL`/`CORS_ALLOWED_ORIGINS` in sync, as the network section
  of `.env` notes).
- **Method** `POST`, **type** `application/json`
- **Secret** — the same `FORGEJO_WEBHOOK_SECRET`
- **Events** — *Push* and *Pull Request*

**The bridge does not move tickets.** Since v1.1.0 (BIP-67), no push, merge or review
changes any ticket's state, in any direction. The bridge recognises which ticket an event
is about, records the outcome durably, and — where it has a write token — comments on the
pull request explaining why it did not act. **You move the ticket.**

**How an event names a ticket.** A commit message or a PR **body** must reference the
ticket explicitly with `ref`/`refs`, `close`/`closes`, `fix`/`fixes` or
`resolve`/`resolves` — e.g. `fixes ENG-42`. Eight spellings, singular and plural; **past
tense is not accepted** (`fixed ENG-42` is not a reference). The keyword is
case-insensitive, the ticket id is uppercase.

- **The directive must own its whole line.** `closes ENG-42 once CI is green` is not a
  reference — that is ordinary English for "not done yet". A single trailing `.` or `!` is
  tolerated.
- **A bare `ENG-42` is deliberately ignored**, so prose and strings like `SHA-256` never
  select anything.
- **PR titles are inert.** Only the body counts. Commit messages are read whole, subject
  line included.

**There is no target state to configure.** Earlier versions resolved a per-project target
from workflow state *groups* — a review-ish `started` state for pushes, a `completed`
state for merges, stamping the completion time. **All of that is deleted with the write
path.** Nothing selects a state, so a project missing a `started` or `completed` state is
no longer a bridge configuration error, and state naming no longer affects the bridge at
all.

**If you expected something to happen.** Deliveries are recorded durably before they're
processed, so nothing is silently dropped. Check `docker compose logs api | grep
git-bridge`, and read the delivery result rather than the board.

- **Nothing moving is the correct behaviour**, not a fault to diagnose.
- **Silence is not approval.** Without `FORGEJO_BRIDGE_WRITE_TOKEN` the bridge records its
  refusal and comments nothing at all — no news is not good news. Check the delivery
  result.
- **A repo missing from `FORGEJO_BRIDGE_REPO_MAP` is not an error** — its deliveries are
  accepted and are entirely inert. There will be no failure to find.
- A mapped project UUID that doesn't exist *is* a defect: the delivery stays pending and
  is retried once the config is fixed.

## Notes

- All state lives in named Docker volumes (`pgdata`, `uploads`, …). Back them up —
  or bind-mount them onto storage your existing backup tooling covers.
- The compose exposes nothing but the proxy port; everything else stays on the
  internal network.
- Telemetry: Biplane images send nothing anywhere unless you explicitly set an
  `OTLP_ENDPOINT` for the api/worker services.
