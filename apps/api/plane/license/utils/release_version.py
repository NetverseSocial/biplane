"""THE release version authority, Python side (BIP-40).

THE DOMAIN IS ONE DATUM, CARRIED IN THIS PACKAGE. ``release_version.datum``
sits beside this module, so the API image — which copies only ``apps/api`` —
has it. An earlier revision resolved the datum through ``parents[4]``, which
lands on ``<repo>/apps/deployments`` in a checkout and ``/deployments`` in the
image, and no ``deployments`` directory is copied: import failed outright
(Morrow RC 3492, read from source because the author could not execute it).

One key, one line, one strict pattern — not JSON — so this parser and the
shell's cannot differ in strictness. Nothing derived is stored: the upper
accepted and first refused values are computed and asserted by the tests.

``[0-9]`` rather than ``\\d``: ``\\d`` matches Unicode decimal digits, so
``v١.٢.٣`` would validate and then fail to compare. ``fullmatch`` rather than
an anchored search: ``$`` also matches before a trailing newline, which is the
line-versus-value defect the shell side had with ``grep -E``.
"""

from __future__ import annotations

import re
from pathlib import Path

DATUM_PATH = Path(__file__).resolve().parent / "release_version.datum"

# THE DATUM IS ONE CANONICAL COMPLETE VALUE. A key/value line format gave two
# readers room to drift: this side stripped whitespace and int()-normalised
# while the shell matched raw text and evaluated it arithmetically, so
# " MAX_COMPONENT_DIGITS=9" and "...=09" were accepted here and refused there,
# and both ignored trailing garbage (Morrow, RC 3492/3496).
#
# fullmatch on the COMPLETE text, with one trailing newline tolerated because a
# text file has one: canonical decimal, 1-2 digits, no leading zero, no
# whitespace, nothing else. int() is applied only AFTER that, so it can never
# normalise something the shell would refuse.
_DATUM = re.compile(r"(?:[1-9]|[1-9][0-9])\n?")


def _read_max_component_digits(path: Path | None = None) -> int:
    """Strict on the complete bytes, or refuse to import."""
    target = path or DATUM_PATH
    raw = target.read_text(encoding="utf-8")
    if _DATUM.fullmatch(raw) is None:
        raise ValueError(
            "release version datum %s must be a single canonical width, got %r"
            % (target, raw)
        )
    return int(raw.strip())


MAX_COMPONENT_DIGITS: int = _read_max_component_digits()

_COMPONENT = r"(?:0|[1-9][0-9]{0,%d})" % (MAX_COMPONENT_DIGITS - 1)
PATTERN = re.compile(r"v%s\.%s\.%s" % (_COMPONENT, _COMPONENT, _COMPONENT))


def is_valid(value: object) -> bool:
    return isinstance(value, str) and PATTERN.fullmatch(value) is not None


def key(value: str) -> str:
    if not is_valid(value):
        raise ValueError("release version %r is outside the accepted release grammar" % (value,))
    return ".".join(part.rjust(MAX_COMPONENT_DIGITS, "0") for part in value[1:].split("."))


def gt(left: str, right: str) -> bool:
    return key(left) > key(right)


def upper_accepted() -> str:
    """Computed, never stored."""
    part = "9" * MAX_COMPONENT_DIGITS
    return "v%s.%s.%s" % (part, part, part)


def first_refused() -> str:
    """Computed, never stored."""
    return "v1%s.0.0" % ("0" * MAX_COMPONENT_DIGITS)
