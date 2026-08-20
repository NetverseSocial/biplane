# The narrow privileged applier

The service behind the Update button. It lets the board's API ask the deploy
host to run `apply-update.sh` — and nothing else. Scope A reserved exactly
this seam: *"The server may later invoke this through a narrow privileged
service, but the command is the implementation."*

## What it refuses, by construction

- any request without the bearer token (401);
- any tag `validate-tag.sh` refuses — one grammar authority (422);
- a second apply while one runs (409);
- `full`-level releases — refused by `apply-update.sh` itself, which this
  service adds nothing to. Full releases take `MANUAL-FULL-UPGRADE.md`.


## The operator operations

Three further fixed operations exist so that a release does not need a human at
a Docker-capable shell. They are the same shape as `/apply`: a fixed argv array,
per-argument allowlists, no shell passthrough, and an audit entry for both
intent and outcome.

| operation | what it does |
|---|---|
| `POST /op/push-images` | `docker tag` + `docker push` the built images, returning each digest **read back from the registry** rather than parsed from push output |
| `POST /op/trigger-update-check` | run the (otherwise hourly) update check now |
| `GET /op/board-status` | what is RUNNING versus what is PINNED — the disagreement that makes `apply-update.sh` refuse, readable *before* an apply rather than after a failed one |

`/op/push-images` takes `{tag, images: [{kind, build}]}` and pushes
`biplane-<kind>:pi5-<build>`.

**The images must already exist in *this host's* Docker daemon.** The operation
does not build and does not fetch: it tags what is here and pushes it. Getting
the images here is the ship step — `docker save … | ssh <this host> docker load`
— and that constraint is the whole reason the ship step exists. Pushing from the
machine that built them instead is the obvious-looking alternative and it fails,
because this registry is HTTP-only and Docker only treats `localhost` as
insecure by default. See "Which host does what" in
`deployments/release/README.md`.

`/op/board-status` is deliberately projecting. `docker inspect` would return
`Config.Env` — every secret in every container — so only three named fields ever
leave this process, and the `.env` read is key-whitelisted to the image pins.

## Install (on the deploy host, as the deploy user)

```sh
# 1. token — one line, owner-only
install -d -m 700 ~/.config/biplane
head -c 32 /dev/urandom | base64 | tr -d '/+=' > ~/.config/biplane/apply-service.token
chmod 600 ~/.config/biplane/apply-service.token

# 2. environment (systemd unit or shell profile)
export APPLY_SERVICE_REPO=$HOME/biplane-prod/repo          # a checkout that CARRIES apply-service
                                                           # (>= the release that first shipped it;
                                                           # a deployment whose installed tag predates
                                                           # it points here at a later tag or main),
                                                           # advanced to the release tag at deploy time
export APPLY_SERVICE_COMPOSE_DIR=$HOME/biplane-prod        # where the .env and compose live
export APPLY_SERVICE_BIND=0.0.0.0                          # only if the api container must
                                                           # reach it via host-gateway; the
                                                           # token remains the gate
python3 deployments/selfhost/apply-service/apply-service.py
```

Systemd example (`~/.config/systemd/user/biplane-apply.service`):

```ini
[Unit]
Description=Biplane apply service

[Service]
Environment=APPLY_SERVICE_REPO=%h/biplane-prod/repo
Environment=APPLY_SERVICE_COMPOSE_DIR=%h/biplane-prod
Environment=APPLY_SERVICE_BIND=0.0.0.0
ExecStart=/usr/bin/python3 %h/biplane-prod/repo/deployments/selfhost/apply-service/apply-service.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user enable --now biplane-apply` (and `loginctl
enable-linger <user>` so it survives logout).

## Wire the server to it

In the board's `.env` (see `env.example`):

```
BIPLANE_APPLY_SERVICE_URL=http://<host-as-seen-from-the-api-container>:7671
BIPLANE_APPLY_SERVICE_TOKEN=<the token file's content>
```

## Firewall — a host process, not a published port

The api container reaches this service at the host's `host-gateway` address (the
`extra_hosts` mapping in the compose). Docker-**published** container ports are
DNAT-forwarded and bypass the host's INPUT filter — but this applier is a **host
process**, so its port is subject to the host firewall like any other. On a host
running a default-deny firewall (e.g. UFW), the docker-bridge → host path is
silently dropped and the api's calls **time out**: the button reads dark while
every other signal (version served, banner current, applier active) looks correct.
Allow the docker bridge to the applier port:

```sh
# UFW. Substitute your compose network's subnet (docker network inspect
# <project>_default) or use the docker range 172.16.0.0/12 for durability across
# a network recreate. 7671 is APPLY_SERVICE_PORT's default — substitute if you
# changed it. The bearer token remains the only capability gate; this rule
# restores REACHABILITY, it does not widen trust.
sudo ufw allow from 172.16.0.0/12 to any port 7671 proto tcp \
  comment 'biplane apply-service (docker bridge -> host applier)'
```

Confirm from inside the api container: a `GET /status` with the bearer token
should return 200. On a host with a public interface, keep the rule scoped to the
docker bridge and never expose the port to the public side.

## The trust boundary, stated plainly

The token holder can make the deploy host run `apply-update.sh <tag>` for any
tag that parses — which pulls images by digest, migrates, and recreates
services. That is the whole point, and why the token lives in two places
only: the applier's token file and the board's server env. It is never
shown to a browser; the UI talks to the board's API, which holds the token
server-side and forwards only the update check's flagged tag.

And say the exposure out loud: with `APPLY_SERVICE_BIND=0.0.0.0` — which the
api-container-reaches-host topology usually requires — **the token is the
only thing between whoever can reach the port and that capability.** On a
LAN-only host that is the LAN. On any host with a public interface, firewall
the port to the docker bridge; do not rely on obscurity or on the token
alone. The default bind stays loopback so that exposure is always a choice
an operator made, never a surprise.

## Test harness

`apply-service.test.sh` — sandbox repo with recording stubs; refusals are
proven to never reach the wrapped command by the stubs' own call records.
