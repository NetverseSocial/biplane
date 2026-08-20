# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# BIP-59: literal 0128 -> 0129 replay of the description_stripped backfill.
#
# Why a real replay and not a direct helper call: migration 0129 runs against the
# HISTORICAL model from apps.get_model("db","Issue"), whose manager surface differs
# from the runtime model. An earlier test called the helper with the global (runtime)
# registry, so it exercised Issue.objects -- present at runtime, ABSENT on the
# historical model -- and could not see that the migration aborted with
# AttributeError: type object 'Issue' has no attribute 'objects' (Rowan/Morrow RC on
# #77). Only a literal replay through the migration executor exercises the path the
# deploy runs.
#
# RUN CONTRACT (Morrow RC 3348 b3, shared with test_migration_0128_replay): the repo's
# addopts carry --nomigrations, under which django-test-migrations SKIPS the `migrator`
# executor rather than forcing it -- a skip is not evidence. This module is DESELECTED
# from the default run by the `migration_replay` marker and fails CLOSED if selected
# without migrations. Run explicitly:
#
#     pytest -m migration_replay --migrations \
#         plane/tests/unit/bridge/test_bip59_migration_replay.py
import uuid as uuid_lib

import pytest

pytestmark = pytest.mark.migration_replay


@pytest.fixture(autouse=True)
def _require_migrations_enabled(django_db_use_migrations):
    """Fail CLOSED if selected without migrations -- a skipped replay is not a pass."""
    if not django_db_use_migrations:
        pytest.fail(
            "migration replay requires migrations ENABLED (got --nomigrations). "
            "Run: pytest -m migration_replay --migrations "
            "plane/tests/unit/bridge/test_bip59_migration_replay.py",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _restore_migrate_signals():
    """django-test-migrations 1.4.0's mute_migrate_signals lacks try/finally; restore
    pre/post_migrate receivers unconditionally (shared mitigation, Vex source-vet)."""
    from django.db.models.signals import post_migrate, pre_migrate

    saved_pre = list(pre_migrate.receivers)
    saved_post = list(post_migrate.receivers)
    try:
        yield
    finally:
        pre_migrate.receivers = saved_pre
        post_migrate.receivers = saved_post
        pre_migrate.sender_receivers_cache.clear()
        post_migrate.sender_receivers_cache.clear()


def _uniq(prefix):
    return f"{prefix}-{uuid_lib.uuid4().hex[:10]}"


@pytest.mark.django_db
def test_replay_0128_to_0129_backfills_stripped_from_html(migrator):
    """A row with HTML body and NULL description_stripped -- the legacy shape a
    save-bypassing path produced -- is repaired to the stripped plain text by the
    real 0129 backfill. Uses _base_manager throughout: the historical Issue has no
    `objects`, which is exactly the abort this replay exists to catch."""
    old = migrator.apply_initial_migration(("db", "0128_forgejodelivery_semantic_key"))
    User = old.apps.get_model("db", "User")
    Workspace = old.apps.get_model("db", "Workspace")
    Project = old.apps.get_model("db", "Project")
    State = old.apps.get_model("db", "State")
    Issue = old.apps.get_model("db", "Issue")

    u = User._base_manager.create(email=_uniq("bip59") + "@example.com", username=_uniq("u"))
    ws = Workspace._base_manager.create(name="W", slug=_uniq("ws"), owner=u)
    proj = Project._base_manager.create(name="P", identifier="RPL", workspace=ws)
    st = State._base_manager.create(
        name="Todo", project=proj, workspace=ws, group="unstarted", sequence=100, color="#000"
    )
    # Entity-bearing body: the discriminating case for strip semantics (Morrow RC 3607).
    html = "<p>Hello <b>world</b> from BIP-59 &amp; more&nbsp;here</p>"
    issue = Issue._base_manager.create(
        workspace=ws,
        project=proj,
        state=st,
        name="legacy null-stripped row",
        description_html=html,
        description_stripped=None,
    )
    assert issue.description_stripped is None

    new = migrator.apply_tested_migration(("db", "0129_backfill_issue_description_stripped"))

    Issue_after = new.apps.get_model("db", "Issue")
    row = Issue_after._base_manager.get(pk=issue.pk)
    # Must equal what Issue.save would write for the same html -- entities DECODED.
    from plane.utils import html_processor

    assert row.description_stripped == html_processor.strip_tags(html)
    assert row.description_stripped == "Hello world from BIP-59 & more\xa0here"


@pytest.mark.django_db
def test_replay_0129_leaves_genuinely_empty_html_rows_untouched(migrator):
    """The backfill only fires when there is real HTML: a row with the empty-paragraph
    default keeps its NULL stripped (nothing to derive), so an empty ticket is not
    fabricated into a non-empty one."""
    old = migrator.apply_initial_migration(("db", "0128_forgejodelivery_semantic_key"))
    User = old.apps.get_model("db", "User")
    Workspace = old.apps.get_model("db", "Workspace")
    Project = old.apps.get_model("db", "Project")
    State = old.apps.get_model("db", "State")
    Issue = old.apps.get_model("db", "Issue")

    u = User._base_manager.create(email=_uniq("bip59e") + "@example.com", username=_uniq("u"))
    ws = Workspace._base_manager.create(name="W", slug=_uniq("ws"), owner=u)
    proj = Project._base_manager.create(name="P", identifier="RPE", workspace=ws)
    st = State._base_manager.create(
        name="Todo", project=proj, workspace=ws, group="unstarted", sequence=100, color="#000"
    )
    empty = Issue._base_manager.create(
        workspace=ws, project=proj, state=st, name="empty", description_html="", description_stripped=None
    )

    new = migrator.apply_tested_migration(("db", "0129_backfill_issue_description_stripped"))

    Issue_after = new.apps.get_model("db", "Issue")
    assert Issue_after._base_manager.get(pk=empty.pk).description_stripped is None
