# Using Biplane

The root [README](../README.md) says what Biplane is. The
[self-hosting guide](../deployments/selfhost/README.md) says how to run it. This says how to
**use** it once it is running.

Written for two audiences at once: a team member who wants to know what a state means, and
someone self-hosting from the public repo who wants to know what the git bridge will do to
their board before they turn it on.

---

## Work items and their states

A **work item** (elsewhere called an issue or a ticket) moves through states. Each state
belongs to a **group**, and both the group and the name matter — but they do different jobs:

- **The group decides what a state means** — whether work in it counts as not started, in
  progress, finished or cancelled. Views, filters and reporting read the group, so a state
  named "Done" sitting outside the `completed` group will not count as finished however it
  is spelled.
- **The name is what people read.** Nothing in the product picks a state for you by its name.

**Renaming is safe as far as the git bridge is concerned**, because since v1.1.0 the bridge
selects no state at all — see [which state you land
on](#which-state-you-land-on-none-the-bridge-does-not-move-tickets). Earlier versions preferred
particular names (Review, Done) when choosing a target, so renaming could change where a push or
merge landed; **that target resolution is deleted**, and no state name or group affects the
bridge any more.

Rename deliberately for your team's sake — states are how everyone reads the board — but not
out of fear of the bridge.

There are six groups: `backlog`, `unstarted`, `started`, `completed`, `cancelled`,
and `triage` (`StateGroup` in `db/models/state.py`).

The built-in **Biplane** template (shipped by migrations 0123/0124, in exactly this
order) looks like this:

| State | Group | Means |
|---|---|---|
| Backlog | `backlog` | Captured, not committed to. No one is expected to pick it up. |
| Todo | `unstarted` | Agreed and queued. Nobody has begun. |
| Design | `started` | Shape being decided before code. |
| Code & TDD | `started` | Being built, with its tests. |
| Review | `started` | Written, waiting on another pair of eyes. |
| Integration Test | `started` | Reviewed, being exercised against the rest of the system. |
| Done | `completed` | Merged and finished. |
| Deploy | `completed` | **Running on a server.** |
| Cancelled | `cancelled` | Deliberately not doing it. |
| Triage | `triage` | Landed from intake; not yet sorted into the flow. |

### Done and Deploy are not the same claim

This is the distinction people get wrong, so it is worth stating twice.

**Done means the code is merged.** **Deploy means it is running somewhere a user can reach.**
A fix sitting on `main` is Done. It is not Deploy until it has actually shipped, and the two
can be days apart.

Note the ordering: **Done comes before Deploy** in the workflow, and both are in the
`completed` group. That surprises people who expect Deploy to be a synonym for "extra done" —
it is a later state, not a higher one, and an item can legitimately sit at Done for a long
time.

Other templates differ. Stock Plane's Default template has no Review state at all. Nothing
below assumes yours matches ours.

---

## The git bridge — how commits name tickets

With the bridge enabled, referencing a work item in a commit message tells the bridge which
ticket the event was about. **It does not move the ticket** — see [which state you land
on](#which-state-you-land-on-none-the-bridge-does-not-move-tickets) below. You still move
the board yourself; what the reference buys you is that the event is recognised and its
outcome durably recorded — and, **if the deployment sets `FORGEJO_BRIDGE_WRITE_TOKEN`**, that
the bridge answers on the pull request when something is wrong instead of the work going
unremarked. Without that token it records the same refusal and says nothing.

### Reference it, or the bridge never even sees it

Recognition happens only behind a **reference keyword**:

```
refs BIP-12
closes APOLLO-3
fixes GB-1
resolves BIP-7
```

Accepted keywords are `ref`, `refs`, `close`, `closes`, `fix`, `fixes`, `resolve`, `resolves`
— followed by a colon or whitespace, then the id.

Those eight are the whole list. **Past tense is not accepted** — `closed`, `fixed` and
`resolved` are not keywords, because they narrate what the commit did rather than say what
the event is about.

**A bare id is deliberately ignored.** Writing `BIP-12` on its own does nothing. This is not
an oversight: an id on its own is indistinguishable from ordinary technical vocabulary, and a
bridge that acted on any `WORD-123` would fire on `SHA-256`, `UTF-8`, and `AES-256`.
Requiring a keyword makes the intent explicit.

**The id must be uppercase.** `refs BIP-12` matches; `refs bip-12` does not. The keyword is
case-insensitive, the id is not.

### Which state you land on: none. The bridge does not move tickets.

Since the write-authority ruling (v1.1.0), **no event moves a ticket.** The bridge recognises
the directive and refuses the board write with a recorded reason. You move the ticket; the
directive only says which ticket the event was about.

Whether anyone is *told* is a separate question with a separate answer: the bridge comments on
the pull request only where one exists **and** `FORGEJO_BRIDGE_WRITE_TOKEN` is configured.
Where it is not — which includes the current Pi deployment — the refusal is durable on the
delivery result and no comment appears. **Do not read silence as approval:** check the
delivery result, not the absence of a complaint.

So there is **no target state to configure, and no state-name trap to avoid.** Everything this
section used to tell you to guard against is gone with the write path:

- The per-workflow target resolution — review-ish started states for pushes, done-ish
  completed states for merges — is **deleted**.
- The warning about **Deploy** being selected on workflows without a preferred completed-state
  name no longer applies: nothing selects a completed state at all. (Renaming states is still
  worth doing deliberately, for the reasons in [Work items and their
  states](#work-items-and-their-states) above — just not because of the bridge.)
- The bridge no longer refuses-and-logs when a project has no `started` or `completed` state,
  because it never looks for one.

All of it returns with the first authorised write, and will be specified then — together with
the caller that performs it, rather than in advance of it.

### Which tickets a repository may name

A repository is mapped to the **projects whose work items a directive in it may name**, via
`FORGEJO_BRIDGE_REPO_MAP` — a JSON object keyed `"<provider instance id>:<stable repository
id>"` (your configured `*_INSTANCE_ID` value, then the numeric id your forge shows in its
API/UI) to a non-empty **list of stable project UUIDs** (shown in Plane's project settings,
or via `/api/v1/workspaces/<slug>/projects/`). With `FORGEJO_INSTANCE_ID=pi5-forgejo` and
`GITLAB_INSTANCE_ID=gitlab`:

```json
{ "pi5-forgejo:42": ["9c1f4e2a-…"], "gitlab:1337": ["4a2b17c0-…", "77de91b5-…"] }
```

The prefix is the **instance id, not the forge family name** — the same notion of provider
identity the bridge's event keys use. Repo ids are per-instance sequences, so the same
number on two same-family instances is likely; instance-keying keeps them isolated. Read
the prefix from your live environment rather than assuming `forgejo`: a key under the wrong
prefix grants nothing and fails **silently-inert** (every delivery 200s, nothing recognised) —
the bridge logs a loud wrong-prefix warning when a mapped repo id sits under another
prefix, which is the signal to fix the key.

A directive that names a ticket in any project **outside** the repo's list is **rejected**:
the ref is never even looked up, and the rejection is durably recorded in the
delivery result (`ignored.cross_project`: ticket, repo, reason) — not just in process logs.
In-scope refs on the same delivery still proceed. This is the BIP-38 scope guard
(`docs/scope-a-architecture.md` §M2): before it, the map granted a whole **workspace**, so a
commit saying `refs SB-3` in one repo really moved a ticket in another team's project. A
workspace-slug value is therefore the **retired** schema and is refused as a configuration
defect naming the migration — list the projects explicitly.

The stable id is the tenancy boundary because a display path is **mutable**: a rename
followed by path reuse would hand the repo's scope to a different repository, and the same
`org/repo` spelling can exist on several forges. The immutable id survives renames and
cannot be reused; the provider prefix denotes that provider's single configured credential.
A bare `"org/repo"` path key is a **legacy migration convenience, Forgejo only** — it still
works, but the bridge logs a loud warning on every use telling you which id-keyed entry to
migrate to, and it never grants any other provider.

A repo that is not in the map is entirely inert. That is a normal, silent no-op — it is how you
run the bridge for some repositories and not others.

Leaving the map **unset entirely** while the bridge secret is configured is treated as a
configuration mistake and logged loudly, on the reasoning that an operator who set up a secret
clearly meant the bridge to be on. An explicit `{}` is accepted as a deliberate "map nothing".

The mapping is what stops a push in one repository from reaching another team's tickets —
and since BIP-38 that boundary is the **project**, not the workspace.

---

## Projects and workspaces

A **workspace** is an organisation; a **project** lives inside one and owns its own work
items, states, and labels. Work item ids are per project — `BIP-12` is the twelfth item in the
project whose identifier is `BIP`.

States are per project too, which is why "what does Review mean" is answerable only for a
given project.

---

## The Wheel and the Traveler

Two views that are ours, not upstream Plane. Both update live.

- **The Wheel** — the whole project as a turning radial view, every work item at once.
  Built for watching activity happen rather than for editing.
- **The Traveler** — one work item's ledger-recorded webhook deliveries as a vertical
  timeline: each delivery the audit ledger recorded, in order, marked with whether its
  signature verified at ingest (✓/⚠).

Recorded deliveries, not a complete lifecycle: a change only appears here if its webhook
delivery reached the ledger. Board changes made by a person — which, since the
write-authority ruling, is **all** of them — are not webhook deliveries and never appear in
the Traveler. What you see there is what the forge told us, not what happened to the ticket.

---

## What is not supported yet

**A private-only instance with no ingress and no polling cannot receive deliveries.**
That is the actual unsupported case. A cloud forge *can* reach a self-hosted
instance given public ingress, a tunnel, a relay, or a pull-based transport —
saying otherwise overstates the limit.

Hosted forges deliver webhooks from the public internet, so an instance with no
route in from there — no ingress, no tunnel, no relay, and nothing polling on
your side — will never receive one. The bridge does not currently poll, so it
expects a forge that can reach it.

Other forges' webhook formats (BIP-15) are recognised: Forgejo/Gitea, GitHub and GitLab
each have a personality with its **own credential**, and GitLab — whose token does not
cover the request body — additionally requires the explicit
`BRIDGE_ALLOW_UNSIGNED_BODY_FORGES` opt-in. The real limits are narrower than "format":
a push the forge itself truncated (GitLab beyond its webhook limit, GitHub at its 2048-
commit cap) can only be range-resolved against the **Forgejo API**, so on other forges it
defers and stays pending — loudly — rather than being processed partially. None of that
changes network reachability: a cloud host still cannot deliver to a private instance
with no route in.

---

## Where these facts come from

Everything above is traced to source rather than remembered, because a usage guide that
drifts is worse than no guide:

- states and groups — `apps/api/plane/db/models/state.py`, and this project's live workflow
- keyword grammar — `KEYWORD_CLASS` and `_DIRECTIVE_RE`, `apps/api/plane/bridge/grammar.py`
- repository mapping — `_project_scope()`, `apps/api/plane/bridge/forgejo_bridge.py`
- the refusals and their reason codes — `apps/api/plane/bridge/write_boundary.py`
- the Wheel and the Traveler — the root README

If you change any of those, change this page in the same commit.
