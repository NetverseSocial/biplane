#!/usr/bin/env bash
# BIP-42 / M5.4: ONE-TIME first-hop update — legacy/unmanaged -> managed.
#
# Pairs with apply-update.sh, which owns steady-state apply. This command exists
# only to cross the lifecycle boundary the steady-state command cannot: a legacy
# deployment (e.g. Pi5 8ca1fa6) whose running image has no metadata resolver, no
# digest pins, and no provable installed release identity, so apply-update.sh
# fails closed before it can act (deployment-update-rowan.md "The missing
# contract").
#
# It is deliberately NOT a second updater. It refuses once the deployment is
# managed (section 1 mutual exclusion); after one successful first hop every
# later update uses apply-update.sh, and this script refuses to run again.
#
# Reuses apply-update.sh's reviewed functions verbatim by sourcing it — its
# main() is guarded by BASH_SOURCE[0] == "$0", so sourcing loads functions only.
# It deliberately does NOT call main()'s managed-baseline capture
# (capture_running_snapshot) as a baseline, nor its running-image metadata call
# (fetch_metadata): the first hop captures a FACTUAL legacy snapshot and runs the
# resolver from the operator's seed bytes instead (sections 3 and 4).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./apply-update.sh
source "$SCRIPT_DIR/apply-update.sh"

# The one image ref the operator supplies as an executable seed (section 2). Its
# provenance/trust as an operator-supplied fact is ADR 011's; this script only
# consumes it and cross-checks it against the forge-resolved record.
usage() {
  cat >&2 <<'USAGE'
usage: first-hop-update.sh <vMAJOR.MINOR.PATCH> <backend-image@sha256:...>
  <tag>         exact canonical stable tag; never "latest", never a listing.
  <backend@sha> the ONE executable seed: the backend image by registry digest.
                The commit and the other three image digests are NOT operator
                input; the canonical resolver produces them from the forge record.
USAGE
  exit 2
}

# ---------------------------------------------------------------------------
# Section 1 — managed/legacy classification (mutually exclusive entry predicate)
# ---------------------------------------------------------------------------
# MANAGED iff the WHOLE section-1 predicate holds together — and it is built from
# SEPARATE facts, not one composite, because the composite hides two of them
# (Rowan, review of 2a7a636): capture_running_snapshot binds images/version/build
# /DB but (a) tolerates an ABSENT BIPLANE_APPLIED_RELEASE and (b) never proves the
# resolver. A digest-pinned deployment with neither an applied tag nor a resolver
# would then read managed, first hop would refuse, and ordinary apply would still
# die at fetch_metadata — the undesigned third state acceptance #2 forbids. So the
# applied tag (present and equal to the running version) and an executable
# resolver are asserted as their own facts. Command presence is thus one required
# fact, never the whole test: a transitional image with the resolver but tag pins
# / unknown identity fails the digest-pin or applied-tag fact and is handled by
# first hop, not refused by both paths.
running_resolver_present() {
  # The running backend can EXECUTE the canonical resolver. Proven by the command
  # initializing in the configured backend image under the deployment's own
  # complete environment via compose (managed deployments have that environment).
  compose run --rm --no-deps -T --entrypoint python api \
    manage.py help biplane_update_metadata >/dev/null 2>&1
}

