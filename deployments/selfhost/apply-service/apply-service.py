#!/usr/bin/env python3
# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The narrow privileged applier (BIP-42 tail / ticket 69).

Scope A names this seam exactly: "The server may later invoke this through a
narrow privileged service, but the command is the implementation." This is
that service, kept narrow on purpose:

  - It runs ON THE DEPLOY HOST as an identity that can drive Docker — which
    is root-equivalent, and this file must not claim less. Two consequences
    follow and are load-bearing: (a) no caller input may ever reach a shell,
    hence fixed argv arrays and allowlists throughout; (b) the tree this
    service EXECUTES from must not be writable by its callers, or /apply is a
    remote-code-execution endpoint no argument check can save (Vex 3980).
    The default bind is loopback; reaching it from the API container usually
    requires APPLY_SERVICE_BIND=0.0.0.0, and then the bearer token is the ONLY
    gate — treat the port accordingly (firewall it on any host with a public
    interface). The README's trust-boundary section states this exposure
    plainly; this file must not claim less.
  - It exposes a FIXED set of actions and no passthrough: POST /apply, GET
    /status, and the operator operations (POST /op/push-images, POST
    /op/trigger-update-check, GET /op/board-status) that exist so a release
    does not require a human at a Docker-capable shell.
  - Every request must carry the bearer token from APPLY_SERVICE_TOKEN_FILE.
  - It validates the tag through the repo's own validate-tag.sh (one grammar
    authority — this file never restates the semver rule).
  - It REFUSES level `full` exactly as apply-update.sh does; the banner
    already tells the operator full releases take the manual path.
  - It runs apply-update.sh detached, one run at a time (a lock file), and
    the status endpoint reports the tail of the run's own log. It adds no
    logic to the apply — backup, digest pull, migrations, atomic pins and
    rollback all belong to the command it wraps.

What this deliberately is NOT: a state machine, a queue, an updater with
opinions. If apply-update.sh cannot express it, this service cannot either.
"""

import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_TOKEN = ""  # set at startup from TOKEN_FILE; see _authed for why "" is refused
_APPLY_MUTEX = threading.Lock()  # serialises check-and-start; one process owns the port

REPO_DIR = os.environ.get("APPLY_SERVICE_REPO", os.path.expanduser("~/biplane-prod/repo"))
COMPOSE_DIR = os.environ.get("APPLY_SERVICE_COMPOSE_DIR", os.path.expanduser("~/biplane-prod"))
TOKEN_FILE = os.environ.get("APPLY_SERVICE_TOKEN_FILE", os.path.expanduser("~/.config/biplane/apply-service.token"))
STATE_DIR = os.environ.get("APPLY_SERVICE_STATE_DIR", os.path.expanduser("~/.local/state/biplane-apply"))
BIND = os.environ.get("APPLY_SERVICE_BIND", "127.0.0.1")
PORT = int(os.environ.get("APPLY_SERVICE_PORT", "7671"))
#: every request body here is a small JSON object; nothing legitimate is larger
MAX_BODY_BYTES = 64 * 1024

LOCK_PATH = os.path.join(STATE_DIR, "apply.lock")
LOG_PATH = os.path.join(STATE_DIR, "apply.log")
RESULT_PATH = os.path.join(STATE_DIR, "last-result.json")


# --- the precondition this service cannot assume about itself -------------
#
# Everything the privileged path READS is executable content. Not "scripts are
# code, config is data": a compose file that can gain `privileged: true` or a
# `/:/host` volume, or an .env that can name which image gets pulled, steers a
# root-equivalent operation just as completely as a shell script does — and
# does it without touching an argument, so argv discipline, allowlists and
# no-shell are all bypassed (Vex 3981).
#
# So the service verifies its own inputs before it will run at all: every
# privileged path, and every ancestor directory, must be unwritable by anyone
# other than root or this service itself. A writable DIRECTORY is enough —
# it lets someone replace a file they cannot write.
#
# "Not writable by anyone but me" is the property; "owned by root" is only a
# shorthand for it, and the shorthand is WRONG for the paths this service must
# write (.env, and the directory its atomic replace creates temp files in).
# See _trusted_tree_targets for the three roles.
#
# HONEST SCOPE: this defends against CALLERS, not against a compromised
# service. This process can drive Docker, Docker is root, so a compromised
# service can rewrite its own tree, this check, and its log. The adversary this
# is for is a token-holding caller with write access to the deployment — which
# is the adversary we actually have.
#
# A guard nobody has watched fire is not a guard: operator-tree.test.py builds
# a deliberately group-writable fixture and asserts this refuses it.

#: The escape hatch exists for test hosts. It is REFUSED on a non-loopback
#: bind: "checks off" plus "reachable from the network" is the worst possible
#: configuration and precisely the one a hurried operator produces (Vex 3982).
REQUIRE_TRUSTED_TREE = os.environ.get("APPLY_SERVICE_REQUIRE_TRUSTED_TREE", "1") != "0"
if not REQUIRE_TRUSTED_TREE and BIND not in ("127.0.0.1", "localhost", "::1"):
    sys.stderr.write(
        "REFUSING: APPLY_SERVICE_REQUIRE_TRUSTED_TREE=0 is a test-host setting "
        f"and must not be combined with a non-loopback bind ({BIND}).\n")
    raise SystemExit(1)


def _untrusted_reason(path: str, service_owned_ok: bool) -> str:
    """Why `path` is unsafe for a root-equivalent process, or "".

    The property is "not writable by anyone but me", NOT "owned by root" —
    those differ for the paths this service must itself WRITE, and conflating
    them makes this check and the applier mutually exclusive (Vex 3982).
    Self-ownership grants this service nothing it does not already have
    through Docker; group- or world-write grants it to OTHER identities, and
    that is the entire threat."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"{path}: cannot stat ({exc.strerror})"
    allowed = {0, os.geteuid()} if service_owned_ok else {0}
    if st.st_uid not in allowed:
        want = "root or this service" if service_owned_ok else "root"
        return f"{path}: owned by uid {st.st_uid}, not {want}"
    if st.st_mode & 0o022:
        return f"{path}: mode {st.st_mode & 0o7777:04o} is group- or world-writable"
    return ""


