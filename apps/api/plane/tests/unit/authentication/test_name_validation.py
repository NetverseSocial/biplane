# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-21 — names containing digits must be registerable.

The sign-up name validator used `ch.isalpha()`, which excludes digits, so any
name with a number in it was rejected outright with INVALID_NAME_SIGN_UP. That
is not a cosmetic warning: it is a hard block on creating an account. `7of9`
could not register at all and had to be renamed to `seven`, which broke her
invite match and left her stuck in first-run onboarding.

The validator exists to reject control characters — specifically the Unicode
separators that `isspace()` lets through. Digits carry none of that risk.

These tests import the production predicate directly, so they fail if the rule
regresses. It was a closure inside the sign-up view and therefore unimportable;
this change lifts it to module scope for exactly that reason — a mirrored copy
of the rule would pass while production stayed broken.
"""

import pytest

from plane.utils.name_policy import name_error_code as _name_error
from plane.utils.name_policy import normalize_name


class TestDigitsAreAllowed:
    """The actual bug."""

    @pytest.mark.parametrize(
        "name",
        [
            "7of9",       # the case that blocked a real account
            "Seven9",
            "R2",
            "X Æ A-12",   # digits, space and hyphen together
            "3",
        ],
    )
    def test_names_with_digits_are_accepted(self, name):
        assert _name_error(name, required=True) is None, f"{name!r} was rejected"


class TestControlCharactersStillRejected:
    """The reason the validator exists — must survive the fix.

    `isspace()` is True for these, which is why the check is character-by-
    character against an allow-set rather than a strip-and-hope.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "BadName",   # NEL
            "Bad Name",   # line separator
            "Bad Name",   # paragraph separator
            "Bad\tName",
            "Bad\nName",
            "Bad<script>",
        ],
    )
    def test_control_and_markup_characters_rejected(self, name):
        assert _name_error(name, required=True) == "INVALID_NAME_SIGN_UP"


class TestUnchangedRules:
    def test_ordinary_names_still_pass(self):
        for name in ["Sable", "Mary-Jane", "O'Brien", "J. R. Smith", "Renée", "李雷"]:
            assert _name_error(name, required=True) is None, name

    def test_first_name_still_required(self):
        assert _name_error("", required=True) == "REQUIRED_FIRST_NAME_SIGN_UP"
        assert _name_error("   ", required=True) == "REQUIRED_FIRST_NAME_SIGN_UP"

    def test_last_name_still_optional(self):
        assert _name_error("", required=False) is None

    def test_length_cap_still_enforced(self):
        assert _name_error("a" * 151, required=True) == "INVALID_NAME_SIGN_UP"
        assert _name_error("a" * 150, required=True) is None

    def test_isalnum_only_numeric_forms_stay_out(self):
        # The reason the rule is isdecimal() and not isalnum(): these are
        # isalnum()-True but not decimal, and no name needs them.
        assert _name_error("Half½", required=True) == "INVALID_NAME_SIGN_UP"
        assert _name_error("RomanⅠ", required=True) == "INVALID_NAME_SIGN_UP"


# THE SHARED BOUNDARY TABLE (Rowan RC 3085).
#
# The identical list, with identical expectations, is asserted against the
# client validator in packages/utils/src/validation.name.test.ts as
# NUMERIC_BOUNDARY. Both directions matter. Proving the server accepts what the
# client accepts is only half of it — if the server admits something the client
# refuses, the account gets created and then stranded in onboarding, which is
# the whole of BIP-21.
#
# The shared boundary is Unicode category Nd: str.isdecimal() here, \p{Nd}
# there. Probed on both runtimes, not reasoned about.
NUMERIC_BOUNDARY = [
    ("7", "ASCII seven", True),
    ("٧", "Arabic-Indic seven", True),
    ("०", "Devanagari zero", True),
    ("²", "superscript two", False),
    ("⁵", "superscript five", False),
    ("½", "vulgar fraction one half", False),
    ("Ⅰ", "Roman numeral one", False),
]


class TestNumericBoundaryMatchesTheClient:
    @pytest.mark.parametrize("ch,label,accepted", NUMERIC_BOUNDARY)
    def test_boundary_char(self, ch, label, accepted):
        result = _name_error(f"Nam{ch}e", required=True)
        if accepted:
            assert result is None, f"{label} should be accepted"
        else:
            assert result == "INVALID_NAME_SIGN_UP", f"{label} should be rejected"

    def test_admits_nothing_the_client_rejects(self):
        wrongly_admitted = [
            label
            for ch, label, accepted in NUMERIC_BOUNDARY
            if not accepted and _name_error(f"Nam{ch}e", required=True) is None
        ]
        assert wrongly_admitted == []


