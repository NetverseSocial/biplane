# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Classify the running deployment against the latest published release (M5).

Rewritten 2026-08-12 with the M5 redesign (scope-a-architecture §M5): the check
is a semantic-version comparison against release metadata fetched over TLS from
an allowlisted origin — no signed manifests, no history walk, no artifact
identity comparison. What survives unchanged is the honesty rule this module
exists to enforce:

- UNKNOWN is a terminal honest answer, never a default that later renders as
  "up to date". Unreachable, malformed or INCOMPARABLE inputs are UNKNOWN.
- Only exact version equality is "current". A running version AHEAD of the
  latest published release is not "current" — it is a dev build, a rollback
  upstream, or a lie somewhere, and all three deserve an operator's eyes.
- The banner side renders UNKNOWN as unknown in both directions: it never
  hides as if current, and it never advertises an update it cannot name.

Pure module by design: no Django, no network, no clock — the service resolves
inputs and passes them in, which keeps these rules testable as plain pytest.
"""

from dataclasses import dataclass
from typing import Mapping, Optional

from plane.license.utils import release_version

STATE_CURRENT = "current"
STATE_UPDATE_AVAILABLE = "update_available"
STATE_UNKNOWN = "unknown"

#: Release `level` values (M5): how the apply path behaves. Metadata may omit
#: it (older releases); the banner treats a missing level as "read the notes".
LEVELS = ("code", "data", "full")

@dataclass(frozen=True)
class Classification:
    state: str
    #: Human-readable refusal reason when state is UNKNOWN; None otherwise.
    reason: Optional[str]
    #: The running version as declared by the deployment, when it was usable.
    running_release: Optional[str]
    #: Latest published release metadata: {"tag", "level", "changelog_url"},
    #: values nullable except tag. Present whenever a source answered.
    latest_release: Optional[Mapping]


def classify(running_version: Optional[str], latest: Optional[Mapping]) -> Classification:
    """Decide current / update_available / unknown.

    :param running_version: the version this deployment is RUNNING, from M4's
        `biplane_installed_version` field (the comparable release tag — never
        `biplane_installed_build`, which is exact commit provenance and must
        not be semver-compared) — or None when unavailable.
    :param latest: latest-release metadata from the fetch, or None when no
        source could answer. Must carry a "tag" when present.
    """
    if latest is None or not latest.get("tag"):
        return Classification(
            STATE_UNKNOWN, "no release source could be reached", None, None
        )
    latest_tag = latest.get("tag")
    if not release_version.is_valid(latest_tag):
        return Classification(
            STATE_UNKNOWN,
            f"latest release tag {latest.get('tag')!r} is not a semantic version",
            None,
            latest,
        )
    if not running_version:
        # M4's biplane_installed_version is NULL (dev build, pre-pipeline
        # image, or the instance is not yet registered). NULL means UNKNOWN,
        # never "up to date" — and nothing is expected to declare a version
        # outside M4, so the reason names availability, not a missing setting.
        return Classification(
            STATE_UNKNOWN,
            "running version not available",
            None,
            latest,
        )
    if not release_version.is_valid(running_version):
        return Classification(
            STATE_UNKNOWN,
            f"running version {running_version!r} is not a semantic version",
            None,
            latest,
        )
    if running_version == latest_tag:
        return Classification(STATE_CURRENT, None, running_version, latest)
    if release_version.gt(latest_tag, running_version):
        return Classification(STATE_UPDATE_AVAILABLE, None, running_version, latest)
    # Running AHEAD of the latest published release: dev build, upstream
    # rollback, or misdeclared version — none of which is "current".
    return Classification(
        STATE_UNKNOWN,
        f"running version {running_version!r} is ahead of the latest published "
        f"release {latest.get('tag')!r}",
        running_version,
        latest,
    )


def status_payload(classification: Classification, checked_at_iso: Optional[str]) -> dict:
    """The GET /api/instances/updates/status/ response body — the banner's ONLY
    input. The apply path is offered exactly when `state` is "update_available";
    UNKNOWN never shows it, in either direction. `checked_at` is None when no
    check has completed yet (which renders as unknown, not as current)."""
    latest = classification.latest_release
    return {
        "state": classification.state,
        "reason": classification.reason,
        "checked_at": checked_at_iso,
        "running_release": classification.running_release,
        "latest_release": (
            {
                "tag": latest.get("tag"),
                "level": latest.get("level"),
                "changelog_url": latest.get("changelog_url"),
            }
            if latest is not None
            else None
        ),
    }
