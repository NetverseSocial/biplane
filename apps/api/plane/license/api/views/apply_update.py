# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The apply half of the update surface (ticket 69 / BIP-42 tail).

Every decision lives in services/apply_client.py — ONE authority shared with
the automatic mode, for both verbs. These views translate its verdicts to
HTTP and add nothing: no transport, no timeout, no prose of their own.
"""

from rest_framework import status
from rest_framework.response import Response

from plane.app.views import BaseAPIView
from plane.license.api.permissions import InstanceAdminPermission
from plane.authentication.session import BaseSessionAuthentication, BoardSessionAuthentication
from plane.license.services import apply_client

_KIND_TO_STATUS = {
    apply_client.NOT_CONFIGURED: status.HTTP_501_NOT_IMPLEMENTED,
    apply_client.NOT_FLAGGED: status.HTTP_409_CONFLICT,
    apply_client.FULL_LEVEL: status.HTTP_409_CONFLICT,
    apply_client.UNREACHABLE: status.HTTP_502_BAD_GATEWAY,
    apply_client.BAD_UPSTREAM: status.HTTP_502_BAD_GATEWAY,
}


def _to_response(verdict: dict) -> Response:
    if verdict["kind"] == apply_client.REQUESTED:
        # The applier's verdict IS the verdict — surfaced verbatim.
        return Response(verdict["body"], status=verdict["status_code"])
    return Response({"error": verdict["detail"]}, status=_KIND_TO_STATUS[verdict["kind"]])


class ApplyUpdateEndpoint(BaseAPIView):
    """POST — ask the host applier to apply the flagged update."""

    # Board sessions of instance admins are accepted here — see
    # BoardSessionAuthentication; the permission below still gates the role.
    authentication_classes = [BaseSessionAuthentication, BoardSessionAuthentication]
    permission_classes = [InstanceAdminPermission]

    def post(self, request):
        return _to_response(apply_client.request_apply_of_flagged())


class ApplyStatusEndpoint(BaseAPIView):
    """GET — the applier's run status, verbatim."""

    # Board sessions of instance admins are accepted here — see
    # BoardSessionAuthentication; the permission below still gates the role.
    authentication_classes = [BaseSessionAuthentication, BoardSessionAuthentication]
    permission_classes = [InstanceAdminPermission]

    def get(self, request):
        return _to_response(apply_client.request_status())
