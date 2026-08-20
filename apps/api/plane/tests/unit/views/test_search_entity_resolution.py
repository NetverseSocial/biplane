# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
#
# BIP-62 changed this contract: the resolver used to return (requested, unknown)
# and rely on every caller to reject a non-empty `unknown`. With a second caller
# (SearchEndpoint) that docstring-enforced contract stops holding, so the resolver
# now RAISES UnknownEntitiesError and there is no return path that hands back an
# unknown name. These unit tests changed WITH the contract; the endpoint-level 400
# behaviour is unchanged and is pinned by tests/contract/app/test_global_search_entities.py.
import pytest

from plane.app.views.search.base import UnknownEntitiesError, resolve_requested_entities

# The live mapper keys, as GlobalSearchEndpoint builds them.
VALID = [
    "workspace",
    "project",
    "issue",
    "cycle",
    "module",
    "issue_view",
    "page",
    "intake",
]


@pytest.mark.unit
class TestResolveRequestedEntities:
    """The resolver must never lose a name quietly. An unrecognised name is an
    error (raised), never filtered away — a filtered 200 is indistinguishable
    from "searched it, found nothing"."""

    def test_absent_param_searches_everything(self):
        assert resolve_requested_entities(None, VALID) == VALID

    def test_empty_param_searches_everything(self):
        assert resolve_requested_entities("", VALID) == VALID

    def test_valid_subset_is_honoured_exactly(self):
        assert resolve_requested_entities("issue,page", VALID) == ["issue", "page"]

    def test_surrounding_whitespace_is_tolerated(self):
        assert resolve_requested_entities("  issue , page  ", VALID) == ["issue", "page"]

    def test_unknown_name_raises(self):
        with pytest.raises(UnknownEntitiesError) as exc:
            resolve_requested_entities("nonsense", VALID)
        assert exc.value.unknown == ["nonsense"]
        # The valid set rides on the exception so an endpoint can render an
        # actionable 400.
        assert "issue" in exc.value.valid

    def test_unknown_name_mixed_with_valid_ones_still_raises(self):
        with pytest.raises(UnknownEntitiesError) as exc:
            resolve_requested_entities("issue,nonsense,page", VALID)
        assert exc.value.unknown == ["nonsense"]

    def test_every_unknown_name_is_listed_not_just_the_first(self):
        with pytest.raises(UnknownEntitiesError) as exc:
            resolve_requested_entities("nope,issue,nada", VALID)
        assert exc.value.unknown == ["nope", "nada"]

    def test_work_item_rename_is_caught(self):
        """The rename is live at the URL layer (plane/api/urls/work_item.py
        serves both spellings), so a caller carrying the new vocabulary into
        this parameter is a realistic case — and the one most likely to be read
        as an empty result rather than a mistake."""
        with pytest.raises(UnknownEntitiesError) as exc:
            resolve_requested_entities("work_item", VALID)
        assert exc.value.unknown == ["work_item"]

    def test_regression_no_return_path_leaks_an_unknown_name(self):
        """Fails against the pre-fix implementation, which RETURNED a
        (requested, unknown) tuple the caller could unpack and use — unknown
        names included. Now the only outcome for an unknown name is the raise;
        there is no second value to inspect, and a valid request returns a plain
        list, never a tuple."""
        with pytest.raises(UnknownEntitiesError):
            resolve_requested_entities("work_item", VALID)
        result = resolve_requested_entities("issue", VALID)
        assert result == ["issue"]
        assert not isinstance(result, tuple)
