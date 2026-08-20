# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import re

# Django imports
from django.db import models
from django.db.models import (
    Q,
    OuterRef,
    Subquery,
    Value,
    UUIDField,
    CharField,
    When,
    Case,
)
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.views.base import BaseAPIView
from plane.db.models import (
    Workspace,
    Project,
    Issue,
    Cycle,
    Module,
    Page,
    IssueView,
    ProjectMember,
    ProjectPage,
    WorkspaceMember,
)


class UnknownEntitiesError(ValueError):
    """A search param named entities outside the valid set (BIP-62).

    The resolver RAISES this instead of returning the unknown names beside the
    valid ones. A two-value ``(requested, unknown)`` return made rejection a
    branch every caller had to remember; a contract enforced by a docstring
    holds only until a second caller reads the signature instead of the prose,
    and BIP-62 adds that second caller. There is no longer a return path that
    hands back unknown names for a caller to ignore — the structure has no
    silent-drop, rather than a comment warning against one. Carries the unknown
    names and the valid set so each endpoint can render its own 400 body; they
    may word it differently but cannot differ on whether it is an error.
    """

    def __init__(self, unknown, valid):
        self.unknown = list(unknown)
        self.valid = list(valid)
        super().__init__(f"Unknown entities requested: {self.unknown}")


def resolve_requested_entities(entities_param, valid_entities):
    """Split a search param into the entity names to search, validated.

    Returns the requested names (a list). An absent or empty parameter means
    "search everything" — the long-standing default, deliberately unchanged.
    Raises ``UnknownEntitiesError`` if any name is not in ``valid_entities``: an
    unrecognised name is an error, never a name to filter away, because a
    filtered 200 is indistinguishable from "searched it, found nothing" (the
    failure and the empty success become the same response). The rename is live
    — ``plane/api/urls/work_item.py`` serves both spellings — so a caller
    carrying the new vocabulary into this param is a realistic case.
    """
    valid = list(valid_entities)
    if not entities_param:
        return valid
    requested = [name.strip() for name in entities_param.split(",") if name.strip()]
    unknown = [name for name in requested if name not in valid]
    if unknown:
        raise UnknownEntitiesError(unknown, valid)
    return requested


# The query_type values SearchEndpoint dispatches (its if/elif branches below).
# A distinct vocabulary from GlobalSearchEndpoint's MODELS_MAPPER — same family
# of defect, different valid set.
SEARCH_QUERY_TYPES = ("user_mention", "project", "issue", "cycle", "module", "page")

# The SQL LIMIT is a signed bigint; a count above this overflows and 500s.
# Clamp so an absurd count returns everything rather than erroring.
_PG_BIGINT_MAX = 9223372036854775807


