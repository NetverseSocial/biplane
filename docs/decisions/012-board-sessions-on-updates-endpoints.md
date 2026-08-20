# 012 — Board sessions are accepted on the updates endpoints

Date: 2026-08-17 · Status: accepted · Owners: Aria (change), John (directive)

## Context

Upstream Plane authenticates every API path containing "instances" against a
separate admin-session cookie: minted only by the admin site's own login,
expiring after one hour (`ADMIN_SESSION_COOKIE_AGE = 3600`), invisible to the
board's session. So an instance admin browsing the board was anonymous on
`/api/instances/updates/*`: Settings → Updates 401'd, bounced to
`/?next_path=…`, and the operator was asked for a second login that expires
hourly. This broke the first one-click update demo twice in one afternoon
(the gray page), and John's directive followed: the update must work from the
board's menu and Settings with no second login.

## Decision

The six updates endpoints (check status, apply, apply status, auto setting,
source setting, changelog) additionally accept the regular board session: a
second DRF authentication class resolves the user from the board session
cookie via the same session store and auth-hash validation the board itself
uses. `InstanceAdminPermission` is unchanged — authentication says WHO,
permission still says WHAT, and a non-admin board session is recognized and
refused (403). The admin-site session keeps precedence when present. All
other instance-admin endpoints keep the upstream posture.

## Tradeoff accepted

`SESSION_COOKIE_AGE` is 604800 (7 days); the admin cookie was 3600. The
window in which one captured cookie can trigger an apply or enable
auto-apply therefore grows 168× (Sable, review 3957). Accepted because:

- the apply endpoint can only start the CHECK's flagged tag from the
  instance's own trusted update source, digest-pinned end to end — a stolen
  cookie cannot choose what gets installed;
- the role gate (`InstanceAdmin`, role ≥ 15) is unchanged;
- the deployment posture is a closed LAN behind WireGuard (the project's
  standing threat model, Active Conventions);
- the alternative — an hourly second login inside a one-click flow — is the
  defect this decision removes.

## Alternatives rejected

- Extending `ADMIN_SESSION_COOKIE_AGE`: weakens the entire admin surface to
  fix six endpoints.
- Client-side rewiring (v1.2.2's attempt): treated the symptom; every fresh
  page load re-hit the middleware.
- CSRF-hardening as part of this change: CSRF is globally disabled for these
  REST APIs upstream (`BaseSessionAuthentication.enforce_csrf` is a no-op);
  re-enabling it is a separate, wider decision.