# THE SHARED BLANKNESS TABLE (Rowan RC 3087).
#
# Mirrored in packages/utils/src/validation.name.test.ts as BLANKNESS_BOUNDARY.
# "Absent" used to mean str.strip() here and String.prototype.trim() there, and
# those sets differ in BOTH directions: U+0085 and U+001C..U+001F are blank to
# Python only, U+FEFF is blank to JavaScript only. A last name of a single NEL
# was absent to this server and a hard error on the client. Both sides now use
# the explicit union in NAME_BLANK_CHARS.
BLANKNESS_BOUNDARY = [
    (0x20, "SPACE", True),
    (0x09, "TAB", True),
    (0x0A, "LF", True),
    (0x85, "NEL - Python-only under library defaults", True),
    (0x1C, "FS - Python-only under library defaults", True),
    (0xFEFF, "BOM - JavaScript-only under library defaults", True),
    (0xA0, "NBSP", True),
    (0x2028, "LINE SEPARATOR", True),
    (0x41, "letter A - not blank", False),
    (0x37, "digit 7 - not blank", False),
]


class TestBlanknessMatchesTheClient:
    @pytest.mark.parametrize("cp,label,blank", BLANKNESS_BOUNDARY)
    def test_blankness(self, cp, label, blank):
        # Blankness is only observable on the REQUIRED path: a blank value is
        # REQUIRED_FIRST_NAME_SIGN_UP, while a non-blank one is judged on its
        # characters. Asserting through the optional path instead would pass
        # for any valid letter, since "absent" and "present and fine" both
        # return None — the mistake this test originally made.
        ch = chr(cp)
        required_result = _name_error(ch, required=True)
        if blank:
            assert required_result == "REQUIRED_FIRST_NAME_SIGN_UP", f"{label} should be absent"
            assert _name_error(ch, required=False) is None, f"{label} optional should be absent"
        else:
            assert required_result != "REQUIRED_FIRST_NAME_SIGN_UP", f"{label} should not be absent"

    def test_mixed_blank_run_is_absent(self):
        mixed = "".join(chr(c) for c in (0x20, 0x85, 0x1C, 0xFEFF, 0x09))
        assert _name_error(mixed, required=False) is None


# THE MIXED TABLE — blank characters ADJACENT to a valid name.
# (Morrow RC 3092 / Rowan RC 3091.)
#
# Mirrored in packages/utils/src/validation.name.test.ts as MIXED_BOUNDARY.
#
# This is the case the two previous tables both missed. They covered "which
# characters are digits" and "which characters mean absent", but not "a blank
# character sitting next to a real name". The server stripped before validating
# while the client validated the raw string, so "<NEL>Alice" was accepted here
# and refused there — and, worse, the RAW value was what got stored, so the
# leading control character reached the database having never been checked.
#
# The policy: normalize_name is the canonical form, that is what is validated,
# and that is what is stored. These assert both halves.
MIXED_BOUNDARY = [
    ((0x85,), "Alice", "leading NEL"),
    ((0xFEFF,), "Alice", "leading BOM"),
    ((0x1C,), "Alice", "leading FS"),
    ((0x20,), "Alice", "leading space"),
    ((0x85, 0xFEFF), "Alice", "leading NEL + BOM"),
]


class TestMixedBlankAdjacentToValidName:
    @pytest.mark.parametrize("prefix,name,label", MIXED_BOUNDARY)
    def test_leading_blanks_accepted_and_normalized(self, prefix, name, label):
        raw = "".join(chr(c) for c in prefix) + name
        assert _name_error(raw, required=True) is None, f"{label} should be accepted"
        assert normalize_name(raw) == name, f"{label} must normalise to the bare name"

    @pytest.mark.parametrize("prefix,name,label", MIXED_BOUNDARY)
    def test_trailing_blanks_too(self, prefix, name, label):
        raw = name + "".join(chr(c) for c in prefix)
        assert _name_error(raw, required=True) is None, f"trailing {label}"
        assert normalize_name(raw) == name, f"trailing {label} must normalise"

    def test_blank_INSIDE_a_name_is_still_rejected(self):
        # Only the ENDS are stripped. A separator in the middle is exactly what
        # RC 3029 exists to refuse, and must stay refused.
        for cp in (0x85, 0xFEFF, 0x1C, 0x2028):
            raw = "Al" + chr(cp) + "ice"
            assert _name_error(raw, required=True) == "INVALID_NAME_SIGN_UP", hex(cp)

    def test_what_is_validated_is_what_is_stored(self):
        # The defect: validate the stripped form, store the raw one. If these
        # ever diverge again, unvalidated characters reach the database.
        raw = chr(0xFEFF) + "Alice" + chr(0x85)
        canonical = normalize_name(raw)
        assert canonical == "Alice"
        assert _name_error(canonical, required=True) is None
        assert canonical != raw, "this case is only meaningful if raw differs"
