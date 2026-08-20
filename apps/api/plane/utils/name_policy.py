# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""biplane (BIP-21): the single server-side name policy.

This module is the Python half of a policy whose other half is
packages/utils/src/validation.ts. The two must agree exactly, in both
directions, because a disagreement is not cosmetic: whichever side is more
permissive lets a value through that the other refuses, and the user ends up
either stranded in required onboarding or unable to edit their own stored name.

That happened four times in one pull request, each time in a different
dimension, so the rules are stated here rather than inherited from library
defaults:

  WHICH CHARACTERS ARE DIGITS
    ``str.isdecimal()`` here, ``\\p{Nd}`` there. Not ``isdigit()`` (it admits
    superscripts the client refuses) and not ``isalnum()`` (it admits fractions
    and Roman numerals that neither side wants).

  WHICH CHARACTERS MEAN "ABSENT"
    ``NAME_BLANK_CODEPOINTS`` below: the union of Python's ``str.strip()`` set
    and JavaScript's ``String.prototype.trim()`` set. Those differ in BOTH
    directions -- NEL and U+001C..U+001F are blank to Python only, U+FEFF is
    blank to JavaScript only -- so neither library default can be the policy.

  WHAT GETS VALIDATED, AND WHAT GETS STORED
    The value returned by ``normalize_name``, and it must be the SAME value.
    Validating a stripped form while persisting the raw one let the leading and
    trailing control characters this guard exists to reject (RC 3029) reach the
    database unchecked.

This lives in plane.utils rather than in a view so the sign-up endpoint and the
credentials provider share one implementation instead of two that drift.

The set is written as code points on purpose. Pasting the characters themselves
into source makes them invisible to a reviewer and easy to mangle in transit --
which is exactly the class of bug this module exists to prevent.
"""

NAME_BLANK_CODEPOINTS = (
    0x09,  # TAB
    0x0A,  # LF
    0x0B,  # VT
    0x0C,  # FF
    0x0D,  # CR
    0x1C,  # FILE SEPARATOR        -- Python-blank only
    0x1D,  # GROUP SEPARATOR       -- Python-blank only
    0x1E,  # RECORD SEPARATOR      -- Python-blank only
    0x1F,  # UNIT SEPARATOR        -- Python-blank only
    0x20,  # SPACE
    0x85,  # NEL                   -- Python-blank only
    0xA0,  # NO-BREAK SPACE
    0x1680,  # OGHAM SPACE MARK
    0x2000,  # EN QUAD .. HAIR SPACE
    0x2001,
    0x2002,
    0x2003,
    0x2004,
    0x2005,
    0x2006,
    0x2007,
    0x2008,
    0x2009,
    0x200A,
    0x2028,  # LINE SEPARATOR
    0x2029,  # PARAGRAPH SEPARATOR
    0x202F,  # NARROW NO-BREAK SPACE
    0x205F,  # MEDIUM MATHEMATICAL SPACE
    0x3000,  # IDEOGRAPHIC SPACE
    0xFEFF,  # BYTE ORDER MARK       -- JavaScript-blank only
)

NAME_BLANK_CHARS = "".join(chr(cp) for cp in NAME_BLANK_CODEPOINTS)

MAX_NAME_LENGTH = 150

# Allowed alongside letters and decimal digits. Curly quotes are included
# because macOS substitutes the typographic apostrophe, so "O’Brien" types
# cleanly.
NAME_EXTRA_CHARS = "'’‘.-"


def normalize_name(value):
    """Canonical form of a name field: shared blank characters off both ends.

    Idempotent, so calling it defensively costs nothing.
    """
    return str(value or "").strip(NAME_BLANK_CHARS)


def name_error_code(value, required):
    """Return an auth error code for a name, or None if it is acceptable.

    Operates on the canonical form. Callers must normalise once and persist that
    same value -- see the module docstring.
    """
    value = normalize_name(value)
    if not value:
        return "REQUIRED_FIRST_NAME_SIGN_UP" if required else None
    if len(value) > MAX_NAME_LENGTH:
        return "INVALID_NAME_SIGN_UP"
    for ch in value:
        # A plain space ONLY. ``isspace()`` is True for NEL and the Unicode
        # line/paragraph separators, which are exactly the controls this
        # rejects (RC 3029): normalize_name removes them at the ends, and this
        # loop refuses them in the middle.
        if not (ch.isalpha() or ch.isdecimal() or ch == " " or ch in NAME_EXTRA_CHARS):
            return "INVALID_NAME_SIGN_UP"
    return None