is_managed() {
  local backend web admin space ref snapshot applied
  backend="$(env_value BIPLANE_BACKEND_IMAGE || true)"
  web="$(env_value BIPLANE_WEB_IMAGE || true)"
  admin="$(env_value BIPLANE_ADMIN_IMAGE || true)"
  space="$(env_value BIPLANE_SPACE_IMAGE || true)"
  for ref in "$backend" "$web" "$admin" "$space"; do
    [[ "$ref" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || return 1
  done
  verify_rendered_identity "$ENV_FILE" "$backend" "$web" "$admin" "$space" || return 1
  snapshot="$(capture_running_snapshot "$backend" "$web" "$admin" "$space" 2>/dev/null)" || return 1
  # Separate fact: applied release tag is PRESENT and equals the running version.
  applied="$(env_value BIPLANE_APPLIED_RELEASE || true)"
  [ -n "$applied" ] || return 1
  [ "$applied" = "$(jq -r .release <<<"$snapshot")" ] || return 1
  # Separate fact: the running backend can execute the resolver.
  running_resolver_present || return 1
  return 0
}

# ---------------------------------------------------------------------------
# Section 3 — resolve target metadata from the SEED bytes, least authority
# ---------------------------------------------------------------------------
# Runs the canonical biplane_update_metadata resolver from the operator-supplied
# backend digest in an ephemeral container — NOT from the running image (which may
# lack it, or be an older second authority for the release record). The container
# gets ONLY a disposable non-routable bootstrap env plus the configured release
# origin: no Docker socket, no deployment mounts/filesystem, no mutation
# credential, no service-control authority (acceptance #4).
#
# The bootstrap env is the minimum for Django to INITIALIZE the command
# (plane.celery constructs Redis at import, so an unset REDIS_URL aborts before
# dispatch — Rowan blocker 2), made non-routable so the container cannot reach or
# mutate any deployment service. It is NEVER the live Redis/DB credentials or the
# real SECRET_KEY.
# The host of the configured Forgejo release origin, or empty.
_forge_origin_host() {
  local url="$1" host
  [ -n "$url" ] || return 0
  host="${url#*://}"; host="${host%%/*}"; host="${host%%:*}"
  printf '%s' "$host"
}

# Narrow, explicit host resolution for the configured Forgejo origin (Rowan RC
# 3723): Docker's ordinary bridge resolves public DNS (the GitHub fallback) but
# NOT host mDNS, so a `.local` forge origin is unreachable from the resolver
# container. Resolve it ON THE HOST to exactly one IPv4 and pin a single
# --add-host, giving the container reachability to that one origin WITHOUT joining
# the Compose service network (which would expose DB/Redis DNS and weaken the
# isolation contract). Ambiguity — zero or many addresses — is refused.
_resolver_add_host_args() {
  local host="$1" addrs count
  [ -n "$host" ] || return 0
  addrs="$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u)"
  count="$(printf '%s\n' "$addrs" | grep -c .)"
  [ "$count" -ge 1 ] || die "configured release origin '$host' does not resolve on the host; cannot pin resolver reachability"
  [ "$count" -eq 1 ] || die "configured release origin '$host' resolves to $count addresses on the host; refusing ambiguous host resolution"
  printf -- '--add-host\n%s:%s\n' "$host" "$addrs"
}

# Resolve the configured origin's --add-host lines ONCE, on the main path, so its
# ambiguity/unresolvable refusal is fatal and visible (computing it inside a
# process substitution would exit only the subshell — the refusal must stop the
# script). Empty when no Forgejo origin is configured (GitHub-only, public DNS).
_resolver_addhost_lines() {
  local forge_host
  forge_host="$(_forge_origin_host "$(env_value BIPLANE_FORGEJO_URL || true)")"
  [ -n "$forge_host" ] || return 0
  # Pin ONLY an mDNS (.local) origin — the one reachability failure actually
  # demonstrated: Docker's bridge DNS does not carry host mDNS, so a .local forge
  # is container-unresolvable. Ordinary DNS names, INCLUDING multi-A hosts behind
  # a load balancer, resolve in the resolver container over bridge DNS (which also
  # preserves selection/failover), so they get NO pin and are never refused for
  # having several A records (Rowan RC 3730). Ambiguity is refused ONLY on the
  # .local pin path, in _resolver_add_host_args.
  case "$forge_host" in
    *.local) _resolver_add_host_args "$forge_host" ;;
    *) return 0 ;;
  esac
}

