# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# BIP-59: backfill Issue.description_stripped from description_html for legacy rows
# where the HTML body is present but the stripped plain-text projection is NULL.
#
# Such rows read as empty to any consumer of description_stripped (the app serializer,
# the space .values() projection, and the now-exposed api/v1 field), even though the
# body is present. New rows cannot reach this state: Issue.save() sets description_stripped
# on every write. This one-time, idempotent pass corrects the rows that predate the
# guarantee (or were written by a path that bypassed the model save).
#
# STRIP SEMANTICS ARE FROZEN HERE, not imported (Morrow RC 3607). The runtime model uses
# plane.utils.html_processor.strip_tags -- an HTMLParser with convert_charrefs=True, which
# DECODES entities (``A&nbsp;B &amp; C`` -> ``A\xa0B & C``). django.utils.html.strip_tags
# leaves them encoded, so importing the wrong one would backfill values that DIFFER from
# what the model writes for the same html. A migration must also not import runtime code
# whose behaviour can later change; the current MLStripper semantics are copied verbatim so
# this migration is immutable and produces byte-identical output to the model at this head.
# (Copied from plane/utils/html_processor.py as of the 0129 head; a parity test pins them.)
#
# Scope: Issue only -- the ticket names GET issues/{id}/. Draft/Page/Sticky/IssueComment
# carry the same shape but are out of scope for BIP-59 and not touched here.
from io import StringIO
from html.parser import HTMLParser

from django.db import migrations

_BATCH = 500


class _MLStripper(HTMLParser):
    """Frozen copy of plane.utils.html_processor.MLStripper (see module note)."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = StringIO()

    def handle_data(self, d):
        self.text.write(d)

    def get_data(self):
        return self.text.getvalue()


def _strip_tags(html):
    s = _MLStripper()
    s.feed(html)
    return s.get_data()


def backfill_description_stripped(apps, schema_editor):
    Issue = apps.get_model("db", "Issue")
    db_alias = schema_editor.connection.alias
    # Match Issue.save() semantics exactly: stripped is NULL only when html is NULL/"".
    # So every row with non-empty html must have a (possibly empty-string) stripped value.
    # _base_manager, not objects: the historical model has no `objects` manager.
    qs = (
        Issue._base_manager.using(db_alias)
        .filter(description_stripped__isnull=True)
        .exclude(description_html__isnull=True)
        .exclude(description_html="")
    )
    batch = []
    for issue in qs.iterator(chunk_size=_BATCH):
        issue.description_stripped = _strip_tags(issue.description_html)
        batch.append(issue)
        if len(batch) >= _BATCH:
            Issue._base_manager.using(db_alias).bulk_update(batch, ["description_stripped"])
            batch = []
    if batch:
        Issue._base_manager.using(db_alias).bulk_update(batch, ["description_stripped"])


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0128_forgejodelivery_semantic_key"),
    ]

    operations = [
        # Reverse is a no-op: re-nulling a correctly-derived column would only
        # reintroduce the defect, and the value is losslessly recomputable from html.
        migrations.RunPython(backfill_description_stripped, migrations.RunPython.noop),
    ]
