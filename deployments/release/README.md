# Biplane release pipeline (BIP-40 / M5.1) — producer

> **THIS PIPELINE IS RUN BY HAND. THERE IS NO CI.**
>
> Until 2026-08-15 this file described CI as the enforcing authority. It was not.
> `example/biplane` had **zero registered Actions runners and zero workflow runs in
> its entire history** — measured, not assumed — so the workflow that held every
> gate had never executed and could not. The gate scripts below were called by
> **nothing**: outside that workflow they appeared only in this file, in a table
> of what they do (Vex, 2026-08-15).
>
> That is the defect this project has already paid for once, one layer up: a
> control read from the design and asserted as behaviour, in the document that
> tells a human how to release. The release note that claimed every bridge write
> shipped off was the same mistake.
>
> The release workflow — and, by Morrow's later class ruling, every other
> workflow able to publish outward (`build-branch.yml` and
> `feature-deployment.yml`, which pushed our images to **Docker Hub** through a
> third-party action, and `codeql.yml`, which uploaded source-derived analysis
> to GitHub) — has been **deleted** rather than ported (2026-08-15). Four inert
> workflows remain; none can publish anything.
>
> **On `codeql.yml` specifically, because a deletion should not read as a
> judgement it is not** (Vex, accepted by Morrow): it was removed because its
> only implementation uploads source-derived analysis to **GitHub** and has
> never run here — not because code scanning is unwanted. Nothing operational is
> lost today, since it has never executed. The consequence to be honest about:
> if a runner appears later, **this instance will have no code scanning at all**
> until a deliberate Forgejo-local capability is built and witnessed. That is a
> gap someone should choose to close, not one to discover. A correct-looking unexecuted replacement would recreate the same
> false contract, and it published to **ghcr.io** — outward, to GitHub —
> against an explicit instruction that Forgejo is private and nothing goes to
> GitHub yet. Leaving it behind a refusal step would have preserved that latent
> outbound capability. CI publication may return only with a runner **and** an
> end-to-end witness against the Forgejo registry.
>
> **`preflight.sh` is now the caller.** It runs every gate below, in the
> workflow's own order, from the path a human actually walks. Run it first; if
> you skip it you are releasing by memory, and a guard with no caller is not a
> guard.

The release builds the images `deployments/selfhost/build-images.sh` defines,
pushes them **digest-pinned** to the registry, and publishes an ordinary release
whose metadata identifies exactly what shipped. There is **no signed manifest,
no key material, and no per-service bundles**: the 2026-08-12 Scope-A rewrite
removed them after three reviews found they added no protection over TLS +
content-addressed registry pulls (see `docs/scope-a-architecture.md` §M5). Image
digests are the executable identity.

## The manual procedure (current authority)

