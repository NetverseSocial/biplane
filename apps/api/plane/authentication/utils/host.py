# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import logging
import threading

# Django imports
from django.conf import settings
from django.http import HttpRequest

# Third party imports
from rest_framework.request import Request

# Module imports
from plane.utils.ip_address import get_client_ip

logger = logging.getLogger("plane.api")

# biplane (BIP-35): origins already reported, so a mismatch is logged ONCE per
# distinct origin rather than on every request. A warning that floods is a
# warning nobody reads.
#
# BOUNDED BY CONSTRUCTION (Morrow RC 3249). The key is derived from the request
# Host header and ALLOWED_HOSTS defaults to a wildcard, so this key space is
# ATTACKER-CONTROLLED. An unbounded set lets an unauthenticated caller grow
# process memory and log volume without limit simply by varying Host — a denial
# of service inside an observability feature. The cap makes BOTH retained state
# and total warning count independent of how many distinct origins an attacker
# invents. A real misconfiguration still reports: genuine deployments have a
# handful of origins, not thousands.
_ORIGIN_REPORT_CAP = 16
_REPORTED_ORIGIN_MISMATCHES: set[str] = set()
_ORIGIN_REPORTING_SUPPRESSED = False

# LOCKED, because the cap is a COMPOUND check-then-act (Morrow RC 3251). Reading
# the suppression flag, testing membership, comparing length and then adding is
# four operations on shared state, and the server is threaded. Without this,
# concurrent requests can all observe "below cap" and then every one of them
# adds and logs — blowing past the bound the cap exists to enforce — and several
# can emit the once-only suppression notice. A sequential cap is not a bound.
_ORIGIN_REPORT_LOCK = threading.Lock()


def _warn_if_origin_mismatch(request, base_origin: str) -> None:
    """Make a base_host/browser-origin mismatch VISIBLE.

    biplane (BIP-35). `base_host` returns CONFIGURED origin and never consults
    the origin the request actually arrived on. When those differ, flows that
    redirect to base_host and then classify the final followed response break
    in a way that looks like a UI bug rather than a misconfiguration.

    Witnessed 2026-08-10: the password-reset weak-password bounce is a 302 to
    base_host; the client classifies only the final response. Served from an
    origin that is not base_host, the follow cannot complete, no verdict
    arrives, the form shows an indeterminate banner, and the override checkbox
    is never revealed. The user cannot complete a reset and nothing is logged.

    This does NOT change what base_host returns — 210 call sites depend on it.
    It only makes the condition diagnosable.
    """
    configured = (base_origin or "").rstrip("/")
    if not configured:
        return
    try:
        actual = request.build_absolute_uri("/").rstrip("/")
    except Exception:  # noqa: BLE001 — observability must never break a request
        return
    if not actual or actual == configured:
        return
    global _ORIGIN_REPORTING_SUPPRESSED

    # Everything below reads AND writes shared state, so it happens under one
    # lock. Splitting it — deciding outside and mutating inside — would leave
    # the same race the lock exists to close. Decide and act together, or the
    # cap is only a cap when requests arrive one at a time.
    with _ORIGIN_REPORT_LOCK:
        if _ORIGIN_REPORTING_SUPPRESSED:
            return
        if actual in _REPORTED_ORIGIN_MISMATCHES:
            return
        if len(_REPORTED_ORIGIN_MISMATCHES) >= _ORIGIN_REPORT_CAP:
            # Cap reached. Stop retaining attacker-suppliable keys and stop
            # logging, permanently for this process. Announce it ONCE so the
            # silence that follows is itself diagnosable rather than mysterious
            # — an operator seeing this line knows why later mismatches are
            # quiet.
            _ORIGIN_REPORTING_SUPPRESSED = True
            suppression_notice = True
        else:
            _REPORTED_ORIGIN_MISMATCHES.add(actual)
            suppression_notice = False

    # Logging is deliberately OUTSIDE the lock: a slow or blocking handler
    # must not serialise request threads. The state decision above is already
    # final, so each branch below runs exactly once per winning thread.
    if suppression_notice:
        logger.warning(
            "biplane: origin-mismatch reporting suppressed after %s distinct origins. "
            "Further mismatches will NOT be logged in this process. Seeing this line "
            "usually means Host is being varied by a caller rather than that you have "
            "many real origins — check WEB_URL and ALLOWED_HOSTS.",
            _ORIGIN_REPORT_CAP,
        )
        return

    logger.warning(
        "biplane: request origin %s does not match configured WEB_URL/APP_BASE_URL %s. "
        "Redirect-and-classify flows (notably the password-reset weak-password bounce) "
        "will degrade to an indeterminate state for users on this origin, and the "
        "override control will not be shown. Set WEB_URL to the origin users actually "
        "reach (scheme, host AND port) and include it in CORS_ALLOWED_ORIGINS.",
        actual,
        configured,
    )


def base_host(
    request: Request | HttpRequest,
    is_admin: bool = False,
    is_space: bool = False,
    is_app: bool = False,
) -> str:
    """Utility function to return host / origin from the request"""
    # Calculate the base origin from request
    base_origin = settings.WEB_URL or settings.APP_BASE_URL

    _warn_if_origin_mismatch(request, base_origin)

    # Admin redirection
    if is_admin:
        admin_base_path = getattr(settings, "ADMIN_BASE_PATH", None)
        if not isinstance(admin_base_path, str):
            admin_base_path = "/admin/"
        if not admin_base_path.startswith("/"):
            admin_base_path = "/" + admin_base_path
        if not admin_base_path.endswith("/"):
            admin_base_path += "/"

        if settings.ADMIN_BASE_URL:
            return settings.ADMIN_BASE_URL + admin_base_path
        else:
            return base_origin + admin_base_path

    # Space redirection
    if is_space:
        space_base_path = getattr(settings, "SPACE_BASE_PATH", None)
        if not isinstance(space_base_path, str):
            space_base_path = "/spaces/"
        if not space_base_path.startswith("/"):
            space_base_path = "/" + space_base_path
        if not space_base_path.endswith("/"):
            space_base_path += "/"

        if settings.SPACE_BASE_URL:
            return settings.SPACE_BASE_URL + space_base_path
        else:
            return base_origin + space_base_path

    # App Redirection
    if is_app:
        if settings.APP_BASE_URL:
            return settings.APP_BASE_URL
        else:
            return base_origin

    return base_origin


def user_ip(request: Request | HttpRequest) -> str:
    return get_client_ip(request=request)
