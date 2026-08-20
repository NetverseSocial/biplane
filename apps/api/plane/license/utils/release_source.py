# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Where Biplane looks for its own latest release (BIP-32, unified into M5).

OWNER RULING (John, 2026-08-10): check OUR repositories, never Plane's.
Forgejo `example/biplane` first; our public GitHub mirror as fallback. Upstream
`makeplane/plane` releases are NOT a source — their version numbers are not
ours, and an upstream tag says nothing about whether this install is current.

UNIFIED 2026-08-12 (M5 rewrite): this module is THE fetch path for release
metadata, and the scheduled update-check service is its SOLE caller and the
sole owner of the latest-release question (RC 3392 #4 — registration fetches
nothing here any more; the audit had found two checkers that did not know
about each other). The naked `requests.get` was replaced by the bounded
transport (origin allowlist, streamed size cap, manual redirect validation,
wall-clock deadline — see plane.license.utils.bounded_fetch).

A check that cannot reach its source must say UNKNOWN, never "current". A
false all-clear on an update check is worse than no check, because it actively
discourages the upgrade. Returning None here is that UNKNOWN.
"""

import logging
import re
from typing import Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit

from django.conf import settings

from plane.license.utils.bounded_fetch import FetchRefused, bounded_get_json
from plane.license.utils import release_version

logger = logging.getLogger("plane.license")

# Source identifiers — recorded in the M4 columns (`biplane_latest_source`)
# so the UI and the logs can say which forge answered.
SOURCE_FORGEJO = "forgejo"
SOURCE_GITHUB = "github"
SOURCE_CUSTOM = "custom"

# UI-selectable update-server preference (Settings → Updates). "biplane_dev"
# is reserved for the future official host and is refused by the endpoint
# until it exists — the UI shows it disabled, never silently ignored.
UPDATE_SOURCE_KEY = "BIPLANE_UPDATE_SOURCE"
UPDATE_SOURCE_URL_KEY = "BIPLANE_UPDATE_SOURCE_URL"


def _update_source_preference():
    """(preference, custom_url) from instance configuration; ("forgejo", None)
    when unset — the current server is the default and the existing order."""
    from plane.license.models import InstanceConfiguration

    try:
        rows = {
            r.key: r.value
            for r in InstanceConfiguration.objects.filter(key__in=[UPDATE_SOURCE_KEY, UPDATE_SOURCE_URL_KEY])
        }
    except Exception:  # noqa: BLE001 — a check that cannot read its preference
        # still checks: default order beats a crashed beat task (and the
        # release-source unit tests run this path with no database at all).
        logger.info("biplane: update-source preference unavailable; using the default order")
        rows = {}
    pref = rows.get(UPDATE_SOURCE_KEY) or "forgejo"
    if pref not in ("forgejo", "github", "custom"):
        pref = "forgejo"
    # No origin pre-check here: bounded_fetch enforces the allowlist on every
    # fetch and every redirect hop — restating that guarantee would be a
    # second owner of one rule.
    return pref, rows.get(UPDATE_SOURCE_URL_KEY) or None


def _forgejo_releases_url() -> Optional[str]:
    """Forgejo release endpoint for our own repo, if one is configured."""
    base = getattr(settings, "BIPLANE_FORGEJO_URL", None)
    repo = getattr(settings, "BIPLANE_FORGEJO_REPO", None)
    if not base or not repo:
        return None
    return f"{base.rstrip('/')}/api/v1/repos/{repo}/releases/latest"


def _github_releases_url() -> Optional[str]:
    """Our OWN public mirror. Never makeplane/plane."""
    repo = getattr(settings, "BIPLANE_GITHUB_REPO", None)
    if not repo:
        return None
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _forgejo_release_tag_url(tag: str) -> Optional[str]:
    """Forgejo's exact-tag endpoint; apply never selects from a listing."""
    latest = _forgejo_releases_url()
    if latest is None:
        return None
    return f"{latest.rsplit('/', 1)[0]}/tags/{quote(tag, safe='')}"


def _github_release_tag_url(tag: str) -> Optional[str]:
    """GitHub's exact-tag endpoint for the public fallback mirror."""
    latest = _github_releases_url()
    if latest is None:
        return None
    return f"{latest.rsplit('/', 1)[0]}/tags/{quote(tag, safe='')}"


