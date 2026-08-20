# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A non-ASCII signature header must be REFUSED, not crash the endpoint.

`hmac.compare_digest` raises TypeError on str operands containing non-ASCII:

    TypeError: comparing strings with non-ASCII characters is not supported

The signature is an attacker-controlled header, so before this fix a single
unauthenticated request carrying one non-ASCII byte in `X-Forgejo-Signature`
produced an unhandled 500 — while every other wrong signature produced a clean
403. Witnessed against the running bridge:

    X-Forgejo-Signature: deadbeef   -> 403
    X-Forgejo-Signature: <U+00E9>   -> 500

These pin the refusal. The parametrisation walks the categories an attacker
would actually reach for — accented Latin, CJK, emoji, a lone high byte, and a
NUL — rather than one token example, because "non-ASCII" is a class and a fix
that handled only the first of them would still leave the endpoint open.

BIP-15 slice 2 replaced `_verify_signature` with `_authenticate`, which returns
the identified forge instead of a bool — the caller needs it to read that
forge's event and delivery-id headers. These tests were written against the old
name and are carried across rather than dropped: the defect they pin is in the
comparison, which now lives in the per-forge `verify()`, and the endpoint is
just as unauthenticated as it was. `None` is the new refusal; a forge object is
the new acceptance.
"""

import hashlib
import hmac

import pytest
from django.test import override_settings

from plane.bridge.forgejo_bridge import _authenticate

SECRET = "a-secret-long-enough-16"  # gitleaks:allow — synthetic test constant, obviously not a credential
BODY = b'{"repository":{"full_name":"example/biplane"}}'


class FakeRequest:
    def __init__(self, headers=None, body=BODY):
        self.headers = headers or {}
        self.body = body


def good_signature():
    return hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


@override_settings(FORGEJO_WEBHOOK_SECRET=SECRET)
@pytest.mark.parametrize(
    "value,label",
    [
        ("é", "accented latin"),
        ("中文", "CJK"),
        ("\U0001f600", "emoji (astral plane)"),
        ("ÿ" * 64, "a full-length string of high bytes"),
        ("deadébeef", "non-ASCII hidden mid-token"),
        ("\x00" + "a" * 63, "embedded NUL"),
        (good_signature()[:-1] + "é", "a VALID digest with its last char swapped"),
    ],
)
@pytest.mark.parametrize("header", ["X-Forgejo-Signature", "X-Gitea-Signature"])
def test_non_ascii_signature_is_refused_and_does_not_raise(header, value, label):
    # The assertion is as much "did not raise" as "refused": a TypeError
    # escaping here is the 500.
    assert _authenticate(FakeRequest({header: value})) is None, label


@override_settings(FORGEJO_WEBHOOK_SECRET=SECRET)
def test_the_valid_signature_still_verifies():
    # The fix must not have made everything refuse — without this, a function
    # that returned None unconditionally would pass every test above.
    forge = _authenticate(FakeRequest({"X-Forgejo-Signature": good_signature()}))
    assert forge is not None
    # And it must identify the RIGHT forge. Authenticating as one personality
    # while reading another's event headers is the mismatch this module exists
    # to prevent, so "some forge accepted it" is not good enough.
    assert forge.name == "forgejo"


@override_settings(FORGEJO_WEBHOOK_SECRET=SECRET)
def test_ordinary_ascii_junk_is_still_refused():
    assert _authenticate(FakeRequest({"X-Forgejo-Signature": "deadbeef"})) is None
