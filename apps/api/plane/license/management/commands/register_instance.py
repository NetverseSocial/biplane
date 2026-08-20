# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json
import secrets
import os

# Django imports
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


# Module imports
from plane.license.models import Instance, InstanceEdition
from plane.license.bgtasks.tracer import instance_traces


class Command(BaseCommand):
    help = "Check if instance in registered else register"

    def add_arguments(self, parser):
        # Positional argument
        parser.add_argument("machine_signature", type=str, help="Machine signature")

    def check_for_current_version(self):
        if os.environ.get("APP_VERSION", False):
            return os.environ.get("APP_VERSION")

        try:
            with open("package.json", "r") as file:
                data = json.load(file)
                return data.get("version", "v0.1.0")
        except Exception:
            self.stdout.write("Error checking for current version")
            return "v0.1.0"

    def handle(self, *args, **options):
        # Check if the instance is registered
        instance = Instance.objects.first()

        current_version = self.check_for_current_version()

        # biplane (M5, Morrow RC 3392 #4): registration records INSTALLED
        # IDENTITY ONLY. The latest-release check has exactly one owner — the
        # scheduled update service — so this command no longer fetches,
        # reports or writes any biplane_latest_* value. Two fields, both
        # baked into the image (never compose-settable, RC 3271):
        # - biplane_installed_build: the exact commit-derived build id.
        # - biplane_installed_version: the RELEASE TAG on release builds
        #   (empty on dev builds) — the value the version check compares.
        biplane_installed = getattr(settings, "BIPLANE_BUILD", None) or None
        if biplane_installed is None:
            self.stdout.write(
                "Installed Biplane build UNKNOWN — BIPLANE_BUILD is unset. "
                "Storing NULL rather than guessing from the Plane base version."
            )
        biplane_version = getattr(settings, "BIPLANE_VERSION", None) or None
        if biplane_version is None:
            self.stdout.write(
                "Installed Biplane release version UNKNOWN — BIPLANE_VERSION is "
                "unset (a dev build, or a pre-pipeline image). The update check "
                "will honestly report UNKNOWN rather than comparing a guess."
            )

        # If instance is None then register this instance
        if instance is None:
            machine_signature = options.get("machine_signature", "machine-signature")

            if not machine_signature:
                raise CommandError("Machine signature is required")

            instance = Instance.objects.create(
                instance_name="Plane Community Edition",
                instance_id=secrets.token_hex(12),
                current_version=current_version,
                # latest_version deliberately unset (null): we do not track a
                # Plane-namespaced "latest", and a Biplane tag does not belong
                # here. It lives in the biplane_* fields below (BIP-36).
                last_checked_at=timezone.now(),
                biplane_installed_build=biplane_installed,
                biplane_installed_version=biplane_version,
                # biplane_latest_* deliberately untouched: the scheduled
                # update service is their SOLE writer (RC 3392 #4).
                is_test=os.environ.get("IS_TEST", "0") == "1",
                edition=InstanceEdition.PLANE_COMMUNITY.value,
            )

            self.stdout.write(self.style.SUCCESS("Instance registered"))
        else:
            self.stdout.write(self.style.SUCCESS("Instance already registered"))

            # Update the instance details
            instance.last_checked_at = timezone.now()
            instance.current_version = current_version
            # latest_version is NOT touched. Whatever an older build wrote
            # stays as it is; this command no longer makes a claim it cannot
            # support (BIP-32 / Morrow RC 3259).
            instance.biplane_installed_build = biplane_installed
            instance.biplane_installed_version = biplane_version
            # biplane_latest_* deliberately untouched here too: one owner —
            # the scheduled update service (RC 3392 #4). Whatever it last
            # KNEW survives registration untouched.
            instance.is_test = os.environ.get("IS_TEST", "0") == "1"
            instance.edition = InstanceEdition.PLANE_COMMUNITY.value
            instance.save()

        # Call the instance traces task
        instance_traces.delay()

        return
