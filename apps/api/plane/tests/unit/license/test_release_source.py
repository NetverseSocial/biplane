# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""biplane (BIP-32): the update check looks at OUR releases, and says UNKNOWN
when it cannot tell.

Two things were wrong with the upstream source: it pointed at
`makeplane/plane`, and on failure it returned the RUNNING version as the
latest, so an unreachable check rendered as "up to date".

SCOPE (Morrow RC 3259): discovery and logging only. An earlier revision of this
file said the `Instance` storage "was already there" and only the source needed
replacing. It was not — `current_version`/`latest_version` are a PLANE-namespaced
pair, and a Biplane tag cannot go in one half of it. The storage-contract tests
at the bottom pin that end to end.
"""

import json as jsonlib
from unittest import mock

import pytest
import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from plane.license.utils import release_source
from plane.license.utils.release_source import (
    SOURCE_FORGEJO,
    SOURCE_GITHUB,
    fetch_release_metadata_by_tag,
    fetch_latest_release_metadata,
)


def fetch_latest_release():
    """Test-local convenience over THE one fetch path: (tag, source). The
    production wrapper of this shape was removed with its last caller (Morrow
    on #54 — dead adapters invite resurrection); these tests keep asserting on
    the tag because that is what they pin, via the metadata function that
    actually ships."""
    release, source = fetch_latest_release_metadata()
    return (release["tag"] if release else None), source

FORGEJO = dict(BIPLANE_FORGEJO_URL="http://forge.test:3000", BIPLANE_FORGEJO_REPO="example/biplane")
GITHUB = dict(BIPLANE_GITHUB_REPO="NetverseSocial/biplane")


def _resp(payload, status=200):
    """A response in the BOUNDED-TRANSPORT shape (M5 unification): the fetch
    path streams bodies (`iter_content`) and reads `status_code` directly —
    `.json()`/`raise_for_status` no longer exist on this path."""
    m = mock.Mock()
    m.status_code = status
    m.headers = {"content-type": "application/json"}
    body = jsonlib.dumps(payload).encode()
    m.iter_content = lambda chunk_size: iter([body])
    m.close = lambda: None
    return m


@override_settings(**{**FORGEJO, **GITHUB})
def test_forgejo_is_preferred_and_its_source_is_reported():
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": "v1.2.0"})) as g:
        version, source = fetch_latest_release()
    assert (version, source) == ("v1.2.0", SOURCE_FORGEJO)
    # OUR forge, and only one call — GitHub is not consulted when Forgejo answers.
    assert g.call_count == 1
    assert "forge.test:3000" in g.call_args[0][0] and "example/biplane" in g.call_args[0][0]


@override_settings(**{**FORGEJO, **GITHUB})
def test_falls_back_to_our_github_mirror_when_forgejo_is_unreachable():
    with mock.patch.object(
        requests, "get", side_effect=[RuntimeError("forge down"), _resp({"tag_name": "v1.3.0"})]
    ) as g:
        version, source = fetch_latest_release()
    assert (version, source) == ("v1.3.0", SOURCE_GITHUB)
    assert "NetverseSocial/biplane" in g.call_args[0][0]


@override_settings(**{**FORGEJO, **GITHUB})
def test_never_queries_upstream_plane():
    """The owner ruling: our repos, never Plane's. Their tags are not ours."""
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": "v1.2.0"})) as g:
        fetch_latest_release()
    for call in g.call_args_list:
        assert "makeplane" not in call[0][0]
        assert "plane/releases" not in call[0][0].replace("example/biplane", "")


@override_settings(**{**FORGEJO, **GITHUB})
def test_both_sources_unreachable_is_UNKNOWN_not_current():
    """The dangerous half of the old behaviour. A check that cannot reach its
    source returned the RUNNING version, so a network error looked like being
    up to date. UNKNOWN must be unambiguous."""
    with mock.patch.object(requests, "get", side_effect=RuntimeError("no network")):
        version, source = fetch_latest_release()
    assert version is None and source is None


@override_settings(**{**FORGEJO, **GITHUB})
def test_a_200_with_no_tag_name_is_UNKNOWN_not_a_blank_version():
    with mock.patch.object(requests, "get", return_value=_resp({})):
        assert fetch_latest_release() == (None, None)


@override_settings(**{**FORGEJO, **GITHUB})
def test_a_non_string_tag_is_rejected():
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": 17})):
        assert fetch_latest_release() == (None, None)


@override_settings(BIPLANE_GITHUB_REPO="NetverseSocial/biplane", BIPLANE_FORGEJO_URL=None, BIPLANE_FORGEJO_REPO=None)
def test_github_only_when_no_forge_is_configured():
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": "v2.0.0"})) as g:
        version, source = fetch_latest_release()
    assert (version, source) == ("v2.0.0", SOURCE_GITHUB)
    assert g.call_count == 1


@override_settings(BIPLANE_FORGEJO_URL=None, BIPLANE_FORGEJO_REPO=None, BIPLANE_GITHUB_REPO=None)
def test_nothing_configured_is_UNKNOWN_and_makes_no_requests():
    with mock.patch.object(requests, "get") as g:
        assert fetch_latest_release() == (None, None)
    g.assert_not_called()


def test_registration_makes_no_release_fetch_at_all():
    """RC 3392 #4: the scheduled update service is the SOLE latest-check
    owner. Registration neither fetches nor reports a latest release — the
    helpers are GONE, not merely unused (`check_for_latest_version` was
    removed under RC 3259 for inviting the forbidden write;
    `report_latest_release` followed under RC 3392 for being a second
    checker). The module no longer even imports the fetch."""
    import inspect

    from plane.license.management.commands import register_instance

    assert not hasattr(register_instance.Command, "check_for_latest_version")
    assert not hasattr(register_instance.Command, "report_latest_release")
    assert not hasattr(register_instance, "fetch_latest_release")
    source = inspect.getsource(register_instance)
    assert "fetch_latest_release" not in source
    # No write site: neither keyword form nor attribute assignment.
    assert "biplane_latest_version=" not in source
    assert "instance.biplane_latest" not in source


# ---------------------------------------------------------------------------
# Morrow RC 3250. Three connected blockers, all real:
#   1. BIPLANE_GITHUB_REPO existed ONLY in these tests' override_settings and
#      was never loaded in shipped settings — so the fallback worked in the
#      suite and was ABSENT in production. A setting that exists only under
#      override_settings is a false green.
#   2. Forgejo carried no credential, and example/biplane is PRIVATE (probed:
#      unauthenticated → 404), so the preferred source could never answer.
#   3. The returned source was printed and discarded, contrary to the claim
#      that it was stored for the UI. Claim narrowed rather than faked.
# ---------------------------------------------------------------------------


def test_the_settings_are_bound_in_shipped_settings_not_only_in_tests():
    """The false green this closes: without these in common.py, the GitHub
    fallback is unreachable in production no matter what the suite says."""
    from django.conf import settings as django_settings

    for name in (
        "BIPLANE_FORGEJO_URL",
        "BIPLANE_FORGEJO_REPO",
        "BIPLANE_FORGEJO_RELEASE_TOKEN",
        "BIPLANE_GITHUB_REPO",
    ):
        assert hasattr(django_settings, name), f"{name} is not bound in shipped settings"


@override_settings(**{**FORGEJO, **GITHUB}, BIPLANE_FORGEJO_RELEASE_TOKEN="forge-secret")
def test_forgejo_request_carries_the_token():
    """example/biplane is private; unauthenticated returns 404, so the preferred
    source is unreachable without this."""
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": "v1.2.0"})) as g:
        fetch_latest_release()
    headers = g.call_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "token forge-secret"


@override_settings(**{**FORGEJO, **GITHUB}, BIPLANE_FORGEJO_RELEASE_TOKEN="forge-secret")
def test_the_forge_credential_is_NEVER_forwarded_to_github():
    """No cross-forge credential forwarding. If Forgejo cannot answer we fall
    back to a DIFFERENT host, which must not receive our forge token."""
    with mock.patch.object(
        requests, "get", side_effect=[RuntimeError("forge down"), _resp({"tag_name": "v1.3.0"})]
    ) as g:
        version, source = fetch_latest_release()

    assert (version, source) == ("v1.3.0", SOURCE_GITHUB)
    github_call = g.call_args_list[-1]
    assert "api.github.com" in github_call[0][0]
    headers = github_call.kwargs.get("headers") or {}
    assert "Authorization" not in headers, "forge credential leaked to the GitHub fallback"
    assert "forge-secret" not in str(headers)


@override_settings(**{**FORGEJO, **GITHUB}, BIPLANE_FORGEJO_RELEASE_TOKEN=None)
def test_no_token_configured_sends_no_authorization_header():
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": "v1.2.0"})) as g:
        fetch_latest_release()
    headers = g.call_args.kwargs.get("headers") or {}
    assert "Authorization" not in headers


@override_settings(BIPLANE_FORGEJO_URL="http://forge.test:3000", BIPLANE_FORGEJO_REPO=None, **GITHUB)
def test_forgejo_needs_BOTH_url_and_repo_or_it_is_skipped():
    """Half-configured is not configured — a URL built from a missing repo
    would request a nonsense path and look like an unreachable forge."""
    with mock.patch.object(requests, "get", return_value=_resp({"tag_name": "v9.9.9"})) as g:
        version, source = fetch_latest_release()
    assert source == SOURCE_GITHUB
    assert g.call_count == 1


# ---------------------------------------------------------------------------
# Morrow RC 3252: binding the settings in common.py was ONE LAYER TOO HIGH.
# Compose injected none of the four vars and env.example documented none, so
# the API entrypoint could never receive them — hasattr(settings) passes while
# the container has nothing. Same error as before, one layer down: I proved the
# link I had just written instead of the whole chain.
# ---------------------------------------------------------------------------


def test_stock_github_fallback_is_the_canonical_repo_not_none():
    """With NOTHING configured, the fallback must still resolve. Previously the
    stock value was None, so an out-of-the-box install had no fallback at all —
    the feature existed only for someone who had already set an env var."""
    from django.conf import settings as django_settings

    assert django_settings.BIPLANE_GITHUB_REPO == "NetverseSocial/biplane"


def test_stock_settings_produce_the_exact_github_release_url():
    assert (
        release_source._github_releases_url()
        == "https://api.github.com/repos/NetverseSocial/biplane/releases/latest"
    )


def _repo_root():
    """Repo root, resolved and FAIL-CLOSED.

    Morrow RC 3255: the previous version used `parents[4].parent`, which lands
    on `<repo>/apps` and made these tests look under `apps/deployments`. They
    skipped in the container AND in a full checkout — so my note that they
    "will run in a full checkout" was false. A skip that can never not-skip is
    a test that does not exist, wearing the costume of one.

    This file is <root>/apps/api/plane/tests/unit/license/test_release_source.py
    so the root is parents[6]. Verified by asserting a landmark, and raising
    rather than skipping if it is wrong — a misresolved path must fail the
    test, not silently excuse it.
    """
    import pathlib

    # Walk UP looking for the landmark rather than counting parents. A fixed
    # index encodes one directory layout and breaks silently in another — which
    # is exactly how the previous version ended up pointing at apps/deployments
    # and skipping everywhere. Searching for what we actually need works in a
    # full checkout and in any mount that includes the root.
    here = pathlib.Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "deployments" / "selfhost" / "docker-compose.override.yml").exists():
            return candidate

    raise AssertionError(
        "could not locate the repo root by walking up from "
        f"{here} — no ancestor contains deployments/selfhost/docker-compose.override.yml. "
        "Failing rather than skipping: a path bug must not read as coverage. "
        "Run against a FULL checkout, not an apps/api-only mount."
    )


