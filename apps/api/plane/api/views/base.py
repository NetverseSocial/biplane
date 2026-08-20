# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import zoneinfo
import logging

# Django imports
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.urls import resolve
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.exceptions import APIException
from rest_framework.generics import GenericAPIView

# Module imports
from plane.db.models.api import APIToken
from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle, ServiceTokenRateThrottle
from plane.utils.exception_logger import log_exception
from plane.utils.paginator import BasePaginator
from plane.utils.core.mixins import ReadReplicaControlMixin


logger = logging.getLogger("plane.api")


class TimezoneMixin:
    """
    This enables timezone conversion according
    to the user set timezone
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.user.is_authenticated:
            timezone.activate(zoneinfo.ZoneInfo(request.user.user_timezone))
        else:
            timezone.deactivate()


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def dispatch_after_commit(task, *args, **kwargs):
    """Register task.delay(*args, **kwargs) to run AFTER the request's
    transaction commits — the ONLY way a token-API handler may dispatch
    async work (BIP-18, Morrow's option-(a) ruling; enforced by an AST gate
    in the test suite).

    Why it exists: the mutation boundary wraps every unsafe request in one
    transaction, so an immediate .delay() would hand the worker a row that
    is not committed yet (race: absent/stale reads) and would survive a
    rollback (a task for a mutation that never happened). Registering on
    commit restores the visibility order autocommit used to give call sites.

    Arguments are evaluated HERE, eagerly, before registration — a bare
    lambda over caller locals would capture variables that may change before
    commit. robust=True: one failing callback must neither turn a committed
    write into a 500 nor suppress later callbacks.

    What this does and does not fix: it restores PRE-COMMIT VISIBILITY order
    (the worker never sees an uncommitted row) and ROLLBACK EMISSION (a
    rolled-back mutation dispatches nothing). It is NOT durability — a broker
    outage after commit still loses the dispatch until the audit outbox
    worker lands (PR 23 onward).

    Refuses outside a transaction (Morrow): Django's on_commit runs the
    callback IMMEDIATELY when no atomic block is open, which silently
    recreates the exact false-deferral trap this helper exists to eliminate
    — e.g. a future caller outside the base boundary. Same fail-loud rule as
    enqueue_audit; the name stays truthful."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "dispatch_after_commit called outside a transaction: on_commit "
            "would run the callback immediately (false deferral). Token-API "
            "handlers run inside MutationDispatchMixin's boundary — a caller "
            "hitting this is outside it."
        )
    transaction.on_commit(lambda: task.delay(*args, **kwargs), robust=True)


