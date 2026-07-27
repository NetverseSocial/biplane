# biplane-sync (optional)

Keeps your Biplane fork tracking upstream Plane CE releases. Deterministic pipeline:
check `makeplane/plane` latest release → if new: merge onto a `sync/<tag>` candidate
branch → candidate image build → **Forgejo issue** on the fork repo with the outcome
(READY / MERGE CONFLICT / BUILD FAILED). It never merges to your integration branch
itself — candidate PRs go through normal review (author ≠ merger).

Runs **in a container**; the host crontab holds one line that invokes it.

## Build

```bash
cd deployments/selfhost/sync
docker build -t biplane-sync .
```

## Run (host cron)

```cron
17 3 * * * docker run --rm \
  -v biplane-sync-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e FORGEJO_URL=http://your-forgejo:3000 \
  -e FORK_REPO=your-org/biplane \
  -e FORK_BRANCH=main \
  -e TOKEN_FILE=/data/forgejo.token \
  biplane-sync
```

Put a Forgejo token (repo+issue write) at `/data/forgejo.token` inside the volume, or
pass `-e FORGEJO_TOKEN=...` from your secret store. Daily is plenty; the GitHub check
uses conditional requests (ETag), so 304 responses don't consume API rate limit.

## Configuration (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `FORGEJO_URL` | `http://localhost:3000` | Your Forgejo/Gitea base URL |
| `FORK_REPO` | **required** | Fork repo (owner/name), e.g. `your-org/biplane` |
| `FORK_BRANCH` | `main` | Integration branch to merge candidates against |
| `SYNC_ASSIGNEE` | *(none)* | Forgejo user to assign outcome issues to |
| `UPSTREAM_REPO` | `makeplane/plane` | Upstream GitHub repo |
| `BUILD_TIMEOUT` | `7200` | Seconds before the candidate build is aborted |
| `TOKEN_FILE` | `/data/forgejo.token` | File holding the Forgejo token (or pass `FORGEJO_TOKEN` directly) |
| `STATUS_FILE` | *(none)* | Optional path for a JSON status drop (dashboards) |
| `SYNC_GIT_EMAIL` | `sync@noreply.biplane.dev` | Committer identity for merge commits (non-deliverable) |
| `SYNC_BASE` | `/data` | State/log/work directory (persistent volume) |

State: `/data/state` (last handled tag). Log: `/data/last-run.log`.

## Security notes

- Mounting `/var/run/docker.sock` gives this container **host-equivalent privilege**;
  it is needed for the candidate image builds. Run the watcher on a build box you
  already trust with that, not on an exposed host.
- The Forgejo token is passed to git via `GIT_CONFIG_*` environment variables and to
  curl via an on-disk header file (0600) — it never appears in process argv.

## Tests

`bash test-sync.sh` — deterministic PATH-stub tests covering the happy path (all four
images, valid tags), failed push, Forgejo 5xx on report, merge conflict, build failure,
and missing FORK_REPO. No network, no real docker.

- The git auth header is **URL-scoped to `FORGEJO_URL`** (`http.<forgejo>/.extraHeader`).
  An unscoped `http.extraHeader` would send your Forgejo token to github.com on the
  upstream fetch. Witnessed both directions with real git + `GIT_CURL_VERBOSE`:
  unscoped → `authorization` header sent to github.com; scoped → zero auth headers to
  github.com, and the private-repo ls-remote against Forgejo still authenticates
  (control: scoping to a wrong host makes Forgejo auth fail). If you change
  `FORGEJO_URL`, note git matches by URL prefix — keep the exact scheme/host/port form.