def _forgejo_credential() -> Optional[Tuple[str, Mapping[str, str]]]:
    """Auth for OUR Forgejo, and for nowhere else.

    `example/biplane` is PRIVATE — probed 2026-08-10: an unauthenticated request
    returns 404, so without a token the preferred source can never answer and
    the check would silently always fall through to the mirror.

    Scoped to one origin BY CONSTRUCTION: the bounded transport attaches this
    header only on hops whose exact scheme+host match the Forgejo base URL.
    It never travels to GitHub or any redirect target — forwarding a forge
    credential to a different forge is a credential leak, and it is pinned by
    test.
    """
    base = getattr(settings, "BIPLANE_FORGEJO_URL", None)
    token = getattr(settings, "BIPLANE_FORGEJO_RELEASE_TOKEN", None)
    if not base or not token:
        return None
    return (base, {"Authorization": f"token {token}"})


def _extra_origins() -> tuple:
    """Operator-configured EXTRA origins (comma-separated), unioned with the
    transport defaults — never replacing them. The Forgejo base URL is always
    included when configured, so the preferred source is reachable without
    repeating it here."""
    raw = getattr(settings, "BIPLANE_UPDATE_ALLOWED_ORIGINS", None)
    configured = tuple(
        entry.strip() for entry in (raw or "").split(",") if entry.strip()
    )
    base = getattr(settings, "BIPLANE_FORGEJO_URL", None)
    if base:
        configured = configured + (base,)
    return configured


def _trusted_changelog_url(url: object, trusted_origins) -> Optional[str]:
    """A release's html_url is metadata from the forge response. It is only
    handed to the admin's browser when it points at an origin we would fetch
    from ourselves; anything else renders no link (Rowan 3363 #3 survives the
    redesign — a link is still a link)."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if "@" in parts.netloc or not parts.netloc:
        return None
    for origin in trusted_origins:
        trusted = urlsplit(origin)
        if (trusted.scheme, trusted.netloc) == (parts.scheme, parts.netloc):
            return url
    return None


#: Trusted web origins for changelog links, beyond configured origins:
#: github.com is where our mirror's release pages live (api.github.com serves
#: the API; html_url points at the web host).
_CHANGELOG_WEB_ORIGINS = ("https://github.com",)


#: The producer→consumer contract asset (PR #55, BIP-40 rewrite):
#: {schema_version: 1, tag, commit_sha, level, images: [{image, digest}]}.
RELEASE_JSON_ASSET = "release.json"
RELEASE_JSON_MAX_BYTES = 64 * 1024
_LEVELS = ("code", "data", "full")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXECUTING_IMAGE_BASENAMES = frozenset(
    {"biplane-backend", "biplane-web", "biplane-admin", "biplane-space"}
)


def _release_json_url(payload: dict) -> Optional[str]:
    for asset in payload.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == RELEASE_JSON_ASSET:
            url = asset.get("browser_download_url")
            return url if isinstance(url, str) and url else None
    return None


def _parse_release_json(doc: object, expected_tag: str) -> Optional[dict]:
    """Strict read of the producer's release.json — the whole asset is IGNORED
    (returns None) on any deviation; the version answer never depends on it.

    - schema_version must be exactly the int 1 (bool refused): an UNKNOWN
      version means a future producer whose claims this consumer cannot read —
      degrade the banner detail, never the availability answer.
    - tag must equal the release's own tag_name: an asset copied across
      releases binds nothing (unsigned now, but the lesson still pays rent).
    - level/commit_sha/images mirror exactly what the producer's build gate
      enforces (make-release-metadata.sh) — a v1 asset violating them should
      not exist, so one that does is tampering or a foreign release.
    """
    if not isinstance(doc, dict) or set(doc) != {
        "schema_version",
        "tag",
        "commit_sha",
        "level",
        "images",
    }:
        return None
    version = doc.get("schema_version")
    if isinstance(version, bool) or version != 1:
        return None
    if doc.get("tag") != expected_tag:
        return None
    level = doc.get("level")
    if level not in _LEVELS:
        return None
    commit_sha = doc.get("commit_sha")
    if not isinstance(commit_sha, str) or not _HEX40.match(commit_sha):
        return None
    images = doc.get("images")
    if not isinstance(images, list) or len(images) != len(_EXECUTING_IMAGE_BASENAMES):
        return None
    parsed_images = []
    basenames = set()
    for entry in images:
        if not isinstance(entry, dict) or set(entry) != {"image", "digest"}:
            return None
        image, digest = entry.get("image"), entry.get("digest")
        if not (isinstance(image, str) and image):
            return None
        if not (isinstance(digest, str) and _DIGEST.match(digest)):
            return None
        basename = image.rsplit("/", 1)[-1]
        if basename in basenames:
            return None
        basenames.add(basename)
        parsed_images.append({"image": image, "digest": digest})
    if basenames != _EXECUTING_IMAGE_BASENAMES:
        return None
    return {
        "tag": expected_tag,
        "level": level,
        "commit_sha": commit_sha,
        "images": parsed_images,
    }


def _fetch_release_json(payload: dict, tag: str, credential, origins) -> Optional[dict]:
    """Fetch and strictly read the release.json asset, or None. A missing,
    oversized, unreachable or malformed asset degrades the banner's DETAIL
    (level renders null); it must never degrade or fail the version answer."""
    url = _release_json_url(payload)
    if url is None:
        return None
    try:
        status, doc = bounded_get_json(
            url,
            allowed_origins=origins,
            credential=credential,
            max_bytes=RELEASE_JSON_MAX_BYTES,
        )
    except Exception as e:  # noqa: BLE001 — asset detail must not break the check
        logger.info(f"biplane: release.json fetch failed at {url}: {e}")
        return None
    if status != 200:
        logger.info(f"biplane: release.json fetch at {url} returned {status}")
        return None
    parsed = _parse_release_json(doc, tag)
    if parsed is None:
        logger.info(f"biplane: release.json at {url} refused (schema/tag/shape) — ignored")
    return parsed


def _parse_release(payload: object, source_url: str, trusted_origins) -> Optional[dict]:
    """The small schema the check consumes: a JSON object with a non-empty
    string `tag_name`, with `html_url` (origin-validated) riding along for
    the banner. `level` deliberately does NOT come from this object — the
    forge API schema cannot carry it; it lives only in the producer's
    release.json asset (see _fetch_release_json). Anything else is "no usable
    answer" — never a guess."""
    if not isinstance(payload, dict):
        logger.info(f"biplane: release check at {source_url} returned non-object JSON")
        return None
    tag = payload.get("tag_name")
    if not tag or not isinstance(tag, str):
        logger.info(f"biplane: release check at {source_url} returned no usable tag_name")
        return None
    return {
        "tag": tag,
        "level": None,
        "changelog_url": _trusted_changelog_url(
            payload.get("html_url"), tuple(trusted_origins) + _CHANGELOG_WEB_ORIGINS
        ),
    }


