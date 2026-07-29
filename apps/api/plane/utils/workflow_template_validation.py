# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# biplane: workflow-template state validation/normalization. Lives in utils (not a
# view module) because it is used at BOTH write time (template endpoint) and point
# of use (project creation revalidates persisted templates) — importing one view
# from another would be circular through plane.app.views.__init__.

from plane.db.models.state import StateGroup

# State.name is CharField(max_length=255); longer names raise DataError at project creation.
STATE_NAME_MAX_LENGTH = 255
# Sanity cap — a template beyond this is junk input, not a workflow.
MAX_TEMPLATE_STATES = 50


def _validate_states(states):
    """Every template must cover the required groups, use only real groups, and have
    unique, storable names — so every project created from it is valid (State has a
    unique (name, project) constraint and a 255-char name column). Canonical on
    TYPES as well as values: name/group/color must be strings before any use, so
    unhashable or numeric junk is a clean 400, never a TypeError 500."""
    required = {"backlog", "unstarted", "started", "completed", "cancelled"}
    valid_groups = set(StateGroup.values)
    if not isinstance(states, list) or not states:
        return "A workflow needs at least one state."
    if len(states) > MAX_TEMPLATE_STATES:
        return f"Too many states (max {MAX_TEMPLATE_STATES})."
    groups = set()
    seen_names = set()
    for s in states:
        if not isinstance(s, dict):
            return "Each state needs a name and a group."
        name = s.get("name")
        group = s.get("group")
        if not isinstance(name, str) or not isinstance(group, str):
            return "State name and group must be text."
        color = s.get("color")
        if color is not None and not isinstance(color, str):
            return "State color must be text."
        # State.color is CharField(max_length=255) — bound it here, not as a DataError.
        if isinstance(color, str) and len(color) > 255:
            return "State color value too long."
        name = name.strip()
        if not name or not group:
            return "Each state needs a name and a group."
        if len(name) > STATE_NAME_MAX_LENGTH:
            return f"State name too long (max {STATE_NAME_MAX_LENGTH} characters): {name[:40]}…"
        if name.casefold() in seen_names:
            return f"Duplicate state name: {name}."
        seen_names.add(name.casefold())
        if group not in valid_groups:
            return f"Unknown state group: {group}."
        groups.add(group)
    missing = required - groups
    if missing:
        return f"Missing a state for: {', '.join(sorted(missing))}."
    return None


def _normalize_states(states):
    out = []
    for i, s in enumerate(states):
        entry = {
            "name": str(s["name"]).strip(),
            "group": s["group"],
            "color": s.get("color") or "#60646C",
            "sequence": (i + 1) * 15000,
        }
        if s.get("default"):
            entry["default"] = True
        out.append(entry)
    # guarantee EXACTLY one default: keep the first flagged state, drop the rest;
    # none flagged -> first backlog state, else first state.
    flagged = [e for e in out if e.get("default")]
    for e in flagged[1:]:
        del e["default"]
    if not flagged:
        backlog = next((e for e in out if e["group"] == "backlog"), out[0])
        backlog["default"] = True
    return out