def _trusted_tree_targets() -> list:
    """(path, service_may_own) for every privileged input, BY ROLE.

    Three classes, because one rule cannot cover them (Vex 3982):

    1. NEVER written by this service — the scripts it executes and the compose
       files it reads. Root-owned; nobody else writes. `release-version.sh` is
       here because BOTH apply-update.sh and validate-tag.sh source it, so it
       runs even on a request that is about to be refused.

    2. WRITTEN by this service — `.env` (the applier rewrites the image pins)
       and the directories it creates temp files in. apply-update.sh requires
       `.env` to be writable BEFORE it does anything, and its atomic replace
       needs write on the containing DIRECTORY. Demanding root ownership here
       would mean a tree that PASSES this check is a tree where every apply
       dies immediately, and a tree where apply works is one this service
       refuses to boot on — the two would be mutually exclusive.

    3. Operator-editable BY DESIGN (`operator.env`, when the authority split
       lands) — group-writable on purpose, so it is excluded from the
       permission rule entirely and constrained by a CONTENT refusal instead:
       the applier rejects it if it sets any image pin or release identity."""
    return [
        (os.path.join(REPO_DIR, "deployments", "selfhost", "apply-update.sh"), False),
        (os.path.join(REPO_DIR, "deployments", "release", "validate-tag.sh"), False),
        (os.path.join(REPO_DIR, "deployments", "release", "release-version.sh"), False),
        (os.path.join(COMPOSE_DIR, "docker-compose.yml"), False),
        (os.path.join(COMPOSE_DIR, "docker-compose.override.yml"), False),
        (os.path.join(COMPOSE_DIR, ".env"), True),
        (COMPOSE_DIR, True),
        (STATE_DIR, True),
    ]


#: Excluded from the permission rule by design — class 3 above.
#:
#: THE PROPERTY THIS RESTS ON (Vex 3985): class 3 is safe only because
#: COMPOSE_DIR is NOT group-writable. Dev members can therefore EDIT
#: operator.env in place, but cannot unlink and recreate it — so they cannot
#: change its owner or mode, and cannot escape the content refusal that
#: constrains it.
#:
#: AND THAT PROPERTY IS ENFORCED, not merely relied upon — an earlier version of
#: this comment claimed it would fail SILENTLY, which was wrong in the safe
#: direction and worth correcting (Sia 4016): COMPOSE_DIR is itself a checked
#: target, and _untrusted_reason flags group/world write regardless of the
#: service_owned_ok relaxation, which affects OWNERSHIP only. Left uncorrected,
#: the danger is someone adding a redundant guard for a hole that does not
#: exist — or worse, "resolving" the contradiction by loosening the COMPOSE_DIR
#: check and thereby creating it.
OPERATOR_EDITABLE = ("operator.env",)


