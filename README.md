<p align="center">
  <img src="biplane-logo.svg" width="120" alt="Biplane logo — an ascending biplane" />
</p>

<h1 align="center">Biplane</h1>
<p align="center"><b>Two wings. More lift.</b></p>
<p align="center">A Plane Community Edition fork for a dev team with its own agent fleet.</p>

---

[Plane](https://plane.so) is an excellent open-source project tracker — boards, sprints, work items — built for human teams. **Biplane** is an independent community fork of it, made by a dev team where AI agents do much of the building. It adds a **multi-agent layer**: commits, pull requests, and merges that reference a work item drive its state via the git bridge, a tamper-evident ledger separately records signature-verified webhook deliveries, and humans watch it happen live on the board, the **Wheel** (a real-time radial view of every ticket), and the **Traveler** (each work item's recorded lifecycle, like a medical record).

We're not competing with Plane — we're using a good app a bit differently, and sharing it in case your team needs the same.

## Why fork, instead of bolting on?

We didn't set out to build a project-management product. We needed a tracker that fits how our agents work: everything on our own hardware, sign-in against our own identity provider, tickets that follow the git activity in our Forgejo instance, and a tamper-evident record of the tracker changes that work produced. Plane's Community Edition was the best open-source base we found — genuinely excellent — but those needs meant changing the application itself, not bolting things on. So we forked it, and we're publishing the result under the same AGPL license.

- **Security & self-hosting** — everything can run on your own hardware, behind your own network; project and audit data stay on infrastructure you control, with outbound services limited to the providers you choose.
- **Your own identity provider** — generic OIDC single sign-on isn't part of Plane's Community Edition, and local-first deployment needs it; Biplane signs in against your own idP (we run a lightweight one, with Forgejo as a fallback), wired natively into the fork.
- **Forgejo-native git** — Plane's documented git integrations (GitHub, GitLab, Bitbucket) don't currently include Forgejo. Biplane's bridge speaks Forgejo webhooks directly.
- **Bring your own agent fleet** — Biplane's automation is driven by your own agents and your git events (any stack that can commit code or call an API); Biplane adds no marketplace requirement and no usage meter of its own — the whole layer is yours to run.
- **Receipts you can verify** — signature-verified webhook deliveries land in an append-only, tamper-evident ledger you host yourself.
- **Views woven in, not bolted on** — the kanban, list, and calendar boards, cycle and module views, the Wheel, and the Traveler all update live on screen as agents work, no page refresh — useful when updates are frequent, and made practical by owning the frontend.

Biplane supports a larger project of ours — we think AI agents can do more for people than today's tools show, and we'd rather demonstrate that than talk about it. More when it's ready.

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

This repository contains the Biplane fork of Plane Community Edition, including its upstream application components; Biplane-specific product changes are currently concentrated in the web frontend (the Wheel, the Traveler, watch-mode boards, branding). It is AGPL-3.0 and contains the corresponding source for our hosted web application.

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

Biplane is **built on, not competing with, [Plane](https://plane.so)** (Community Edition, AGPL-3.0). The web app in this repository is a modified Plane frontend and remains **AGPL-3.0** — this repository contains the corresponding source for our hosted web application. The agent-side services (bridge, ledger, policy, write-path) are separate programs that talk to Plane only over its public API and webhooks.

Biplane is an independent community fork and is **not affiliated with or endorsed by Plane**.

Plane is a product of Plane Software, Inc. Thank you, [Plane team](https://github.com/makeplane/plane). 🙏

## License

- This repository (the web application, derived from Plane): **AGPL-3.0** (see [LICENSE.txt](LICENSE.txt))
- Biplane's agent services are separate programs, distributed and licensed separately; they are not part of this repository.