```bash
# 1. every gate, in order — refuses rather than warns
deployments/release/preflight.sh v1.1.0 /tmp/release
#    -> /tmp/release/release-notes.md, /tmp/release/level

# 2. build on a machine of the TARGET ARCHITECTURE, with the tag baked in
BIPLANE_RELEASE_TAG=v1.1.0 bash deployments/selfhost/build-images.sh

# 3. move the images to the host that can reach the registry
docker save biplane-backend:pi5-<sha> ... | ssh <host> docker load

# 4. ON THAT HOST: push and take the digest FROM THE REGISTRY, not from stdout.
#    Forgejo's registry is HTTP-only and Docker refuses plain-HTTP pushes to a
#    named host — but treats localhost as insecure by default, so pushing from
#    the box that HOSTS the registry needs no daemon change and no restart of
#    the services running on it. Measured 2026-08-15 with a real push.
docker login localhost:3000 -u <user> --password-stdin
docker tag biplane-<svc>:pi5-<sha> localhost:3000/<owner>/biplane-<svc>:v1.1.0
docker push localhost:3000/<owner>/biplane-<svc>:v1.1.0 > push.out
digest="$(deployments/release/parse-push-digest.sh push.out)"
docker buildx imagetools inspect "localhost:3000/<owner>/biplane-<svc>@${digest}" \
  --format '{{json .Manifest.Digest}}'   # MUST equal $digest, or stop

# 5. assemble the metadata
deployments/release/make-release-metadata.sh --tag v1.1.0 --commit <sha> \
  --level "$(cat /tmp/release/level)" --images images.json --out /tmp/release

# 6. TAG the exact merge commit and push the tag
git tag v1.1.0 <merge-sha> && git push origin v1.1.0

# 7. CREATE the release at that commit and ATTACH release.json (witnessed flow:
#    create, upload, then read back and byte-diff — a publish you have not read
#    back is a hope, not a release)
curl -sf -X POST -H "Authorization: token $FORGEJO_TOKEN" -H "Content-Type: application/json" \
  -d "{\"tag_name\":\"v1.1.0\",\"target_commitish\":\"<merge-sha>\",\"name\":\"v1.1.0\",
       \"body\":$(jq -Rs . /tmp/release/release-notes.md)}" \
  "$FORGEJO_API/repos/<owner>/<repo>/releases"
RID=$(curl -sf -H "Authorization: token $FORGEJO_TOKEN" \
  "$FORGEJO_API/repos/<owner>/<repo>/releases/tags/v1.1.0" | jq .id)
curl -sf -X POST -H "Authorization: token $FORGEJO_TOKEN" \
  -F "attachment=@/tmp/release/release.json;filename=release.json" \
  "$FORGEJO_API/repos/<owner>/<repo>/releases/$RID/assets?name=release.json"
# READBACK: download the asset and diff byte-for-byte against the local file
curl -sfL -H "Authorization: token $FORGEJO_TOKEN" \
  "$(curl -sf -H "Authorization: token $FORGEJO_TOKEN" \
     "$FORGEJO_API/repos/<owner>/<repo>/releases/tags/v1.1.0" \
     | jq -r '.assets[0].browser_download_url')" -o /tmp/readback.json
diff /tmp/release/release.json /tmp/readback.json   # MUST be identical
```

Without steps 6-7 the "manual procedure" stopped at a local file: no tag, no
release, no asset — nothing `first-hop-update.sh`'s consumer could ever fetch.
These are the exact commands witnessed end-to-end on 2026-08-15 (throwaway
repo, byte-identical readback), committed here because with no workflow they
have no other caller.

## Which host does what — read this BEFORE you start

The steps above are **not interchangeable between machines**. Each one lives
where it does because of a property of the infrastructure, not a preference:

| step | host | why it must be there |
|---|---|---|
| install the applier (**once, before any of the below**) | the deploy host, as the deploy user | `APPLY_SERVICE_REPO` must point at a checkout that *carries* `apply-service`, and `APPLY_SERVICE_COMPOSE_DIR` at wherever the `.env` and compose actually live. Nothing in the `push` or `apply` rows works until this is done — see `deployments/selfhost/apply-service/README.md` |
| build | the build host (`~/biplane/build`, `builder` tmux) | target architecture, and the build's output is the thing an operator inspects |
| ship | build host → deploy host | `docker save … \| ssh <deploy-host> docker load` |
| push | **the deploy host, which also hosts the registry** | Forgejo's registry is HTTP-only. Docker refuses plain-HTTP pushes to a *named* host but treats `localhost` as insecure by default, so pushing from the registry's own box needs no daemon change and no restart of what is running on it |
| tag / release / readback | anywhere with an API token | ordinary Forgejo API |
| apply | the deploy host | `code` via the button or `/op/apply`; `full` by hand |

**Pushing from a third machine is the tempting shortcut, and it does not work.**
It needs either an edit to `/etc/docker/daemon.json` and a Docker restart on that
box — which stops everything else running there — or a registry token carrying
package scopes. Ship to the deploy host and push from it. (Learned by walking
into it on 2026-08-19; the reason was already written above and not read.)

## One command: `release.sh`

`deployments/release/release.sh <tag>` sequences the whole procedure for a
**`code`-level** release: preflight → publish → metadata → tag → release with
byte readback → update check → apply → verify. Every step fails closed and says
which gate stopped it.

