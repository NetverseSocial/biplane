#!/usr/bin/env python3
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The trusted-tree check must FIRE, not merely exist.

A guard nobody has watched fire is not a guard (Vex 3981), so these tests build
deliberately-loosened fixtures and assert the service refuses them. They run as
an ordinary user and need no Docker and no root.

What cannot be tested from here: the clean case, because this user cannot
create root-owned files. That half is asserted against _untrusted_reason with
a stubbed stat.

AND A STUBBED POSITIVE IS NOT A POSITIVE CONTROL. Refusals prove only that the
guard refuses; they cannot prove that a tree which PASSES is a tree an apply
can work on. Those are different claims, and the second is the one that broke:
requiring root ownership of .env made the check and the applier mutually
exclusive (Vex 3982).

That control has now been RUN, against the real service on devboard.test
(2026-08-18, internal evidence record):
a compliant three-class tree — root-owned scripts and compose, service-owned
.env and COMPOSE_DIR at 600/755 — was built, and on it
  * the service BOOTS (listening, zero refusal lines), and on that SAME tree
  * .env is readable AND writable by the service, and
  * a temp file can be created beside it (what atomic_replace needs), while
  * the executed script is NOT writable by the service.
Four negatives on the same harness each refused naming the specific path
(group-writable script; group-writable compose override; the SOURCED
release-version.sh alone; group-writable .env), and restoring the modes
booted it again — so the guard is not merely stuck on, which would have been
a false green in both directions.

    python3 operator-tree.test.py
