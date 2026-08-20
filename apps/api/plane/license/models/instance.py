# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
from enum import Enum

# Django imports
from django.db import models
from django.conf import settings

# Module imports
from plane.db.models import BaseModel

ROLE_CHOICES = ((20, "Admin"),)


class InstanceEdition(Enum):
    PLANE_COMMUNITY = "PLANE_COMMUNITY"


class Instance(BaseModel):
    # General information
    instance_name = models.CharField(max_length=255)
    whitelist_emails = models.TextField(blank=True, null=True)
    instance_id = models.CharField(max_length=255, unique=True)
    # PLANE's namespace. current_version is APP_VERSION / root package.json
    # ("1.3.1"), which the admin renders as "on Plane CE v{...}". latest_version
    # is its pair. Biplane release tags MUST NOT be written here — the two are
    # different products' numbering and the pair is exposed to the UI as one
    # comparable sequence (BIP-32, Morrow RC 3259).
    current_version = models.CharField(max_length=255)
    latest_version = models.CharField(max_length=255, null=True, blank=True)

    # BIPLANE's namespace (BIP-32). Kept strictly separate from the Plane pair
    # above so neither can be mistaken for the other. Every one of these is
    # nullable and NULL means UNKNOWN — never "up to date". The original defect
    # was a check that reported the running version as the latest on failure, so
    # an unreachable source rendered as an all-clear; nothing here may recreate
    # that by defaulting a missing value to a present-looking one.
    biplane_installed_build = models.CharField(max_length=255, null=True, blank=True)
    # The RELEASE TAG this deployment runs (e.g. v1.2.0) — what the update
    # check compares (RC 3392 #2). Baked into the image by build-images.sh on
    # RELEASE builds only; NULL on dev builds, which the check reports as an
    # honest UNKNOWN. Distinct from biplane_installed_build on purpose: the
    # build id is exact commit identity, this is the comparable version, and
    # conflating them is how a commit hash ended up in a semver comparison.
    biplane_installed_version = models.CharField(max_length=255, null=True, blank=True)
    biplane_latest_version = models.CharField(max_length=255, null=True, blank=True)
    # Which forge answered: "forgejo" | "github". Persisted so the UI can name
    # its source; release_source.py previously returned this and the caller threw
    # it away, and an earlier comment of mine claimed it was stored when it was
    # not (Morrow RC 3250).
    biplane_latest_source = models.CharField(max_length=32, null=True, blank=True)
    # When the release check last SUCCEEDED. Deliberately distinct from
    # last_checked_at, which is stamped on every run whether or not the check
    # resolved — reading that one as "when we last knew" is how a stale value
    # passes for a fresh one.
    biplane_latest_checked_at = models.DateTimeField(null=True, blank=True)
    edition = models.CharField(max_length=255, default=InstanceEdition.PLANE_COMMUNITY.value)
    domain = models.TextField(blank=True)
    # Instance specifics
    last_checked_at = models.DateTimeField()
    namespace = models.CharField(max_length=255, blank=True, null=True)
    # telemetry and support
    is_telemetry_enabled = models.BooleanField(default=False)
    is_support_required = models.BooleanField(default=True)
    # is setup done
    is_setup_done = models.BooleanField(default=False)
    # signup screen
    is_signup_screen_visited = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    is_test = models.BooleanField(default=False)
    # field for validating if the current version is deprecated
    is_current_version_deprecated = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Instance"
        verbose_name_plural = "Instances"
        db_table = "instances"
        ordering = ("-created_at",)


class InstanceAdmin(BaseModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="instance_owner",
    )
    instance = models.ForeignKey(Instance, on_delete=models.CASCADE, related_name="admins")
    role = models.PositiveIntegerField(choices=ROLE_CHOICES, default=20)
    is_verified = models.BooleanField(default=False)

    class Meta:
        unique_together = ["instance", "user"]
        verbose_name = "Instance Admin"
        verbose_name_plural = "Instance Admins"
        db_table = "instance_admins"
        ordering = ("-created_at",)


class InstanceConfiguration(BaseModel):
    # The instance configuration variables
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(null=True, blank=True, default=None)
    category = models.TextField()
    is_encrypted = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Instance Configuration"
        verbose_name_plural = "Instance Configurations"
        db_table = "instance_configurations"
        ordering = ("-created_at",)


class ChangeLog(BaseModel):
    """Change Log model to store the release changelogs made in the application."""

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    version = models.CharField(max_length=255)
    tags = models.JSONField(default=list)
    release_date = models.DateTimeField(null=True)
    is_release_candidate = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Change Log"
        verbose_name_plural = "Change Logs"
        db_table = "changelogs"
        ordering = ("-created_at",)


class BiplaneAutoApplyAttempt(models.Model):
    """The automatic mode's once-per-tag guard (ticket 69), as an APPEND-ONLY
    record with a DB unique constraint — not a single mutable field. A single
    last-tag field regresses under newer -> stale-worker -> newer ordering:
    the stale worker's compare-and-set succeeds against the newer value and
    rolls the guard backward, so the newer tag is attempted twice (Rowan,
    review 3834). A row here can never be un-attempted; the claim is the
    INSERT, and the database enforces exactly-once whatever order workers
    arrive in. Written before the request is sent, so a failed or
    process-killing apply is attempted once, not hourly."""

    tag = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "biplane_auto_apply_attempts"
        ordering = ("-created_at",)