It **does not build**. Step 2 prints the exact commands and stops, because the
build is the step whose *output* an operator must inspect — what was compiled,
from which commit, with which version baked in. Automating that behind a prompt
converts inspection into a click.

The Docker-requiring steps go through the applier's **operator operations**
(`APPLY_SERVICE_URL` + `APPLY_SERVICE_TOKEN`), so the caller does not need to be
in the `docker` group — see `deployments/selfhost/apply-service/README.md`. That
is what removes the twenty hand-run pastes; twenty pastes was never security
either, it was a human used as an access control.

**It refuses a `full` tag at step 1**, because a full release changes the runtime
itself and takes the hand-applied path (`deployments/selfhost/MANUAL-FULL-UPGRADE.md`).

**Known gap, stated rather than discovered:** steps 3–6 (publish, metadata, tag,
release) are *level-agnostic* — they publish bytes and write a record, and
neither cares how the release will later be installed. The refusal covers the
whole script, so a `full` release has no automated path even for those steps and
they must be run by hand from the procedure above.

Doing so is **not** stepping around the guard, and the difference is worth being
precise about because it is *cannot* rather than *do not*: `apply-update.sh`
refuses `full` **independently**, reading the level out of the release metadata
at apply time (`[ "$level" != "full" ] || die`). Hand-publishing a `full`
release therefore cannot produce one the button will install — the two refusals
are separate, and the second one does not care how the first was reached.

## What a release publishes

- **Refused before anything publishes:** a tag outside `vMAJOR.MINOR.PATCH`, or a
  tag whose commit is not an ancestor of protected `main` (an off-main tag could
  otherwise get a legitimate release issued against it — RC 3412; `preflight.sh`
  proves the ancestry before anything builds).
- **Digest-pinned images** pushed to the registry. The pushed digest is read
  back FROM the registry before it is recorded — push stdout is not trusted.
- **`release.json`** (unsigned) — the producer->consumer contract the update
  check (M5.2) reads:

  | field | meaning |
  |---|---|
  | `schema_version` | `1` |
  | `tag` | the release tag |
  | `commit_sha` | full 40-hex commit |
  | `level` | `code` \| `data` \| `full` (see below) |
  | `images` | `[{image, digest}]` — resolved registry digests, the executable identity |

- **Release notes** = the CHANGELOG entry for the tag. A release without one is
  invalid.
- The **same tag is baked into the images** as the installed version
  (`BIPLANE_RELEASE_TAG` → `build-images.sh`, #54), so the running side is a
  semver the update check can compare. One value feeds both.

## Release level (`code` \| `data` \| `full`)

Not a security control — it decides how an operator applies the update: `code`
swaps code, `data` runs a migration (backup first), `full` is the manual image
path. **`preflight.sh` derives the minimum level from the diff and enforces it**:
a hand-declared level below the derived minimum refuses the release, so a
`code`-labelled release cannot silently skip a migration — provided preflight is
run. It is the caller that makes this a guarantee rather than a description;
before it existed, this paragraph named an enforcement that nothing performed.

- migrations ⇒ at least `data`
- lockfile / dependency / Docker / base-image / runtime / packaging ⇒ `full`

## Scripts (the source of truth; `preflight.sh` is the caller)

| script | job |
|---|---|
| **`preflight.sh <tag> <outdir>`** | **runs every gate below, in order — the caller** |
| `derive-level.sh <prev> <ref>` | minimum level from the diff |
| `enforce-level.sh <declared> <derived>` | refuse an under-labelled release |
| `parse-changelog.sh <CHANGELOG> <tag>` | the one exact entry + its declared level |
| `parse-push-digest.sh <push-output>` | the one authoritative pushed digest |
| `validate-tag.sh <tag>` | refuse a tag the update check can't compare (RC 3412) |
| `make-release-metadata.sh …` | assemble + validate `release.json` |

Run the harness: `deployments/release/release-pipeline.test.sh`.
