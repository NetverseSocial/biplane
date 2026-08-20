# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# BIP-59: the external api/v1 issue API returned no plain-text description.
# description_stripped was excluded from IssueSerializer, so a consumer keying on it
# saw an absent key (null to jq / dict.get) while description_html was populated -- a
# well-formed ticket read as empty, twice, by two agents in one hour on 2026-08-13.
#
# These tests DEMONSTRATE the fix rather than assert a mechanism: each fails under the
# pre-fix code (field excluded), so a regression turns them red. The migration backfill
# is pinned by a real 0128->0129 replay in test_bip59_migration_replay.py -- NOT by
# calling the helper with the global app registry, which exercises the runtime model and
# structurally cannot see a historical-model failure (Rowan RC on #77).
from importlib import import_module

import pytest

from plane.api.serializers.issue import IssueSerializer
from plane.db.models import Issue, Project, User, Workspace
from plane.utils import html_processor


_HTML = "<p>Hello <b>world</b> from BIP-59</p>"
_STRIPPED = "Hello world from BIP-59"


def test_migration_strip_matches_the_model_strip_semantics():
    """The 0129 backfill must produce the SAME plain text Issue.save writes, or a
    backfilled row and a model-written row disagree on the same html (Morrow RC 3607).
    The model uses plane.utils.html_processor.strip_tags (HTMLParser, convert_charrefs
    -> entities DECODE); django.utils.html.strip_tags leaves them encoded. The migration
    freezes the model's semantics locally; this pins the frozen copy equal to the source.

    Falsifiable: the entity row below diverges the moment the migration uses django's
    strip_tags (``A&nbsp;B &amp; C`` stays encoded instead of ``A\xa0B & C``).
    """
    mig = import_module("plane.db.migrations.0129_backfill_issue_description_stripped")
    corpus = [
        "<p>A&nbsp;B &amp; C</p>",              # the discriminating entity case
        "<p>Hello <b>world</b></p>",
        "plain text no tags",
        "<div>x</div>&lt;not a tag&gt;",
        "<p>trailing</p><!-- unclosed comment then words",
    ]
    for html in corpus:
        assert mig._strip_tags(html) == html_processor.strip_tags(html), html


def test_api_serializer_contract_exposes_stripped_not_json():
    """The external contract: stripped is serialized, heavy json is not.

    Falsifiable: re-adding description_stripped to Meta.exclude drops it from
    .fields and this fails -- exactly the pre-BIP-59 state.
    """
    assert "description_stripped" not in IssueSerializer.Meta.exclude
    assert "description_json" in IssueSerializer.Meta.exclude
    field = IssueSerializer().fields.get("description_stripped")
    assert field is not None
    # Derived from description_html on write; exposing it for READ must not make
    # it caller-writable (a supplied value would be silently overwritten by
    # Issue.save) -- read-only is the honest contract (Morrow RC 3591 on #77).
    assert field.read_only is True


@pytest.mark.django_db
def test_api_serializer_returns_populated_stripped_for_html_body():
    """End-to-end: an issue with an HTML body serializes a non-null plain-text field.

    Under the pre-fix serializer the key is absent and `.get` returns None -- the
    silent-empty this ticket is about. Here it must be present and equal to the
    stripped HTML.
    """
    owner = User.objects.create(email="bip59@example.com", first_name="B", last_name="59")
    ws = Workspace.objects.create(name="BIP59 WS", slug="bip59-ws", owner=owner)
    project = Project.objects.create(name="BIP59", identifier="BIP59", workspace=ws)

    issue = Issue.objects.create(
        name="ticket with a real body", workspace=ws, project=project, description_html=_HTML
    )
    # Guard: the model itself populated the column on write (the invariant the API leans on).
    issue.refresh_from_db()
    assert issue.description_stripped == _STRIPPED

    data = IssueSerializer(issue).data
    assert "description_stripped" in data, "external API omitted the plain-text field"
    assert data["description_stripped"] == _STRIPPED
    assert data["description_stripped"] is not None
    # The heavy ProseMirror JSON stays out of the external payload.
    assert "description_json" not in data
