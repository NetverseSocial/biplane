# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bind authorship and server time at the write boundary (BIP-18).

The public API accepted `created_by` and `created_at` straight from the request
body and wrote them onto the row AFTER the response had been serialised. So a
caller could attribute a work item, a comment or a link to any other user, and
the API would answer with the honest value while the database kept the forged
one. Witnessed on our own board:

    POST /issues/  {"created_by": "<another agent>"}

    response says created_by = <me>
    storage  says created_by = <the other agent>

Everything downstream inherits that: audit, blame, and the Traveler timeline.
For comments it was worse — `actor_id` took the same caller value, and the
actor is exactly what the Traveler renders.

WHY STRIP RATHER THAN REJECT

Rejecting outright is the louder option and was the first instinct, but the
field is genuinely useful for a migration or importer that has to preserve an
original author. Rather than remove that, it is gated: only a token flagged
`is_service` may assert authorship. Every ordinary token — which is every
per-agent token — gets its own identity and the server clock, whatever it sent.

Fail closed. Anything that is not provably a service token cannot assert.
"""

import uuid as uuid_lib
from datetime import timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from plane.db.models import APIToken, User


class InvalidAssertedIdentity(ValueError):
    """A service token asserted an identity the database cannot honour.

    Trusted importer does not mean infallible payload (Morrow 10161): a
    canonical-but-nonexistent created_by would otherwise surface as a deferred
    FK failure when the request transaction commits — outside DRF's handled
    path, a 500 with the write half-made. Callers turn this into a controlled
    400 BEFORE any write."""


def caller_may_assert_authorship(request) -> bool:
    """True only for an active token explicitly marked as a service token.

    `request.auth` is the raw token string, set by APIKeyAuthentication. A
    session-authenticated request has no token and therefore cannot assert —
    correct, since the app surface is people, and a person is never importing
    someone else's history through it.
    """
    token = getattr(request, "auth", None)
    if not token:
        return False
    return APIToken.objects.filter(token=token, is_service=True, is_active=True).exists()


def creation_identity(request, default_actor_id=None):
    """Return the (actor_id, timestamp) to stamp on a row being created.

    Caller-supplied values are honoured ONLY for a service token. For anyone
    else the request body is ignored entirely — not merged, not validated,
    ignored — because a partial trust here is the same bug in a smaller costume.
    """
    actor_id = default_actor_id if default_actor_id is not None else request.user.id
    stamped_at = timezone.now()

    if not caller_may_assert_authorship(request):
        return actor_id, stamped_at

    asserted_by = request.data.get("created_by")
    if asserted_by is not None:
        try:
            asserted_by = uuid_lib.UUID(str(asserted_by))
        except (ValueError, AttributeError, TypeError):
            raise InvalidAssertedIdentity("created_by must be a canonical UUID")
        if not User.objects.filter(id=asserted_by).exists():
            raise InvalidAssertedIdentity("created_by does not reference an existing user")
        actor_id = asserted_by

    asserted_at = request.data.get("created_at")
    if asserted_at is not None:
        parsed = parse_datetime(str(asserted_at))
        if parsed is None:
            raise InvalidAssertedIdentity("created_at must be an ISO-8601 datetime")
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        stamped_at = parsed

    return actor_id, stamped_at