def test_compose_scopes_the_update_check_vars_to_api_and_worker_only():
    """The chain the settings test cannot see: compose → container env →
    os.environ → settings.

    Morrow RC 3255, second half: credential spread for no functional gain —
    the token was injected into api, worker AND beat-worker when only api
    used it. The RULE is least privilege, not a fixed count: with the M5
    unification the WORKER executes the scheduled check (beat only schedules
    it), so api (register_instance) and worker (check task) each need the
    source config — and beat-worker still gets NOTHING. Two occurrences is
    now the least-privilege shape; three would again be spread.
    """
    import re

    text = (_repo_root() / "deployments/selfhost/docker-compose.override.yml").read_text()
    for var in (
        "BIPLANE_FORGEJO_URL",
        "BIPLANE_FORGEJO_REPO",
        "BIPLANE_FORGEJO_RELEASE_TOKEN",
        "BIPLANE_GITHUB_REPO",
    ):
        found = len(re.findall(rf"^\s+{var}:", text, re.M))
        assert found == 2, (
            f"{var} appears {found}x in compose; it belongs to api (register) "
            "and worker (check task) ONLY — beat-worker gets nothing"
        )


def test_env_example_documents_the_four_vars():
    """An operator who never reads the source still needs to find these."""
    text = (_repo_root() / "deployments/selfhost/env.example").read_text()
    for var in (
        "BIPLANE_FORGEJO_URL",
        "BIPLANE_FORGEJO_REPO",
        "BIPLANE_FORGEJO_RELEASE_TOKEN",
        "BIPLANE_GITHUB_REPO",
    ):
        assert f"{var}=" in text, f"{var} is undocumented in env.example"


