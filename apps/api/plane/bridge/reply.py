# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Tell the person who acted, where they acted (John's ruling, 2026-08-15).

The bridge records why it declined to move a ticket — near misses, refs outside
the repository's scope, a delivery from an unmapped repository — in the durable
delivery result. Until now that was the end of it: the reason existed in a
database row and reached nobody.

John's question was the design: *"Why is the bot alerting an agent of a
condition when they're right there doing the action?"* When an agent merges a
pull request and the bridge cannot act on it, that agent is PRESENT. The
refusal belongs in the thing they just did, not in a notification they may open
tomorrow. A stored alert is for the other case — a ticket sitting unmoved with
nobody around.

**This is the bridge's ONLY outbound WRITE, to a forge or anywhere.** Every
other call under this package is a `get` — the pull object and the compare range
— and posts nothing. (It once also read the review record; that lookup is
deleted, since the ask reads the signed event body instead.) There is no board
write at all in this release, so this comment is the entire write surface:

- **It cannot change a delivery's outcome.** Every failure path returns quietly.
  The decision was already made and durably recorded as a refusal — no board
  write happened or could have — and telling someone about it afterwards is
  best-effort by construction. A comment that fails must never turn a processed
  delivery into a retry, because the retry would re-run the decision to produce
  a message.
- **It is idempotent against redelivery.** Forge redeliveries and lease retries
  re-run processing, and a bridge that comments every time is a bridge people
  mute. Each comment carries a marker naming the delivery, and the existing
  comments are read first.
- **It says what was NOT done and what would fix it.** A refusal that only says
  "ignored" tells the reader to go and read code.
