# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Startup checks for the git-bridge provider-instance lifecycle (ADR 010 §1).

Two checks, one owner. The STATIC contract (presence, validity, uniqueness) is
decided by `plane.bridge.instance_config` and merely surfaced here as Errors, so
a misconfigured deployment fails startup (Morrow: the endpoint 500 is
defence-in-depth BEHIND this refusal, not instead of it). STABILITY (rename
detection) is DB-backed and split into a Tags.database check that runs when the
schema is ready — so an unexpected DB/query failure is fatal and no broad catch
exists; only a genuinely-absent table/column (pre-0128) is skipped, proven by
introspection RETURN VALUES, never by swallowing its exceptions.

INVOCATION is part of the contract (Morrow RC 3484 #1): Django passes
`databases` to this check only via an explicit `manage.py check --database
default` — BaseCommand.execute()'s self-check never does — so every entrypoint
runs that command after wait_for_migrations (migrator: after migrate). A
registered check nobody invokes is a sentence, not a control."""

from django.core.checks import Error, Tags, register

from plane.bridge import instance_config

_CODE_TO_ID = {"missing": "bridge.E001", "malformed": "bridge.E004", "duplicate": "bridge.E002"}


@register()
def check_provider_instance_config(app_configs, **kwargs):
    """Static provider-instance contract, consumed from the single owner."""
    return [
        Error(
            e.detail,
            hint=f"Set {e.setting} to a stable, unique id with no control characters.",
            id=_CODE_TO_ID.get(e.code, "bridge.E001"),
        )
        for e in instance_config.static_errors()
    ]


@register(Tags.database)
def check_provider_instance_stability(app_configs, databases=None, **kwargs):
    """A configured id differing from what stored rows used is a RENAME — a
    namespace migration, not a config edit. Runs only with the DB in scope."""
    if not databases:
        return []
    from django.db import connection

    from plane.db.models import ForgejoDelivery

    table = ForgejoDelivery._meta.db_table
    # Genuine pre-0128 absence is fully representable by these RETURN VALUES —
    # table missing, or column missing. No exception is caught here on purpose
    # (Morrow RC 3484 #2 / Rowan RC 3485 #1): after wait_for_db and
    # wait_for_migrations, an OperationalError or ProgrammingError from
    # introspection is an outage or a denied metadata query, and laundering it
    # into "nothing to check" is fail-open on exactly the boundary this check
    # exists to guard. Unexpected failure PROPAGATES and startup fails closed.
    if table not in connection.introspection.table_names():
        return []
    with connection.cursor() as cur:
        cols = {c.name for c in connection.introspection.get_table_description(cur, table)}
    if "semantic_key" not in cols:
        return []  # schema predates 0128 — nothing stored to check

    problems = []
    for forge in instance_config.enabled_forges():
        try:
            inst = instance_config.resolve(forge)
        except instance_config.InstanceConfigError:
            continue  # the static check already flags a broken static config
        stored = set()
        for key in (
            ForgejoDelivery.objects.filter(forge=forge.name)
            .exclude(semantic_key__isnull=True)
            .values_list("semantic_key", flat=True)
            .iterator()
        ):
            parts = key.split(instance_config.SEPARATOR)
            if len(parts) >= 2:
                stored.add(parts[1])
            if len(stored) > 4:
                break
        others = stored - {inst}
        if others:
            problems.append(Error(
                f"{forge.name}: stored delivery keys use instance id(s) {sorted(others)} but "
                f"{forge.instance_id_setting} is now {inst!r}. Renaming an instance id after "
                f"delivery rows exist orphans the old namespace — a NAMESPACE MIGRATION, not a "
                f"config edit (ADR 010 §1).",
                hint=f"Restore {forge.instance_id_setting} to the prior id, or run a namespace migration.",
                id="bridge.E003",
            ))
    return problems