# ---------------------------------------------------------------------------
# Morrow RC 3259 — THE STORAGE CONTRACT.
#
# The blocker was semantic, not a URL: `current_version` is PLANE's namespace
# (APP_VERSION / root package.json — 1.3.1 here, rendered by the admin as
# "on Plane CE v{...}") while this feature discovers a BIPLANE release tag.
# `InstanceSerializer` exposes both via fields = "__all__", so persisting the
# tag in the paired field publishes "current Plane CE 1.3.1, latest Biplane
# v1.0.0" as one comparable sequence. His bar, quoted:
#
#   "a stock build must never expose or persist Plane 1.3.1 and Biplane v1.0.0
#    as current/latest of the same product"
#
# These run against a real database and drive the actual management command,
# because the claim is about what is STORED. A mocked save cannot see it.
# ---------------------------------------------------------------------------

PLANE_NS = "1.3.1"  # what check_for_current_version() yields in this tree
BIPLANE_NS = "v1.0.0"  # what a release source hands back


def _run_register(build=None, version=None):
    """Drive register_instance. It performs NO release fetch (RC 3392 #4), so
    there is nothing network-shaped to mock — only the baked identity."""
    from django.core.management import call_command
    from django.test import override_settings as _os

    with mock.patch.dict("os.environ", {"APP_VERSION": PLANE_NS}), mock.patch(
        "plane.license.management.commands.register_instance.instance_traces"
    ) as traces, _os(BIPLANE_BUILD=build, BIPLANE_VERSION=version):
        call_command("register_instance", "test-machine-signature")
    return traces


