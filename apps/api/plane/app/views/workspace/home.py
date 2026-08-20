# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Module imports
from ..base import BaseAPIView
from plane.db.models.workspace import WorkspaceHomePreference
from plane.app.permissions import allow_permission, ROLE
from plane.db.models import Workspace
from plane.app.serializers.workspace import WorkspaceHomePreferenceSerializer

# Third party imports
from django.db import transaction
from rest_framework.response import Response
from rest_framework import status


def _served_widget_keys():
    """The widget keys this endpoint actually serves.

    biplane (BIP-28): two keys exist in the model's choices but are filtered
    out of the GET. The create path is validated against THIS set, not the raw
    choices — otherwise a PATCH could create a row for a widget the GET will
    never return, which is a row nothing can read back or clear.
    """
    return [
        str(key)
        for key, _ in WorkspaceHomePreference.HomeWidgetKeys.choices
        if key not in ["quick_tutorial", "new_at_plane"]
    ]


class WorkspaceHomePreferenceViewSet(BaseAPIView):
    model = WorkspaceHomePreference

    def get_serializer_class(self):
        return WorkspaceHomePreferenceSerializer

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def get(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)

        # biplane (BIP-28): a GET must not write. This used to bulk_create the
        # missing widget rows; the defaults are now computed IN MEMORY and any
        # stored row overlays its default, so the response is unchanged.
        # Persistence happens on PATCH, where the change originates.
        keys = _served_widget_keys()

        rows = {
            str(key): {
                "key": str(key),
                "is_enabled": True,
                "config": {},
                "sort_order": 1000 - (i + 1),
            }
            for i, key in enumerate(keys)
        }

        for stored in WorkspaceHomePreference.objects.filter(
            user=request.user, workspace_id=workspace.id
        ).values("key", "is_enabled", "config", "sort_order"):
            rows[str(stored["key"])] = {**stored, "key": str(stored["key"])}

        return Response(
            sorted(rows.values(), key=lambda r: r["sort_order"]),
            status=status.HTTP_200_OK,
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def patch(self, request, slug, key):
        # biplane (BIP-28): the row is created HERE now that the GET no longer
        # does it. This handler was already correctly scoped to request.user,
        # so it only gains the create.
        workspace = Workspace.objects.get(slug=slug)

        # The model's `key` is a plain CharField with no `choices`, so an
        # arbitrary string would otherwise create a row for a widget that
        # does not exist. Reject before writing anything. The domain is the
        # set the GET serves, not the raw choices — see _served_widget_keys.
        if str(key) not in set(_served_widget_keys()):
            return Response(
                {"error": f"Invalid widget key: {key}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            preference, _ = WorkspaceHomePreference.objects.get_or_create(
                key=key, workspace=workspace, user=request.user
            )
            serializer = WorkspaceHomePreferenceSerializer(preference, data=request.data, partial=True)

            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)

            # A rejected request must not leave behind the row it just
            # created. Returning from inside atomic() does NOT roll back —
            # DRF returns a response rather than raising — so say so.
            transaction.set_rollback(True)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
