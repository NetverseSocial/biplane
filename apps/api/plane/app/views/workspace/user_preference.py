# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Module imports
from ..base import BaseAPIView
from plane.db.models.workspace import WorkspaceUserPreference
from plane.app.serializers.workspace import WorkspaceUserPreferenceSerializer
from plane.app.permissions import allow_permission, ROLE
from plane.db.models import Workspace

# Third party imports
from django.db import transaction


# Third party imports
from rest_framework.response import Response
from rest_framework import status


class WorkspaceUserPreferenceViewSet(BaseAPIView):
    model = WorkspaceUserPreference
    use_read_replica = True

    def get_serializer_class(self):
        return WorkspaceUserPreferenceSerializer

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def get(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)

        # biplane (BIP-28): a GET must not write. This used to bulk_create the
        # missing default rows, which is idempotent but still a write behind a
        # method that prefetchers, crawlers and link scanners may issue freely.
        # The defaults are computed IN MEMORY and stored rows are overlaid on
        # top, so the response is identical; persistence now happens on PATCH,
        # which is where a change actually originates.
        defaults = {}
        for i, key in enumerate(k for k, _ in WorkspaceUserPreference.UserPreferenceKeys.choices):
            defaults[str(key)] = {
                "is_pinned": key
                in [
                    WorkspaceUserPreference.UserPreferenceKeys.DRAFTS,
                    WorkspaceUserPreference.UserPreferenceKeys.YOUR_WORK,
                    WorkspaceUserPreference.UserPreferenceKeys.STICKIES,
                ],
                "sort_order": 65535 + (i * 10000),
            }

        stored = (
            WorkspaceUserPreference.objects.filter(user=request.user, workspace_id=workspace.id)
            .order_by("sort_order")
            .values("key", "is_pinned", "sort_order")
        )
        for preference in stored:
            defaults[str(preference["key"])] = {
                "is_pinned": preference["is_pinned"],
                "sort_order": preference["sort_order"],
            }

        # Preserve the stored-first ordering the old query produced.
        user_preferences = dict(sorted(defaults.items(), key=lambda kv: kv[1]["sort_order"]))
        return Response(
            user_preferences,
            status=status.HTTP_200_OK,
        )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def patch(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)

        if not isinstance(request.data, list):
            return Response(
                {"error": "Expected a list of preferences"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # biplane (BIP-28): validate the WHOLE batch before writing any of
        # it. The model's `key` is a plain CharField with no `choices`, so
        # nothing further down would reject an arbitrary key — a caller
        # could grow this table by any string it liked. Validating up front
        # also means a bulk PATCH that names one bad key writes nothing at
        # all, instead of leaving behind the rows it got through first.
        valid_keys = {str(k) for k, _ in WorkspaceUserPreference.UserPreferenceKeys.choices}
        batch = []
        for data in request.data:
            # An item that is not an object has no .get(), so the old loop
            # raised AttributeError and returned a 500 for what is plainly a
            # bad request.
            if not isinstance(data, dict):
                return Response(
                    {"error": "Each preference must be an object"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # A missing or empty key used to `continue`, so a malformed item
            # was silently dropped while the rest of the batch applied and
            # the caller still got a 200. That is a partial apply reported as
            # a success. It is a bad request, and the whole batch is refused.
            key = data.get("key")
            if not key or not isinstance(key, str) or not key.strip():
                return Response(
                    {"error": "Each preference must carry a non-empty string key"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if str(key) not in valid_keys:
                return Response(
                    {"error": f"Invalid preference key: {key}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # These go straight onto model fields with no serializer in the
            # path, so the types are checked here or not at all. bool is a
            # subclass of int, so sort_order has to exclude it explicitly.
            if "is_pinned" in data and not isinstance(data["is_pinned"], bool):
                return Response(
                    {"error": f"is_pinned must be a boolean for key: {key}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if "sort_order" in data and (
                isinstance(data["sort_order"], bool) or not isinstance(data["sort_order"], (int, float))
            ):
                return Response(
                    {"error": f"sort_order must be a number for key: {key}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            batch.append(data)

        # biplane (BIP-28), two defects were on the lookup line below:
        #
        # 1. TENANCY. It matched on key + workspace only, with no `user=`,
        #    so `.first()` could return ANOTHER member's preference row and
        #    this handler would then write to it. A user could silently
        #    reorder or unpin a colleague's sidebar.
        # 2. It silently did nothing when the row was absent, which is why
        #    the GET was creating rows — the write was pushed onto a safe
        #    method to cover for this one. With the GET no longer writing,
        #    the row is created HERE, where the change actually originates.
        with transaction.atomic():
            for data in batch:
                preference, _ = WorkspaceUserPreference.objects.get_or_create(
                    key=data["key"], workspace=workspace, user=request.user
                )

                if "is_pinned" in data:
                    preference.is_pinned = data["is_pinned"]

                if "sort_order" in data:
                    preference.sort_order = data["sort_order"]

                preference.save(update_fields=["is_pinned", "sort_order"])

        return Response({"message": "Successfully updated"}, status=status.HTTP_200_OK)