@pytest.mark.django_db
def test_a_fresh_install_never_persists_the_two_namespaces_as_a_pair():
    """Morrow's RC 3259 bar, still literal after RC 3392: the Plane pair is
    never touched with Biplane values, and registration writes NO latest at
    all — installed identity only."""
    from plane.license.models import Instance

    assert Instance.objects.count() == 0
    _run_register(build="06bcb6f", version=BIPLANE_NS)

    instance = Instance.objects.get()
    assert instance.current_version == PLANE_NS
    assert instance.latest_version in (None, "")
    # Installed identity recorded; latest belongs to the service alone.
    assert instance.biplane_installed_build == "06bcb6f"
    assert instance.biplane_installed_version == BIPLANE_NS
    assert instance.biplane_latest_version is None
    assert instance.biplane_latest_source is None
    assert instance.biplane_latest_checked_at is None


@pytest.mark.django_db
def test_registration_never_touches_what_the_service_last_knew():
    """RC 3392 #4, the ownership rule from the other side: values the SOLE
    owner (the scheduled service) wrote survive a re-registration untouched —
    including Plane-namespaced remnants an older build left."""
    from django.utils import timezone

    from plane.license.models import Instance

    instance = Instance.objects.create(
        instance_name="Plane Community Edition",
        instance_id="deadbeefcafe",
        current_version="1.3.0",
        latest_version="1.3.1",  # Plane-namespaced, written by an older build
        last_checked_at=timezone.now(),
        biplane_latest_version="v1.1.0",  # written by the service
        biplane_latest_source="forgejo",
        biplane_latest_checked_at=timezone.now(),
    )

    _run_register(build="06bcb6f", version=BIPLANE_NS)

    instance.refresh_from_db()
    assert instance.current_version == PLANE_NS  # this IS updated
    assert instance.latest_version == "1.3.1"  # Plane pair NOT touched
    assert instance.biplane_latest_version == "v1.1.0"  # service's value survives
    assert instance.biplane_latest_source == "forgejo"
    assert instance.biplane_installed_version == BIPLANE_NS


