# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.urls import reverse
from rest_framework import status


@pytest.mark.contract
class TestGlobalSearchEntities:
    """The `entities` param is validated at the endpoint, not filtered.

    No workspace fixture is needed: the rejection happens before any queryset is
    built, and BaseAPIView gates only on IsAuthenticated. The 200 cases below
    search a slug with no rows, which is fine — these assert status and response
    shape, not search results.
    """

    @pytest.mark.django_db
    def test_unknown_entity_is_rejected(self, session_client):
        url = reverse("global-search", kwargs={"slug": "any-workspace"})

        response = session_client.get(url, {"search": "x", "entities": "work_item"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["unknown_entities"] == ["work_item"]
        # The caller is told what it may send, so the error is actionable.
        assert "issue" in response.data["valid_entities"]

    @pytest.mark.django_db
    def test_unknown_entity_mixed_with_valid_is_still_rejected(self, session_client):
        """A partially-valid request must not half-succeed — that is the shape
        that returned a 200 missing one key."""
        url = reverse("global-search", kwargs={"slug": "any-workspace"})

        response = session_client.get(url, {"search": "x", "entities": "issue,work_item"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["unknown_entities"] == ["work_item"]

    @pytest.mark.django_db
    def test_valid_subset_returns_only_those_keys(self, session_client):
        url = reverse("global-search", kwargs={"slug": "any-workspace"})

        response = session_client.get(url, {"search": "x", "entities": "issue,page"})

        assert response.status_code == status.HTTP_200_OK
        assert sorted(response.data["results"].keys()) == ["issue", "page"]

    @pytest.mark.django_db
    def test_absent_entities_param_searches_everything(self, session_client):
        """The long-standing default, asserted so the fix cannot narrow it."""
        url = reverse("global-search", kwargs={"slug": "any-workspace"})

        response = session_client.get(url, {"search": "x"})

        assert response.status_code == status.HTTP_200_OK
        assert "issue" in response.data["results"]
        assert "workspace" in response.data["results"]
