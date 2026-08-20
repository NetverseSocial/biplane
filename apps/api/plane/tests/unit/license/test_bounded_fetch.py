# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bounded transport (M5): every bound pinned on both edges.

TLS authenticates the endpoint; these tests pin what TLS does not — origin
allowlisting on EVERY hop, streamed size caps, the redirect budget, the
wall-clock deadline, and credential scoping to one exact origin."""

from unittest import mock

import pytest
import requests

from plane.license.utils import bounded_fetch
from plane.license.utils.bounded_fetch import (
    DEFAULT_ALLOWED_ORIGINS,
    FETCH_DEADLINE_SECONDS,
    FetchRefused,
    bounded_get_json,
)


class _FakeResponse:
    def __init__(self, status=200, location=None, body=b"{}", content_type="application/json"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        if location:
            self.headers["Location"] = location
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        self.closed = True


def _fake_requests(monkeypatch, responses=None):
    """responses: {url: _FakeResponse}; default 200 {}."""
    seen = {}
    responses = responses or {}

    def fake_get(url, headers=None, allow_redirects=None, stream=None, **kwargs):
        assert allow_redirects is False, "library redirect-following must be OFF"
        assert stream is True, "bodies must be STREAMED so the size cap binds"
        seen[url] = headers or {}
        return responses.get(url, _FakeResponse())

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


API = "https://api.github.com/repos/x/releases/latest"


def test_https_default_origins_fetch_and_undeclared_origins_refuse(monkeypatch):
    seen = _fake_requests(monkeypatch)
    status, payload = bounded_get_json(API)
    assert status == 200 and payload == {}

    with pytest.raises(FetchRefused, match="not allowlisted"):
        bounded_get_json("https://evil.example/api.github.com/steal")
    assert "https://evil.example/api.github.com/steal" not in seen


def test_http_is_refused_unless_the_operator_explicitly_declared_that_origin(monkeypatch):
    """A closed-LAN forge (our own forge.test Forgejo) is legitimately plain
    http — but ONLY as an explicit configuration statement. http is never
    reachable by default, and a redirect cannot reach an undeclared http
    origin."""
    _fake_requests(monkeypatch)
    with pytest.raises(FetchRefused, match="non-HTTPS"):
        bounded_get_json("http://api.github.com/downgrade")

    status, _ = bounded_get_json(
        "http://forge.test:3000/api/v1/repos/example/biplane/releases/latest",
        allowed_origins=("http://forge.test:3000",),
    )
    assert status == 200


def test_redirect_hops_are_revalidated_not_library_followed(monkeypatch):
    _fake_requests(
        monkeypatch,
        {f"{API}/a": _FakeResponse(302, location="https://evil.example/grab")},
    )
    with pytest.raises(FetchRefused, match="not allowlisted"):
        bounded_get_json(f"{API}/a")

    loop = {f"{API}/loop": _FakeResponse(302, location=f"{API}/loop")}
    _fake_requests(monkeypatch, loop)
    with pytest.raises(FetchRefused, match="redirects"):
        bounded_get_json(f"{API}/loop")


def test_stock_github_release_asset_redirect_shape_is_reachable(monkeypatch):
    """CAPTURED shape (live probe 2026-08-12, `curl -sI` on an official GitHub
    release download): 302 with Location on release-assets.githubusercontent.com
    carrying signed query params. The defaults must cover it — that is where
    real GitHub release assets live today."""
    captured = (
        "https://release-assets.githubusercontent.com/github-production-release-asset"
        "/212613049/16afac99-3d2b-4e60-8e39-38aa01debb17"
        "?sp=r&sv=2018-11-09&sr=b&rsct=application%2Foctet-stream&sig=REDACTED&jwt=REDACTED"
    )
    seen = _fake_requests(
        monkeypatch,
        {
            "https://github.com/cli/cli/releases/download/v2.65.0/x.txt": _FakeResponse(
                302, location=captured
            )
        },
    )
    status, _ = bounded_get_json("https://github.com/cli/cli/releases/download/v2.65.0/x.txt")
    assert status == 200
    assert captured in seen
    assert "release-assets.githubusercontent.com" in str(DEFAULT_ALLOWED_ORIGINS)


def test_configured_origins_union_with_defaults_never_replace(monkeypatch):
    _fake_requests(monkeypatch)
    # With an extra origin configured, the defaults still fetch.
    status, _ = bounded_get_json(API, allowed_origins=("https://forge.example",))
    assert status == 200
    status, _ = bounded_get_json("https://forge.example/x", allowed_origins=("https://forge.example",))
    assert status == 200


def test_credential_travels_only_to_its_exact_origin(monkeypatch):
    """The credential is attached on hops matching its origin and NOWHERE
    else — not to another allowlisted host, and not across a redirect."""
    forge = "http://forge.test:3000"
    seen = _fake_requests(monkeypatch)
    credential = (forge, {"Authorization": "token forge-secret"})

    bounded_get_json(f"{forge}/api/v1/repos/example/biplane/releases/latest", credential=credential)
    assert seen[f"{forge}/api/v1/repos/example/biplane/releases/latest"]["Authorization"] == "token forge-secret"

    bounded_get_json(API, credential=credential)
    assert "Authorization" not in seen[API]

    cdn = "https://objects.githubusercontent.com/asset"
    seen2 = _fake_requests(monkeypatch, {API: _FakeResponse(302, location=cdn)})
    bounded_get_json(API, credential=("https://api.github.com", {"Authorization": "Bearer x"}))
    assert "Authorization" in seen2[API]
    assert "Authorization" not in seen2[cdn], "credential followed a redirect off its origin"


def test_oversized_body_is_refused_mid_stream(monkeypatch):
    big = _FakeResponse(body=b"x" * 4097, content_type="application/octet-stream")
    _fake_requests(monkeypatch, {API: big})
    with pytest.raises(FetchRefused, match="byte cap"):
        bounded_get_json(API, max_bytes=4096)
    assert big.closed is True  # the connection did not linger


def test_wall_clock_deadline_covers_all_redirect_hops(monkeypatch):
    """Per-request timeouts alone let five slow hops stack; the deadline is
    one budget for the WHOLE fetch."""
    ticks = iter([0.0, 5.0, FETCH_DEADLINE_SECONDS + 1.0])
    monkeypatch.setattr(bounded_fetch.time, "monotonic", lambda: next(ticks))
    _fake_requests(monkeypatch, {API: _FakeResponse(302, location=API)})
    with pytest.raises(FetchRefused, match="wall-clock"):
        bounded_get_json(API)


def test_non_json_body_returns_none_payload_not_a_crash(monkeypatch):
    _fake_requests(monkeypatch, {API: _FakeResponse(body=b"<html>", content_type="text/html")})
    status, payload = bounded_get_json(API)
    assert status == 200 and payload is None


@pytest.mark.parametrize(
    "body",
    [b'{"tag":"v1","tag":"v2"}', b'{"outer":{"level":"code","level":"full"}}'],
)
def test_duplicate_json_keys_are_refused_at_every_depth(monkeypatch, body):
    _fake_requests(monkeypatch, {API: _FakeResponse(body=body)})
    with pytest.raises(FetchRefused, match="duplicate JSON key"):
        bounded_get_json(API)


def test_slow_trickle_body_cannot_outlive_the_budget(monkeypatch):
    """RC 3392 #1, the executed counterexample, pinned: every chunk arrives
    within the socket timeout, but the wall clock passes the declared budget
    MID-STREAM — the fetch must refuse, never return 200. Tick sequence:
    deadline calc, hop check, timeout calc, first chunk (in budget), second
    chunk (past budget)."""
    ticks = iter([0.0, 1.0, 2.0, 10.0, FETCH_DEADLINE_SECONDS + 14.0])
    monkeypatch.setattr(bounded_fetch.time, "monotonic", lambda: next(ticks))
    body = b"x" * (128 * 1024)  # two 64KiB chunks => two mid-stream checks
    _fake_requests(
        monkeypatch, {API: _FakeResponse(body=body, content_type="application/octet-stream")}
    )
    with pytest.raises(FetchRefused, match="wall-clock"):
        bounded_get_json(API)


def test_each_request_timeout_is_bounded_by_the_remaining_budget(monkeypatch):
    """RC 3392 #1, second half: a fixed 10s per-request timeout lets hops
    stack past the total. With ~4s of budget left, the request must receive
    ~4s, not 10."""
    ticks = iter([0.0, 25.0, 26.0, 27.0])
    monkeypatch.setattr(bounded_fetch.time, "monotonic", lambda: next(ticks))
    captured = {}

    def fake_get(url, headers=None, allow_redirects=None, stream=None, timeout=None, **kw):
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    bounded_get_json(API)
    assert captured["timeout"] == pytest.approx(FETCH_DEADLINE_SECONDS - 26.0)