@pytest.mark.django_db
def test_the_service_is_the_sole_latest_writer_end_to_end():
    """Registration then one service check: latest lands via the service and
    only the service — the ownership split executed, not asserted."""
    from plane.license.models import Instance
    from plane.license.services import update_check as svc

    _run_register(build="06bcb6f", version="v1.0.0")
    instance = Instance.objects.get()
    assert instance.biplane_latest_version is None  # registration wrote none

    latest = {"tag": "v1.2.0", "level": "code", "changelog_url": None}
    payload = svc.run_update_check(fetch=lambda: (latest, SOURCE_FORGEJO))
    assert payload["state"] == "update_available"
    instance.refresh_from_db()
    assert instance.biplane_latest_version == "v1.2.0"
    assert instance.biplane_latest_source == SOURCE_FORGEJO


@pytest.mark.django_db
def test_the_serialized_instance_carries_no_cross_namespace_pair():
    """`fields = "__all__"` means the API surface is the model. Assert on what
    a client actually receives rather than on the row alone — the exposure is
    half of what RC 3259 objected to."""
    from plane.license.models import Instance
    from plane.license.api.serializers import InstanceSerializer

    _run_register(build="06bcb6f", version=BIPLANE_NS)

    data = InstanceSerializer(Instance.objects.get()).data
    assert data["current_version"] == PLANE_NS
    assert data.get("latest_version") in (None, "")
    assert data["biplane_installed_version"] == BIPLANE_NS
    assert BIPLANE_NS not in {data.get("current_version"), data.get("latest_version")}


# ---------------------------------------------------------------------------
# BIP-36 — the Biplane version namespace, split out of BIP-32.
#
# BIP-32 could only REPORT the discovered release because there was nowhere
# correct to store it. These fields are that somewhere. The bar carried over
# from RC 3259 is unchanged and is asserted below: a stock build must never
# expose or persist Plane 1.3.1 and Biplane v1.0.0 as current/latest of the
# same product. The Plane pair stays untouched; Biplane gets its own.
# ---------------------------------------------------------------------------

BIPLANE_BUILD_ID = "06bcb6f"


@pytest.mark.django_db
def test_unset_build_and_version_record_UNKNOWN_rather_than_guessing():
    """A dev image bakes neither value; registration stores NULL for both —
    and the update check reads the NULL version as an honest UNKNOWN rather
    than comparing a guess."""
    from plane.license.models import Instance

    _run_register(build=None, version=None)
    instance = Instance.objects.get()
    assert instance.biplane_installed_build is None
    assert instance.biplane_installed_version is None


def test_BIPLANE_BUILD_and_VERSION_are_bound_in_shipped_settings():
    """RC 3250/3252 in one line: a setting that exists only under
    override_settings, or that compose injects into nothing, is a false green.
    BIPLANE_VERSION joins under RC 3392 #2 — same bake, same rule."""
    from django.conf import settings as django_settings

    assert hasattr(django_settings, "BIPLANE_BUILD")
    assert hasattr(django_settings, "BIPLANE_VERSION")


