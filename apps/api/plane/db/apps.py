# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.apps import AppConfig


class DbConfig(AppConfig):
    name = "plane.db"

    def ready(self):
        # Register the git-bridge boot-time system checks (ADR 010 §1 / 4e:
        # an enabled forge with no configured instance id is flagged at startup).
        from plane.bridge import checks  # noqa: F401
