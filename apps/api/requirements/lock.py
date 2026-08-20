#!/usr/bin/env python3
"""THE owner of the dependency lock: what its inputs are, how it is generated,
and how the build verifies it still describes them.

WHY ONE OWNER (Morrow RC 3678). These were three things and the seams between
them were the defects:

  * The documented `pip-compile` recipe rewrote `requirements.lock` and dropped
    the freshness stamp, because nothing wrote it back. **Following the
    procedure produced a lock that failed the gate** — a procedure that does
    not work is worse than none, because it is followed.
  * The input walk hand-parsed only `-r PATH`. pip also accepts
    `--requirement`, `-c` and `--constraint`, and with a long-form include the
    nested file was never hashed: editing it changed the resolved graph while
    the digest stayed identical, so the gate passed on a stale lock. Executed
    witness, `production.txt` using `--requirement base.txt`:

        digest before editing base.txt   cbc10881f85e…
        digest after  editing base.txt   cbc10881f85e…

Generation and verification now read the SAME input walk, and generation always
stamps. Usage, from `apps/api`:

    python requirements/lock.py generate    # regenerate + stamp (needs the
                                            # build toolchain — see README)
    python requirements/lock.py check       # what the image build runs
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys

ENTRY = "requirements.txt"
LOCK = "requirements.lock"
STAMP_PREFIX = "# inputs-sha256: "
STAMP_RE = re.compile(r"^#\s*inputs-sha256:\s*([0-9a-f]{64})\s*$", re.MULTILINE)

#: Every form pip accepts for pulling in another requirements file. `-c` and
#: `--constraint` are included because a constraint file changes what resolves
#: just as surely as a requirement file does.
_INCLUDE_RE = re.compile(r"^\s*(?:-r|--requirement|-c|--constraint)(?:[=\s]+)(\S+)\s*$")

#: pip's OWN comment rule: a hash preceded by start-of-line or whitespace.
#: The leading-whitespace requirement is load-bearing (Sable) — a naive split
#: on `#` would destroy the egg fragment in a VCS URL such as
#: `git+https://host/repo#egg=name`, where the hash is preceded by a path
#: character and is part of the requirement rather than a comment.
_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")

#: An option line this walk does NOT understand. Unknown syntax must be LOUD:
#: silently ignoring it is exactly how a long-form include went unhashed.
_KNOWN_OPTIONS = ("-r", "--requirement", "-c", "--constraint", "-e", "--editable",
                  "--index-url", "-i", "--extra-index-url", "--find-links", "-f",
                  "--no-index", "--pre", "--hash", "--only-binary", "--no-binary",
                  "--prefer-binary", "--require-hashes", "--trusted-host")


class InputSyntaxError(RuntimeError):
    """A requirements file used option syntax this walk does not understand."""


def _includes(text: str, path: pathlib.Path) -> list[str]:
    """Includes named by one requirements file.

    COMMENTS ARE STRIPPED BEFORE BOTH CHECKS, and that ordering is the fix for
    a hole of exactly the class this walk exists to close (Sable). The include
    pattern anchors the path to end of line, so `-r nested.txt  # core deps`
    did not match — while the unknown-option guard saw a head of `-r`, which IS
    known, so nothing raised. pip follows that form; this walk silently did not,
    and editing the nested file left the freshness stamp unchanged.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = _COMMENT_RE.sub("", raw).strip()
        if not line or not line.startswith("-"):
            continue
        found = _INCLUDE_RE.match(line)
        if found:
            out.append(found.group(1))
            continue
        head = re.split(r"[=\s]", line, maxsplit=1)[0]
        if head not in _KNOWN_OPTIONS:
            raise InputSyntaxError(
                f"{path}: unrecognised option {head!r}. If it can pull in another "
                "file, this walk must learn it — an include it cannot see is an "
                "input whose edits the freshness stamp will not notice."
            )
    return out


def inputs(root: pathlib.Path, entry: str = ENTRY) -> list[pathlib.Path]:
    """Every file the lock is generated from, deterministically ordered."""
    seen: list[pathlib.Path] = []
    pending = [(root / entry).resolve()]
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        if not path.exists():
            raise SystemExit(f"lock: declared input {path} does not exist")
        seen.append(path)
        for rel in _includes(path.read_text(), path):
            pending.append((path.parent / rel).resolve())
    return seen


def digest(root: pathlib.Path, entry: str = ENTRY) -> str:
    h = hashlib.sha256()
    for path in inputs(root, entry):
        # The NAME is hashed as well as the content: moving a requirement
        # between two input files leaves the concatenation identical.
        h.update(path.name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def stamp(root: pathlib.Path) -> str:
    """Write the current input digest into the lock, replacing any prior one."""
    lock = root / LOCK
    text = STAMP_RE.sub("", lock.read_text()).lstrip("\n")
    value = digest(root)
    lock.write_text(f"{STAMP_PREFIX}{value}\n{text}")
    return value


def generate(root: pathlib.Path) -> int:
    """Regenerate the lock AND stamp it. One command, so the stamp cannot be
    forgotten by following the documented recipe."""
    cmd = [
        "pip-compile", "--generate-hashes", "--strip-extras",
        f"--output-file={LOCK}", ENTRY,
    ]
    print("lock: " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=root)
    if result.returncode != 0:
        return result.returncode
    print(f"lock: stamped {stamp(root)[:12]}…")
    return 0


def check(root: pathlib.Path) -> int:
    lock = root / LOCK
    if not lock.exists():
        print(f"lock: {lock} does not exist", file=sys.stderr)
        return 1
    found = STAMP_RE.search(lock.read_text())
    if not found:
        print(
            f"lock: {LOCK} carries no `{STAMP_PREFIX.strip()}` stamp.\n"
            "Regenerate with `python requirements/lock.py generate`.",
            file=sys.stderr,
        )
        return 1
    actual = digest(root)
    if found.group(1) != actual:
        print(
            f"lock: {LOCK} does not describe the current inputs.\n"
            f"  stamped: {found.group(1)}\n"
            f"  inputs:  {actual}\n"
            "An input was edited without regenerating the lock, so this build\n"
            "would install the OLD dependency graph.\n"
            "Regenerate with `python requirements/lock.py generate`.",
            file=sys.stderr,
        )
        return 1
    print(f"lock: freshness OK ({actual[:12]}…)")
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "check"
    root = pathlib.Path(argv[2] if len(argv) > 2 else ".").resolve()
    if mode == "generate":
        return generate(root)
    if mode == "check":
        return check(root)
    print(f"lock: unknown mode {mode!r}; expected 'generate' or 'check'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
