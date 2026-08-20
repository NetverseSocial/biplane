# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""biplane (BIP-35): a base_host / browser-origin mismatch must be VISIBLE.

`base_host` returns the CONFIGURED origin and never consults the origin a
request actually arrived on. When they differ, flows that redirect to
base_host and then classify the final followed response break in a way that
looks like a UI bug rather than a misconfiguration — witnessed on the farm,
where the password-reset weak bounce degraded to an indeterminate banner with
the override control never revealed, and nothing was logged.

These pin the warning, and — more importantly — pin that base_host's RETURN
VALUE is unchanged. 210 call sites depend on it; observability must not become
a behaviour change.
"""

import logging

import pytest
from django.test import RequestFactory, override_settings

from plane.authentication.utils import host as host_mod
from plane.authentication.utils.host import base_host


@pytest.fixture(autouse=True)
def _clear_reported():
    """Reset BOTH pieces of module state.

    The suppression flag is process-global and sticky by design. Forgetting it
    here would let one flood test silence every later test in the file — they
    would pass or fail on test ORDER rather than on behaviour, which is the
    quiet kind of false green.
    """
    host_mod._REPORTED_ORIGIN_MISMATCHES = set()
    host_mod._ORIGIN_REPORTING_SUPPRESSED = False
    yield
    host_mod._REPORTED_ORIGIN_MISMATCHES = set()
    host_mod._ORIGIN_REPORTING_SUPPRESSED = False


def _request(origin_host="devboard.test:8912"):
    return RequestFactory().get("/", HTTP_HOST=origin_host)


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_matching_origin_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        base_host(_request("devboard.test:8912"))
    assert "does not match configured" not in caplog.text


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_mismatched_origin_warns_loudly(caplog):
    """The exact farm case: same host, different origin."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        base_host(_request("203.0.113.14:8912"))

    assert "does not match configured" in caplog.text
    assert "203.0.113.14:8912" in caplog.text
    assert "devboard.test:8912" in caplog.text
    # The message must name the CONSEQUENCE, not just the fact — an operator
    # has to connect "users cannot reset passwords" to this line.
    assert "password-reset" in caplog.text
    assert "CORS_ALLOWED_ORIGINS" in caplog.text


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_a_port_only_difference_still_warns(caplog):
    """Scheme, host AND port matter. A port-only mismatch breaks the follow
    just as completely and is easier to miss by eye."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        base_host(_request("devboard.test:9999"))
    assert "does not match configured" in caplog.text


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_the_warning_is_logged_once_per_origin_not_per_request(caplog):
    """A warning that floods is a warning nobody reads."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        for _ in range(5):
            base_host(_request("203.0.113.14:8912"))
    assert caplog.text.count("does not match configured") == 1


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_a_second_distinct_origin_is_reported_separately(caplog):
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        base_host(_request("203.0.113.14:8912"))
        base_host(_request("192.168.1.5:8912"))
    assert caplog.text.count("does not match configured") == 2


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_return_value_is_unchanged_by_the_warning():
    """The load-bearing assertion: 210 call sites depend on this value, so
    adding observability must not alter it — on match OR mismatch."""
    assert base_host(_request("devboard.test:8912")) == "http://devboard.test:8912"
    assert base_host(_request("203.0.113.14:8912")) == "http://devboard.test:8912"


@override_settings(WEB_URL="", APP_BASE_URL="")
def test_unconfigured_base_origin_does_not_warn(caplog):
    """Nothing to compare against is not a mismatch — do not cry wolf."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        base_host(_request("203.0.113.14:8912"))
    assert "does not match configured" not in caplog.text


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="")
def test_a_broken_request_object_cannot_break_the_caller():
    """Observability must never take down a request path."""

    class Hostile:
        def build_absolute_uri(self, _):
            raise RuntimeError("no host available")

    assert base_host(Hostile()) == "http://devboard.test:8912"


# ---------------------------------------------------------------------------
# Morrow RC 3249: the cache key is derived from the request Host header and
# ALLOWED_HOSTS defaults to a wildcard, so the KEY SPACE IS ATTACKER-CONTROLLED.
# An unbounded set turns an observability feature into a denial of service:
# an unauthenticated caller varying Host grows process memory and log volume
# without limit. These pin that BOTH retained state and warning count are
# bounded independently of how many origins an attacker invents — while a real
# mismatch still reports.
# ---------------------------------------------------------------------------


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_hostile_host_header_cannot_grow_retained_state_without_bound(caplog):
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        for i in range(500):
            base_host(_request(f"evil-{i}.example.com"))

    assert len(host_mod._REPORTED_ORIGIN_MISMATCHES) <= host_mod._ORIGIN_REPORT_CAP, (
        "retained origin state grew with attacker origin cardinality"
    )


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_hostile_host_header_cannot_grow_log_volume_without_bound(caplog):
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        for i in range(500):
            base_host(_request(f"evil-{i}.example.com"))

    emitted = caplog.text.count("does not match configured")
    assert emitted <= host_mod._ORIGIN_REPORT_CAP, (
        f"log volume scaled with attacker input: {emitted} warnings from 500 origins"
    )
    # The silence that follows must itself be diagnosable.
    assert caplog.text.count("reporting suppressed after") == 1


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_a_real_mismatch_still_reports_under_wildcard_hosts(caplog):
    """The guard must not be bought with the feature: with ALLOWED_HOSTS
    wildcard and no flood in progress, a genuine misconfiguration still warns."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        base_host(_request("203.0.113.14:8912"))

    assert "does not match configured" in caplog.text
    assert "203.0.113.14:8912" in caplog.text


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_suppression_is_announced_once_not_per_request(caplog):
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        for i in range(200):
            base_host(_request(f"evil-{i}.example.com"))
        for i in range(200):
            base_host(_request(f"more-{i}.example.com"))

    assert caplog.text.count("reporting suppressed after") == 1


