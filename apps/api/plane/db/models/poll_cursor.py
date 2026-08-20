# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Poll cursor for the git-bridge polling transport (BIP-46 PR-B2).

One row per watched (provider instance, repository). The cursor records the
boundary the last poll reached; the next poll fetches WITH OVERLAP back past it
and de-duplicates by semantic key, so an event landing exactly on the boundary
is never skipped. The cursor advances ONLY after a page's observations are
durably inserted into the delivery inbox — a crash between insert and advance
re-polls and re-ingests idempotently, losing nothing.

IDENTITY IS (PROVIDER INSTANCE, STABLE REPOSITORY ID). Not the display path,
and not the provider family.

An earlier revision keyed uniqueness on ``(forge, repo_full_name)`` with
``repo_stable_id`` nullable (Rowan RC on PR #42, defect 1). Both halves are
ruled out by ADR 010 §1, which merged after that revision was written:

    `stable_repo_id` is `repository.id` on GitHub and Forgejo … **Display
    paths are never identity** — they survive rename and reuse.

A rename would have created a second cursor and re-polled history already
ingested; path reuse would have inherited a prior repository's boundary and
skipped real events. Both are losslessness failures, which is the one property
this transport exists to provide.

The instance half goes beyond that review's literal ask of
``(forge, repo_stable_id)``, and it follows from the same ADR §1 clause the
review cites: *"The namespace is the PROVIDER INSTANCE, not the provider
family … two Forgejo instances both numbering repository 42 would collide, and
the collision would be invisible."* A cursor keyed on the family has exactly
that collision — two instances would share one boundary and starve each other's
history. ``forge`` is retained because the processing path needs the
personality to select payload accessors; it is not identity.
"""

from django.db import models

from .base import BaseModel


class PollCursor(BaseModel):
    #: Configured provider instance id — the authority, from instance_config.
    #: Never a product family, never read from a payload (ADR 010 §1).
    provider_instance = models.CharField(max_length=255)
    #: Immutable repository id from the provider. Identity, with the instance.
    #:
    #: SEEDING THIS BY HAND IS THE ONE THING NOTHING DOWNSTREAM CAN CATCH (7of9,
    #: reviewing PR #82). The cross-transport collapse works because a poll and a
    #: webhook derive the SAME semantic key from the same primitives, and the
    #: provider's numeric ``repository.id`` is one of them. The poller enforces
    #: internal consistency — a page whose observations do not match this value
    #: is refused whole — but consistency with a wrong seed is still wrong: every
    #: polled event of that repository keys to a namespace no webhook will ever
    #: produce, so one real event quietly becomes two holders and executes twice.
    #: There is no signal for it, because nothing here can ask the provider what
    #: this repository's id actually is. Seed it FROM the provider's API, never
    #: from a display path and never by hand.
    repo_stable_id = models.BigIntegerField()

    #: Which forge personality sent it — needed to select payload accessors on
    #: the processing path. Derivable from the instance today; stored because
    #: the read path needs it without a config round-trip. NOT identity.
    forge = models.CharField(max_length=32)
    #: Display only. Renames are expected and change nothing about identity.
    repo_full_name = models.CharField(max_length=512, blank=True, default="")

    # The boundary the last completed poll reached. Opaque per forge: for
    # GitHub commit polling it is the newest seen commit SHA per ref (stored in
    # `position`); `watermark_at` is the server time of that boundary, used to
    # bound the overlap re-fetch. Null until the first successful poll.
    position = models.JSONField(default=dict, blank=True)
    watermark_at = models.DateTimeField(null=True, blank=True)

    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    # A repo whose history moved beyond the provider's retention window before
    # we could read it: fail closed, name the operator recovery step, and STOP
    # advancing rather than skip the gap. Enforced in poller.poll_repo_page —
    # an earlier revision set this flag and never read it (Rowan defect 2), so
    # any ordinary caller could ingest and advance straight past the gap.
    gap_detected = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Poll Cursor"
        verbose_name_plural = "Poll Cursors"
        db_table = "git_bridge_poll_cursors"
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["provider_instance", "repo_stable_id"],
                name="uniq_poll_cursor_instance_repo",
            ),
        ]

    def __str__(self):
        return (
            f"{self.provider_instance}:{self.repo_stable_id} "
            f"({self.repo_full_name or 'unnamed'}, gap={self.gap_detected})"
        )
