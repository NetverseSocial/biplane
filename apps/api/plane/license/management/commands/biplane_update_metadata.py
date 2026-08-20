# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Print complete exact-tag release metadata for the host updater."""

import json

from django.core.management.base import BaseCommand, CommandError

from plane.license.utils.release_source import fetch_release_metadata_by_tag


class Command(BaseCommand):
    help = "Fetch one exact Biplane release for the host-side apply command"

    def add_arguments(self, parser):
        parser.add_argument("tag", help="exact stable tag (vMAJOR.MINOR.PATCH)")

    def handle(self, *args, **options):
        tag = options["tag"]
        release, source = fetch_release_metadata_by_tag(tag)
        if release is None or source is None:
            raise CommandError(
                f"release {tag!r} did not resolve to complete apply metadata"
            )
        self.stdout.write(
            json.dumps(
                {"source": source, "release": release},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