def _untrusted_paths() -> list:
    """Every privileged input, plus the ancestors that could be used to swap it.

    An ancestor is judged under the WEAKER of the rules that reach it: a
    directory this service must write cannot simultaneously be required to be
    root-owned, so a class-2 target relaxes its own ancestors. Ancestors above
    the deployment (/opt, /) are reached only by class-1 targets and so stay
    strict."""
    # NAME THE DIRECTION: the value is True when the node MAY be service-owned,
    # i.e. True == RELAXED, and `or` means loosest-wins. Naming this `strictest`
    # inverted its meaning on the one line where reversing the direction
    # silently WIDENS the trusted set (Vex 3985).
    service_owned_ok_for = {}
    for target, service_owned_ok in _trusted_tree_targets():
        if not os.path.exists(target):
            continue
        if os.path.basename(target) in OPERATOR_EDITABLE:
            continue
        node = os.path.realpath(target)
        while True:
            service_owned_ok_for[node] = service_owned_ok_for.get(node, False) or service_owned_ok
            parent = os.path.dirname(node)
            if parent == node:
                break
            node = parent
    problems = []
    for node in sorted(service_owned_ok_for):
        reason = _untrusted_reason(node, service_owned_ok_for[node])
        if reason:
            problems.append(reason)
    return problems


def _assert_trusted_tree(when: str) -> list:
    """Refuse rather than proceed. Returns the problems (empty when clean)."""
    problems = _untrusted_paths()
    if problems:
        _record("trusted-tree", "refused", {"when": when, "problems": problems[:12]}, 500)
        if REQUIRE_TRUSTED_TREE:
            return problems
        sys.stderr.write(
            "WARNING: privileged tree is writable by callers and the check is "
            "DISABLED via APPLY_SERVICE_REQUIRE_TRUSTED_TREE=0; this is a test "
            "configuration and must never be production:\n  "
            + "\n  ".join(problems[:12]) + "\n")
    return []


def _token() -> str:
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        value = f.read().strip()
    if not value:
        raise RuntimeError(f"empty token file: {TOKEN_FILE}")
    return value


#: no docker call may hang forever — a hung push holds a thread and, because
#: ThreadingHTTPServer does not cap threads, enough of them exhaust the host
#: (Vex 3982). push is the one that actually hangs in practice.
DOCKER_TIMEOUT = int(os.environ.get("APPLY_SERVICE_DOCKER_TIMEOUT", "900"))


def _run(cmd, **kw):
    kw.setdefault("timeout", DOCKER_TIMEOUT)
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _tag_is_valid(tag: str) -> tuple[bool, str]:
    """Delegate to the repo's validate-tag.sh — the single grammar authority."""
    script = os.path.join(REPO_DIR, "deployments", "release", "validate-tag.sh")
    try:
        proc = _run(["bash", script, tag], cwd=REPO_DIR, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "validate-tag.sh timed out"
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _apply_running() -> bool:
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip() or "0")
        os.kill(pid, 0)
        # Footnote (Sia 4016): a RECYCLED pid reads as live, so a stale lock
        # whose number was reused wedges applies at 409 until it is cleared.
        # Needs pid wraparound to reach, and fails toward refusing rather than
        # double-applying, so it is recorded rather than guarded.
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        # Stale lock: the process is gone. Report not-running; the next
        # apply overwrites the lock.
        return False