def _fetch_release(url: str, credential=None, extra_origin=None) -> Optional[dict]:
    """Fetch one source's latest release under the transport bounds.
    Returns the parsed metadata, or None. Never raises, never guesses.

    A 404 is normal and expected when a repo has no published release yet —
    probed 2026-08-10: Forgejo `example/biplane` answers 404 on `/releases/latest`.
    That is "no answer", reported as UNKNOWN rather than an invented version.
    """
    origins = list(_extra_origins() or ())
    if extra_origin:
        # The saved custom update server authorizes its OWN origin: the admin
        # typing it into Settings is the declaration (John's ruling — forks
        # run their own update servers and the page must be enough). Threaded
        # as a parameter so the shared fetch path takes no ambient DB read;
        # the fetch primitive remains the sole enforcer.
        origins.append(extra_origin)
    try:
        status, payload = bounded_get_json(
            url, allowed_origins=origins, credential=credential
        )
    except Exception as e:  # noqa: BLE001 — an update check must never break the caller
        logger.info(f"biplane: release check could not reach {url}: {e}")
        return None
    if status != 200:
        logger.info(f"biplane: release check at {url} returned {status}")
        return None
    release = _parse_release(payload, url, origins)
    if release is None:
        return None
    # The producer's release.json asset carries what the forge API schema
    # cannot (level; commit_sha/images for the apply path). Its absence or
    # refusal degrades the banner's DETAIL only — never the version answer.
    detail = _fetch_release_json(payload, release["tag"], credential, origins)
    if detail:
        release["level"] = detail["level"]
    return release


def _fetch_release_for_apply(
    url: str, expected_tag: str, credential=None, extra_origin=None
) -> Optional[dict]:
    """Fetch one exact release and require its complete executable identity.

    Unlike the banner, apply cannot degrade a missing/refused ``release.json``
    to partial detail: there is nothing safe to pull without the exact image
    set, level and commit identity. The requested tag is supplied by the
    operator and checked against both the forge object and the asset.
    """
    origins = list(_extra_origins() or ())
    if extra_origin:
        # The saved custom update server authorizes its OWN origin: the admin
        # typing it into Settings is the declaration (John's ruling — forks
        # run their own update servers and the page must be enough). Threaded
        # as a parameter so the shared fetch path takes no ambient DB read;
        # the fetch primitive remains the sole enforcer.
        origins.append(extra_origin)
    try:
        status, payload = bounded_get_json(
            url, allowed_origins=origins, credential=credential
        )
    except Exception as error:  # noqa: BLE001 - fallback source may still answer
        logger.info("biplane apply: exact release fetch failed at %s: %s", url, error)
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    if payload.get("tag_name") != expected_tag:
        logger.warning(
            "biplane apply: exact-tag endpoint %s returned tag %r, expected %r",
            url,
            payload.get("tag_name"),
            expected_tag,
        )
        return None
    detail = _fetch_release_json(payload, expected_tag, credential, origins)
    if detail is None:
        return None
    return detail


