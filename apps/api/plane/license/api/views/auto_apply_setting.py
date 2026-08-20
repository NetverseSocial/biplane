# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The automatic-updates switch (Settings → Updates, John's design 2026-08-16).

The setting lives in InstanceConfiguration so the UI switch is real; the
BIPLANE_APPLY_AUTO env var remains a deployment-level override (env wins when
set to "1" — an operator who forced it on at deploy time keeps it on)."""

from rest_framework import status
from rest_framework.response import Response

from plane.app.views import BaseAPIView
from plane.license.api.permissions import InstanceAdminPermission
from plane.authentication.session import BaseSessionAuthentication, BoardSessionAuthentication
from plane.license.models import InstanceConfiguration

CONFIG_KEY = "BIPLANE_APPLY_AUTO"


def auto_apply_enabled() -> bool:
    """One authority, read by the beat task and this endpoint."""
    from django.conf import settings

    if getattr(settings, "BIPLANE_APPLY_AUTO", False):
        return True
    row = InstanceConfiguration.objects.filter(key=CONFIG_KEY).first()
    return bool(row and row.value == "1")


class AutoApplySettingEndpoint(BaseAPIView):
    # Board sessions of instance admins are accepted here — see
    # BoardSessionAuthentication; the permission below still gates the role.
    authentication_classes = [BaseSessionAuthentication, BoardSessionAuthentication]
    permission_classes = [InstanceAdminPermission]

    def get(self, request):
        from django.conf import settings

        return Response(
            {
                "enabled": auto_apply_enabled(),
                "env_forced": bool(getattr(settings, "BIPLANE_APPLY_AUTO", False)),
            },
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        from django.conf import settings

        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            return Response({"error": "enabled must be true or false"}, status=status.HTTP_400_BAD_REQUEST)
        InstanceConfiguration.objects.update_or_create(
            key=CONFIG_KEY,
            defaults={"value": "1" if enabled else "0", "category": "BIPLANE", "is_encrypted": False},
        )
        return Response(
            {"enabled": auto_apply_enabled(), "env_forced": bool(getattr(settings, "BIPLANE_APPLY_AUTO", False))},
            status=status.HTTP_200_OK,
        )


class OurChangelogEndpoint(BaseAPIView):
    """GET — Biplane's own CHANGELOG.md, shipped in the image (Settings →
    Updates renders it scrollable). Ours, not upstream Plane's; entries link
    to Plane's changelog themselves when we incorporate their changes."""

    # Board sessions of instance admins are accepted here — see
    # BoardSessionAuthentication; the permission below still gates the role.
    authentication_classes = [BaseSessionAuthentication, BoardSessionAuthentication]
    permission_classes = [InstanceAdminPermission]

    def get(self, request):
        import os

        path = os.environ.get("BIPLANE_CHANGELOG_PATH", "/code/CHANGELOG.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Response({"markdown": f.read()}, status=status.HTTP_200_OK)
        except OSError:
            return Response(
                {"markdown": None, "error": "no changelog shipped in this build (dev image)"},
                status=status.HTTP_200_OK,
            )


class UpdateSourceSettingEndpoint(BaseAPIView):
    """GET/PATCH — where the update check looks (Settings → Updates).
    Choices: forgejo (current server, the default) | github | custom (+url).
    "biplane_dev" is reserved for the future official host and refused until
    it exists — the UI shows it disabled rather than silently ignored."""

    # Board sessions of instance admins are accepted here — see
    # BoardSessionAuthentication; the permission below still gates the role.
    authentication_classes = [BaseSessionAuthentication, BoardSessionAuthentication]
    permission_classes = [InstanceAdminPermission]

    def get(self, request):
        from plane.license.utils.release_source import _update_source_preference

        pref, url = _update_source_preference()
        return Response({"source": pref, "custom_url": url}, status=status.HTTP_200_OK)

    def patch(self, request):
        from plane.license.utils.release_source import UPDATE_SOURCE_KEY, UPDATE_SOURCE_URL_KEY

        source = request.data.get("source")
        custom_url = request.data.get("custom_url") or ""
        if source == "biplane_dev":
            return Response(
                {"error": "Biplane.dev is not live yet — coming soon"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if source not in ("forgejo", "github", "custom"):
            return Response({"error": "source must be forgejo, github or custom"}, status=status.HTTP_400_BAD_REQUEST)
        if source == "custom" and not custom_url.startswith(("http://", "https://")):
            return Response({"error": "custom needs an http(s) URL"}, status=status.HTTP_400_BAD_REQUEST)
        InstanceConfiguration.objects.update_or_create(
            key=UPDATE_SOURCE_KEY, defaults={"value": source, "category": "BIPLANE", "is_encrypted": False}
        )
        InstanceConfiguration.objects.update_or_create(
            key=UPDATE_SOURCE_URL_KEY,
            defaults={"value": custom_url if source == "custom" else "", "category": "BIPLANE", "is_encrypted": False},
        )
        from plane.license.utils.release_source import _update_source_preference

        pref, url = _update_source_preference()
        return Response({"source": pref, "custom_url": url}, status=status.HTTP_200_OK)
