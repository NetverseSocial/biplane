# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("biplane_board.py")
SPEC = importlib.util.spec_from_file_location("biplane_board", MODULE_PATH)
board = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(board)
PRINCIPAL_ID = "6d8104b5-2e70-4c95-bca3-08d7a7987772"


def _response(envelope, replayed=False):
    return {
        **{
            key: envelope[key]
            for key in ("op_key", "source", "verb", "workspace", "project")
        },
        "outcome": {"changed": True},
        "replayed": replayed,
    }


def test_operation_is_journalled_before_the_first_network_call(tmp_path):
    observed = {}

    def transport(method, path, body):
        files = list(tmp_path.glob("*.json"))
        assert method == "POST" and path == "board/ops/"
        assert len(files) == 1
        observed.update(json.loads(files[0].read_text()))
        return 201, _response(body)

    client = board.BoardClient(
        "http://board", "secret-token", PRINCIPAL_ID, tmp_path, transport
    )
    result = client.transition(
        "netverse", "BIP", 37, "4b92cf74-a9c0-4b87-99a4-c52f9b70db30", "agent"
    )

    assert observed["status"] == "pending"
    assert observed["envelope"]["op_key"] == result["op_key"]
    assert "secret-token" not in next(tmp_path.glob("*.json")).read_text()
    assert next(tmp_path.glob("*.json")).stat().st_mode & 0o777 == 0o600


def test_unknown_post_queries_before_retrying_the_exact_envelope(tmp_path):
    calls = []

    def transport(method, path, body):
        calls.append((method, path, body))
        if len(calls) == 1:
            raise board.UnknownTransport("lost response")
        if len(calls) == 2:
            return 404, {"detail": "not committed"}
        return 201, _response(body)

    client = board.BoardClient(
        "http://board", "token", PRINCIPAL_ID, tmp_path, transport
    )
    result = client.transition(
        "netverse", "BIP", 37, "4b92cf74-a9c0-4b87-99a4-c52f9b70db30", "agent"
    )

    assert [call[0] for call in calls] == ["POST", "GET", "POST"]
    assert calls[0][2] == calls[2][2]
    assert result["op_key"] == calls[0][2]["op_key"]


def test_resume_returns_a_stored_outcome_without_reposting(tmp_path):
    envelope = {
        "op_key": "durable-key",
        "expected_principal_id": PRINCIPAL_ID,
        "source": "agent",
        "verb": "transition",
        "workspace": "netverse",
        "project": "BIP",
        "payload": {
            "sequence_id": 37,
            "state_id": "4b92cf74-a9c0-4b87-99a4-c52f9b70db30",
        },
    }
    board._write_atomic(
        tmp_path / "durable-key.json",
        {"status": "pending", "envelope": envelope},
        exclusive=True,
    )
    calls = []

    def transport(method, path, body):
        calls.append((method, path, body))
        return 200, _response(envelope, replayed=True)

    result = board.BoardClient(
        "http://board", "token", PRINCIPAL_ID, tmp_path, transport
    ).resume("durable-key")

    assert result["replayed"] is True
    assert calls == [("GET", "board/ops/durable-key/", None)]


def test_list_exhausts_every_honestly_signalled_page(tmp_path):
    responses = iter(
        [
            (
                200,
                {
                    "items": [{"sequence_id": 1}],
                    "complete": False,
                    "truncated": True,
                    "next_cursor": "1",
                },
            ),
            (
                200,
                {
                    "items": [{"sequence_id": 2}],
                    "complete": True,
                    "truncated": False,
                    "next_cursor": None,
                },
            ),
        ]
    )
    paths = []

    def transport(method, path, body):
        paths.append(path)
        return next(responses)

    result = board.BoardClient(
        "http://board", "token", PRINCIPAL_ID, tmp_path, transport
    ).list_work_items("netverse", "BIP", 1)

    assert [row["sequence_id"] for row in result] == [1, 2]
    assert "cursor=1" in paths[1]


def test_list_refuses_a_truncated_page_without_a_new_cursor(tmp_path):
    def transport(_method, _path, _body):
        return 200, {
            "items": [],
            "complete": False,
            "truncated": True,
            "next_cursor": None,
        }

    client = board.BoardClient(
        "http://board", "token", PRINCIPAL_ID, tmp_path, transport
    )
    with pytest.raises(board.AdapterError, match="inconsistent truncation"):
        client.list_work_items("netverse", "BIP", 1000)


def test_detail_reads_the_scoped_work_item_route(tmp_path):
    calls = []

    def transport(method, path, body):
        calls.append((method, path, body))
        return 200, {"sequence_id": 37, "name": "Board service"}

    client = board.BoardClient(
        "http://board", "token", PRINCIPAL_ID, tmp_path, transport
    )
    result = client.get_work_item("netverse", "BIP", 37)

    assert result["sequence_id"] == 37
    assert calls == [("GET", "board/work-items/netverse/BIP/37/", None)]
