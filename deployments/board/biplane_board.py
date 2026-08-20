#!/usr/bin/env python3
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Policy-free client for the BIP-37 board operation service."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class AdapterError(RuntimeError):
    pass


class UnknownTransport(AdapterError):
    pass


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _write_atomic(path, value, *, exclusive=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    target = path if exclusive else path.with_suffix(path.suffix + ".tmp")
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(target, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if not exclusive:
            target.unlink(missing_ok=True)
        raise


class BoardClient:
    def __init__(
        self, base_url, token, expected_principal_id, state_dir, transport=None
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.expected_principal_id = expected_principal_id
        self.state_dir = Path(state_dir)
        self.transport = transport or self._request

    def _request(self, method, path, body=None):
        data = None if body is None else _json_bytes(body)
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/{path.lstrip('/')}",
            data=data,
            method=method,
            headers={"X-API-Key": self.token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, TypeError):
                payload = {"detail": str(exc)}
            return exc.code, payload
        except (OSError, urllib.error.URLError) as exc:
            raise UnknownTransport(str(exc)) from exc

    def _journal_path(self, op_key):
        return self.state_dir / f"{op_key}.json"

    def _record(self, document, *, exclusive=False):
        _write_atomic(
            self._journal_path(document["envelope"]["op_key"]),
            document,
            exclusive=exclusive,
        )

    def _accept_outcome(self, document, status, response):
        if status not in (200, 201):
            raise AdapterError(
                f"board operation refused with HTTP {status}: {response}"
            )
        if response.get("op_key") != document["envelope"]["op_key"]:
            raise AdapterError("board response named a different operation key")
        document = {**document, "status": "committed", "response": response}
        self._record(document)
        return response

    def _query(self, op_key):
        return self.transport(
            "GET", f"board/ops/{urllib.parse.quote(op_key, safe='')}/", None
        )

    def _post_after_query(self, document):
        op_key = document["envelope"]["op_key"]
        try:
            status, response = self._query(op_key)
        except UnknownTransport as exc:
            raise AdapterError(
                f"outcome for {op_key} is unknown; resume by key"
            ) from exc
        if status == 200:
            return self._accept_outcome(document, status, response)
        if status != 404:
            raise AdapterError(f"outcome query failed with HTTP {status}: {response}")
        try:
            status, response = self.transport(
                "POST", "board/ops/", document["envelope"]
            )
        except UnknownTransport as exc:
            raise AdapterError(
                f"outcome for {op_key} is unknown; resume by key"
            ) from exc
        return self._accept_outcome(document, status, response)

    def transition(self, workspace, project, sequence_id, state_id, source):
        op_key = str(uuid.uuid4())
        document = {
            "status": "pending",
            "envelope": {
                "op_key": op_key,
                "expected_principal_id": self.expected_principal_id,
                "source": source,
                "verb": "transition",
                "workspace": workspace,
                "project": project,
                "payload": {"sequence_id": sequence_id, "state_id": state_id},
            },
        }
        self._record(document, exclusive=True)
        print(f"operation_key={op_key}", file=sys.stderr, flush=True)
        try:
            status, response = self.transport(
                "POST", "board/ops/", document["envelope"]
            )
        except UnknownTransport:
            return self._post_after_query(document)
        return self._accept_outcome(document, status, response)

    def resume(self, op_key):
        path = self._journal_path(op_key)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterError(f"no readable journal for operation {op_key}") from exc
        if document.get("status") == "committed":
            return document["response"]
        return self._post_after_query(document)

    def list_work_items(self, workspace, project, page_size):
        items = []
        cursor = None
        seen = set()
        while True:
            query = {"limit": str(page_size)}
            if cursor is not None:
                query["cursor"] = cursor
            path = (
                f"board/work-items/{urllib.parse.quote(workspace, safe='')}/"
                f"{urllib.parse.quote(project, safe='')}/?{urllib.parse.urlencode(query)}"
            )
            status, response = self.transport("GET", path, None)
            if status != 200:
                raise AdapterError(f"board read failed with HTTP {status}: {response}")
            items.extend(response.get("items", []))
            if response.get("complete") is True and response.get("truncated") is False:
                return items
            cursor = response.get("next_cursor")
            if response.get("truncated") is not True or not cursor or cursor in seen:
                raise AdapterError(
                    "board read returned an inconsistent truncation boundary"
                )
            seen.add(cursor)

    def get_work_item(self, workspace, project, sequence_id):
        path = (
            f"board/work-items/{urllib.parse.quote(workspace, safe='')}/"
            f"{urllib.parse.quote(project, safe='')}/{sequence_id}/"
        )
        status, response = self.transport("GET", path, None)
        if status != 200:
            raise AdapterError(f"board read failed with HTTP {status}: {response}")
        return response


def _client_from_env(args):
    base_url = args.api or os.environ.get("BIPLANE_API")
    token = os.environ.get("BIPLANE_TOKEN")
    expected_principal_id = os.environ.get("BIPLANE_EXPECTED_USER_ID")
    if not base_url or not token or not expected_principal_id:
        raise AdapterError(
            "BIPLANE_API, BIPLANE_TOKEN and BIPLANE_EXPECTED_USER_ID are required"
        )
    state_dir = args.state_dir or Path.home() / ".local/state/biplane-board/operations"
    return BoardClient(base_url, token, expected_principal_id, state_dir)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--api")
    parser.add_argument("--state-dir")
    subparsers = parser.add_subparsers(dest="command", required=True)

    transition = subparsers.add_parser("transition")
    transition.add_argument("workspace")
    transition.add_argument("project")
    transition.add_argument("sequence_id", type=int)
    transition.add_argument("state_id")
    transition.add_argument("--source", default="agent_adapter")

    resume = subparsers.add_parser("resume")
    resume.add_argument("op_key")

    listing = subparsers.add_parser("list")
    listing.add_argument("workspace")
    listing.add_argument("project")
    listing.add_argument("--page-size", type=int, default=1000)

    detail = subparsers.add_parser("get")
    detail.add_argument("workspace")
    detail.add_argument("project")
    detail.add_argument("sequence_id", type=int)

    args = parser.parse_args(argv)
    try:
        client = _client_from_env(args)
        if args.command == "transition":
            result = client.transition(
                args.workspace,
                args.project,
                args.sequence_id,
                args.state_id,
                args.source,
            )
        elif args.command == "resume":
            result = client.resume(args.op_key)
        elif args.command == "list":
            if args.page_size <= 0 or args.page_size > 1000:
                raise AdapterError("page size must be between 1 and 1000")
            result = client.list_work_items(
                args.workspace, args.project, args.page_size
            )
        else:
            if args.sequence_id <= 0:
                raise AdapterError("sequence id must be positive")
            result = client.get_work_item(
                args.workspace, args.project, args.sequence_id
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except AdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
