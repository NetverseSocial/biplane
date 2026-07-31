"""
Copyright (c) 2026 The Biplane Authors
SPDX-License-Identifier: AGPL-3.0-only
See the LICENSE file for details.

Guide-asset seeding tests (design record: PR #9 comments 9737-9741).
The concurrent gate is required by review 9740; the partial-failure case is the
VIOLATING case (Sable, 7/30): a mid-loop failure after an earlier success must
publish nothing — testing only the fails-before-anything neighbour lets the
dangling-id bug through.
"""

import threading
import uuid as uuid_lib
from unittest import mock

import pytest
from django.db import connection

from plane.bgtasks.workspace_seed_task import (
    GUIDE_SEED_ASSETS,
    seed_guide_assets,
    substitute_guide_assets,
)
from plane.db.models import FileAsset, Issue, Project, User, Workspace


class TestSubstitution:
    MAPPING = {"31": "aaaa-1111", "41": "bbbb-2222"}

    def test_token_becomes_asset_id(self):
        html = '<p>Step.</p><image-component src="{{seed_asset:31}}" width="395px" height="367px" aspectratio="1.07"></image-component>'
        out = substitute_guide_assets(html, self.MAPPING)
        assert 'src="aaaa-1111"' in out
        assert "seed_asset" not in out

    def test_unmapped_token_strips_whole_component_keeps_text(self):
        html = '<p>Read the guide.</p><image-component src="{{seed_asset:33}}" width="10px" height="10px"></image-component><p>Next step.</p>'
        out = substitute_guide_assets(html, {})
        assert "image-component" not in out
        assert "Read the guide." in out and "Next step." in out

    def test_mixed_mapped_and_unmapped(self):
        html = (
            '<image-component src="{{seed_asset:31}}" width="1px" height="1px"></image-component>'
            '<image-component src="{{seed_asset:33}}" width="1px" height="1px"></image-component>'
        )
        out = substitute_guide_assets(html, self.MAPPING)
        assert 'src="aaaa-1111"' in out
        assert out.count("image-component") == 2  # one open+close pair survives
        assert "seed_asset:33" not in out

    def test_no_tokens_passthrough_untouched(self):
        html = '<p>plain</p><image-component src="https://x/y.png"></image-component>'
        assert substitute_guide_assets(html, self.MAPPING) == html

    def test_none_and_empty(self):
        assert substitute_guide_assets("", self.MAPPING) == ""
        assert substitute_guide_assets(None, self.MAPPING) is None