# ---------------------------------------------------------------------------
# Morrow RC 3251: the cap was a COMPOUND check-then-act on shared state with no
# lock — read suppression flag, test membership, compare length, add. The
# server is threaded, so concurrent requests could all observe "below cap" and
# then every one of them add and log, blowing past the bound, and several could
# emit the once-only suppression notice. A sequential cap is not a bound.
#
# These use a SEAM rather than luck. A probe set records how many threads are
# inside the critical region at once; under the lock that is provably 1. No
# sleeps, no retries, no "run it 1000 times and hope".
# ---------------------------------------------------------------------------

import threading


class _ConcurrencyProbeSet(set):
    """A set that reports peak simultaneous occupancy of the critical region.

    `__contains__` is called inside the locked region, so entering it means a
    thread is in there. Threads pause at a barrier so that IF the region is
    unlocked they will genuinely overlap; the barrier has a timeout so that
    when the lock DOES serialise them the test finishes instead of hanging.
    """

    def __init__(self, parties):
        super().__init__()
        self._barrier = threading.Barrier(parties)
        self._live = 0
        self._peak = 0
        self._bookkeeping = threading.Lock()

    @property
    def peak(self):
        return self._peak

    def __contains__(self, item):
        with self._bookkeeping:
            self._live += 1
            self._peak = max(self._peak, self._live)
        try:
            self._barrier.wait(timeout=0.4)
        except threading.BrokenBarrierError:
            pass
        try:
            return super().__contains__(item)
        finally:
            with self._bookkeeping:
                self._live -= 1


def _run_concurrently(origins, timeout=15):
    """Run base_host on N workers and REQUIRE every one to finish cleanly.

    Morrow RC 3254: the previous version used `t.join(timeout=...)` and then
    asserted on shared state. That is not evidence the work finished. A worker
    that RAISED left the test green, because a thread's exception dies with the
    thread. A worker still RUNNING when the join timed out also left it green —
    and the autouse fixture would then reset module state underneath a live
    thread, so a *later* test could fail for a reason invented here.

    Futures supply both missing properties: `result()` re-raises whatever the
    worker raised, so an exception fails THIS test; and a worker past the
    deadline raises TimeoutError instead of being silently abandoned. The
    executor context manager joins every worker before returning, so the
    assertions that follow are made against a settled process.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    with ThreadPoolExecutor(max_workers=len(origins)) as pool:
        futures = [pool.submit(base_host, _request(o)) for o in origins]
        for i, f in enumerate(futures):
            try:
                f.result(timeout=timeout)
            except FutureTimeout:
                raise AssertionError(
                    f"worker {i} did not finish within {timeout}s — without this check "
                    "the test would pass while a thread was still running"
                )

    assert all(f.done() for f in futures), "a worker was still live after executor shutdown"


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_the_critical_region_is_entered_by_one_thread_at_a_time():
    """Removing the lock makes this red: peak occupancy becomes the thread
    count instead of 1."""
    parties = 8
    probe = _ConcurrencyProbeSet(parties)
    host_mod._REPORTED_ORIGIN_MISMATCHES = probe

    _run_concurrently([f"race-{i}.example.com" for i in range(parties)], timeout=10)

    assert probe.peak == 1, (
        f"{probe.peak} threads were inside the cap's check-then-act at once — "
        "the compound decision is not serialised"
    )


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_concurrent_floods_cannot_exceed_the_cap(caplog):
    """The invariant that matters: whatever the interleaving, retained state
    and warning volume stay bounded."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        _run_concurrently([f"flood-{i}.example.com" for i in range(120)])

    assert len(host_mod._REPORTED_ORIGIN_MISMATCHES) <= host_mod._ORIGIN_REPORT_CAP
    assert caplog.text.count("does not match configured") <= host_mod._ORIGIN_REPORT_CAP


@override_settings(WEB_URL="http://devboard.test:8912", APP_BASE_URL="", ALLOWED_HOSTS=["*"])
def test_the_suppression_notice_is_emitted_exactly_once_under_concurrency(caplog):
    """Several threads could previously all see 'cap reached' and each announce
    suppression."""
    with caplog.at_level(logging.WARNING, logger="plane.api"):
        _run_concurrently([f"dupe-{i}.example.com" for i in range(120)])

    assert caplog.text.count("reporting suppressed after") == 1
