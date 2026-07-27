# Biplane self-host

Run the Biplane **web/CE stack** (13 containers: web, space, admin, live, api, worker,
beat-worker, migrator + postgres, valkey, rabbitmq, minio, proxy) with Docker Compose.

Scope, honestly: `web`, `space`, `admin`, and the backend (`api`/`worker`/`beat-worker`/
`migrator`) run **forked Biplane images**. `live` and `proxy` intentionally run upstream
Plane images (unforked). The separately distributed agent services (bridge, ledger,
write-path, idP-lite) are **not** part of this compose.

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

## Install

**1. Build (or pull) the forked images** — the override has no `build:` stanza, so the
images must exist before `up`. Build on a machine matching your deploy architecture:

```bash
docker build -f apps/web/Dockerfile.web     -t biplane-web:latest .
docker build -f apps/space/Dockerfile.space -t biplane-space:latest .
docker build -f apps/admin/Dockerfile.admin -t biplane-admin:latest .
docker build -f apps/api/Dockerfile.api     -t biplane-backend:latest apps/api
```

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

## Notes

- All state lives in named Docker volumes (`pgdata`, `uploads`, …). Back them up —
  or bind-mount them onto storage your existing backup tooling covers.
- The compose exposes nothing but the proxy port; everything else stays on the
  internal network.
- Telemetry: Biplane images send nothing anywhere unless you explicitly set an
  `OTLP_ENDPOINT` for the api/worker services.