def test_BIPLANE_BUILD_is_BAKED_INTO_THE_IMAGE_and_compose_does_not_blank_it():
    """Morrow RC 3271. My first version of this test asserted the OPPOSITE and
    was itself the defect — it required `BIPLANE_BUILD:` to appear once in
    compose, which is precisely the line that broke the feature.

    Compose environment WINS over image ENV, and `${BIPLANE_BUILD:-}` resolves
    to the empty string when the var is absent from .env. So every stock,
    repo-supported build silently stored NULL: the field existed and nothing
    could ever fill it. Same shape as RC 3255, where my test asserted three
    occurrences and thereby encoded the credential overexposure it should have
    caught. A test that pins the broken arrangement is worse than no test.

    The build id belongs to the IMAGE. An operator-supplied runtime value can
    disagree with the code actually running, which is worse than UNKNOWN
    because it looks authoritative.
    """
    import re

    root = _repo_root()

    compose = (root / "deployments/selfhost/docker-compose.override.yml").read_text()
    live = [ln for ln in compose.splitlines() if re.match(r"^\s+BIPLANE_BUILD:", ln)]
    assert not live, (
        "compose sets BIPLANE_BUILD, which OVERRIDES the value baked into the "
        f"image and blanks it when unset: {live}"
    )

    dockerfile = (root / "apps/api/Dockerfile.api").read_text()
    assert re.search(r"^ARG BIPLANE_BUILD=", dockerfile, re.M), "Dockerfile.api declares no BIPLANE_BUILD ARG"
    assert re.search(r"^ENV BIPLANE_BUILD=", dockerfile, re.M), "Dockerfile.api never promotes the ARG to ENV"
    # BIPLANE_VERSION (RC 3392 #2): the comparable release tag, same bake rule.
    assert re.search(r"^ARG BIPLANE_VERSION=", dockerfile, re.M), "Dockerfile.api declares no BIPLANE_VERSION ARG"
    assert re.search(r"^ENV BIPLANE_VERSION=", dockerfile, re.M), "Dockerfile.api never promotes BIPLANE_VERSION to ENV"

    build_sh = (root / "deployments/selfhost/build-images.sh").read_text()
    assert '--build-arg BIPLANE_BUILD="${BUILD_ID}"' in build_sh, (
        "build-images.sh does not supply BIPLANE_BUILD to the backend image, so "
        "no repo-supported build can populate biplane_installed_build"
    )
    assert '--build-arg BIPLANE_VERSION="${BIPLANE_RELEASE_TAG:-}"' in build_sh, (
        "build-images.sh does not pass the release tag through, so no release "
        "build can populate biplane_installed_version"
    )
    # Same variable as the frontends: backend and UI must not be able to
    # disagree. COUNTED, not substring-matched (Sable RC 3813, executed): the
    # literal appears once per frontend image, so `in` is satisfied by either
    # alone — drop the web build-arg and both web surfaces say "dev build" on
    # a real release while admin shows the version, and nothing goes red.
    # Surfaces disagreeing is worse than the original bug: whichever one a
    # person checks is the answer they get.
    assert build_sh.count('--build-arg VITE_BIPLANE_BUILD="${BUILD_ID}"') == 2, (
        "VITE_BIPLANE_BUILD must be supplied to BOTH frontend images (web and admin)"
    )
    assert build_sh.count('--build-arg VITE_BIPLANE_VERSION="${BIPLANE_RELEASE_TAG:-}"') == 2, (
        "VITE_BIPLANE_VERSION must be supplied to BOTH frontend images (web and admin)"
    )
    for df in ("apps/web/Dockerfile.web", "apps/admin/Dockerfile.admin"):
        text = (root / df).read_text()
        assert re.search(r"^ARG VITE_BIPLANE_VERSION=", text, re.M), f"{df} declares no VITE_BIPLANE_VERSION ARG"
        assert re.search(r"^ENV VITE_BIPLANE_VERSION=", text, re.M), f"{df} never promotes VITE_BIPLANE_VERSION to ENV"

    # And neither may be reintroduced as a runtime knob.
    compose_text = compose
    env_example = (root / "deployments/selfhost/env.example").read_text()
    # Anchored (Vex RC 3811): the bare substring also matched
    # VITE_BIPLANE_VERSION= — right outcome, wrong blame, had anyone added
    # the frontend var to env.example.
    assert not re.search(r"^BIPLANE_BUILD=", env_example, re.M)
    assert not re.search(r"^BIPLANE_VERSION=", env_example, re.M)
    assert not [ln for ln in compose_text.splitlines() if re.match(r"^\s+BIPLANE_VERSION:", ln)], (
        "compose sets BIPLANE_VERSION, which would override the baked value"
    )


# ---------------------------------------------------------------------------
# M5 unification (2026-08-12): the metadata surface for the update check.
# ---------------------------------------------------------------------------

from plane.license.utils.release_source import fetch_latest_release_metadata


ASSET_URL = "http://forge.test:3000/attachments/release-json-uuid"


def _release_payload(tag="v1.2.3", with_asset=True, html_url=None):
    """A REAL provider-shaped release object (Forgejo/GitHub /releases/latest):
    tag_name + html_url + assets[] — level is NOT a field these APIs have
    (RC 3392 #3); it rides only in the release.json ASSET."""
    payload = {
        "tag_name": tag,
        "html_url": html_url or f"http://forge.test:3000/example/biplane/releases/tag/{tag}",
        "assets": [],
    }
    if with_asset:
        payload["assets"] = [
            {"name": "release.json", "browser_download_url": ASSET_URL},
            {"name": "notes.txt", "browser_download_url": ASSET_URL + "-notes"},
        ]
    return payload


