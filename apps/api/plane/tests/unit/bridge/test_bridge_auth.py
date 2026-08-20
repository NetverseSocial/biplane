# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""BIP-15 slice 2: the bridge's authentication gate, wired to forge personalities.

`_authenticate` decides two things a bool could not carry: *whether* the
delivery is authentic, and *which* forge sent it. The second matters because
the caller then reads the event and delivery-id headers, and reading those with
one forge's names while having authenticated with another's is precisely the
mismatch this work exists to remove.

The cases below are the refusals. In particular the GitLab one: its credential
is not body-bound, and the bridge's whole design — a delivery inbox keyed on a
body digest — assumes tampering is detectable. Accepting that weaker guarantee
has to be an operator's explicit decision, so the default must be NO.
"""

import hashlib
import hmac

import pytest
from django.test import override_settings

from plane.bridge.forgejo_bridge import _authenticate

SECRET = "a-secret-long-enough-16"  # gitleaks:allow — synthetic test constant, obviously not a credential
GH_SECRET = "a-github-secret-long-enough"
GL_TOKEN = "a-gitlab-token-long-enough"
SHORT_SECRET = "tooshort"
BODY = b'{"repository":{"full_name":"example/biplane"}}'

# Every door configured, every credential DISTINCT (Morrow 10146).
ALL_CREDS = dict(
    FORGEJO_WEBHOOK_SECRET=SECRET,
    GITHUB_WEBHOOK_SECRET=GH_SECRET,
    GITLAB_WEBHOOK_TOKEN=GL_TOKEN,
)


class FakeRequest:
    def __init__(self, headers=None, body=BODY):
        self.headers = headers or {}
        self.body = body


def sig(secret=SECRET, body=BODY):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@override_settings(**ALL_CREDS)
def test_forgejo_delivery_authenticates_and_identifies_itself():
    forge = _authenticate(FakeRequest({"X-Forgejo-Signature": sig()}))
    assert forge is not None
    assert forge.name == "forgejo"


@override_settings(**ALL_CREDS)
def test_gitea_delivery_is_the_forgejo_personality():
    forge = _authenticate(FakeRequest({"X-Gitea-Signature": sig()}))
    assert forge is not None and forge.name == "forgejo"


@override_settings(**ALL_CREDS)
def test_github_delivery_now_authenticates():
    # The door this slice opens — signed with GITHUB's OWN secret.
    forge = _authenticate(FakeRequest({"X-Hub-Signature-256": f"sha256={sig(secret=GH_SECRET)}"}))
    assert forge is not None and forge.name == "github"


@override_settings(**ALL_CREDS)
def test_gitlab_is_refused_by_default_because_it_is_not_body_bound():
    assert _authenticate(FakeRequest({"X-Gitlab-Token": GL_TOKEN})) is None


@override_settings(BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True, **ALL_CREDS)
def test_gitlab_is_accepted_only_when_an_operator_opts_in():
    forge = _authenticate(FakeRequest({"X-Gitlab-Token": GL_TOKEN}))
    assert forge is not None and forge.name == "gitlab"


@override_settings(BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True, **ALL_CREDS)
def test_the_opt_in_does_not_weaken_the_other_forges():
    # Turning GitLab on must not turn the HMAC check off for anyone else.
    assert _authenticate(FakeRequest({"X-Forgejo-Signature": "not-the-signature"})) is None
    assert _authenticate(FakeRequest({"X-Hub-Signature-256": "sha256=nope"})) is None


@override_settings(**ALL_CREDS)
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forgejo-Signature": sig(secret="a-completely-different-secret")},
        {"X-Hub-Signature-256": f"sha256={sig(secret='a-completely-different-secret')}"},
    ],
)
def test_a_wrong_secret_is_refused_for_every_forge(headers):
    assert _authenticate(FakeRequest(headers)) is None


@override_settings(**ALL_CREDS)
def test_a_tampered_body_is_refused():
    r = FakeRequest({"X-Forgejo-Signature": sig()}, body=BODY + b" ")
    assert _authenticate(r) is None


@override_settings(**ALL_CREDS)
def test_no_credential_header_is_refused_and_not_defaulted_to_a_forge():
    # An event header alone must not select a personality.
    assert _authenticate(FakeRequest({"X-GitHub-Event": "push"})) is None
    assert _authenticate(FakeRequest({})) is None


@override_settings(FORGEJO_WEBHOOK_SECRET=None, GITHUB_WEBHOOK_SECRET=None, GITLAB_WEBHOOK_TOKEN=None)
def test_an_unconfigured_bridge_accepts_nothing():
    assert _authenticate(FakeRequest({"X-Forgejo-Signature": sig()})) is None


@override_settings(FORGEJO_WEBHOOK_SECRET=SECRET, GITHUB_WEBHOOK_SECRET=None, GITLAB_WEBHOOK_TOKEN=None, BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True)
def test_a_forge_with_no_credential_is_closed_even_when_others_are_open():
    # Fail closed PER FORGE (Morrow 10146): Forgejo's secret opens Forgejo's
    # door and nobody else's — a correctly-formed GitHub/GitLab delivery is
    # refused while its own setting is unset, opt-in or not.
    assert _authenticate(FakeRequest({"X-Hub-Signature-256": f"sha256={sig()}"})) is None
    assert _authenticate(FakeRequest({"X-Gitlab-Token": SECRET})) is None
    assert _authenticate(FakeRequest({"X-Forgejo-Signature": sig()})) is not None


@override_settings(
    FORGEJO_WEBHOOK_SECRET=SHORT_SECRET, GITHUB_WEBHOOK_SECRET=SHORT_SECRET, GITLAB_WEBHOOK_TOKEN=SHORT_SECRET
)
def test_a_short_credential_refuses_its_forge_not_just_forgejo():
    # The length floor is a property of the bridge, not of one personality.
    for headers in (
        {"X-Forgejo-Signature": sig(secret=SHORT_SECRET)},
        {"X-Hub-Signature-256": f"sha256={sig(secret=SHORT_SECRET)}"},
        {"X-Gitlab-Token": SHORT_SECRET},
    ):
        assert _authenticate(FakeRequest(headers)) is None


class TestCredentialSeparationContainsGitLab:
    """Morrow 10146: the GitLab token travels VERBATIM on the wire while the
    HMAC forges use their credentials as signing keys. These are the executed
    proofs that observing the GitLab token buys nothing at the other doors."""

    @override_settings(BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True, **ALL_CREDS)
    def test_the_valid_gitlab_token_cannot_sign_a_forgejo_delivery(self):
        # The attack from the review: an observer of the echoed GitLab token
        # uses it as an HMAC key. With separated credentials the signature is
        # simply wrong.
        forged = sig(secret=GL_TOKEN)
        assert _authenticate(FakeRequest({"X-Forgejo-Signature": forged})) is None

    @override_settings(BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True, **ALL_CREDS)
    def test_the_valid_gitlab_token_cannot_sign_a_github_delivery(self):
        forged = sig(secret=GL_TOKEN)
        assert _authenticate(FakeRequest({"X-Hub-Signature-256": f"sha256={forged}"})) is None

    @override_settings(BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True, **ALL_CREDS)
    def test_and_the_same_token_still_opens_gitlabs_own_door(self):
        # Sanity for the two refusals above: the token is genuinely valid.
        forge = _authenticate(FakeRequest({"X-Gitlab-Token": GL_TOKEN}))
        assert forge is not None and forge.name == "gitlab"

    @override_settings(
        BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True,
        FORGEJO_WEBHOOK_SECRET=SECRET,
        GITHUB_WEBHOOK_SECRET=GH_SECRET,
        GITLAB_WEBHOOK_TOKEN=SECRET,  # the misconfiguration under test
    )
    def test_gitlab_is_refused_outright_when_its_token_equals_an_hmac_secret(self):
        # Separation that exists only in variable names contains nothing: if
        # an operator reuses the Forgejo secret as the GitLab token, the
        # bridge refuses GitLab (loudly) rather than exposing the HMAC key.
        assert _authenticate(FakeRequest({"X-Gitlab-Token": SECRET})) is None
        # The body-bound doors stay open — the defect is contained, not spread.
        assert _authenticate(FakeRequest({"X-Forgejo-Signature": sig()})) is not None

    @override_settings(
        BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=True,
        FORGEJO_WEBHOOK_SECRET=SECRET,
        GITHUB_WEBHOOK_SECRET=GH_SECRET,
        GITLAB_WEBHOOK_TOKEN=GH_SECRET,  # equals the OTHER hmac secret
    )
    def test_the_equality_refusal_covers_every_hmac_secret_not_just_forgejos(self):
        assert _authenticate(FakeRequest({"X-Gitlab-Token": GH_SECRET})) is None


class TestUnsignedBodyOptInReachesDjango:
    """RC 3170: the opt-in must be loadable from the ENVIRONMENT, not merely
    settable in tests. `override_settings` writes the object the gate reads,
    which proves nothing about whether an env var ever reaches it — the exact
    defect this closes (the getter existed, the setting was never defined, and
    a deployed `BRIDGE_ALLOW_UNSIGNED_BODY_FORGES=1` was invisible to Django).
    So: boot a fresh interpreter the way a deploy does and read the value back.
    """

    def _booted_value(self, env_value):
        import os
        import subprocess
        import sys

        env = os.environ.copy()
        env.pop("BRIDGE_ALLOW_UNSIGNED_BODY_FORGES", None)
        if env_value is not None:
            env["BRIDGE_ALLOW_UNSIGNED_BODY_FORGES"] = env_value
        env.setdefault("DJANGO_SETTINGS_MODULE", "plane.settings.test")
        api_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
        )
        run = subprocess.run(
            [
                sys.executable,
                "-c",
                "import django; django.setup(); from django.conf import settings; "
                "print(settings.BRIDGE_ALLOW_UNSIGNED_BODY_FORGES)",
            ],
            env=env,
            cwd=api_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert run.returncode == 0, run.stderr[-1000:]
        return run.stdout.strip()

    def test_env_1_loads_as_true(self):
        assert self._booted_value("1") == "True"

    def test_unset_is_fail_closed_false(self):
        assert self._booted_value(None) == "False"

    def test_env_0_is_false(self):
        assert self._booted_value("0") == "False"