"""

import importlib.util
import os
import stat as stat_mod
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("apply_service", os.path.join(HERE, "apply-service.py"))
svc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(svc)

FAILURES = []


def check(name, condition):
    print(("ok   - " if condition else "not ok - ") + name)
    if not condition:
        FAILURES.append(name)


def build_fixture(root, *, file_mode=0o644, dir_mode=0o755):
    """A deployment tree shaped like the real one."""
    repo = os.path.join(root, "repo")
    for rel in ("deployments/selfhost", "deployments/release"):
        os.makedirs(os.path.join(repo, rel), exist_ok=True)
    files = [
        os.path.join(repo, "deployments/selfhost/apply-update.sh"),
        os.path.join(repo, "deployments/release/validate-tag.sh"),
        os.path.join(repo, "deployments/release/release-version.sh"),
        os.path.join(root, "docker-compose.yml"),
        os.path.join(root, "docker-compose.override.yml"),
        os.path.join(root, ".env"),
    ]
    for path in files:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# fixture\n")
        os.chmod(path, file_mode)
    for dirpath, dirnames, _ in os.walk(repo):
        os.chmod(dirpath, dir_mode)
    return repo, files


# --- the check fires on a caller-writable tree ---------------------------

with tempfile.TemporaryDirectory() as tmp:
    repo, files = build_fixture(tmp, file_mode=0o664)   # group-writable FILES
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    problems = svc._untrusted_paths()
    check("group-writable files are refused", len(problems) > 0)
    check("the refusal names the writable mode",
          any("group- or world-writable" in p or "not root" in p for p in problems))

    # the sourced helper is reached even by a request about to be refused, so
    # naming only the two entry points would not be enough (Vex 3981)
    check("release-version.sh is among the checked paths",
          any("release-version.sh" in p for p in problems))
    check("the compose override is among the checked paths",
          any("docker-compose.override.yml" in p for p in problems))
    check(".env is among the checked paths", any(".env" in p for p in problems))

with tempfile.TemporaryDirectory() as tmp:
    # FILES tight, DIRECTORY loose: a writable directory lets someone replace a
    # file they cannot write, so mode-checking the files alone is not enough.
    repo, files = build_fixture(tmp, file_mode=0o644, dir_mode=0o775)
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    problems = svc._untrusted_paths()
    check("a writable DIRECTORY with unwritable files is still refused",
          any("writable" in p for p in problems))

with tempfile.TemporaryDirectory() as tmp:
    repo, files = build_fixture(tmp, file_mode=0o2775, dir_mode=0o2775)
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    problems = svc._untrusted_paths()
    check("the exact 2775 our relocate script set is refused",
          len(problems) > 0)


# --- /apply refuses rather than proceeding -------------------------------

with tempfile.TemporaryDirectory() as tmp:
    repo, files = build_fixture(tmp, file_mode=0o664)
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    svc.REQUIRE_TRUSTED_TREE = True
    check("_assert_trusted_tree returns problems when strict",
          len(svc._assert_trusted_tree("test")) > 0)
    svc.REQUIRE_TRUSTED_TREE = False
    check("the escape hatch permits the run but is explicit",
          svc._assert_trusted_tree("test") == [])
    svc.REQUIRE_TRUSTED_TREE = True


# --- ownership is checked, not just mode ---------------------------------

_real_stat = os.stat
try:
    def fake_stat(path, *a, **kw):
        st = _real_stat(path, *a, **kw)
        fields = list(st)
        fields[stat_mod.ST_UID] = 0          # pretend root owns it
        fields[stat_mod.ST_MODE] = st.st_mode & ~0o022  # and it is tight
        return os.stat_result(fields)

    with tempfile.TemporaryDirectory() as tmp:
        repo, files = build_fixture(tmp, file_mode=0o664, dir_mode=0o775)
        svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
        os.stat = fake_stat
        check("a root-owned, tight tree produces no problems",
              svc._untrusted_paths() == [])
finally:
    os.stat = _real_stat

with tempfile.TemporaryDirectory() as tmp:
    # owned by this user (not root), tight modes — still refused, because
    # "the service's own identity can rewrite it" is the same escalation
    repo, files = build_fixture(tmp, file_mode=0o644, dir_mode=0o755)
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    problems = svc._untrusted_paths()
    check("a non-root-owned tree is refused even with tight modes",
          any("not root" in p for p in problems))


# --- class 2: the paths the service must WRITE -------------------------
#
# This is the arm that would have caught Vex 3982. Requiring root ownership of
# .env and of COMPOSE_DIR makes the check and the applier mutually exclusive:
# apply-update.sh demands .env be writable before it does anything, and its
# atomic replace needs write on the containing directory. So a tree that passed
# the old rule was a tree where every apply died immediately — and the six
# refusal arms above could not see that, because they only ever proved the
# guard says no.

with tempfile.TemporaryDirectory() as tmp:
    repo, files = build_fixture(tmp, file_mode=0o644, dir_mode=0o755)
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    svc.STATE_DIR = tmp
    problems = svc._untrusted_paths()
    # Match on PATH + "owned by", never on the exact sentence: keying the
    # assertion to message wording made this arm survive a mutant that broke
    # the behaviour, because the mutant changed the rule and not the phrasing.
    check("a service-owned .env is NOT refused for its ownership",
          not [p for p in problems if ".env" in p and "owned by" in p])
    check("a service-owned COMPOSE_DIR is NOT refused for its ownership",
          not [p for p in problems if p.startswith(tmp + ":") and "owned by" in p])
    # ...while the scripts it EXECUTES are still held to root ownership
    check("class 1 is still strict: a non-root apply-update.sh is refused",
          any("apply-update.sh" in p and "not root" in p for p in problems))

with tempfile.TemporaryDirectory() as tmp:
    repo, files = build_fixture(tmp, file_mode=0o644, dir_mode=0o755)
    os.chmod(os.path.join(tmp, ".env"), 0o664)          # group-writable
    svc.REPO_DIR, svc.COMPOSE_DIR = repo, tmp
    svc.STATE_DIR = tmp
    problems = svc._untrusted_paths()
    check("a GROUP-writable .env is refused even though the service may own it",
          any(".env" in p and "writable" in p for p in problems))

# class 3: operator-editable by design is excluded from the permission rule
check("operator.env is excluded from the permission rule",
      "operator.env" in svc.OPERATOR_EDITABLE)


print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("all trusted-tree tests passed")
