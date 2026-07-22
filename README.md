<p align="center">
  <img src="biplane-logo.svg" width="120" alt="Biplane logo — an ascending biplane" />
</p>

<h1 align="center">Biplane</h1>
<p align="center"><b>Two wings. More lift.</b></p>
<p align="center">Agent-automated project management — humans on one wing, agents on the other.</p>

---

Biplane is a community fork of [Plane](https://plane.so) that adds a **multi-agent layer**: AI agents file, review, and move work automatically — commits, pull requests, and merges drive ticket state through a signed audit ledger — while humans watch it happen live on the board, the **Wheel** (a real-time radial view of every ticket), and the **Traveler** (each work item's full lifecycle, like a medical record).

## What Biplane adds

| Piece | What it does |
|---|---|
| **Forgejo/Git bridge** | Webhooks from your git host move tickets automatically: first commit → *In Progress*, PR opened → *In Review*, merge → *Done*. Monotonic (never moves a ticket backwards), retry-safe, serialized per issue. |
| **Audit Ledger** | Append-only, HMAC-verified record of every change — each timeline entry carries a signature-verified checkmark. |
| **Workflow Policy** | Per-work-item-type routing rules for what agents may do. |
| **Agent Write-Path** | The gated proxy agents use to write to the tracker — idempotent creates, fail-closed authorization. |
| **Wheel & Traveler** | Native live views: the whole project as a turning wheel; each item's history as a vertical timeline. |
| **Watch mode** | Boards and views refresh themselves only when something actually changed (change-signal gated — no polling flicker). |

## Documentation

- **Getting started & architecture** — this README and [`docs/`](docs/) *(growing — ask in Discussions)*
- **What's new** — see [Releases](../../releases)
- **Forum** — see [Discussions](../../discussions)
- Plane's originals: [Plane docs](https://docs.plane.so) · [Plane changelog](https://plane.so/changelog) · [Plane forum](https://forum.plane.so)

## Relationship to Plane

Biplane is **built on, not competing with, [Plane](https://plane.so)** (Community Edition, AGPL-3.0). The web app in this repository is a modified Plane frontend and remains **AGPL-3.0** — this repo is the corresponding-source offer for our hosted instances. The agent-side services (bridge, ledger, policy, write-path) are separate programs that talk to Plane only over its public API and webhooks.

Thank you, [Plane team](https://github.com/makeplane/plane). 🙏

## License

- `apps/web` and everything derived from Plane: **AGPL-3.0** (see [LICENSE](LICENSE))
- Biplane's separate agent services live in their own repositories with their own licenses.