_resolver_run() {
  local addhost="$1" seed="$2"; shift 2
  local secret line
  secret="first-hop-ephemeral-$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
  local -a host_args=()
  if [ -n "$addhost" ]; then
    while IFS= read -r line; do [ -n "$line" ] && host_args+=("$line"); done <<<"$addhost"
  fi
  docker run --rm -i \
    --network "${BIPLANE_FIRST_HOP_RESOLVER_NETWORK:-bridge}" \
    "${host_args[@]+"${host_args[@]}"}" \
    --entrypoint python \
    -e "REDIS_URL=redis://127.0.0.1:1/0" \
    -e "DATABASE_URL=postgresql://disabled:disabled@127.0.0.1:1/disabled" \
    -e "SECRET_KEY=$secret" \
    -e "BIPLANE_FORGEJO_URL=$(env_value BIPLANE_FORGEJO_URL || true)" \
    -e "BIPLANE_FORGEJO_REPO=$(env_value BIPLANE_FORGEJO_REPO || true)" \
    -e "BIPLANE_FORGEJO_RELEASE_TOKEN=$(env_value BIPLANE_FORGEJO_RELEASE_TOKEN || true)" \
    -e "BIPLANE_GITHUB_REPO=$(env_value BIPLANE_GITHUB_REPO || true)" \
    -e "BIPLANE_UPDATE_ALLOWED_ORIGINS=$(env_value BIPLANE_UPDATE_ALLOWED_ORIGINS || true)" \
    "$seed" "$@"
}

ephemeral_resolve_metadata() {
  local seed="$1" tag="$2" addhost
  # Resolve the configured origin's host mapping ONCE, up front, so an ambiguity
  # or unresolvable-origin refusal is fatal and visible here rather than swallowed
  # by the command-start control's output redirection below.
  addhost="$(_resolver_addhost_lines)" || \
    die "could not pin the configured release origin for the resolver container"
  # Command-start control (Rowan blocker 2): prove the resolver actually
  # INITIALIZES in the seed bytes before trusting a resolve, so a boot failure is
  # named distinctly from a fetch failure and never misread as "no release".
  _resolver_run "$addhost" "$seed" manage.py help biplane_update_metadata >/dev/null 2>&1 || \
    die "seed backend image cannot initialize the metadata resolver (command did not start under the resolver-only bootstrap env)"
  _resolver_run "$addhost" "$seed" manage.py biplane_update_metadata "$tag"
}

# ---------------------------------------------------------------------------
# Section 4 — the legacy baseline is factual, not semantic
# ---------------------------------------------------------------------------
# Records only what the running deployment can prove. Per service: the configured
# image reference (which may legitimately be a mutable TAG, not a digest — it is
# recorded as-is, never promoted) AND every running container's immutable local
# image id, which is the rollback identity (section 4, acceptance #5/#6).
# Installed release version and build are recorded UNKNOWN — never inferred from
# an image tag's spelling.
legacy_service_snapshot() {
  local service="$1" container pair ref image_id result='[]' count=0
  while IFS= read -r container; do
    [ -n "$container" ] || continue
    pair="$(docker inspect --format '{{.Config.Image}} {{.Image}}' "$container")" || return 1
    read -r ref image_id <<<"$pair"
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    result="$(jq -c --arg ref "$ref" --arg id "$image_id" '. + [{ref:$ref, image_id:$id}]' <<<"$result")"
    count=$((count + 1))
  done < <(compose ps -q "$service")
  [ "$count" -gt 0 ] || return 1
  jq -cS 'sort_by(.ref, .image_id)' <<<"$result"
}

capture_legacy_snapshot() {
  local service images='{}' service_images db web_url
  for service in "${SERVICES[@]}"; do
    service_images="$(legacy_service_snapshot "$service")" || return 1
    images="$(jq -c --arg service "$service" --argjson values "$service_images" \
      '. + {($service): $values}' <<<"$images")"
  done
  web_url="$(env_value WEB_URL || true)"
  [ -n "$web_url" ] || return 1
  verify_database_wiring || return 1
  db="$(database_target)" || return 1
  # release/build are UNKNOWN on a legacy deployment and are recorded as such.
  jq -cn --arg release unknown --arg build unknown --argjson database "$db" \
    --argjson images "$images" \
    '{release:$release, build:$build, database:$database, images:$images}'
}