def _release_json(tag="v1.2.3", **overrides):
    """7of9's literal producer bytes (#55, reconciled 2026-08-12): keys are
    `image` and `digest`, schema_version is the JSON integer 1."""
    doc = {
        "schema_version": 1,
        "tag": tag,
        "commit_sha": "a" * 40,
        "level": "data",
        "images": [
            {"image": "ghcr.io/netversesocial/biplane-backend", "digest": "sha256:" + "b" * 64},
            {"image": "ghcr.io/netversesocial/biplane-web", "digest": "sha256:" + "c" * 64},
            {"image": "ghcr.io/netversesocial/biplane-admin", "digest": "sha256:" + "d" * 64},
            {"image": "ghcr.io/netversesocial/biplane-space", "digest": "sha256:" + "e" * 64},
        ],
    }
    doc.update(overrides)
    return doc


def _serve(responses):
    """Patch requests.get with a url->response map (default 404)."""
    def fake_get(url, **kwargs):
        return responses.get(url, _resp({}, status=404))
    return mock.patch.object(requests, "get", side_effect=fake_get)


@override_settings(**{**FORGEJO, **GITHUB})
def test_level_comes_from_the_release_json_asset_and_only_from_it():
    """RC 3392 #3 + the #55 contract: the provider object cannot carry level;
    the producer's release.json asset does. Changelog link stays origin-
    validated provider metadata."""
    responses = {
        "http://forge.test:3000/api/v1/repos/example/biplane/releases/latest": _resp(_release_payload()),
        ASSET_URL: _resp(_release_json()),
    }
    with _serve(responses):
        release, source = fetch_latest_release_metadata()
    assert source == SOURCE_FORGEJO
    assert release["tag"] == "v1.2.3"
    assert release["level"] == "data"
    assert release["changelog_url"] == "http://forge.test:3000/example/biplane/releases/tag/v1.2.3"


@override_settings(**{**FORGEJO, **GITHUB})
def test_release_without_the_asset_still_answers_with_null_level():
    responses = {
        "http://forge.test:3000/api/v1/repos/example/biplane/releases/latest": _resp(
            _release_payload(with_asset=False)
        ),
    }
    with _serve(responses):
        release, _ = fetch_latest_release_metadata()
    assert release["tag"] == "v1.2.3"
    assert release["level"] is None


@override_settings(**{**FORGEJO, **GITHUB})
@pytest.mark.parametrize(
    "bad_asset",
    [
        _release_json(tag="v9.9.9"),  # copied across releases: tag mismatch
        _release_json(schema_version=2),  # future producer: unreadable claims
        _release_json(schema_version=True),  # bool is not the int 1
        _release_json(images=[]),  # producer gate forbids empty: tampering
        _release_json(images=[{"image": "x", "digest": "sha256:short"}]),
        _release_json(level="yolo"),
        _release_json(commit_sha="abc"),
        {"schema_version": 1},  # missing everything
        "not an object",
    ],
)
def test_a_refused_release_json_degrades_detail_never_the_answer(bad_asset):
    """Every deviation IGNORES the whole asset — level renders null — while
    the version answer stands on tag_name. A future schema_version degrades
    the banner detail, never availability."""
    responses = {
        "http://forge.test:3000/api/v1/repos/example/biplane/releases/latest": _resp(_release_payload()),
        ASSET_URL: _resp(bad_asset),
    }
    with _serve(responses):
        release, source = fetch_latest_release_metadata()
    assert source == SOURCE_FORGEJO
    assert release["tag"] == "v1.2.3"  # availability intact
    assert release["level"] is None  # detail degraded


@override_settings(**{**FORGEJO, **GITHUB})
def test_an_unreachable_asset_degrades_detail_never_the_answer():
    responses = {
        "http://forge.test:3000/api/v1/repos/example/biplane/releases/latest": _resp(_release_payload()),
        # ASSET_URL deliberately absent -> 404
    }
    with _serve(responses):
        release, _ = fetch_latest_release_metadata()
    assert release["tag"] == "v1.2.3"
    assert release["level"] is None


@override_settings(**{**FORGEJO, **GITHUB})
def test_hostile_changelog_origin_and_unknown_level_do_not_pass_through():
    responses = {
        "http://forge.test:3000/api/v1/repos/example/biplane/releases/latest": _resp(
            _release_payload(html_url="https://evil.example/phish")
        ),
        ASSET_URL: _resp(_release_json(level="data")),
    }
    with _serve(responses):
        release, _ = fetch_latest_release_metadata()
    assert release["changelog_url"] is None  # hostile origin renders no link
    assert release["level"] == "data"  # asset detail unaffected


