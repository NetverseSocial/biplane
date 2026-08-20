# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An unsafe request that returns an error must keep NO partial writes.

BIP-18, the acceptance bar both reviewers set. This is the regression for the
single line the whole design turns on:

    if self._drf_handled_exception or response.status_code >= 400:
        transaction.set_rollback(True)

WHY THIS FILE EXISTS SEPARATELY

The DB-free dispatch tests pass whether or not that line is present — I removed
it and all 24 stayed green. A mutation that survives means the guard is
unpinned, so the line could be dropped in any future refactor and nothing would
say so. That is exactly how the defects earlier in this ticket happened.

These tests need a real connection: `set_rollback` is a property of a live
transaction, and mocking it would only assert that I called a function I wrote.
`transaction=True` so the test does not itself run inside the wrapper
transaction that would mask the behaviour under test.
"""

import pytest
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from plane.api.views.base import BaseAPIView
from plane.db.models import User

MARKER = "bip18-rollback-probe@biplane.invalid"


class _WriteThenFailView(BaseAPIView):
    """Writes a row, then RETURNS an error rather than raising one.

    This is the shape that defeats a plain atomic block: nothing propagates, so
    the context manager sees an ordinary return and commits.
    """

    authentication_classes = []
    permission_classes = []

    status_to_return = 400

    def post(self, request, *args, **kwargs):
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        return Response({"error": "no"}, status=self.status_to_return)


class _WriteThenRaiseView(_WriteThenFailView):
    def post(self, request, *args, **kwargs):
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        raise ValueError("boom")


class _WriteAndSucceedView(_WriteThenFailView):
    def post(self, request, *args, **kwargs):
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        return Response({"ok": True}, status=201)


def _post(view_cls):
    request = APIRequestFactory().post("/api/v1/probe/", {}, format="json")
    return view_cls.as_view()(request)


def probe_rows():
    return User.objects.filter(email=MARKER).count()


@pytest.fixture(autouse=True)
def _clean():
    User.objects.filter(email=MARKER).delete()
    yield
    User.objects.filter(email=MARKER).delete()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status_code", [400, 403, 404, 409, 422, 500])
def test_a_handled_error_response_keeps_no_partial_write(status_code):
    """The core rule, across the range of error statuses a handler can return."""

    class View(_WriteThenFailView):
        status_to_return = status_code

    response = _post(View)

    assert response.status_code == status_code
    assert probe_rows() == 0, f"a {status_code} response left its write committed"


@pytest.mark.django_db(transaction=True)
def test_a_raised_exception_also_keeps_no_partial_write():
    """DRF maps this to a 500. It must roll back for the same reason."""
    _post(_WriteThenRaiseView)
    assert probe_rows() == 0


@pytest.mark.django_db(transaction=True)
def test_a_SUCCESSFUL_mutation_still_commits():
    """The other half, and the one that makes the tests above meaningful.

    Without this, marking every transaction rollback-only would satisfy every
    assertion above while breaking the entire API.
    """
    response = _post(_WriteAndSucceedView)

    assert response.status_code == 201
    assert probe_rows() == 1, "a successful mutation must commit"


# ---------------------------------------------------------------------------
# BOTH token bases (Morrow, PR 22 gate): the boundary promises BaseAPIView AND
# BaseViewSet. Until this block, only BaseAPIView had the real-DB regression —
# removing MutationDispatchMixin from BaseViewSet left every load-bearing
# rollback test green. Each base gets its own kill.
# ---------------------------------------------------------------------------

from plane.api.views.base import BaseViewSet


class _VSWriteThenFail(BaseViewSet):
    authentication_classes = []
    permission_classes = []
    throttle_classes = []
    status_to_return = 400

    def get_queryset(self):  # pragma: no cover - not used
        return User.objects.none()

    def create(self, request, *args, **kwargs):
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        return Response({"error": "no"}, status=self.status_to_return)


class _VSWriteAndSucceed(_VSWriteThenFail):
    def create(self, request, *args, **kwargs):
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        return Response({"ok": True}, status=201)


def _post_viewset(view_cls):
    request = APIRequestFactory().post("/api/v1/probe/", {}, format="json")
    return view_cls.as_view({"post": "create"})(request)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status_code", [400, 409, 500])
def test_a_viewset_error_response_keeps_no_partial_write(status_code):
    # POST routes to create() via the router mapping — the exact path a real
    # ViewSet request takes, so a BaseViewSet that loses the mixin fails HERE.
    class View(_VSWriteThenFail):
        status_to_return = status_code

    response = _post_viewset(View)
    assert response.status_code == status_code
    assert probe_rows() == 0, f"a ViewSet {status_code} response left its write committed"


@pytest.mark.django_db(transaction=True)
def test_a_viewset_successful_mutation_still_commits():
    response = _post_viewset(_VSWriteAndSucceed)
    assert response.status_code == 201
    assert probe_rows() == 1, "a successful ViewSet mutation must commit"


# ---------------------------------------------------------------------------
# The handled-exception branch, independently of status (Morrow, PR 22 gate).
# Every test above maps raises to >=400, so `status >= 400` alone satisfies
# them and deleting the _drf_handled_exception clause stays green. The mixin
# promises: DRF can map a handled exception to a 2xx, and that STILL rolls
# back — a write made before the exception is not a write anyone chose.
# ---------------------------------------------------------------------------


class _WriteRaiseButRespond200(_WriteThenFailView):
    def post(self, request, *args, **kwargs):
        User.objects.create(email=MARKER, username=MARKER, display_name="probe")
        raise ValueError("boom after the write")

    def handle_exception(self, exc):
        # A swallowing handler: the caller sees success-shaped output. The
        # flag recorded by MutationDispatchMixin.handle_exception must still
        # mark the transaction rollback-only.
        super().handle_exception(exc)  # records _drf_handled_exception
        return Response({"ok": "smoothed over"}, status=200)


@pytest.mark.django_db(transaction=True)
def test_a_handled_exception_rolls_back_even_when_the_response_is_2xx():
    response = _post(_WriteRaiseButRespond200)
    assert response.status_code == 200
    assert probe_rows() == 0, (
        "a handled exception with a 2xx response kept its write — the "
        "_drf_handled_exception branch is not firing"
    )
