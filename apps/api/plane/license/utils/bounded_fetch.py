# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bounded resource-fetch primitive (M5): allowlist, streamed size cap,
redirect budget.

EVERY outbound fetch the update check performs goes through here — the
release-object read and the release.json asset read, each with possible
redirect hops. This module owns the BOUNDS of a fetch, not how many the
caller makes. TLS authenticates https endpoints (explicitly configured
LAN-http origins are the operator's own boundary statement, below); this
module bounds everything the transport does not:

- EVERY URL — including every REDIRECT HOP — must be on the origin allowlist,
  and HTTPS unless the operator EXPLICITLY declared that exact origin in
  configuration (a self-hosted forge on a closed LAN is legitimately plain
  http — e.g. our own Forgejo at forge.test:3000 — and that declaration is the
  operator's own boundary statement; nothing http is ever reachable by
  default or via a redirect to an undeclared origin). Location headers are
  response-controlled, so redirects are followed MANUALLY
  (`allow_redirects=False` on every hop, each Location re-validated, bounded
  hop count). Library redirect-following bypassed all of this once before
  (Morrow 3350 #3); that lesson is kept.
- The response body is STREAMED and the byte count enforced as bytes arrive
  (Rowan 3363 #1) — a size limit checked after materialization is not a limit.
- One wall-clock deadline covers the whole fetch including every hop —
  per-request timeouts alone let slow hops stack.
- A credential is attached ONLY on hops whose exact scheme+host match the
  origin it belongs to. It never travels a redirect to another host.

Live-probed 2026-08-12: official GitHub release-asset downloads 302 to
`release-assets.githubusercontent.com` (the older `objects.githubusercontent.com`
also still appears in the wild); both are in the defaults so a stock GitHub
release is actually reachable. Configured extra origins UNION with the
defaults — they never replace them (Morrow 3356 #5).
"""

import json
import time
from typing import Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

DEFAULT_ALLOWED_ORIGINS = (
    "https://api.github.com",
    "https://github.com",
    "https://objects.githubusercontent.com",
    "https://release-assets.githubusercontent.com",
)
MAX_REDIRECT_HOPS = 5
FETCH_DEADLINE_SECONDS = 30
PER_REQUEST_TIMEOUT_SECONDS = 10
#: Release metadata is a small JSON document; a page of releases with bodies
#: fits comfortably. Anything bigger is not release metadata.
MAX_RESPONSE_BYTES = 1024 * 1024


class FetchRefused(Exception):
    """The fetch violated a bound (origin, scheme, size, hops, deadline).
    The message is operator text; callers map it to UNKNOWN."""


def _strict_object(pairs):
    """Build one JSON object while refusing duplicate keys at every depth."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise FetchRefused(f"response contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _origin(url: str) -> Tuple[str, str]:
    parts = urlsplit(url)
    return (parts.scheme, parts.netloc)


def bounded_get_json(
    url: str,
    *,
    allowed_origins: Tuple[str, ...] = (),
    credential: Optional[Tuple[str, Mapping[str, str]]] = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Tuple[int, object]:
    """GET `url` under the module's bounds. Returns (status, parsed_json);
    parsed_json is None when the body is not valid JSON.

    :param allowed_origins: EXTRA origins beyond the defaults (unioned, never
        replacing). The url's own origin is NOT implicitly trusted — it must
        be covered. An explicitly configured origin may be plain http (a
        closed-LAN forge); http is never accepted for anything else.
    :param credential: (origin, headers) — headers attached only on hops whose
        exact scheme+host match that origin. The credential origin is treated
        as explicitly declared.
    :raises FetchRefused: on any violated bound. Network errors from the
        underlying library propagate as-is; callers treat both as "no answer".
    """
    import requests

    explicit = set()
    for entry in allowed_origins or ():
        parts = urlsplit(entry)
        if parts.scheme and parts.netloc:
            explicit.add((parts.scheme, parts.netloc))
    credential_origin = _origin(credential[0]) if credential else None
    if credential_origin:
        explicit.add(credential_origin)
    allowed = {_origin(entry) for entry in DEFAULT_ALLOWED_ORIGINS} | explicit

    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS

    def _remaining() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchRefused(
                f"fetch exceeded the {FETCH_DEADLINE_SECONDS}s wall-clock budget "
                f"for {url}"
            )
        return remaining

    current = url
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        _remaining()
        parts = urlsplit(current)
        hop_origin = (parts.scheme, parts.netloc)
        # Scheme first: an http URL for an undeclared origin is a DOWNGRADE and
        # the refusal should say so, not merely "unknown origin".
        if parts.scheme != "https" and hop_origin not in explicit:
            raise FetchRefused(f"refusing non-HTTPS fetch: {current}")
        if hop_origin not in allowed:
            raise FetchRefused(f"origin not allowlisted: {parts.netloc}")
        headers = {"Accept": "application/json"}
        if credential and (parts.scheme, parts.netloc) == credential_origin:
            headers.update(credential[1])
        response = requests.get(
            current,
            headers=headers,
            # Each request gets ONLY the remaining budget (RC 3392 #1): a fixed
            # per-request timeout would let hops stack past the declared total.
            timeout=min(PER_REQUEST_TIMEOUT_SECONDS, _remaining()),
            allow_redirects=False,
            stream=True,
        )
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise FetchRefused(f"redirect from {current} carries no Location")
                current = urljoin(current, location)
                continue
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                # RC 3392 #1: the wall clock binds DURING the stream too — a
                # slow-trickle body must not outlive the budget just because
                # each read arrives within the socket timeout.
                _remaining()
                total += len(chunk)
                if total > max_bytes:
                    raise FetchRefused(
                        f"response from {current} exceeds the {max_bytes}-byte cap "
                        "— refusing to materialize"
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            parsed = None
            try:
                parsed = json.loads(body, object_pairs_hook=_strict_object)
            except json.JSONDecodeError:
                parsed = None
            return response.status_code, parsed
        finally:
            response.close()
    raise FetchRefused(f"more than {MAX_REDIRECT_HOPS} redirects from {url}")