@override_settings(BIPLANE_FORGEJO_URL=None, BIPLANE_FORGEJO_REPO=None, **GITHUB)
def test_github_release_page_link_is_trusted_for_the_mirror():
    payload = {
        "tag_name": "v1.3.0",
        "html_url": "https://github.com/NetverseSocial/biplane/releases/tag/v1.3.0",
    }
    with mock.patch.object(requests, "get", return_value=_resp(payload)):
        release, source = fetch_latest_release_metadata()
    assert source == SOURCE_GITHUB
    assert release["changelog_url"] == payload["html_url"]


def test_the_tag_only_adapter_is_gone_from_production():
    """Morrow on #54: the (tag, source) wrapper survived the removal of its
    last caller. Dead adapters invite resurrection — the shape lives on only
    as the test-local helper above, where its purpose is visible."""
    assert not hasattr(release_source, "fetch_latest_release")


@override_settings(**{**FORGEJO, **GITHUB})
def test_apply_fetches_the_explicit_tag_without_listing_or_latest_selection():
    exact = "http://forge.test:3000/api/v1/repos/example/biplane/releases/tags/v2.4.1"
    responses = {
        exact: _resp(_release_payload(tag="v2.4.1")),
        ASSET_URL: _resp(_release_json(tag="v2.4.1", level="code")),
    }
    with _serve(responses) as get:
        release, source = fetch_release_metadata_by_tag("v2.4.1")

    assert source == SOURCE_FORGEJO
    assert release == {
        "tag": "v2.4.1",
        "commit_sha": "a" * 40,
        "level": "code",
        "images": _release_json(tag="v2.4.1")["images"],
    }
    urls = [call.args[0] for call in get.call_args_list]
    assert urls == [exact, ASSET_URL]
    assert not any("/latest" in url or "?page=" in url for url in urls)


@override_settings(**{**FORGEJO, **GITHUB})
@pytest.mark.parametrize(
    "tag",
    [
        "latest",
        "v2.4",
        "v2.4.1-rc1",
        "../v2.4.1",
        "",
        "v01.2.3",
        "v١.٢.٣",
        "v1000000000.0.0",
        "v2.4.1 ",
    ],
)
def test_apply_refuses_a_non_stable_explicit_tag_before_transport(tag):
    with mock.patch.object(requests, "get") as get:
        assert fetch_release_metadata_by_tag(tag) == (None, None)
    get.assert_not_called()


@override_settings(**{**FORGEJO, **GITHUB})
@pytest.mark.parametrize("tag", ["v01.2.3", "v١.٢.٣", "v1000000000.0.0"])
def test_apply_command_refuses_foreign_grammar_before_transport(tag):
    """The real host adapter must inherit the authority, not only its helper."""
    with mock.patch.object(requests, "get") as get:
        with pytest.raises(CommandError, match="did not resolve"):
            call_command("biplane_update_metadata", tag)
    get.assert_not_called()


@override_settings(**{**FORGEJO, **GITHUB})
@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: {**doc, "unexpected": True},
        lambda doc: {**doc, "images": doc["images"][:-1]},
        lambda doc: {**doc, "images": doc["images"] + [doc["images"][0]]},
        lambda doc: {
            **doc,
            "images": [
                *doc["images"][:-1],
                {"image": "ghcr.io/netversesocial/other", "digest": "sha256:" + "f" * 64},
            ],
        },
        lambda doc: {
            **doc,
            "images": [
                {**doc["images"][0], "extra": "ignored?"},
                *doc["images"][1:],
            ],
        },
    ],
)
def test_apply_requires_the_exact_complete_service_digest_set(mutation):
    tag = "v2.4.1"
    exact = f"http://forge.test:3000/api/v1/repos/example/biplane/releases/tags/{tag}"
    responses = {
        exact: _resp(_release_payload(tag=tag)),
        ASSET_URL: _resp(mutation(_release_json(tag=tag))),
        # GitHub fallback also refuses instead of laundering the bad Forgejo
        # release through a different selection mechanism.
        f"https://api.github.com/repos/NetverseSocial/biplane/releases/tags/{tag}": _resp({}, 404),
    }
    with _serve(responses):
        assert fetch_release_metadata_by_tag(tag) == (None, None)