class GlobalSearchEndpoint(BaseAPIView):
    """Endpoint to search across multiple fields in the workspace and
    also show related workspace if found
    """

    def filter_workspaces(self, query, _slug, _project_id, _workspace_search):
        fields = ["name"]
        q = Q()
        if query:
            for field in fields:
                q |= Q(**{f"{field}__icontains": query})
        return (
            Workspace.objects.filter(q, workspace_member__member=self.request.user)
            .order_by("-created_at")
            .distinct()
            .values("name", "id", "slug")
        )

    def filter_projects(self, query, slug, _project_id, _workspace_search):
        fields = ["name", "identifier"]
        q = Q()
        if query:
            for field in fields:
                q |= Q(**{f"{field}__icontains": query})
        return (
            Project.objects.filter(
                q,
                project_projectmember__member=self.request.user,
                project_projectmember__is_active=True,
                archived_at__isnull=True,
                workspace__slug=slug,
            )
            .order_by("-created_at")
            .distinct()
            .values("name", "id", "identifier", "workspace__slug")
        )

    def filter_issues(self, query, slug, project_id, workspace_search):
        fields = ["name", "sequence_id", "project__identifier"]
        q = Q()
        if query:
            for field in fields:
                if field == "sequence_id":
                    # Match whole integers only (exclude decimal numbers)
                    sequences = re.findall(r"\b\d+\b", query)
                    for sequence_id in sequences:
                        q |= Q(**{"sequence_id": sequence_id})
                else:
                    q |= Q(**{f"{field}__icontains": query})

        issues = Issue.issue_objects.filter(
            q,
            project__project_projectmember__member=self.request.user,
            project__project_projectmember__is_active=True,
            project__archived_at__isnull=True,
            workspace__slug=slug,
        )

        if workspace_search == "false" and project_id:
            issues = issues.filter(project_id=project_id)

        return issues.distinct().values(
            "name",
            "id",
            "sequence_id",
            "project__identifier",
            "project_id",
            "workspace__slug",
        )[:100]

    def filter_cycles(self, query, slug, project_id, workspace_search):
        fields = ["name"]
        q = Q()
        if query:
            for field in fields:
                q |= Q(**{f"{field}__icontains": query})

        cycles = Cycle.objects.filter(
            q,
            project__project_projectmember__member=self.request.user,
            project__project_projectmember__is_active=True,
            project__archived_at__isnull=True,
            workspace__slug=slug,
        )

        if workspace_search == "false" and project_id:
            cycles = cycles.filter(project_id=project_id)

        return (
            cycles.order_by("-created_at")
            .distinct()
            .values("name", "id", "project_id", "project__identifier", "workspace__slug")
        )

    def filter_modules(self, query, slug, project_id, workspace_search):
        fields = ["name"]
        q = Q()
        if query:
            for field in fields:
                q |= Q(**{f"{field}__icontains": query})

        modules = Module.objects.filter(
            q,
            project__project_projectmember__member=self.request.user,
            project__project_projectmember__is_active=True,
            project__archived_at__isnull=True,
            workspace__slug=slug,
        )

        if workspace_search == "false" and project_id:
            modules = modules.filter(project_id=project_id)

        return (
            modules.order_by("-created_at")
            .distinct()
            .values("name", "id", "project_id", "project__identifier", "workspace__slug")
        )

    def filter_pages(self, query, slug, project_id, workspace_search):
        fields = ["name"]
        q = Q()
        if query:
            for field in fields:
                q |= Q(**{f"{field}__icontains": query})

        pages = (
            Page.objects.filter(
                q,
                projects__project_projectmember__member=self.request.user,
                projects__project_projectmember__is_active=True,
                projects__archived_at__isnull=True,
                workspace__slug=slug,
            )
            .annotate(
                project_ids=Coalesce(
                    ArrayAgg("projects__id", distinct=True, filter=~Q(projects__id=True)),
                    Value([], output_field=ArrayField(UUIDField())),
                )
            )
            .annotate(
                project_identifiers=Coalesce(
                    ArrayAgg(
                        "projects__identifier",
                        distinct=True,
                        filter=~Q(projects__id=True),
                    ),
                    Value([], output_field=ArrayField(CharField())),
                )
            )
        )

        if workspace_search == "false" and project_id:
            project_subquery = ProjectPage.objects.filter(page_id=OuterRef("id"), project_id=project_id).values_list(
                "project_id", flat=True
            )[:1]

            pages = pages.annotate(project_id=Subquery(project_subquery)).filter(project_id=project_id)

        return (
            pages.order_by("-created_at")
            .distinct()
            .values("name", "id", "project_ids", "project_identifiers", "workspace__slug")
        )

    def filter_views(self, query, slug, project_id, workspace_search):
        fields = ["name"]
        q = Q()
        if query:
            for field in fields:
                q |= Q(**{f"{field}__icontains": query})

        issue_views = IssueView.objects.filter(
            q,
            project__project_projectmember__member=self.request.user,
            project__project_projectmember__is_active=True,
            project__archived_at__isnull=True,
            workspace__slug=slug,
        )

        if workspace_search == "false" and project_id:
            issue_views = issue_views.filter(project_id=project_id)

        return (
            issue_views.order_by("-created_at")
            .distinct()
            .values("name", "id", "project_id", "project__identifier", "workspace__slug")
        )

    def filter_intakes(self, query, slug, project_id, workspace_search):
        fields = ["name", "sequence_id", "project__identifier"]
        q = Q()
        if query:
            for field in fields:
                if field == "sequence_id":
                    # Match whole integers only (exclude decimal numbers)
                    sequences = re.findall(r"\b\d+\b", query)
                    for sequence_id in sequences:
                        q |= Q(**{"sequence_id": sequence_id})
                else:
                    q |= Q(**{f"{field}__icontains": query})

        issues = Issue.objects.filter(
            q,
            project__project_projectmember__member=self.request.user,
            project__project_projectmember__is_active=True,
            project__archived_at__isnull=True,
            workspace__slug=slug,
        ).filter(models.Q(issue_intake__status=0) | models.Q(issue_intake__status=-2))

        if workspace_search == "false" and project_id:
            issues = issues.filter(project_id=project_id)

        return (
            issues.order_by("-created_at")
            .distinct()
            .values(
                "name",
                "id",
                "sequence_id",
                "project__identifier",
                "project_id",
                "workspace__slug",
            )[:100]
        )

    def get(self, request, slug):
        query = request.query_params.get("search", False)
        entities_param = request.query_params.get("entities")
        workspace_search = request.query_params.get("workspace_search", "false")
        project_id = request.query_params.get("project_id", False)

        MODELS_MAPPER = {
            "workspace": self.filter_workspaces,
            "project": self.filter_projects,
            "issue": self.filter_issues,
            "cycle": self.filter_cycles,
            "module": self.filter_modules,
            "issue_view": self.filter_views,
            "page": self.filter_pages,
            "intake": self.filter_intakes,
        }

        # Determine which entities to search. An unrecognised name is an error,
        # not something to filter away — see resolve_requested_entities.
        try:
            requested_entities = resolve_requested_entities(entities_param, MODELS_MAPPER.keys())
        except UnknownEntitiesError as exc:
            return Response(
                {
                    "error": "Unknown entity requested.",
                    "unknown_entities": exc.unknown,
                    "valid_entities": exc.valid,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = {}

        # Every name is known by here, so no per-entity guard: the lookup cannot
        # miss, and a guard would restate what the check above already promised.
        for entity in requested_entities:
            results[entity] = MODELS_MAPPER[entity](query or None, slug, project_id, workspace_search)

        return Response({"results": results}, status=status.HTTP_200_OK)


class SearchEndpoint(BaseAPIView):
    def get(self, request, slug):
        query = request.query_params.get("query", False)
        # Validate query_type the same way GlobalSearchEndpoint validates entities
        # (BIP-62): an unrecognised type is a 400, not a branch that matches no
        # if/elif and returns a silent, successful, empty nothing.
        #
        # An explicit empty query_type (query_type= -- a live web caller serializes
        # an empty selection array to exactly this) means "no types selected" ->
        # empty result, preserving pre-BIP-62 behaviour. It must NOT expand to all
        # six types: the resolver's empty->all contract is GlobalSearchEndpoint's,
        # where an ABSENT param means everything. Here an absent param already
        # defaults to user_mention, so an explicit empty string is a distinct case,
        # not "search everything" (Morrow RC 3631).
        raw_query_type = request.query_params.get("query_type", "user_mention")
        if raw_query_type == "":
            query_types = []
        else:
            try:
                query_types = resolve_requested_entities(raw_query_type, SEARCH_QUERY_TYPES)
            except UnknownEntitiesError as exc:
                return Response(
                    {
                        "error": "Unknown query_type requested.",
                        "unknown_query_types": exc.unknown,
                        "valid_query_types": exc.valid,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        # count must be a NON-NEGATIVE integer. int() guards a non-numeric value
        # (would 500); a negative value would reach QuerySet[:count] and raise
        # "Negative indexing is not supported" -> 500 (Morrow RC 3631). Both are
        # 400s with an actionable body, before any queryset is built.
        raw_count = request.query_params.get("count", 5)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return Response(
                {"error": "count must be an integer.", "count": raw_count},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if count < 0:
            return Response(
                {"error": "count must not be negative.", "count": raw_count},
                status=status.HTTP_400_BAD_REQUEST,
            )
        count = min(count, _PG_BIGINT_MAX)
        project_id = request.query_params.get("project_id", None)

        response_data = {}

        if project_id:
            for query_type in query_types:
                if query_type == "user_mention":
                    fields = [
                        "member__first_name",
                        "member__last_name",
                        "member__display_name",
                    ]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    users = (
                        ProjectMember.objects.filter(
                            q,
                            is_active=True,
                            workspace__slug=slug,
                            member__is_bot=False,
                            project_id=project_id,
                        )
                        .annotate(
                            member__avatar_url=Case(
                                When(
                                    member__avatar_asset__isnull=False,
                                    then=Concat(
                                        Value("/api/assets/v2/static/"),
                                        "member__avatar_asset",
                                        Value("/"),
                                    ),
                                ),
                                When(
                                    member__avatar_asset__isnull=True,
                                    then="member__avatar",
                                ),
                                default=Value(None),
                                output_field=CharField(),
                            )
                        )
                        .order_by("-created_at")
                    )

                    users = users.distinct().values(
                        "member__avatar_url",
                        "member__display_name",
                        "member__id",
                    )

                    response_data["user_mention"] = list(users[:count])

                elif query_type == "project":
                    fields = ["name", "identifier"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})
                    projects = (
                        Project.objects.filter(
                            q,
                            Q(project_projectmember__member=self.request.user) | Q(network=2),
                            workspace__slug=slug,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values("name", "id", "identifier", "logo_props", "workspace__slug")[:count]
                    )
                    response_data["project"] = list(projects)

                elif query_type == "issue":
                    fields = ["name", "sequence_id", "project__identifier"]
                    q = Q()

                    if query:
                        for field in fields:
                            if field == "sequence_id":
                                sequences = re.findall(r"\b\d+\b", query)
                                for sequence_id in sequences:
                                    q |= Q(**{"sequence_id": sequence_id})
                            else:
                                q |= Q(**{f"{field}__icontains": query})

                    issues = (
                        Issue.issue_objects.filter(
                            q,
                            project__project_projectmember__member=self.request.user,
                            project__project_projectmember__is_active=True,
                            workspace__slug=slug,
                            project_id=project_id,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "sequence_id",
                            "project__identifier",
                            "project_id",
                            "priority",
                            "state_id",
                            "type_id",
                        )[:count]
                    )
                    response_data["issue"] = list(issues)

                elif query_type == "cycle":
                    fields = ["name"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    cycles = (
                        Cycle.objects.filter(
                            q,
                            project__project_projectmember__member=self.request.user,
                            project__project_projectmember__is_active=True,
                            workspace__slug=slug,
                            project_id=project_id,
                        )
                        .annotate(
                            status=Case(
                                When(
                                    Q(start_date__lte=timezone.now()) & Q(end_date__gte=timezone.now()),
                                    then=Value("CURRENT"),
                                ),
                                When(
                                    start_date__gt=timezone.now(),
                                    then=Value("UPCOMING"),
                                ),
                                When(end_date__lt=timezone.now(), then=Value("COMPLETED")),
                                When(
                                    Q(start_date__isnull=True) & Q(end_date__isnull=True),
                                    then=Value("DRAFT"),
                                ),
                                default=Value("DRAFT"),
                                output_field=CharField(),
                            )
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "project_id",
                            "project__identifier",
                            "status",
                            "workspace__slug",
                        )[:count]
                    )
                    response_data["cycle"] = list(cycles)

                elif query_type == "module":
                    fields = ["name"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    modules = (
                        Module.objects.filter(
                            q,
                            project__project_projectmember__member=self.request.user,
                            project__project_projectmember__is_active=True,
                            workspace__slug=slug,
                            project_id=project_id,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "project_id",
                            "project__identifier",
                            "status",
                            "workspace__slug",
                        )[:count]
                    )
                    response_data["module"] = list(modules)

                elif query_type == "page":
                    fields = ["name"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    pages = (
                        Page.objects.filter(
                            q,
                            projects__project_projectmember__member=self.request.user,
                            projects__project_projectmember__is_active=True,
                            projects__id=project_id,
                            workspace__slug=slug,
                            access=0,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "logo_props",
                            "projects__id",
                            "workspace__slug",
                        )[:count]
                    )
                    response_data["page"] = list(pages)
            return Response(response_data, status=status.HTTP_200_OK)

        else:
            for query_type in query_types:
                if query_type == "user_mention":
                    fields = [
                        "member__first_name",
                        "member__last_name",
                        "member__display_name",
                    ]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})
                    users = (
                        WorkspaceMember.objects.filter(
                            q,
                            is_active=True,
                            workspace__slug=slug,
                            member__is_bot=False,
                        )
                        .annotate(
                            member__avatar_url=Case(
                                When(
                                    member__avatar_asset__isnull=False,
                                    then=Concat(
                                        Value("/api/assets/v2/static/"),
                                        "member__avatar_asset",
                                        Value("/"),
                                    ),
                                ),
                                When(
                                    member__avatar_asset__isnull=True,
                                    then="member__avatar",
                                ),
                                default=Value(None),
                                output_field=models.CharField(),
                            )
                        )
                        .order_by("-created_at")
                        .values("member__avatar_url", "member__display_name", "member__id")[:count]
                    )
                    response_data["user_mention"] = list(users)

                elif query_type == "project":
                    fields = ["name", "identifier"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})
                    projects = (
                        Project.objects.filter(
                            q,
                            Q(project_projectmember__member=self.request.user) | Q(network=2),
                            workspace__slug=slug,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values("name", "id", "identifier", "logo_props", "workspace__slug")[:count]
                    )
                    response_data["project"] = list(projects)

                elif query_type == "issue":
                    fields = ["name", "sequence_id", "project__identifier"]
                    q = Q()

                    if query:
                        for field in fields:
                            if field == "sequence_id":
                                sequences = re.findall(r"\b\d+\b", query)
                                for sequence_id in sequences:
                                    q |= Q(**{"sequence_id": sequence_id})
                            else:
                                q |= Q(**{f"{field}__icontains": query})

                    issues = (
                        Issue.issue_objects.filter(
                            q,
                            project__project_projectmember__member=self.request.user,
                            project__project_projectmember__is_active=True,
                            workspace__slug=slug,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "sequence_id",
                            "project__identifier",
                            "project_id",
                            "priority",
                            "state_id",
                            "type_id",
                        )[:count]
                    )
                    response_data["issue"] = list(issues)

                elif query_type == "cycle":
                    fields = ["name"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    cycles = (
                        Cycle.objects.filter(
                            q,
                            project__project_projectmember__member=self.request.user,
                            project__project_projectmember__is_active=True,
                            workspace__slug=slug,
                        )
                        .annotate(
                            status=Case(
                                When(
                                    Q(start_date__lte=timezone.now()) & Q(end_date__gte=timezone.now()),
                                    then=Value("CURRENT"),
                                ),
                                When(
                                    start_date__gt=timezone.now(),
                                    then=Value("UPCOMING"),
                                ),
                                When(end_date__lt=timezone.now(), then=Value("COMPLETED")),
                                When(
                                    Q(start_date__isnull=True) & Q(end_date__isnull=True),
                                    then=Value("DRAFT"),
                                ),
                                default=Value("DRAFT"),
                                output_field=CharField(),
                            )
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "project_id",
                            "project__identifier",
                            "status",
                            "workspace__slug",
                        )[:count]
                    )
                    response_data["cycle"] = list(cycles)

                elif query_type == "module":
                    fields = ["name"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    modules = (
                        Module.objects.filter(
                            q,
                            project__project_projectmember__member=self.request.user,
                            project__project_projectmember__is_active=True,
                            workspace__slug=slug,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "project_id",
                            "project__identifier",
                            "status",
                            "workspace__slug",
                        )[:count]
                    )
                    response_data["module"] = list(modules)

                elif query_type == "page":
                    fields = ["name"]
                    q = Q()

                    if query:
                        for field in fields:
                            q |= Q(**{f"{field}__icontains": query})

                    pages = (
                        Page.objects.filter(
                            q,
                            projects__project_projectmember__member=self.request.user,
                            projects__project_projectmember__is_active=True,
                            workspace__slug=slug,
                            access=0,
                            is_global=True,
                        )
                        .order_by("-created_at")
                        .distinct()
                        .values(
                            "name",
                            "id",
                            "logo_props",
                            "projects__id",
                            "workspace__slug",
                        )[:count]
                    )
                    response_data["page"] = list(pages)
            return Response(response_data, status=status.HTTP_200_OK)
