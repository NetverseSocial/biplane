# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# BIP-62: SearchEndpoint (entity-search) is the twin of BIP-58's GlobalSearchEndpoint,
# four hundred lines below it in the same file. It validates query_type and count at
# the endpoint instead of silently mishandling them. Three faces the pre-fix code had:
#   query_type=work_item          -> 200 {}            (silent drop; no terminal else)
#   query_type=project,work_item  -> 200 {"project":..} (half-success)
#   count=abc                     -> 500                (unguarded int())
#
# These are ENDPOINT-level tests on purpose. A resolver-only test that asserts the raise
# would still pass if nobody caught it — an uncaught raise is a 500. Asserting the 400
# here proves the raise REACHES the caller and is rendered, not that it merely fires.
import pytest
from django.urls import reverse
from rest_framework import status


@pytest.mark.contract
class TestSearchEndpointValidation:
    """query_type/count validated at the endpoint. The 400 cases reject before any
    queryset is built, so no workspace fixture is needed; BaseAPIView gates on
    IsAuthenticated, which session_client satisfies."""

    @pytest.mark.django_db
    def test_unknown_query_type_is_rejected(self, session_client):
        url = reverse("entity-search", kwargs={"slug": "any-workspace"})
        response = session_client.get(url, {"query": "x", "query_type": "work_item"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["unknown_query_types"] == ["work_item"]
        # Actionable: the caller is told what it may send.
        assert "user_mention" in response.data["valid_query_types"]

    @pytest.mark.django_db
    def test_partially_valid_query_type_does_not_half_succeed(self, session_client):
        """The shape BIP-58's contract rejects, live in this endpoint until now:
        a partially-valid request must not return a 200 missing the bad key."""
        url = reverse("entity-search", kwargs={"slug": "any-workspace"})
        response = session_client.get(url, {"query": "x", "query_type": "project,work_item"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["unknown_query_types"] == ["work_item"]

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "count_value,expected",
        [
            ("abc", status.HTTP_400_BAD_REQUEST),          # non-numeric
            ("", status.HTTP_400_BAD_REQUEST),             # empty -> int("") ValueError
            ("-1", status.HTTP_400_BAD_REQUEST),           # negative -> QuerySet[:-1] would 500
            ("0", status.HTTP_200_OK),                     # zero -> empty slice, not a 500
            ("9" * 40, status.HTTP_200_OK),                # absurdly large -> clamped, not a LIMIT overflow
        ],
    )
    def test_count_never_reaches_a_500(self, session_client, count_value, expected):
        """The PROPERTY, not one input (Aria RC 3631): no caller-supplied count
        reaches a 500. `test_non_numeric_count` pinned one input; the class is
        every way a count can break — non-numeric, empty, negative, zero, absurdly
        large. Non-numeric/empty/negative are actionable 400s; zero returns 200 via
        an empty slice; a huge count returns 200 via a clamp that cannot overflow
        the SQL LIMIT. query_type is valid so each reaches the count path."""
        url = reverse("entity-search", kwargs={"slug": "any-workspace"})
        response = session_client.get(
            url, {"query": "x", "query_type": "user_mention", "count": count_value}
        )
        assert response.status_code != 500, f"count={count_value!r} 500'd"
        assert response.status_code == expected

    @pytest.mark.django_db
    def test_valid_query_type_succeeds(self, session_client):
        """A recognised type returns 200 (empty results against a slug with no
        rows) — the fix rejects the unknown without narrowing the valid path."""
        url = reverse("entity-search", kwargs={"slug": "any-workspace"})
        response = session_client.get(url, {"query": "x", "query_type": "user_mention"})
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_explicit_empty_query_type_returns_empty_not_all_types(self, session_client):
        """An explicit empty query_type (query_type= — a live web caller serialises
        an empty selection array to exactly this) means "no types selected" ->
        empty result, preserving pre-BIP-62 behaviour. It must NOT expand to all
        six search branches, which is what inheriting the resolver's empty->all
        contract did (Morrow RC 3631)."""
        url = reverse("entity-search", kwargs={"slug": "any-workspace"})
        response = session_client.get(url, {"query": "x", "query_type": ""})
        assert response.status_code == status.HTTP_200_OK
        # No types selected -> no result keys; must not have fanned out to all six.
        assert response.data == {}
