# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Read-only BIP-41 update-check status — the banner's only input."""

from rest_framework import status
from rest_framework.response import Response

from plane.app.views import BaseAPIView
from plane.license.api.permissions import InstanceAdminPermission
from plane.authentication.session import BaseSessionAuthentication, BoardSessionAuthentication
from plane.license.services.update_check import check_status_payload


class UpdateCheckStatusEndpoint(BaseAPIView):
    """Return the last update-check classification to an instance admin.

    The payload's `state` drives the banner: "update_available" (and ONLY
    that) offers the apply action; "unknown" renders as unknown in both
    directions — never as up to date — with `reason` as the operator's
    explanation. No check is triggered here: this surface is read-only and
    local (cache + durable columns) — the request path performs zero outbound
    work; fetching belongs to the scheduled check alone.
    """

    # Board sessions of instance admins are accepted here — see
    # BoardSessionAuthentication; the permission below still gates the role.
    authentication_classes = [BaseSessionAuthentication, BoardSessionAuthentication]
    permission_classes = [InstanceAdminPermission]

    def get(self, request):
        return Response(check_status_payload(), status=status.HTTP_200_OK)
