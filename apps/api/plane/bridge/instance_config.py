# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The provider-instance configuration contract, owned in ONE place (ADR 010 §1).

The lifecycle fact — which forges are supported, and that an enabled forge needs
a present, well-formed, unique namespace id — was previously re-encoded in
compose env, a Django check, the endpoint helper and the frozen migration, so a
consumer got omitted and runtime/migration classified malformed config
differently (Morrow's ruling after RC 3481/3483: one job, four authorities —
invariant 8 on the implementation).

This module is the single authority for the STATIC contract: presence, component
validity (the reserved separator), and cross-forge uniqueness. The endpoint
defence-in-depth (`_provider_instance`) and the startup system check CONSUME its
result; neither restates the predicates. DB-backed STABILITY (rename detection)
needs a ready schema and is a separate check (Tags.database), not this module.

The supported-forge corpus is `forges.FORGES` itself — name, secret_setting,
instance_id_setting and stable_id_path are class-attribute DATA — so this module
derives from it rather than becoming a second copy, and the frozen migration
pins a copy equal to it by test."""

from django.conf import settings

from plane.bridge import forges

SEPARATOR = "\x1f"


class InstanceConfigError(Exception):
    """A static provider-instance config defect for one forge.
    code: 'missing' | 'malformed' | 'duplicate'."""

    def __init__(self, code, forge_name, setting, detail):
        self.code = code
        self.forge_name = forge_name
        self.setting = setting
        self.detail = detail
        super().__init__(detail)


def _configured_value(forge):
    return (getattr(settings, forge.instance_id_setting, None) or "").strip()


def is_enabled(forge):
    """A forge is enabled to receive when its credential is configured."""
    return bool(getattr(settings, forge.secret_setting, None))


def enabled_forges():
    return [f for f in forges.FORGES if is_enabled(f)]


def resolve(forge):
    """The validated instance id for `forge`, or raise InstanceConfigError
    (presence + component validity). The ONE per-forge predicate — the endpoint
    calls this and MUST NOT re-check."""
    value = _configured_value(forge)
    if not value:
        raise InstanceConfigError(
            "missing", forge.name, forge.instance_id_setting,
            f"{forge.instance_id_setting} is not configured for {forge.name}",
        )
    if SEPARATOR in value:
        raise InstanceConfigError(
            "malformed", forge.name, forge.instance_id_setting,
            f"{forge.instance_id_setting} for {forge.name} contains the reserved "
            f"separator (0x1f); a configured id that could forge a key boundary is refused",
        )
    return value


def static_errors():
    """Every STATIC config defect across ENABLED forges — missing, malformed, and
    cross-forge uniqueness collisions — as a list of InstanceConfigError. The one
    authority the startup check consumes."""
    errors = []
    seen = {}
    for forge in enabled_forges():
        try:
            value = resolve(forge)
        except InstanceConfigError as e:
            errors.append(e)
            continue
        if value in seen:
            errors.append(InstanceConfigError(
                "duplicate", forge.name, forge.instance_id_setting,
                f"{forge.instance_id_setting} ({value!r}) collides with {seen[value]}'s "
                f"instance id: distinct forges must have distinct namespaces, or their "
                f"events produce byte-identical keys and collide (ADR 010 §1)",
            ))
        else:
            seen[value] = forge.name
    return errors
