# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# BIP-63: the test-database name is built from xdist's own identifiers (Morrow RC
# 3649) — testrun_uid (shared across a run's workers -> invocation-owned) and
# worker_id (per-worker). These are deterministic unit pins on the naming; the
# live -n2 integration is executed separately.
import pytest

from plane.tests.conftest import isolated_test_db_name

_UID = "0a1b2c3d4e5f6a7b"


@pytest.mark.unit
class TestIsolatedTestDbName:
    def test_uses_worker_id_then_invocation_uid(self):
        assert isolated_test_db_name("test_plane", {}, _UID, "gw0") == "test_plane_gw0_" + _UID

    def test_non_xdist_worker_is_master(self):
        assert isolated_test_db_name("test_plane", {}, _UID, "master") == "test_plane_master_" + _UID

    def test_label_is_a_readable_prefix(self):
        assert isolated_test_db_name("test_plane", {"BIP_TEST_DB_SUFFIX": "aria"}, _UID, "gw0") == (
            "test_plane_aria_gw0_" + _UID
        )

    def test_one_runs_workers_share_the_uid_but_differ_by_worker(self):
        # invocation-owned AND per-worker: same testrun_uid, different worker.
        a = isolated_test_db_name("test_plane", {}, "samerun", "gw0")
        b = isolated_test_db_name("test_plane", {}, "samerun", "gw1")
        assert a != b
        assert "samerun" in a and "samerun" in b

    def test_two_invocations_differ_for_the_same_worker(self):
        assert isolated_test_db_name("test_plane", {}, "run1", "gw0") != isolated_test_db_name(
            "test_plane", {}, "run2", "gw0"
        )

    def test_label_is_sanitised_to_identifier_safe_chars(self):
        assert isolated_test_db_name("test_plane", {"BIP_TEST_DB_SUFFIX": "a/b 3!"}, _UID, "gw0") == (
            "test_plane_ab3_gw0_" + _UID
        )

    def test_multibyte_label_keeps_worker_and_uid_within_the_63_BYTE_cut(self):
        # Morrow's byte witness: isalnum() admits multibyte (界 is 3 bytes), so a
        # label under 63 CHARS can exceed 63 BYTES. Bound by bytes; worker_id and
        # the uid — which carry uniqueness — must survive whole inside 63 bytes.
        name = isolated_test_db_name("test_plane", {"BIP_TEST_DB_SUFFIX": "界" * 100}, _UID, "gw3")
        raw = name.encode("utf-8")
        assert len(raw) <= 63
        assert ("_gw3_" + _UID).encode("utf-8") in raw[:63]
        assert name.endswith("_gw3_" + _UID)
        raw.decode("utf-8")  # truncation never leaves a split multibyte byte