class MutationDispatchMixin:
    """One transaction boundary for every unsafe request on the token API.

    BIP-18. Both reviewers ruled the same boundary: not per-call-site atomic
    blocks, and not global ATOMIC_REQUESTS — one shared mixin used by both
    token bases, wrapping only POST/PUT/PATCH/DELETE.

    WHY A PLAIN `with transaction.atomic()` IS NOT ENOUGH

    DRF catches exceptions inside its own dispatch and RETURNS an error
    response. The atomic block therefore sees an ordinary return and commits.
    Witnessed against a live database rather than assumed:

        write a row, return a handled 400  ->  the row was COMMITTED
        the same, plus set_rollback(True)  ->  rolled back

    So an error response must explicitly mark the transaction rollback-only.
    The product rule this states: an unsafe token-API request that returns an
    error does not keep partial writes.

    WHY THE MARK IS AT THE END, NOT INSIDE handle_exception

    DRF may still finalize the response after handling, and code must not query
    a connection already marked rollback-only. So handle_exception only records
    that it ran; the rollback is marked after the finalized response is in hand
    and before the atomic block exits. An exception that escapes dispatch
    entirely unwinds the block on its own and needs no help.

    Both conditions matter and neither implies the other: DRF can map a handled
    exception to a 2xx, and a handler can return 4xx without any exception.

    This also consolidates the two near-identical dispatch bodies that used to
    live in BaseAPIView and BaseViewSet. That duplication was not harmless —
    one of them returned the exception object instead of the handled response,
    and the other did not. Consolidating removes the branch rather than
    preserving it.
    """

    def handle_exception(self, exc):
        # Record only. See the docstring for why the rollback is not marked here.
        self._drf_handled_exception = True
        return super().handle_exception(exc)

    def _dispatch_inner(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
            if settings.DEBUG:
                from django.db import connection

                print(f"{request.method} - {request.get_full_path()} of Queries: {len(connection.queries)}")
            return response
        except Exception as exc:
            # Reached only for exceptions raised outside DRF's own try block.
            # Return the HANDLED RESPONSE — returning `exc` gives Django
            # something that is not a response at all.
            return self.handle_exception(exc)

    def dispatch(self, request, *args, **kwargs):
        self._drf_handled_exception = False

        if request.method not in UNSAFE_METHODS:
            return self._dispatch_inner(request, *args, **kwargs)

        with transaction.atomic():
            response = self._dispatch_inner(request, *args, **kwargs)
            if self._drf_handled_exception or getattr(response, "status_code", 200) >= 400:
                transaction.set_rollback(True)
            return response


class BaseAPIView(MutationDispatchMixin, TimezoneMixin, GenericAPIView, ReadReplicaControlMixin, BasePaginator):
    authentication_classes = [APIKeyAuthentication]

    permission_classes = [IsAuthenticated]

    use_read_replica = False

    def filter_queryset(self, queryset):
        for backend in list(self.filter_backends):
            queryset = backend().filter_queryset(self.request, queryset, self)
        return queryset

    def get_throttles(self):
        throttle_classes = []
        api_key = self.request.headers.get("X-Api-Key")

        if api_key:
            service_token = APIToken.objects.filter(token=api_key, is_service=True).first()

            if service_token:
                throttle_classes.append(ServiceTokenRateThrottle())
                return throttle_classes

        throttle_classes.append(ApiKeyRateThrottle())

        return throttle_classes

    def handle_exception(self, exc):
        """
        Handle any exception that occurs, by returning an appropriate response,
        or re-raising the error.
        """
        try:
            response = super().handle_exception(exc)
            return response
        except Exception as e:
            if isinstance(e, IntegrityError):
                return Response(
                    {"error": "The payload is not valid"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if isinstance(e, ValidationError):
                return Response(
                    {"error": "Please provide valid detail"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if isinstance(e, ObjectDoesNotExist):
                return Response(
                    {"error": "The requested resource does not exist."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if isinstance(e, KeyError):
                return Response(
                    {"error": "The required key does not exist."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            log_exception(e)
            return Response(
                {"error": "Something went wrong please try again later"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def finalize_response(self, request, response, *args, **kwargs):
        # Call super to get the default response
        response = super().finalize_response(request, response, *args, **kwargs)

        # Add custom headers if they exist in the request META
        ratelimit_remaining = request.META.get("X-RateLimit-Remaining")
        if ratelimit_remaining is not None:
            response["X-RateLimit-Remaining"] = ratelimit_remaining

        ratelimit_reset = request.META.get("X-RateLimit-Reset")
        if ratelimit_reset is not None:
            response["X-RateLimit-Reset"] = ratelimit_reset

        return response

    @property
    def workspace_slug(self):
        return self.kwargs.get("slug", None)

    @property
    def project_id(self):
        project_id = self.kwargs.get("project_id", None)
        if project_id:
            return project_id

        if resolve(self.request.path_info).url_name == "project":
            return self.kwargs.get("pk", None)

    @property
    def fields(self):
        fields = [field for field in self.request.GET.get("fields", "").split(",") if field]
        return fields if fields else None

    @property
    def expand(self):
        expand = [expand for expand in self.request.GET.get("expand", "").split(",") if expand]
        return expand if expand else None


class BaseViewSet(MutationDispatchMixin, TimezoneMixin, ReadReplicaControlMixin, ModelViewSet, BasePaginator):
    model = None

    authentication_classes = [APIKeyAuthentication]
    permission_classes = [
        IsAuthenticated,
    ]
    use_read_replica = False

    def get_queryset(self):
        try:
            return self.model.objects.all()
        except Exception as e:
            log_exception(e)
            raise APIException("Please check the view", status.HTTP_400_BAD_REQUEST)

    def handle_exception(self, exc):
        """
        Handle any exception that occurs, by returning an appropriate response,
        or re-raising the error.
        """
        try:
            response = super().handle_exception(exc)
            return response
        except Exception as e:
            if isinstance(e, IntegrityError):
                log_exception(e)
                return Response(
                    {"error": "The payload is not valid"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if isinstance(e, ValidationError):
                logger.warning(
                    "Validation Error",
                    extra={
                        "error_code": "VALIDATION_ERROR",
                        "error_message": str(e),
                    },
                )
                return Response(
                    {"error": "Please provide valid detail"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if isinstance(e, ObjectDoesNotExist):
                logger.warning(
                    "Object Does Not Exist",
                    extra={
                        "error_code": "OBJECT_DOES_NOT_EXIST",
                        "error_message": str(e),
                    },
                )
                return Response(
                    {"error": "The required object does not exist."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if isinstance(e, KeyError):
                logger.error(
                    "Key Error",
                    extra={
                        "error_code": "KEY_ERROR",
                        "error_message": str(e),
                    },
                )
                return Response(
                    {"error": "The required key does not exist."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            log_exception(e)
            return Response(
                {"error": "Something went wrong please try again later"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @property
    def workspace_slug(self):
        return self.kwargs.get("slug", None)

    @property
    def project_id(self):
        project_id = self.kwargs.get("project_id", None)
        if project_id:
            return project_id

        if resolve(self.request.path_info).url_name == "project":
            return self.kwargs.get("pk", None)

    @property
    def fields(self):
        fields = [field for field in self.request.GET.get("fields", "").split(",") if field]
        return fields if fields else None

    @property
    def expand(self):
        expand = [expand for expand in self.request.GET.get("expand", "").split(",") if expand]
        return expand if expand else None