# Rollback proof by BYTES, not names (section 4, acceptance #6): every running
# container's local image id must match the id captured in the legacy snapshot.
# Restoring the old .env alone is not proof the old bytes are running.
verify_legacy_bytes() {
  local snapshot="$1" service current stored
  for service in "${SERVICES[@]}"; do
    current="$(legacy_service_snapshot "$service")" || return 1
    stored="$(jq -cS --arg service "$service" '.images[$service]' <<<"$snapshot")"
    [ "$current" = "$stored" ] || return 1
  done
}

# ---------------------------------------------------------------------------
# Section 6 — success is the managed state, or it is not success
# ---------------------------------------------------------------------------
adopted_as_managed() {
  # After activation the deployment MUST satisfy the full managed predicate, so
  # the next update reaches apply-update.sh with no first-hop path. Reuses the
  # same predicate the classification used.
  is_managed
}

main() {
  [ "$#" -eq 2 ] || usage
  local tag="$1" seed="$2"

  # Section 2: explicit selection; seed is a registry digest, tag is grammatical.
  release_version_valid "$tag" || die "tag is outside the accepted release version grammar"
  [[ "$seed" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || \
    die "seed backend reference must be an exact registry digest (image@sha256:...)"

  for command in docker jq curl awk flock sha256sum stat sync; do need "$command"; done
  assert_env_target
  cd "$SELFHOST_DIR"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another Biplane update is already running"

  # Section 1: mutual exclusion. First hop refuses on a managed deployment.
  if is_managed; then
    die "deployment is already managed; use apply-update.sh. First hop refuses managed state."
  fi

  # Section 3: resolve the target from the SEED bytes (least authority), then
  # validate the envelope exactly as steady-state apply does, and add the
  # seed-binding check. Pulling the seed digest is the ONE pre-backup mutation.
  docker pull "$seed" >/dev/null || die "could not pull the operator seed backend image by digest"
  local metadata source level commit selected backend web admin space build
  metadata="$(ephemeral_resolve_metadata "$seed" "$tag")" || die "seed-bytes metadata resolution failed"
  jq -e --arg tag "$tag" '
    type == "object" and keys == ["release", "source"] and
    (.source == "forgejo" or .source == "github") and
    .release.tag == $tag and
    (.release.level == "code" or .release.level == "data" or .release.level == "full") and
    (.release.commit_sha | test("^[0-9a-f]{40}$")) and
    (.release.images | type == "array" and length == 4)
  ' <<<"$metadata" >/dev/null || die "resolver returned an incomplete release identity"
  source="$(jq -r .source <<<"$metadata")"
  level="$(jq -r .release.level <<<"$metadata")"
  commit="$(jq -r .release.commit_sha <<<"$metadata")"
  selected="$(jq -r .release.tag <<<"$metadata")"
  [ "$selected" = "$tag" ] || die "release source returned $selected for requested $tag"
  [ "$level" != "full" ] || die "$tag is level full; apply it by hand — see deployments/selfhost/MANUAL-FULL-UPGRADE.md"

  backend="$(image_ref_for "$metadata" biplane-backend)"
  web="$(image_ref_for "$metadata" biplane-web)"
  admin="$(image_ref_for "$metadata" biplane-admin)"
  space="$(image_ref_for "$metadata" biplane-space)"

  # Seed binding (section 3, acceptance #3): the record's backend digest MUST be
  # the operator seed — refused BEFORE backup or any deployment mutation.
  [ "$backend" = "$seed" ] || \
    die "resolved backend digest ($backend) does not match the operator seed ($seed); refusing before backup"

  # The backend bytes' baked identity binds them to the record's tag and commit.
  build="$(inspect_release_image "$backend" "$tag" "$commit")"

  # Section 4: capture the FACTUAL legacy baseline (local image ids = rollback
  # identity; version/build unknown), then back up config + DB before any pull
  # beyond the seed or any mutation (section 5).
  local snapshot stamp backup_dir
  snapshot="$(capture_legacy_snapshot)" || \
    die "legacy baseline is unprovable: could not bind every running service to a local image id, capture config, or prove the database target/dump"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="$BACKUP_ROOT/${stamp}-firsthop-${tag}"
  mkdir -p "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  create_backup "$backup_dir" "$snapshot" "$metadata"

  # Section 5: target service pulls (AFTER backup), display-build binding, and
  # migration admission — identical to steady-state apply.
  for image in "$web" "$admin" "$space"; do docker pull "$image" >/dev/null; done
  inspect_display_build "$web" "$build" /usr/share/nginx/html
  inspect_display_build "$admin" "$build" /usr/share/nginx/html/admin

  local plan
  plan="$(migration_plan "$backend")" || die "new-image migration plan failed"
  if [ "$level" = "code" ] && ! grep -q 'No planned migration operations' <<<"$plan"; then
    die "release is level code but the new image reports pending migrations"
  fi

  local rendered_env migration_started=0
  rendered_env="$(mktemp "$SELFHOST_DIR/.env.biplane-render.XXXXXX")"
  trap 'rm -f "${rendered_env:-}"' EXIT
  write_pinned_env "$ENV_FILE" "$rendered_env" "$tag" "$backend" "$web" "$admin" "$space"
  verify_rendered_identity "$rendered_env" "$backend" "$web" "$admin" "$space" || \
    die "rendered Compose graph does not bind every executing service to the selected digest set"

  if [ "$level" = "data" ]; then
    migration_started=1
    stop_application || die "could not quiesce every mutation-serving service before migration"
    if ! run_migrations "$backend"; then
      printf 'Biplane first hop failed: migration did not complete.\n' >&2
      printf 'Prior config remains active at %s; database backup: %s/database.dump\n' "$ENV_FILE" "$backup_dir" >&2
      printf 'Operator action is required before restarting services; do not describe this as an automatic rollback.\n' >&2
      exit 1
    fi
  fi

  if ! atomic_replace "$ENV_FILE" "$rendered_env"; then
    printf 'Biplane first hop failed while committing image pins (outcome=%s); restoring the saved config.\n' \
      "$ATOMIC_REPLACE_OUTCOME" >&2
    if ! verify_backup "$backup_dir" || ! atomic_replace "$ENV_FILE" "$backup_dir/config.env"; then
      printf 'RECOVERY REQUIRED: pin commit failed and the saved config could not be restored. Inspect %s.\n' "$backup_dir" >&2
      exit 1
    fi
    if [ "$migration_started" -eq 1 ]; then
      printf 'RECOVERY REQUIRED: prior config restored but the data migration completed. Reconcile the database (%s/database.dump) before restarting services.\n' "$backup_dir" >&2
    fi
    exit 1
  fi

  # Activation + readback. On failure, restore prior config AND prove the legacy
  # BYTES are running again (local image ids), not merely the old .env.
  if recreate_application && verify_running_release "$tag" "$build" "$backend" "$web" "$admin" "$space"; then
    if adopted_as_managed; then
      rm -f "$rendered_env"; trap - EXIT
      printf 'Biplane first hop succeeded: %s (%s, %s) — deployment is now managed; use apply-update.sh henceforth.\n' \
        "$tag" "$level" "$source"
      printf 'Backup retained at %s\n' "$backup_dir"
      return 0
    fi
    printf 'RECOVERY REQUIRED: services activated on %s but the deployment does not satisfy the managed predicate; ordinary apply remains unauthorized. Inspect %s.\n' \
      "$tag" "$backup_dir" >&2
    return 1
  fi

  printf 'Biplane first hop failed after config commit; stopping services before restoring prior pins.\n' >&2
  if ! stop_application; then
    printf 'RECOVERY REQUIRED: failed activation could not be fully stopped; mutation-serving state is unproven. Inspect %s.\n' "$backup_dir" >&2
    exit 1
  fi
  if [ "$migration_started" -eq 0 ] && verify_backup "$backup_dir" \
     && atomic_replace "$ENV_FILE" "$backup_dir/config.env" \
     && recreate_application && verify_legacy_bytes "$snapshot"; then
    printf 'Prior legacy release restored and proven by local image id. Backup: %s\n' "$backup_dir" >&2
  else
    printf 'RECOVERY REQUIRED: a data migration was attempted or the legacy bytes could not be proven restored. Restore/reconcile %s/database.dump explicitly before starting the old release.\n' "$backup_dir" >&2
  fi
  return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