def _start_apply(tag: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    script = os.path.join(REPO_DIR, "deployments", "selfhost", "apply-update.sh")
    with open(LOG_PATH, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            ["bash", script, tag],
            cwd=COMPOSE_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    # A reaper thread records the exit code durably; the HTTP request has
    # long since returned by the time it fires.
    def _reap():
        code = proc.wait()
        with open(RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump({"tag": tag, "exit_code": code, "finished_at": time.time()}, f)
        _record("apply", "outcome", {"tag": tag, "exit_code": code}, 200 if code == 0 else 500)

    threading.Thread(target=_reap, daemon=True).start()


# --- operator operations (the deploy steps that otherwise need a human at a
# --- Docker-capable shell). Each is a FIXED argv, never a shell string, with
# --- its arguments matched against a tight allowlist first: the service's
# --- privilege is Docker, and Docker is root-equivalent, so an argument that
# --- could reach a shell would hand the caller the host. There is deliberately
# --- no passthrough operation — the set below is the whole surface.

# The caller names WHICH service and WHICH build. It never names a registry, a
# repository or a published tag: the service composes both refs itself from
# constants below. Vex 3980 — "don't validate what you can refuse to accept."
# Two consequences worth stating, because they are the point:
#   - `docker push` is an EGRESS primitive. If a caller could name the registry,
#     an authenticated caller could push our images anywhere. The registry is a
#     constant here, so that channel does not exist rather than being guarded.
#   - Two regexes disappear entirely; what remains is one hex check and a set
#     membership. Less validation because there is less to validate.
SERVICES = ("backend", "web", "admin", "space")
REGISTRY = "localhost:3000"
REGISTRY_OWNER = os.environ.get("APPLY_SERVICE_REGISTRY_OWNER", "biplane")

#: the build identity of a local image: the short (or full) commit sha it was
#: built from. fullmatch, not match: `match` with `$` accepts a TRAILING
#: NEWLINE, and a newline inside a validated argument is an audit-forgery
#: primitive the moment those arguments reach a line-oriented log — the
#: validator and the log are coupled whether or not that was intended.
_BUILD_RE = re.compile(r"[0-9a-f]{7,40}")
#: a published release tag
_TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
#: what the registry must answer with when asked for a manifest digest
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: the API container that owns the update-check task. This is DEPLOYMENT
#: configuration, not caller input — the caller cannot name a container.
API_CONTAINER = os.environ.get("APPLY_SERVICE_API_CONTAINER", "biplane-prod-api-1")

_UPDATE_CHECK_PY = (
    "from plane.bgtasks.update_check_task import run_update_check; "
    "print('state:', run_update_check())"
)


def _clean(value) -> str:
    """A string with no control characters, or "" — which every allowlist below
    rejects. Control characters are stripped at the boundary rather than deeper
    in, so no validated value can carry a newline into a log line."""
    if not isinstance(value, str) or len(value) > 200:
        return ""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return ""
    if value.startswith("-"):
        return ""  # a leading dash is a flag to every command; refuse by RULE
    return value


def _record(op: str, phase: str, detail, code=None) -> None:
    """Emit a record of a privileged operation to the journal.

    TWO records per action — intent before the work, outcome after. An intent
    with no outcome is the single most informative line this service can
    produce, and a service that logs only its successes is blind to the whole
    reconnaissance phase, so refusals are recorded too.

    Deliberately NO caller field. With one shared token there is no identity to
    record: the peer address is not identity, and a caller-supplied name is
    self-attestation, which is a comment rather than attribution. Logging a
    field that cannot be populated truthfully is worse than omitting it — it
    manufactures evidence. Per-caller tokens are the prerequisite for
    attribution and are tracked separately (Vex 3980); until they exist this
    log answers "what happened", never "who did it".

    The medium is stdout -> journald: this process is root-equivalent, so a
    file it appends to is a file it can also rewrite. Append-only has to be a
    property of the medium, not of intent."""
    payload = {"ts": time.time(), "op": op, "phase": phase, "detail": detail}
    if code is not None:
        payload["code"] = code
    print("OPERATOR " + json.dumps(payload, sort_keys=True), flush=True)


def _op_push_images(images, tag) -> tuple[int, dict]:
    """Publish the built images, returning the digest the REGISTRY reports.

    The caller names {kind, build} per image and the release tag; every ref is
    composed here. Never trust push stdout for the digest — the release
    procedure's own rule is that stdout is a claim and the registry is the
    fact, and this operation exists to hand back facts."""
    tag = _clean(tag)
    if not _TAG_RE.fullmatch(tag):
        return 422, {"error": "release tag refused"}
    if not isinstance(images, list) or not images:
        return 400, {"error": "images must be a non-empty array"}
    if len(images) > len(SERVICES):
        return 400, {"error": "too many images"}

    plan = []
    seen = set()
    for entry in images:
        if not isinstance(entry, dict):
            return 400, {"error": "each image must be an object"}
        kind, build = _clean(entry.get("kind")), _clean(entry.get("build"))
        if kind not in SERVICES:
            return 422, {"error": "unknown service", "detail": kind[:60]}
        if not _BUILD_RE.fullmatch(build):
            return 422, {"error": "build identity refused", "detail": build[:60]}
        if kind in seen:
            return 422, {"error": "service named twice", "detail": kind}
        seen.add(kind)
        plan.append((kind, build))

    results = []
    try:
        return _push_plan(plan, tag)
    except subprocess.TimeoutExpired as exc:
        _record("push-images", "outcome", {"timeout": str(exc.cmd[:3])}, 504)
        return 504, {"error": "a docker command timed out", "detail": str(exc)[:200]}


def _push_plan(plan, tag) -> tuple[int, dict]:
    results = []
    for kind, build in plan:
        local = f"biplane-{kind}:pi5-{build}"
        ref = f"{REGISTRY}/{REGISTRY_OWNER}/biplane-{kind}:{tag}"
        _record("push-images", "intent", {"local": local, "ref": ref})
        proc = _run(["docker", "tag", local, ref])
        if proc.returncode != 0:
            _record("push-images", "outcome", {"ref": ref, "step": "tag"}, 500)
            return 500, {"error": "docker tag failed", "detail": proc.stderr.strip()[:400]}
        proc = _run(["docker", "push", ref])
        if proc.returncode != 0:
            _record("push-images", "outcome", {"ref": ref, "step": "push"}, 500)
            return 500, {"error": "docker push failed", "image": ref,
                         "detail": proc.stderr.strip()[:400]}
        proc = _run(["docker", "buildx", "imagetools", "inspect", ref,
                     "--format", "{{json .Manifest.Digest}}"])
        digest = proc.stdout.strip().strip('"')
        if proc.returncode != 0 or not _DIGEST_RE.fullmatch(digest):
            _record("push-images", "outcome", {"ref": ref, "step": "readback"}, 500)
            return 500, {"error": "digest readback failed", "image": ref,
                         "detail": (proc.stdout + proc.stderr).strip()[:400]}
        _record("push-images", "outcome", {"ref": ref, "digest": digest}, 200)
        results.append({"image": f"{REGISTRY}/{REGISTRY_OWNER}/biplane-{kind}", "digest": digest})
    return 200, {"pushed": results}


def _op_trigger_update_check() -> tuple[int, dict]:
    """Run the update check now (it is otherwise hourly).

    The caller supplies NOTHING: the container is deployment configuration and
    the python is a module constant, so this operation has no input to escape."""
    _record("trigger-update-check", "intent", {"container": API_CONTAINER})
    try:
        proc = _run(["docker", "exec", API_CONTAINER, "python", "manage.py", "shell",
                     "-c", _UPDATE_CHECK_PY], timeout=180)
    except subprocess.TimeoutExpired:
        _record("trigger-update-check", "outcome", {"timeout": True}, 504)
        return 504, {"error": "update check timed out"}
    out = (proc.stdout + proc.stderr).strip()
    match = re.search(r"state:\s*(\w+)", out)
    if proc.returncode != 0:
        _record("trigger-update-check", "outcome", {"rc": proc.returncode}, 500)
        return 500, {"error": "update check failed", "detail": out[-600:]}
    state = match.group(1) if match else ""
    _record("trigger-update-check", "outcome", {"state": state}, 200)
    return 200, {"state": state, "output": out[-600:]}


def _op_board_status() -> tuple[int, dict]:
    """What is actually RUNNING versus what is PINNED — the disagreement that
    makes apply-update.sh refuse, readable BEFORE an apply rather than after a
    failed one.

    Read-only, and deliberately projecting: `docker inspect` would return
    Config.Env, which is every secret in every container, so nothing here
    passes a docker blob through — only the three named fields below. The .env
    read is key-whitelisted to the image pins for the same reason."""
    try:
        proc = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"])
    except subprocess.TimeoutExpired:
        return 504, {"error": "docker ps timed out"}
    if proc.returncode != 0:
        return 500, {"error": "docker ps failed", "detail": proc.stderr.strip()[:400]}
    services = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and "biplane" in parts[0]:
            services.append({"name": parts[0], "image": parts[1], "status": parts[2]})
    pins = {}
    env_path = os.path.join(COMPOSE_DIR, ".env")
    allowed = {f"BIPLANE_{k.upper()}_IMAGE" for k in SERVICES}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key in allowed:
                    pins[key] = value
    return 200, {"services": services, "pins": pins}


class Handler(BaseHTTPRequestHandler):
    server_version = "biplane-apply-service"

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        # An EMPTY token must be refused HERE, where the comparison happens.
        # hmac.compare_digest("Bearer ", f"Bearer {''}") is True — measured, not
        # reasoned — so an empty SERVICE_TOKEN authenticates the literal header
        # "Bearer " with its trailing space. What prevents that today is _token()
        # raising at startup, which is a guarantee in a DIFFERENT block and
        # invisible from here: the moment someone makes startup resilient (a
        # natural thing to want) the hole opens with nothing to notice it.
        # Sia 4016.
        if not SERVICE_TOKEN:
            return False
        # Constant-time: a plain == leaks the token prefix-wise through timing.
        return hmac.compare_digest(header, f"Bearer {SERVICE_TOKEN}")

    def do_GET(self):  # noqa: N802 — stdlib contract
        if not self._authed():
            return self._reply(401, {"error": "unauthorized"})
        if self.path.split("?", 1)[0] == "/op/board-status":
            code, payload = _op_board_status()
            return self._reply(code, payload)
        if self.path != "/status":
            return self._reply(404, {"error": "unknown path"})
        result = None
        if os.path.exists(RESULT_PATH):
            with open(RESULT_PATH, "r", encoding="utf-8") as f:
                result = json.load(f)
        log_tail = ""
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                log_tail = "".join(f.readlines()[-40:])
        return self._reply(200, {
            "running": _apply_running(),
            "last_result": result,
            "log_tail": log_tail,
        })

    def do_POST(self):  # noqa: N802 — stdlib contract
        if not self._authed():
            return self._reply(401, {"error": "unauthorized"})
        if self.path not in ("/apply", "/op/push-images", "/op/trigger-update-check"):
            return self._reply(404, {"error": "unknown path"})
        # Bounded read. Reachable only by a token holder — _authed() answers 401
        # above, and that was verified rather than assumed (Sia 4016) — so this
        # is resource hygiene rather than a control. A malformed Content-Length
        # is ANSWERED, not raised: an exception here would be a 500 for what is
        # plainly a bad request.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._reply(400, {"error": "Content-Length is not a number"})
        if length < 0 or length > MAX_BODY_BYTES:
            return self._reply(413, {"error": f"body exceeds {MAX_BODY_BYTES} bytes"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._reply(400, {"error": "body is not JSON"})
        if self.path == "/op/push-images":
            code, body = _op_push_images(payload.get("images"), payload.get("tag"))
            if code >= 400:
                _record("push-images", "refused", {"reason": body.get("error")}, code)
            return self._reply(code, body)
        if self.path == "/op/trigger-update-check":
            code, body = _op_trigger_update_check()
            return self._reply(code, body)
        tag = payload.get("tag", "")
        if not isinstance(tag, str) or not tag:
            return self._reply(400, {"error": "missing tag"})
        ok, detail = _tag_is_valid(tag)
        if not ok:
            _record("apply", "refused", {"tag": tag[:60], "reason": "invalid tag"}, 422)
            return self._reply(422, {"error": "tag refused by validate-tag.sh", "detail": detail})
        # Check-and-start is one atomic step. Without the mutex two concurrent
        # POSTs both pass the running check and BOTH applies run (witnessed —
        # Vex RC 3801: two 202s, two invocations, one of them invisible to
        # /status because the lock file holds a single pid).
        problems = _assert_trusted_tree("pre-apply")
        if problems:
            return self._reply(500, {
                "error": "refusing to apply: the deployment tree is writable by callers",
                "detail": problems[:6]})
        with _APPLY_MUTEX:
            if _apply_running():
                _record("apply", "refused", {"tag": tag, "reason": "already running"}, 409)
                return self._reply(409, {"error": "an apply is already running"})
            _record("apply", "intent", {"tag": tag})
            _start_apply(tag)
        return self._reply(202, {"started": tag, "log": LOG_PATH})

    def log_message(self, fmt, *args):  # quiet; the apply log is the record
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    # A silent RCE becomes a failed boot. This runs BEFORE the token is read,
    # so a service that cannot vouch for its own inputs never opens the port.
    _startup_problems = _assert_trusted_tree("startup")
    if _startup_problems:
        sys.stderr.write(
            "REFUSING TO START: this service executes and reads files that its "
            "callers can modify, which makes it a remote-code-execution "
            "endpoint rather than a boundary. Fix the ownership/modes below, "
            "or set APPLY_SERVICE_REQUIRE_TRUSTED_TREE=0 for a test host.\n  "
            + "\n  ".join(_startup_problems) + "\n")
        sys.exit(1)
    SERVICE_TOKEN = _token()  # noqa: F811 — module global, set once at boot
    os.makedirs(STATE_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"apply-service listening on {BIND}:{PORT}", flush=True)
    httpd.serve_forever()
