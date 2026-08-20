#!/usr/bin/env python3
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Tests for the operator operations.

The property under test is NOT "does push work" — it is "can a caller reach
anything the allowlist does not name". This service drives Docker, and Docker
is root-equivalent, so an argument that escaped into a shell would hand the
caller the host. The refusal cases are therefore the subject, and they all run
without Docker.

Note what these tests CANNOT establish: that the service is a boundary at all.
That depends on the tree it executes from being unwritable by its callers, a
host fact no unit test can assert (Vex 3980). It is checked on the box.

    python3 operator-ops.test.py
"""

import importlib.util
import re
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("apply_service", os.path.join(HERE, "apply-service.py"))
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)

FAILURES = []


def check(name, condition):
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"not ok - {name}")
        FAILURES.append(name)


def push(kind="web", build="69117ec", tag="v1.2.6"):
    return svc._op_push_images([{"kind": kind, "build": build}], tag)


# --- _clean is the boundary: control characters, length, leading dash

check("_clean rejects a trailing newline", svc._clean("69117ec\n") == "")
check("_clean rejects an embedded newline", svc._clean("69117ec\nrm -rf /") == "")
check("_clean rejects a carriage return", svc._clean("69117ec\r") == "")
check("_clean rejects a NUL", svc._clean("69117ec\x00") == "")
check("_clean rejects DEL", svc._clean("69117ec\x7f") == "")
check("_clean rejects a leading dash (flag injection) BY RULE",
      svc._clean("--privileged") == "")
check("_clean rejects an over-long value", svc._clean("a" * 201) == "")
check("_clean rejects a non-string", svc._clean(None) == "" and svc._clean(7) == "")
check("_clean passes an ordinary value", svc._clean("69117ec") == "69117ec")

# fullmatch, not match: `match` with `$` would accept "69117ec\n"
check("build regex is anchored against a trailing newline",
      not svc._BUILD_RE.fullmatch("69117ec\n"))
check("tag regex is anchored against a trailing newline",
      not svc._TAG_RE.fullmatch("v1.2.6\n"))
check("digest regex is anchored against a trailing newline",
      not svc._DIGEST_RE.fullmatch("sha256:" + "a" * 64 + "\n"))


# --- the caller cannot name a registry, a repository or a ref

check("registry is a module constant, not caller input", svc.REGISTRY == "localhost:3000")


# --- refusals happen BEFORE any docker call

for bad in ["evil", "backend; rm -rf /", "../../etc/passwd", "", "Backend"]:
    code, body = push(kind=bad)
    check(f"unknown service refused: {bad!r}", code == 422)

for bad in ["$(id)", "`id`", "69117ec; rm -rf /", "69117ec\nrm -rf /", "zzzzzzz",
            "abc", "--flag", "69117ec && curl evil"]:
    code, body = push(build=bad)
    check(f"build identity refused: {bad!r}", code == 422)

for bad in ["latest", "v1.2", "$(id)", "v1.2.6; rm -rf /", "v1.2.6\n", "--tag"]:
    code, body = push(tag=bad)
    check(f"release tag refused: {bad!r}", code == 422)

code, body = svc._op_push_images(None, "v1.2.6")
check("push refuses a non-list", code == 400)
code, body = svc._op_push_images([], "v1.2.6")
check("push refuses an empty list", code == 400)
code, body = svc._op_push_images(["web"], "v1.2.6")
check("push refuses a non-object entry", code == 400)
code, body = svc._op_push_images(
    [{"kind": k, "build": "69117ec"} for k in svc.SERVICES] + [{"kind": "web", "build": "69117ec"}],
    "v1.2.6")
check("push refuses more images than there are services", code == 400)
code, body = svc._op_push_images(
    [{"kind": "web", "build": "69117ec"}, {"kind": "web", "build": "aaaaaaa"}], "v1.2.6")
check("push refuses the same service twice", code == 422)


# --- board-status must never pass a docker blob through

src = open(os.path.join(HERE, "apply-service.py"), encoding="utf-8").read()
check("board-status does not call docker inspect (Config.Env is every secret)",
      "docker\", \"inspect" not in src)
check("the .env read is key-whitelisted", "allowed = {" in src)


# --- an EMPTY token must not authenticate anything
#
# The hazard is not hypothetical and this demonstrates it rather than asserting
# the guard exists: with an empty SERVICE_TOKEN the constant-time comparison
# succeeds against the literal header "Bearer " (Sia 4016). What prevented that
# before was _token() raising at startup — a guarantee in another block, which
# stops being one the moment someone makes startup resilient.

import hmac as _hmac
check("the hazard is real: an empty token MATCHES the header 'Bearer '",
      _hmac.compare_digest("Bearer ", f"Bearer {''}"))


def _authed(header, token):
    """The refusal as implemented in Handler._authed."""
    if not token:
        return False
    return _hmac.compare_digest(header, f"Bearer {token}")


check("and the guard refuses it at the comparison site",
      _authed("Bearer ", "") is False)
check("a real token still authenticates", _authed("Bearer s3cret", "s3cret") is True)
check("a wrong token still fails", _authed("Bearer wrong", "s3cret") is False)
# Pin the COMPLETE statement, not an identifier fragment. Sia 4017 proved the
# fragment insufficient by the mutation that restores the bypass while keeping
# the line: `if not SERVICE_TOKEN:` with its body changed to `pass`. Whitespace
# is collapsed first so the assertion survives reformatting without weakening to
# a substring of the condition — the repo's consumer-seams idiom.
_flat = re.sub(r"\s+", " ", src)
check("the service refuses an empty token with the COMPLETE statement",
      "if not SERVICE_TOKEN: return False" in _flat)

# --- the token comparison is constant-time

check("token comparison uses hmac.compare_digest", "hmac.compare_digest" in src)


# --- there is no passthrough and no shell

check("no shell=True anywhere", "shell=True" not in src)
check("no os.system", "os.system" not in src)


# --- attribution is NOT claimed: no caller field is logged
#     With a single shared token there is no identity to record; a field that
#     cannot be populated truthfully manufactures evidence (Vex 3980).

check("no self-attested caller field is logged", '"caller"' not in src)
check("records carry an explicit phase (intent/outcome/refused)",
      '"phase": phase' in src)


print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print(f"all operator-op tests passed")