def _custom_release_tag_url(base_url: str, tag: str) -> str:
    """Exact-tag endpoint for a custom update server. THE CONVENTION: the
    saved URL answers the LATEST release; the same URL + /tags/<tag> answers
    one exact release, in the same Forgejo/GitHub-style JSON shape. A static
    host (the Biplane.dev model) satisfies this with plain files."""
    return f"{base_url.rstrip('/')}/tags/{quote(tag, safe='')}"


def fetch_release_metadata_by_tag(
    tag: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Return complete apply metadata for one explicit stable release tag.

    There is deliberately no ``latest`` mode and no release-list traversal in
    this function. Selection is operator-owned; the fetch proves that the
    selected release exists and carries one complete executable identity.
    """
    if not release_version.is_valid(tag):
        return None, None

    # The selected custom server is tried FIRST for apply metadata too —
    # a source that can flag a release must be able to resolve it, or the
    # button and automatic mode dead-end on a custom deployment (Morrow's
    # hold on this branch). Same authorization as the check: the saved URL
    # declares its own origin. Fallbacks stay behind it.
    preference, custom_url = _update_source_preference()
    if preference == "custom" and custom_url:
        release = _fetch_release_for_apply(
            _custom_release_tag_url(custom_url, tag), tag, extra_origin=custom_url
        )
        if release is not None:
            return release, SOURCE_CUSTOM
        # NO fallback for apply when a custom server is selected (Vex 3905's
        # collision case): a fork tracking upstream versions collides on tags
        # BY CONSTRUCTION, so falling back would install a different
        # project's release under a tag the fork's own server advertised.
        # The check may fall back — a wrong banner is recoverable; a wrong
        # install is not. Refusing here surfaces as apply's honest
        # cannot-resolve rather than a silent wrong artifact.
        return None, None

    forgejo_url = _forgejo_release_tag_url(tag)
    if forgejo_url:
        release = _fetch_release_for_apply(
            forgejo_url, tag, credential=_forgejo_credential()
        )
        if release is not None:
            return release, SOURCE_FORGEJO

    github_url = _github_release_tag_url(tag)
    if github_url:
        release = _fetch_release_for_apply(github_url, tag)
        if release is not None:
            return release, SOURCE_GITHUB

    return None, None


def fetch_latest_release_metadata() -> Tuple[Optional[dict], Optional[str]]:
    """(release metadata, source) for the latest release of THIS project.
    Metadata: {"tag", "level", "changelog_url"} — what the update check and
    the banner consume.

    Forgejo is preferred when configured — for self-hosters whose forge is the
    source of truth — with our GitHub mirror as fallback.

    Returns (None, None) when no source could answer. That is UNKNOWN, and
    callers MUST NOT render it as "up to date". Deliberately does not fall
    back to the running version: that is the exact defect this replaced.
    """
    # Update-server preference (John's design, 2026-08-16): the operator picks
    # where the check looks — current server (Forgejo) | github | a custom
    # URL. The chosen source is tried FIRST; the existing fallback order
    # stays behind it, because a preference must not turn an outage into
    # a silent never-checks.
    preference, custom_url = _update_source_preference()
    if preference == "custom" and custom_url:
        release = _fetch_release(custom_url, extra_origin=custom_url)
        if release:
            return release, SOURCE_CUSTOM
        logger.info("biplane: custom update server did not answer; falling back")
    if preference == "github":
        github_first = _github_releases_url()
        if github_first:
            release = _fetch_release(github_first)
            if release:
                return release, SOURCE_GITHUB
            logger.info("biplane: preferred GitHub mirror did not answer; falling back")

    forgejo_url = _forgejo_releases_url()
    if forgejo_url:
        release = _fetch_release(forgejo_url, credential=_forgejo_credential())
        if release:
            return release, SOURCE_FORGEJO
        logger.info("biplane: Forgejo release check did not answer; trying the GitHub mirror")

    github_url = _github_releases_url()
    if github_url:
        # NO credential: the Forgejo token must not follow us to another
        # forge. Our mirror is public, so none is needed.
        release = _fetch_release(github_url)
        if release:
            return release, SOURCE_GITHUB

    logger.warning(
        "biplane: no release source could be reached (forgejo=%s, github=%s). "
        "Latest version is UNKNOWN — this must NOT be reported as up to date.",
        forgejo_url or "<unconfigured>",
        github_url or "<unconfigured>",
    )
    return None, None
