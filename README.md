<p align="center">
  <img src="biplane-logo.svg" width="120" alt="Biplane logo — an ascending biplane" />
</p>

<h1 align="center">Biplane</h1>
<p align="center"><b>Two wings. More lift.</b></p>
<p align="center">Agent-automated project management — humans on one wing, agents on the other.</p>

---

Biplane is an independent community fork of [Plane](https://plane.so) that adds a **multi-agent layer**: AI agents file, review, and move work automatically — commits, pull requests, and merges that reference a work item drive its state via the git bridge, while a tamper-evident ledger separately records every signature-verified change event — and humans watch it happen live on the board, the **Wheel** (a real-time radial view of every ticket), and the **Traveler** (each work item's full lifecycle, like a medical record).

## Why fork, instead of bolting on?

- **Security & self-hosting** — everything runs on your own hardware, behind your own network; project data and agent activity never leave the building.
- **Your own identity provider** — generic OIDC single sign-on isn't part of Plane's Community Edition, and local-first deployment needs it; Biplane signs in against your own idP (we run a lightweight one, with Forgejo as a fallback), wired natively into the fork.
- **Forgejo-native git** — Plane's git integrations target GitHub/GitLab in its commercial tiers; self-hosted Forgejo isn't supported. Biplane's bridge speaks Forgejo webhooks directly.
- **Agents without seat licenses** — per-seat pricing makes an agent fleet absurd; self-hosted, agents are first-class actors with their own gated write-path.
- **Receipts for every action** — audit logs are enterprise-tier features elsewhere; the append-only, signature-verified ledger is built in.
- **Views woven in, not bolted on** — the kanban, list, and calendar boards, cycle and module views, the Wheel, and the Traveler all update live on screen as agents work, no page refresh — essential at agent speed, and only possible by owning the frontend.

## What Biplane adds

| Piece | What it does |
|---|---|
| **Forgejo/Git bridge** | Webhooks from your git host move tickets automatically: a commit that references the ticket → *In Progress*, PR opened → *In Review*, merge → *Done*. Monotonic (never moves a ticket backwards), retry-safe, serialized per issue. |
| **Audit Ledger** | Append-only, tamper-evident record of signature-verified webhook deliveries — each timeline entry carries a verified checkmark. |
| **Workflow Policy** | Per-work-item-type routing rules for what agents may do. |
| **Agent Write-Path** | The gated proxy agents use to write to the tracker — idempotent creates, fail-closed authorization. |
| **Wheel & Traveler** | Native live views: the whole project as a turning wheel; each item's history as a vertical timeline. |
| **Watch mode** | Boards and views refresh themselves only when something actually changed (change-signal gated — no polling flicker). |

## What's in this repository

**This repo contains the Biplane web application only** — the forked Plane frontend with the native agent-layer UI (the Wheel, the Traveler, watch-mode boards, branding). It is AGPL-3.0 and is the corresponding source for our hosted instances.

The agent services described above (git bridge, audit ledger, workflow policy, agent write-path) are **separate programs** that talk to Plane only over its API and webhooks. They are distributed and licensed separately and are **not part of this repository**.

### Quick start (web application)

```bash
git clone https://github.com/NetverseSocial/biplane
cd biplane
# build the static web app (Docker; no local toolchain needed)
docker build -f apps/web/Dockerfile.web -t biplane-web .
# the built site lives in the image at /usr/share/nginx/html —
# serve it in place of the stock web frontend of a Plane CE v1.3.x deployment
```

The frontend expects a Plane Community Edition v1.3.x backend (`/api`, `/auth`) behind the same origin, exactly like stock Plane. Agent features appear as they are wired to your instance; without them the app behaves like Plane with Biplane branding.

## Documentation

- **Getting started & architecture** — this README and [`docs/`](docs/) *(growing — ask in Discussions)*
- **What's new** — see [Releases](../../releases)
- **Forum** — see [Discussions](../../discussions)
- Plane's originals: [Plane docs](https://docs.plane.so) · [Plane changelog](https://plane.so/changelog) · [Plane forum](https://forum.plane.so)

## Relationship to Plane

Biplane is **built on, not competing with, [Plane](https://plane.so)** (Community Edition, AGPL-3.0). The web app in this repository is a modified Plane frontend and remains **AGPL-3.0** — this repo is the corresponding-source offer for our hosted instances. The agent-side services (bridge, ledger, policy, write-path) are separate programs that talk to Plane only over its public API and webhooks.

Biplane is an independent community fork and is **not affiliated with or endorsed by Plane**.

Thank you, [Plane team](https://github.com/makeplane/plane). 🙏

## License

- This repository (the web application, derived from Plane): **AGPL-3.0** (see [LICENSE](LICENSE))
- Biplane's agent services are separate programs, distributed and licensed separately; they are not part of this repository.
