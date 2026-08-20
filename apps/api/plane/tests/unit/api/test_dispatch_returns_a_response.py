# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""dispatch() must return the handled RESPONSE, never the exception.

BIP-18, found by Morrow. `BaseAPIView.dispatch` computed the handled response
and then returned `exc` instead:

    except Exception as exc:
        response = self.handle_exception(exc)
        return exc              # <- the exception object

Django expects an HttpResponse out of dispatch, so every handled exception on
the token API produced a confusing failure rather than the error response DRF
had already built.

`BaseViewSet.dispatch`, forty lines further down the same file, returns
`response` correctly. That asymmetry is why it survived review: the two blocks
look identical at a glance and only one is wrong.

It also blocks the BIP-18 transaction boundary outright. Both reviewers'
designs decide whether to roll back by inspecting the finalized response
status, which is impossible if dispatch hands back something that is not a
response.
"""

from unittest.mock import patch

import pytest
from rest_framework.response import Response

from rest_framework.views import APIView

from plane.api.views.base import BaseAPIView, BaseViewSet

BASES = [
    pytest.param(BaseAPIView, id="BaseAPIView"),
    pytest.param(BaseViewSet, id="BaseViewSet"),
]


class FakeRequest:
    """A safe-method request: exercises the response path without a database.

    GET deliberately, so these stay DB-free. The transaction boundary applies
    only to POST/PUT/PATCH/DELETE and is covered separately — the rollback
    behaviour needs a real connection and belongs in a DB-backed regression.
    """

    method = "GET"

    def get_full_path(self):
        return "/api/v1/probe/"


class Boom(Exception):
    """Any exception the view layer handles rather than letting escape."""


@pytest.mark.parametrize("base", BASES)
def test_dispatch_returns_the_handled_response_not_the_exception(base):
    view = base()
    handled = Response({"error": "handled"}, status=400)

    with patch.object(APIView, "dispatch", side_effect=Boom("boom")):
        with patch.object(view, "handle_exception", return_value=handled) as handler:
            result = view.dispatch(FakeRequest())

    handler.assert_called_once()
    assert result is handled, "dispatch returned something other than the handled response"
    assert not isinstance(result, Exception), "dispatch returned the exception object"


@pytest.mark.parametrize("base", BASES)
def test_the_handled_response_keeps_its_status(base):
    # Without this, returning any Response at all would pass the test above and
    # the rollback decision would still be made on the wrong status.
    view = base()
    handled = Response({"error": "nope"}, status=409)

    with patch.object(APIView, "dispatch", side_effect=Boom("boom")):
        with patch.object(view, "handle_exception", return_value=handled):
            result = view.dispatch(FakeRequest())

    assert result.status_code == 409


@pytest.mark.parametrize("base", BASES)
def test_a_clean_request_passes_its_response_straight_through(base):
    # The happy path must be untouched: this fix is about the except branch only.
    view = base()
    ok = Response({"ok": True}, status=200)

    with patch.object(APIView, "dispatch", return_value=ok):
        result = view.dispatch(FakeRequest())

    assert result is ok
