# Applying a `full` release by hand

> **This is the document `first-hop-update.sh` and `apply-update.sh` send you to.**
> Until 2026-08-15 it did not exist: both scripts refused a `full` release with
> *"use the documented manual full/runtime upgrade path"*, and that phrase
> appeared nowhere else in the repository. An operator following the refusal had
> nothing to follow.

## Why a `full` release is refused, and why THIS deployment's first release measured `full`

The release level says how an update must be applied: `code` swaps code, `data`
additionally runs migrations, and **`full` means the runtime itself changed** —
a base image, a pinned dependency, the lock file, packaging. The automated apply
path deliberately does not attempt those, because it cannot verify the parts of
the change that live outside the images it pulls.

**This deployment's first release measured `full` — a measured fact about this
repo, not a law about first releases** (the CHANGELOG records it that way). With
no prior published release the level-derivation has nothing to diff against, so
it baselines from the repository ROOT, and every migration, Dockerfile and
requirements change made *after* the root commit — everything the `root..HEAD`
diff contains (the root commit's own content is not in that diff) — falls inside
it, which is what made the level `full`. A repository whose root already carried
its runtime would derive differently. Either way, the automated path becomes
usable from the *second* release onward.

That is not a defect in either script. It does mean the first hop is walked by
hand, which is what this file is for.

## What this procedure must preserve

Do not improvise a shorter version. The automated path's guarantees are the
point, and each step below exists because skipping it makes a failure
unrecoverable rather than merely annoying:

- **The backup is taken BEFORE anything is pulled**, so a failed pull leaves a
  deployment you can still restore.
- **The backup is verified by checksum before it is ever relied on.** A backup
  you have not verified is a belief, not a backup.
- **The images are pulled by DIGEST, never by tag**, and each is cross-checked
  against the release record before anything is recreated.
- **A failure restores the previous pins atomically**, or says
  `RECOVERY REQUIRED` and stops. It never half-restores and reports success.

## Procedure

Run from the deployment's compose working directory on the deployment host —
the one `docker compose` was invoked from, which you can confirm without
guessing:

```bash
docker inspect <a running biplane container> \
  --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

`TAG` is the exact release tag; the four digests come from the release's
`release.json`.

Set these ONCE, before step 0 — the backup directory must exist before step 0's
legacy baseline writes into it:

```bash
TAG=v1.1.0                                   # the exact release tag you are applying
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=.biplane-backups/${STAMP}-${TAG}
mkdir -m 0700 -p "$BACKUP"
```

### 0. Classify the deployment first: LEGACY or MANAGED

This changes what step 6 means, and getting it wrong is how you end up with a
config that describes a deployment you do not have.

```bash
docker compose --env-file .env config | grep -E 'image:.*biplane-'
grep -c '^BIPLANE_.*_IMAGE=' .env ; grep -c '^BIPLANE_APPLIED_RELEASE=' .env
```

- **MANAGED** — the four `BIPLANE_*_IMAGE` keys are present and hold `@sha256:`
  digests, and `BIPLANE_APPLIED_RELEASE` names a tag. Step 6 REPLACES existing
  values.
- **LEGACY** — the services run mutable tags (`biplane-backend:pi5-<sha>`,
  `biplane-web:pi5-prod`) and some or all of those keys are absent. **This is
  the normal state for a deployment that has never had a release applied**, and
  it is what `first-hop-update.sh` exists to convert. **The image keys may
  well be PRESENT already, holding mutable tags rather than digests** — that is
  the case on this project's own deployment — so step 6 REPLACES those four and
  CREATES only `BIPLANE_APPLIED_RELEASE`. Check, do not assume either way.

**Record the running baseline before you change anything — the rollback
identity, captured the SAME way whether you are legacy or managed.** A restored
config file is not a restored deployment; the image IDs are the bytes.

```bash
# Sorted, so docker ps ordering cannot false-red the rollback diff later.
for c in $(docker ps --format '{{.Names}}' | grep biplane-); do
  printf '%s\t%s\n' "$c" "$(docker inspect --format '{{.Image}}' "$c")"
done | sort > "$BACKUP/preupdate-image-ids.txt"

# Fail closed: an EMPTY baseline (grep matched no biplane- container) cannot prove
# a rollback — an empty post-restore capture would diff clean against it and lie.
# At least the six mutation-serving services must be present.
n=$(wc -l < "$BACKUP/preupdate-image-ids.txt")
[ "$n" -ge 6 ] || { echo "baseline captured $n biplane containers (<6) — refusing before any change"; exit 1; }
```

This replaces the automated path's deployment snapshot, and because it is
classification-independent the rollback below needs one proof, not a legacy arm
and a managed arm.

Write the pre-existing installed release and build as **`unknown`**, never
inferred from an image tag. A tag like `pi5-prod` is a name someone chose; it is
not a version, and guessing one here produces a deployment that confidently
reports a release it never ran. (`first-hop-update.sh` has a case pinning
exactly this: *"legacy baseline records installed release/build as unknown,
never inferred"*.)

A real example of why, measured on this project's own deployment: the backend
ran `biplane-backend:pi5-8ca1fa6` while the three display services ran
`biplane-web:pi5-prod`, `-space:pi5-prod`, `-admin:pi5-prod`. **Two different
tag schemes, and nothing proving they were built from the same commit.** There
is no single "installed version" to record there — which is the whole reason
the answer is `unknown` rather than a best guess.

### 1. Read the release record

```bash
# TAG was set in Setup, above; this verifies the record matches it.
# release.json is attached to the release; take the digests from it, never from
# a `docker push` transcript — a digest that was not read back FROM the registry
# is not an identity.
jq -r '.tag, .commit_sha, .level, (.images[] | "\(.image)@\(.digest)")' release.json
```

Stop if `.tag` is not the release you intend to apply, or if `.level` is not
`full` — a `code` or `data` release should go through `apply-update.sh`, which
does all of this for you and is tested.

### 2. Back up, before pulling anything

```bash
# STAMP / BACKUP / mkdir were set in Setup, before step 0 — do not repeat them.
cp --preserve=mode,timestamps .env "$BACKUP/config.env"
docker compose --env-file .env config > "$BACKUP/compose.rendered.yaml"
cp release.json "$BACKUP/release.json"
# PGPASSWORD + -h localhost are load-bearing: the plane-db image requires a
# password even locally, so a bare `pg_dump` fails "no password supplied" and the
# whole apply aborts before it starts. Authenticate the way the deployment's own
# backup does (/usr/local/bin/restic-backup on the deploy host uses this form).
docker compose --env-file .env exec -T plane-db \
  sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -w -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP/database.dump"
[ -s "$BACKUP/database.dump" ] || { echo "database backup is empty"; exit 1; }
docker compose --env-file .env exec -T plane-db pg_restore --list < "$BACKUP/database.dump" >/dev/null
( cd "$BACKUP" && sha256sum config.env compose.rendered.yaml release.json database.dump preupdate-image-ids.txt > SHA256SUMS )
```

The `pg_restore --list` is not decoration: it is the difference between a file
that exists and a dump that can be read back. A zero-byte or truncated dump
passes `-s` and fails when you need it.

### 3. Verify the backup

```bash
( cd "$BACKUP" && sha256sum --check --strict SHA256SUMS )
```

**If this fails, stop.** Do not continue and do not attempt an automatic
restore afterwards — you have no verified point to return to.

### 4. Pull by digest and cross-check the images

```bash
# the four refs are exactly release.json's images[]; pull each by digest
jq -r '.images[] | "\(.image)@\(.digest)"' release.json | while read -r ref; do
  docker pull "$ref"
done

# The backend must DECLARE the tag it claims to be, and its build id must match
# the release commit. An image that reports a different version is the wrong
# image, whatever its digest resolves to.
docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' <backend@digest> \
  | grep -E '^BIPLANE_(VERSION|BUILD)='
```

`BIPLANE_VERSION` must equal `$TAG`, and the release's `commit_sha` must start
with `BIPLANE_BUILD`.

Then the display bundles must contain that same build id — this is what stops a
half-swapped deployment serving a new backend behind an old interface:

```bash
docker run --rm --entrypoint sh <web@digest>   -c 'grep -Rqs -- "$1" /usr/share/nginx/html'       sh "$BUILD"
docker run --rm --entrypoint sh <admin@digest> -c 'grep -Rqs -- "$1" /usr/share/nginx/html/admin' sh "$BUILD"
```

**web and admin only, at exactly those roots** — that is what `apply-update.sh`
checks and what its harness pins. `space` is deliberately NOT checked here; if
you think it should be, change the CODE and the harness first and then this
document, because a document that checks more than the code is a document that
will be quietly wrong in one direction or the other (7of9).

### 5. Migrations — plan first, then apply

**THE INLINE `BIPLANE_BACKEND_IMAGE` IS LOAD-BEARING. Do not drop it.**

```bash
# Quiesce every mutation-serving service BEFORE migrating — apply-update.sh does
# this for data/full so nothing commits against a schema that is mid-change. From
# here until step 7 the deployment is intentionally DOWN.
docker compose --env-file .env stop web space admin api worker beat-worker

NEW_BACKEND=<backend@sha256:... from release.json>
BIPLANE_BACKEND_IMAGE="$NEW_BACKEND" docker compose --env-file .env \
  run --rm --no-deps -T --entrypoint python migrator manage.py migrate --plan
BIPLANE_BACKEND_IMAGE="$NEW_BACKEND" docker compose --env-file .env \
  run --rm --no-deps -T --entrypoint python migrator manage.py migrate --noinput
```

Why the override, and this is the failure mode this whole document exists to
prevent (found by 7of9 reading the draft as its executor): `migrator` resolves
its image from `BIPLANE_BACKEND_IMAGE` in `.env`, and at this point `.env`
**still names the OLD backend** — step 6 has not run. Without the inline
override you would run the OLD image's migrations, which are already applied, so
the command SUCCEEDS and does nothing. The new release's migrations never land.
Step 7 then brings up the new code against the un-migrated schema: missing
tables, missing columns, and a deployment that broke at the step meant to make
it safe. It reads perfectly and quietly does the wrong thing.

(The alternative — moving step 6 before step 5 — also works, but then a failed
migration leaves the config pinned to a release that is not running. Keeping the
pin last and overriding inline is what `apply-update.sh` does.)

Read the plan before running the second command. **A `full` release may carry
runtime changes the plan cannot show you** — that is the reason this path is
manual, so this is the step to slow down on rather than the one to skip.

### 6. Pin the new release, atomically

Five keys — the four digest pins and the applied tag:

```
BIPLANE_BACKEND_IMAGE   BIPLANE_WEB_IMAGE   BIPLANE_ADMIN_IMAGE
BIPLANE_SPACE_IMAGE     BIPLANE_APPLIED_RELEASE
```

**Do not hand-roll the write. Reuse the one that ships beside this file** (7of9)
— and note SOURCING alone changes nothing; the two CALLS are the step:

```bash
. ./apply-update.sh          # defines write_pinned_env + atomic_replace

# the four release refs are release.json's images[], by digest; APPLIED_RELEASE is TAG
BACKEND=<backend@sha256:...>  WEB=<web@sha256:...>
ADMIN=<admin@sha256:...>      SPACE=<space@sha256:...>

write_pinned_env .env .env.biplane-render "$TAG" "$BACKEND" "$WEB" "$ADMIN" "$SPACE"
atomic_replace   .env .env.biplane-render

# WITNESS the five keys actually changed BEFORE you recreate. If they match the
# backup, step 6 did not run and recreating would bring the OLD images back up.
if diff -q <(grep -E '^BIPLANE_(BACKEND|WEB|ADMIN|SPACE)_IMAGE=|^BIPLANE_APPLIED_RELEASE=' "$BACKUP/config.env" | sort)            <(grep -E '^BIPLANE_(BACKEND|WEB|ADMIN|SPACE)_IMAGE=|^BIPLANE_APPLIED_RELEASE=' .env | sort) >/dev/null; then
  echo "PINS UNCHANGED — step 6 did not run; do NOT recreate"; exit 1
fi
grep -E '^BIPLANE_(BACKEND|WEB|ADMIN|SPACE)_IMAGE=|^BIPLANE_APPLIED_RELEASE=' .env
```

`write_pinned_env` replaces-or-appends all five keys, and `atomic_replace`
writes to a temp file on the **same filesystem**, fsyncs, renames, and fsyncs
the directory. Identical guarantee to the automated path, one owner, and it
handles both cases correctly — replacing four existing tag pins and creating
`BIPLANE_APPLIED_RELEASE` where none existed. A snippet improvised at 2am is
where a half-written `.env` comes from, and a half-written `.env` is a
deployment that will not start and cannot be diagnosed from its own config.

### 7. Recreate — SIX services, not four

**Four images, six services.** `BIPLANE_BACKEND_IMAGE` backs `api`, `worker`
and `beat-worker` (and the run-once `migrator`), so recreating only the four
"image-shaped" services leaves the two background workers running the OLD
backend against the NEW database schema. That is precisely the half-swapped
deployment the identity checks in step 4 exist to prevent, arrived at from the
other direction.

```bash
docker compose --env-file .env up -d --force-recreate --no-deps \
  web space admin api worker beat-worker
```

That list is `SERVICES` in `apply-update.sh`; keep them in step. If you edit one
list, edit the other.

### 8. Prove it, rather than assume it

Check the running version reports `$TAG` and not `UNKNOWN`, and that the board
answers a real request. **Container `Up` is not working** — look at what it
serves. Confirm the two workers came back on the new image too, since they are
the ones nobody watches.

## If anything fails after step 4

**From step 5 the mutation-serving services are intentionally stopped**, so a
failure after step 5 leaves them DOWN by design; `RECOVERY REQUIRED` here means a
stopped deployment to restore, not a limping one to diagnose. Between the step-5
stop and a recreate, `docker ps` shows nothing — that is the quiesced state, not
lost containers.

**If the backup's checksum did not verify (step 3), do NOT restore** — report
`RECOVERY REQUIRED`, keep the backup directory, and get a second pair of eyes.
You have no proven point to return to.

**Decide the database BEFORE restoring old code.** If step 5's migration ran, the
schema is now the new release's. Restarting the OLD code against it is
old-code-on-new-schema, and that is NOT a safe default — additive migrations may
tolerate it, a `full` release's runtime changes may not. Choose explicitly:

- **keep the new code** and finish forward, or
- **roll the database back EXPLICITLY** from the verified dump, then restore old
  code. `--exit-on-error --single-transaction` so a partial restore aborts whole
  rather than leaving a half-restored database:

  ```bash
  docker compose --env-file .env exec -T plane-db \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore --clean --if-exists \
             --exit-on-error --single-transaction \
             -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$BACKUP/database.dump"
  ```

Never restart old code on the migrated schema by default.

**Restore the pins atomically, recreate the quiesced services, and prove the
bytes against the step-0 baseline — one proof, legacy or managed:**

```bash
. ./apply-update.sh                              # defines atomic_replace
atomic_replace .env "$BACKUP/config.env"         # restore the previous pins
docker compose --env-file .env up -d --force-recreate --no-deps \
  web space admin api worker beat-worker         # recreate — docker ps was empty until now

# Compare running image IDs to the step-0 baseline. Sort both (ordering is not
# identity), and assert the post-restore capture is NON-EMPTY first, so an empty
# set cannot diff clean against an empty baseline and report a false restore.
now=$(mktemp)
for c in $(docker ps --format '{{.Names}}' | grep biplane-); do
  printf '%s\t%s\n' "$c" "$(docker inspect --format '{{.Image}}' "$c")"
done | sort > "$now"
[ "$(wc -l < "$now")" -ge 6 ] || { echo "RECOVERY REQUIRED: fewer than six running biplane containers after recreate"; exit 1; }
diff "$now" "$BACKUP/preupdate-image-ids.txt" \
  && echo "restored, proven by image id" \
  || { echo "RECOVERY REQUIRED: running bytes do not match the pre-update baseline"; exit 1; }
```

Restoring a config file is not restoring a deployment; the diff above is what
tells them apart. Rolling code back is reversible, rolling data back is not,
which is why the database restore above is explicit and deliberate, never
automatic.
