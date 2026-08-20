# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module
from types import SimpleNamespace

from django.conf import settings
from django.contrib import auth
from rest_framework.authentication import SessionAuthentication


class BaseSessionAuthentication(SessionAuthentication):
    # Disable csrf for the rest apis
    def enforce_csrf(self, request):
        return


class BoardSessionAuthentication(BaseSessionAuthentication):
    """Authenticate from the regular board session cookie.

    The session middleware binds any path containing "instances" to the
    short-lived admin-session-id cookie only, so an instance admin browsing
    the BOARD is anonymous on the updates endpoints and gets bounced to a
    second, hourly-expiring login (2026-08-17: the demo's gray page). This
    class answers only WHO the caller is, from the same session store the
    board already trusts; WHAT they may do stays with each view's
    InstanceAdminPermission. Listed after BaseSessionAuthentication, so an
    admin-site session still wins when present.
    """

    def authenticate(self, request):
        key = request._request.COOKIES.get(settings.SESSION_COOKIE_NAME)
        if not key:
            return None
        engine = import_module(settings.SESSION_ENGINE)
        shim = SimpleNamespace(session=engine.SessionStore(key))
        user = auth.get_user(shim)
        if user is None or not user.is_authenticated:
            return None
        return (user, None)
