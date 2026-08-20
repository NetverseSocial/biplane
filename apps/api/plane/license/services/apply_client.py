# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The ONE place an apply request is decided and sent (ticket 69).

Two callers — the admin's Update button (views/apply_update.py) and the
automatic mode (bgtasks/update_check_task.py). A rule implemented in a view
gets re-implemented by its second consumer; this module exists so the auto
path cannot drift from the button's refusals.

The decision, identical for both callers:
  - only the update check's flagged tag is ever sent — no caller supplies one;
  - nothing flagged, or a full-level flag: refused (manual path);
  - applier unconfigured: refused, stated plainly;
  - the applier's own verdict passes through verbatim.
"""

import logging

import requests as http_requests
from django.conf import settings

from plane.license.services.update_check import check_status_payload

logger = logging.getLogger("plane.license")

_TIMEOUT_SECONDS = 10

# Refusal kinds — the callers branch on `kind`, never on prose.
NOT_CONFIGURED = "not_configured"
NOT_FLAGGED = "not_flagged"
FULL_LEVEL = "full_level"
UNREACHABLE = "unreachable"
BAD_UPSTREAM = "bad_upstream"
REQUESTED = "requested"


def _applier():
    url = getattr(settings, "BIPLANE_APPLY_SERVICE_URL", None)
    token = getattr(settings, "BIPLANE_APPLY_SERVICE_TOKEN", None)
    if not url or not token:
        return None
    return url.rstrip("/"), token


def is_configured() -> bool:
    """True when an applier is configured. The auto path checks this BEFORE
    writing its once-per-tag guard — an operator who enables auto before
    configuring the applier must not silently lose that release forever
    (Sable RC 3826 #3). Deliberately NOT extended to UNREACHABLE: a timeout
    may mean the applier is applying right now, and clearing the guard there
    reintroduces the hourly loop."""
    return _applier() is not None


def request_apply_of_flagged(*, status_payload=None) -> dict:
    """Ask the host applier to apply the currently flagged update.

    Returns {"kind": ..., "detail": ...} and, for REQUESTED/refused-upstream,
    carries the applier's own {"status_code", "body"} verbatim. Never raises
    for operational problems — the auto caller runs inside the beat task,
    where an exception is an invisible degrade.
    """
    applier = _applier()
    if applier is None:
        return {"kind": NOT_CONFIGURED, "detail": "the apply service is not configured on this deployment"}

    payload = status_payload if status_payload is not None else check_status_payload()
    latest = (payload or {}).get("latest_release") or {}
    flagged_tag = latest.get("tag")
    if (payload or {}).get("state") != "update_available" or not flagged_tag:
        return {"kind": NOT_FLAGGED, "detail": "no update is currently flagged; the check's state is the authority"}
    if latest.get("level") == "full":
        return {"kind": FULL_LEVEL, "detail": "a full-level release requires the manual upgrade path"}

    url, token = applier
    try:
        upstream = http_requests.post(
            f"{url}/apply",
            json={"tag": flagged_tag},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — the contract is NEVER raise: a
        # urllib3 LocationParseError from a typoed env var is not a
        # RequestException and escaped here, failing the hourly beat task
        # forever (Vex RC 3828 #1, executed with a >63-char hostname label).
        return {"kind": UNREACHABLE, "detail": f"the apply service could not be reached: {e.__class__.__name__}"}
    try:
        body = upstream.json()
    except ValueError:
        return {"kind": BAD_UPSTREAM, "detail": "the apply service answered with a non-JSON body"}
    return {"kind": REQUESTED, "tag": flagged_tag, "status_code": upstream.status_code, "body": body}


def request_status() -> dict:
    """The applier's run status, same verdict shape, same never-raise
    contract — the status view re-implemented this transport with its own
    hardcoded timeout in the same commit that created this module (Vex
    RC 3828: the exact drift the module exists to prevent)."""
    applier = _applier()
    if applier is None:
        return {"kind": NOT_CONFIGURED, "detail": "the apply service is not configured on this deployment"}
    url, token = applier
    try:
        upstream = http_requests.get(
            f"{url}/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — same contract as above
        return {"kind": UNREACHABLE, "detail": f"the apply service could not be reached: {e.__class__.__name__}"}
    try:
        body = upstream.json()
    except ValueError:
        return {"kind": BAD_UPSTREAM, "detail": "the apply service answered with a non-JSON body"}
    return {"kind": REQUESTED, "status_code": upstream.status_code, "body": body}