def _fixture():
    u = User.objects.create(email=f"seed-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
    ws = Workspace.objects.create(slug=f"w{uuid_lib.uuid4().hex[:10]}", name="W", owner=u)
    proj = Project.objects.create(workspace=ws, name="P", identifier=uuid_lib.uuid4().hex[:5].upper())
    return u, ws, proj


@pytest.mark.django_db
class TestSeedGuideAssets:
    def test_degrade_on_storage_failure_returns_empty_and_warns(self, caplog):
        u, ws, proj = _fixture()
        with mock.patch("plane.settings.storage.S3Storage.__init__", side_effect=RuntimeError("storage down")):
            mapping = seed_guide_assets(ws, u, proj.id)
        assert mapping == {}
        warnings = [r for r in caplog.records if "guide asset seeding degraded" in r.getMessage()]
        assert len(warnings) == 1  # one loud line, not four

    def test_partial_failure_publishes_nothing_and_never_dangles(self, caplog):
        """THE VIOLATING CASE: succeed on 31, fail on 32. The rollback erases the
        rows; the returned mapping must be empty, and substitution must degrade
        to text — no dangling ids, no surviving image-component."""
        u, ws, proj = _fixture()
        with mock.patch("plane.settings.storage.S3Storage") as MockStorage:
            client = MockStorage.return_value.s3_client
            client.put_object.side_effect = [None, RuntimeError("S3 flaked on the second object")]
            client.delete_object.return_value = None
            mapping = seed_guide_assets(ws, u, proj.id)
        assert mapping == {}, f"partial failure leaked ids: {mapping}"
        assert FileAsset.objects.filter(workspace=ws).count() == 0
        # cleanup of the one uploaded object was attempted
        assert client.delete_object.call_count == 1
        html = (
            '<p>a</p><image-component src="{{seed_asset:31}}" width="1px" height="1px"></image-component>'
            '<p>b</p><image-component src="{{seed_asset:32}}" width="1px" height="1px"></image-component>'
        )
        out = substitute_guide_assets(html, mapping)
        assert "image-component" not in out and "seed_asset" not in out
        assert "<p>a</p>" in out and "<p>b</p>" in out
        warnings = [r for r in caplog.records if "guide asset seeding degraded" in r.getMessage()]
        assert len(warnings) == 1

    def test_missing_project_degrades(self, caplog):
        u, ws, _ = _fixture()
        assert seed_guide_assets(ws, u, None) == {}
        assert any("no seed project resolved" in r.getMessage() for r in caplog.records)

    def test_upload_creates_assets_and_is_reused_on_rerun(self):
        u, ws, proj = _fixture()
        with mock.patch("plane.settings.storage.S3Storage") as MockStorage:
            MockStorage.return_value.s3_client.put_object.return_value = None
            first = seed_guide_assets(ws, u, proj.id)
            second = seed_guide_assets(ws, u, proj.id)
        assert set(first) == set(GUIDE_SEED_ASSETS)
        assert first == second  # rerun reuses, never re-creates
        assert FileAsset.objects.filter(workspace=ws).count() == len(GUIDE_SEED_ASSETS)
        assert FileAsset.objects.filter(workspace=ws, project_id=proj.id).count() == len(GUIDE_SEED_ASSETS)


@pytest.mark.django_db(transaction=True)
class TestConcurrentSeeding:
    """Review 9740: two seeders released together must produce exactly one
    asset set and ONE SUBSTITUTED ID IN WHAT GETS STORED — mapping equality
    alone is a proxy. The storage fake is installed ONCE from the main thread
    before the threads start (per-thread mock.patch of a module global is
    itself a race)."""

    _put_calls = []  # shared across threads; guarded by a lock in the fake

    class _FakeS3Storage:
        _lock = threading.Lock()

        def __init__(self, *a, **k):
            outer = TestConcurrentSeeding

            class _C:
                def put_object(self, **kw):
                    with TestConcurrentSeeding._FakeS3Storage._lock:
                        outer._put_calls.append(kw.get("Key"))
                    return None

                def delete_object(self, **kw):
                    return None

            self.s3_client = _C()

    def test_two_seeders_on_a_barrier(self, monkeypatch):
        import plane.settings.storage as storage_mod

        monkeypatch.setattr(storage_mod, "S3Storage", self._FakeS3Storage)

        u = User.objects.create(email=f"race-{uuid_lib.uuid4().hex[:8]}@example.com", username=uuid_lib.uuid4().hex[:12])
        ws = Workspace.objects.create(slug=f"r{uuid_lib.uuid4().hex[:10]}", name="R", owner=u)
        proj = Project.objects.create(workspace=ws, name="P", identifier="RACEP")
        barrier = threading.Barrier(2)
        results, errors = [], []

        def seeder():
            try:
                barrier.wait(timeout=10)
                results.append(seed_guide_assets(ws, u, proj.id))
            except Exception as e:  # pragma: no cover - failure surface
                errors.append(e)
            finally:
                connection.close()

        threads = [threading.Thread(target=seeder) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert len(results) == 2
        # exactly one asset set, and every mapped id is a COMMITTED row
        db_ids = {str(a.id) for a in FileAsset.objects.filter(workspace=ws)}
        assert len(db_ids) == len(GUIDE_SEED_ASSETS)
        assert results[0] == results[1]
        assert set(results[0].values()) == db_ids
        # exactly one seeder did the uploads (Sable 9761): 4 puts total, not 8
        assert len(self._put_calls) == len(GUIDE_SEED_ASSETS), self._put_calls

        # the locked requirement, at the stored artifact: a redelivered task
        # substituting with EITHER mapping persists byte-identical HTML whose
        # ids all exist. (Issue rows stand in for the seeded descriptions.)
        html = (
            '<p>guide</p><image-component src="{{seed_asset:31}}" width="1px" height="1px"></image-component>'
            '<image-component src="{{seed_asset:41}}" width="1px" height="1px"></image-component>'
        )
        stored = []
        for mapping in results:
            issue = Issue.objects.create(
                workspace=ws,
                project=proj,
                name="race-stored",
                description_html=substitute_guide_assets(html, mapping),
            )
            stored.append(Issue.objects.get(id=issue.id).description_html)
        assert stored[0] == stored[1]
        assert "seed_asset" not in stored[0] and "{{" not in stored[0]  # no token survives to storage
        import re as _re

        for src in _re.findall(r'src="([^"]+)"', stored[0]):
            assert src in db_ids, f"stored description references non-existent asset {src}"