"""

import logging

import requests as http_requests
from django.conf import settings

logger = logging.getLogger("plane.worker")

TIMEOUT_SECONDS = 10

#: Marker carried by every comment this module writes. Keyed on the delivery so
#: a redelivery of the SAME event is recognised, while a genuinely new event
#: about the same pull request still speaks.
_MARKER = "<!-- biplane-bridge: delivery {delivery_id} -->"


#: THE ONE FORGE THIS SLICE MAY ANSWER, and it is a single member on purpose.
#:
#: An earlier revision listed `gitea` too, on the reasoning that it speaks the
#: same comment API. That reintroduced the very defect the provider check
#: exists to close, one step narrower (Morrow RC 3767): there is exactly ONE
#: configured destination, `FORGEJO_BASE_URL`, so a Gitea delivery would have
#: posted its refusal to the FORGEJO instance — same wrong-forge comment, same
#: stranger's pull request, just a shorter list of ways to get there.
#:
#: A forge may be added here only together with a destination of its own.
#: Speaking the same API is not the same as knowing where to speak.
_SPEAKABLE_FORGES = ("forgejo",)


def _creds():
    """The credential for WRITING, which is deliberately not the one for reading.

    `FORGEJO_BRIDGE_API_TOKEN` is the read credential — it authorises the pull
    lookup and the compare range, and NOT a review lookup, which is deleted —
    and it is issued read-only on purpose (Rowan RC 3765:
    a POST with the shipped farm credential returns 403, and on the live board
    the variable is present but EMPTY). Falling back to it would produce a 403
    on every refusal, which is log noise that looks like a broken bridge rather
    than an unconfigured feature.

    So replying requires its own explicitly-granted write credential. Absent it,
    the bridge stays silent rather than failing loudly on every delivery.
    """
    base = getattr(settings, "FORGEJO_BASE_URL", None)
    token = getattr(settings, "FORGEJO_BRIDGE_WRITE_TOKEN", None)
    return (base.rstrip("/"), token) if base and token else (None, None)


def _already_said(base, token, repo, number, marker):
    """Have we already commented for this delivery?

    A read failure returns True — DECLINING TO SPEAK IS THE SAFE DIRECTION.
    If we cannot tell whether we already commented, commenting again is the
    outcome that trains people to ignore the bridge.
    """
    try:
        response = http_requests.get(
            f"{base}/api/v1/repos/{repo}/issues/{number}/comments",
            headers={"Authorization": f"token {token}"},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            return True
        return any(marker in (c.get("body") or "") for c in response.json())
    except (http_requests.RequestException, ValueError):
        return True


def _render(ignored):
    """The comment body: what was not done, and what would change it."""
    # EVENT-NEUTRAL header: this renders for merged PRs AND changes-requested
    # reviews on OPEN pull requests, so "this merge" was false on the review
    # exit (Morrow).
    lines = ["**Biplane bridge — this event did not move a ticket.**", ""]

    for entry in ignored.get("unverified", ()):
        ticket = entry.get("ticket") or "a ticket"
        detail = entry.get("detail") or entry.get("reason") or "the bridge could not verify this move"
        lines.append(f"- **{ticket}** — {detail}")

    for line in ignored.get("near_misses", ()):
        lines.append(
            f"- `{line}` looks like a directive but is not one, so no ticket was selected from it."
        )

    for entry in ignored.get("cross_project", ()):
        lines.append(
            f"- **{entry.get('ticket')}** is outside the scope this repository is mapped to "
            f"({entry.get('reason') or 'cross-project reference'}), so it was not touched."
        )

    if ignored.get("unscoped_repo"):
        lines.append(
            f"- This repository (`{ignored['unscoped_repo']}`) is not mapped to a project, "
            "so nothing here can move a ticket."
        )

    if ignored.get("review"):
        lines.append(f"- Review event not acted on: {ignored['review']}.")

    if ignored.get("no_ticket"):
        # EVENT-level: the body named nothing at all. The actor is present on
        # the PR, and silence here is indistinguishable from a broken bridge.
        lines.append(f"- {ignored['no_ticket'].get('detail') or 'No ticket was named by this event.'}")

    if len(lines) == 2:
        return None  # nothing worth interrupting anyone about

    lines += ["", "*Move the ticket yourself if it should move — the bridge assists, it does not decide.*"]
    return "\n".join(lines)


def refusal_comment(*, delivery_id, result, repo, number, forge=None):
    """Post the refusal on the pull request that caused it. Best-effort.

    Returns True when a comment was written, False otherwise — for tests and
    logging only. NOTHING about a delivery depends on this return value.
    """
    ignored = (result or {}).get("ignored") or {}
    if not ignored or not repo or not number:
        return False

    # THE DELIVERY'S OWN FORGE DECIDES WHETHER WE MAY SPEAK, never the
    # configured base URL (Rowan RC 3765). `FORGEJO_BASE_URL` is a single
    # hardcoded destination, so without this a GitHub-sourced delivery would
    # post its refusal to whatever repository and pull-request NUMBER happen to
    # coincide on the Forgejo instance — a comment on a stranger's work, about
    # an event that did not happen there. Unknown or absent provider is silence.
    if (forge or "").lower() not in _SPEAKABLE_FORGES:
        return False

    base, token = _creds()
    if not base or not token:
        # Reading the forge needs no write credential; replying does. An
        # unconfigured deployment stays silent rather than erroring.
        return False

    marker = _MARKER.format(delivery_id=delivery_id)
    body = _render(ignored)
    if body is None:
        return False
    if _already_said(base, token, repo, number, marker):
        return False

    try:
        response = http_requests.post(
            f"{base}/api/v1/repos/{repo}/issues/{number}/comments",
            headers={"Authorization": f"token {token}"},
            json={"body": f"{marker}\n{body}"},
            timeout=TIMEOUT_SECONDS,
        )
    except http_requests.RequestException as e:
        logger.warning(f"git-bridge: could not reply on {repo}#{number}: {e}")
        return False

    if response.status_code >= 400:
        logger.warning(
            f"git-bridge: forge refused the reply on {repo}#{number} "
            f"(HTTP {response.status_code}); the delivery outcome is unaffected"
        )
        return False
    return True
