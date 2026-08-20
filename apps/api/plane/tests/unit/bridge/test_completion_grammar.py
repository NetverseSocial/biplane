# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The completion grammar, pinned case by case from the Scope-A architecture
(architecture/biplane-a-scope-design.md @ 73d9967, §M2).

Every case here is one the adversarial review named. The grammar is lexically
CLOSED: a directive is an anchored trailer line — start of line, entire line
(terminal `.` or `!` tolerated), colon optional — in a PR body or commit
message body. Anything else is not a directive, and a leading directive with
trailing content is a NEAR MISS that must be reported loudly, never silently
inert (BIP-33's failure class, both directions).

Keyword classes (John's rulings, 2026-08-11):
  complete-class: closes / fixes / resolves   — may complete, PR body + merge only
  advance-class:  refs                        — advances at most
"""

import pytest

from plane.bridge import grammar

from plane.bridge.grammar import (
    ADVANCE,
    COMPLETE,
    parse_directives,
)


def keys(result):
    return [(d.keyword_class, d.ticket_key) for d in result.directives]


def near(result):
    return [n.line for n in result.near_misses]


# --- the anchored trailer form -------------------------------------------------


def test_plain_trailer_is_a_directive():
    r = parse_directives("Closes BIP-7", source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]
    assert near(r) == []


def test_colon_is_optional_both_ways():
    with_colon = parse_directives("Closes: BIP-7", source="pr_body")
    without = parse_directives("Closes BIP-7", source="pr_body")
    assert keys(with_colon) == keys(without) == [(COMPLETE, "BIP-7")]


@pytest.mark.parametrize("kw", ["closes", "CLOSES", "Closes", "cLoSeS"])
def test_keyword_is_case_insensitive(kw):
    r = parse_directives(f"{kw} BIP-7", source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


@pytest.mark.parametrize(
    "kw,klass",
    [("Closes", COMPLETE), ("Fixes", COMPLETE), ("Resolves", COMPLETE), ("Refs", ADVANCE)],
)
def test_keyword_classes_per_john_ruling(kw, klass):
    r = parse_directives(f"{kw} BIP-9", source="pr_body")
    assert keys(r) == [(klass, "BIP-9")]


# --- end anchoring (Vex 3289) and terminal punctuation (7of9 3293) --------------


def test_terminal_period_completes():
    r = parse_directives("Closes BIP-7.", source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_terminal_bang_completes():
    r = parse_directives("Fixes BIP-7!", source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_trailing_prose_is_a_loud_near_miss_not_a_directive():
    # Natural English for "not done yet" — must never complete (Vex 3289).
    r = parse_directives("Closes BIP-7 once CI is green", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


@pytest.mark.parametrize("line", ["Closes BIP-7?", "Closes BIP-7..", "Closes BIP-7 .", "Closes BIP-7,"])
def test_other_trailing_punctuation_is_a_near_miss(line):
    r = parse_directives(line, source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


def test_leading_prose_is_not_a_directive_and_not_a_near_miss():
    # Inline occurrences are plain text — `do not closes BIP-7` (Morrow 3278).
    r = parse_directives("do not closes BIP-7", source="pr_body")
    assert keys(r) == []
    assert near(r) == []


def test_bare_parenthesized_ticket_is_deliberately_inert():
    r = parse_directives("fix(api): tidy the widget (BIP-7)", source="pr_body")
    assert keys(r) == []
    assert near(r) == []


# --- ignored contexts (Morrow 3278) ---------------------------------------------


def test_quoted_line_is_ignored():
    r = parse_directives("> Closes BIP-7", source="pr_body")
    assert keys(r) == []
    assert near(r) == []


def test_fenced_code_block_is_ignored():
    text = "Example:\n```\nCloses BIP-7\n```\nnot a directive above"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == []


def test_fence_state_does_not_leak_past_the_closing_fence():
    text = "```\nnoise\n```\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_html_comment_is_ignored():
    text = "<!--\nCloses BIP-7\n-->"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == []


def test_single_line_html_comment_is_ignored():
    r = parse_directives("<!-- Closes BIP-7 -->", source="pr_body")
    assert keys(r) == []


# --- Morrow 3333: crafted-body completion paths, pinned verbatim ------------------


def test_longer_backtick_fence_is_not_closed_by_a_shorter_run():
    text = "````python\nnoise\n```\nCloses BIP-7\n````"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_tilde_run_does_not_close_a_backtick_fence():
    text = "```python\nnoise\n~~~\nCloses BIP-7\n```"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_closer_with_trailing_text_is_still_code():
    text = "```python\n```still-code\nCloses BIP-7\n```"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_lazy_blockquote_continuation_is_quoted_context():
    # CommonMark: the quote marker may be omitted on paragraph continuation
    # text — this RENDERS inside the blockquote and must not complete.
    text = "> This change is not ready\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_nested_lazy_continuation_is_quoted_context():
    text = ">> nested quote\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == []


def test_blank_line_ends_the_quote_paragraph():
    # The conservative rule must not swallow genuinely top-level trailers.
    text = "> quoted paragraph\n\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


@pytest.mark.parametrize("sep", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
def test_unicode_line_separators_cannot_create_an_anchored_line(sep):
    # splitlines() would promote these into synthetic anchored lines even
    # though no LF/CR exists in the source (Morrow 3333 blocker 3).
    r = parse_directives(f"ordinary prose{sep}Closes BIP-7", source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_trailing_inline_comment_is_a_loud_near_miss_not_silence():
    # Directive-shaped text with disqualifying trailing content must be loud;
    # the comment must not erase the attempt (Morrow 3333 blocker 4).
    r = parse_directives("Closes BIP-7 <!-- lgtm -->", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


def test_leading_inline_comment_is_a_loud_near_miss_not_a_completion():
    # Renders as "Closes BIP-7"-ish but is not a clean trailer line.
    r = parse_directives("<!-- note --> Closes BIP-7", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


# --- Morrow 3334: lexical whitespace is space/tab ONLY --------------------------

_UNICODE_SEPS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("sep", _UNICODE_SEPS)
def test_separator_between_keyword_and_id_never_completes(sep):
    # No CR/LF own-line exists; \s+ used to accept the separator (3334 §1).
    # It IS id-shaped invisible smuggling, so it is loud.
    r = parse_directives(f"Closes{sep}BIP-7", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


@pytest.mark.parametrize("sep", _UNICODE_SEPS)
def test_separator_in_terminal_position_never_completes(sep):
    r = parse_directives(f"Closes BIP-7{sep}", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


@pytest.mark.parametrize("sep", _UNICODE_SEPS)
def test_separator_after_fence_closer_does_not_close_the_fence(sep):
    # Morrow 3334's exact control: the "closer" carrying a unicode separator
    # is CONTENT per CommonMark (spaces/tabs only), so the fence stays open
    # and the directive stays rendered code.
    text = f"```python\nnoise\n```{sep}\nCloses BIP-7\n```"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


# --- Morrow 3334: structural blocks terminate lazy continuation -----------------


def test_thematic_break_ends_the_quote_and_the_trailer_completes():
    # His exact positive control: the break is top-level, so is the trailer.
    text = "> quoted paragraph\n---\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_atx_heading_ends_the_quote_and_the_trailer_completes():
    text = "> quoted paragraph\n# Heading\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_list_item_ends_the_quote_and_absorbs_the_trailer():
    # CORRECTED 2026-08-12, and this one is CONTESTED — read before changing.
    #
    # This previously asserted the trailer COMPLETES, on the theory that the
    # list item ends the blockquote and returns us to top level. The forge
    # says otherwise: Forgejo renders this as
    #     <blockquote><p>quoted paragraph</p></blockquote>
    #     <ul><li>item<br>Closes BIP-7</li></ul>
    # The trailer is lazy continuation of the LIST ITEM, so a human reads it
    # as part of the bullet. It is visible, so this is not a safety question —
    # it is the policy boundary, pinned as SUPPRESS in the conformance
    # module's POLICY table alongside quotes.
    #
    # OPEN DISAGREEMENT: Vex 3354 argues visibility is the positive rule, on
    # which this SHOULD fire. I argue visibility is a one-way safety floor and
    # the positive rule stays prose-container — otherwise `- Closes BIP-7` in
    # a checklist closes a ticket. Awaiting Morrow's ruling; if it goes Vex's
    # way this test and the POLICY entry flip together.
    text = "> quoted paragraph\n- item\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == []


def test_fence_after_quote_opens_code_and_its_contents_stay_code():
    text = "> quoted paragraph\n```\nCloses BIP-7\n```"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_plain_lazy_continuation_still_swallows():
    # The 3333 behavior must survive the 3334 fix: ordinary prose after a
    # quote line without termination is still continuation text.
    text = "> quoted paragraph\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


# --- Morrow 3340: four adjacent grammar boundaries -------------------------------


def test_explicit_blank_quote_line_ends_the_paragraph():
    # His exact control: `>` with no content closes the quoted paragraph, so
    # the following unmarked line is top-level, not lazy continuation.
    text = "> quoted paragraph\n>\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_quoted_heading_opens_no_lazily_continuable_paragraph():
    text = "> # Heading\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_backtick_info_string_with_backtick_is_not_a_fence():
    # CommonMark: a backtick fence's info string cannot contain a backtick,
    # so this "opener" is paragraph text and the directive is top-level.
    text = "``` bad`info\nCloses BIP-7\n```"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_tilde_info_string_may_contain_backticks():
    text = "~~~ ok`info\nCloses BIP-7\n~~~"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


def test_unicode_digit_in_ticket_number_is_a_loud_near_miss():
    # \d would accept every Unicode Nd digit; grammar digits are ASCII only.
    r = parse_directives("Closes BIP-٧", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


def test_unicode_digit_list_marker_does_not_terminate_quote_state():
    # CommonMark ordered-list markers use ASCII digits; an Arabic-digit
    # "list" line is paragraph continuation, so the trailer stays quoted.
    text = "> quoted paragraph\n١. item\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


@pytest.mark.parametrize(
    "line",
    ["Fixes 2 race conditions", "Closes the gap - docs pending", "Closes half-done work"],
)
def test_numbered_and_hyphenated_prose_stays_silent(line):
    # 3327's alert-fatigue bar, restored: a bare digit or spaced dash is not
    # a ticket attempt (Morrow 3340).
    r = parse_directives(line, source="pr_body")
    assert keys(r) == []
    assert near(r) == []


# --- Morrow 3342: paragraph state, not a structural whitelist --------------------


def test_quoted_indented_code_opens_no_paragraph():
    text = ">     code\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_quoted_html_comment_opens_no_paragraph():
    text = "> <!-- note -->\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_tab_separated_quote_marker_heading_opens_no_paragraph():
    text = ">\t# Heading\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_true_lazy_paragraph_still_swallows_after_3342():
    text = "> real paragraph text\nCloses BIP-7"
    r = parse_directives(text, source="pr_body")
    assert keys(r) == [] and near(r) == []


@pytest.mark.parametrize("line", ["Closes phase- 2 followups", "Fixes issue- pending triage"])
def test_whitespace_separated_dash_prose_stays_silent(line):
    # The ticket-attempt token must be CONTIGUOUS (Morrow 3342): a dash
    # followed by whitespace mid-line is prose, not an id attempt.
    r = parse_directives(line, source="pr_body")
    assert keys(r) == []
    assert near(r) == []


# --- duplicates and conflicts ----------------------------------------------------


def test_duplicate_directives_are_idempotent():
    r = parse_directives("Closes BIP-7\nCloses BIP-7", source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7")]


def test_conflicting_directives_weaker_wins_and_conflict_is_recorded():
    r = parse_directives("Closes BIP-7\nRefs BIP-7", source="pr_body")
    assert keys(r) == [(ADVANCE, "BIP-7")]
    assert r.conflicts == ["BIP-7"]


def test_multiple_tickets_multiple_lines():
    r = parse_directives("Closes BIP-7\nRefs BIP-9", source="pr_body")
    assert keys(r) == [(COMPLETE, "BIP-7"), (ADVANCE, "BIP-9")]


def test_two_tickets_on_one_line_is_a_near_miss():
    # Exactly one ticket per line — anything else is not a directive.
    r = parse_directives("Closes BIP-7 BIP-9", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


# --- sources: the one-location rule (Rowan 3285) ----------------------------------


def test_commit_message_directives_are_recognized():
    r = parse_directives("Refs BIP-7", source="commit_message")
    assert keys(r) == [(ADVANCE, "BIP-7")]


def test_title_source_is_rejected_outright():
    # Titles never carry directives; a title-sourced parse is a contract error
    # (Morrow 3288: source enum is pr_body|commit_message ONLY).
    with pytest.raises(ValueError):
        parse_directives("Closes BIP-7", source="pr_title")


# --- ticket key shape --------------------------------------------------------------


def test_ticket_key_preserves_project_prefix():
    r = parse_directives("Closes SB-12", source="pr_body")
    assert keys(r) == [(COMPLETE, "SB-12")]


@pytest.mark.parametrize("line", ["Closes BIP-", "Closes bip-7x"])
def test_malformed_ticket_ids_are_near_misses(line):
    # Directive SHAPE (keyword + id-like token) but not a valid directive.
    r = parse_directives(line, source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


# --- near-miss requires directive shape (7of9 3327) --------------------------------


@pytest.mark.parametrize(
    "line",
    ["Fixes the race condition.", "Closes the gap in coverage", "Refs are updated", "Closes BIP"],
)
def test_prose_opening_with_a_keyword_is_silent_not_near_miss(line):
    # No id-like token after the keyword ⇒ ordinary prose. Flooding the loud
    # channel with prose is alert fatigue — BIP-33's failure class inverted.
    r = parse_directives(line, source="pr_body")
    assert keys(r) == []
    assert near(r) == []


def test_lowercase_ticket_id_is_a_near_miss_not_a_directive():
    # Keyword case-insensitive, ID uppercase-only — matching deployed
    # the deployed matcher, so wiring this module cannot newly complete lowercase refs
    # (7of9 3327). It IS id-shaped, so it is loud, not silent.
    r = parse_directives("closes bip-7", source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


def test_directive_carries_its_source():
    r = parse_directives("Refs BIP-9", source="commit_message")
    assert r.directives[0].source == "commit_message"


# --- Morrow 3346 item 3: oversized ticket attempts are loud ----------------------


@pytest.mark.parametrize("line", ["Closes ABCDEFGHIJKLM-1", "Closes BIP-1234567"])
def test_oversized_ticket_attempts_are_loud_near_misses(line):
    # Directive-shaped beyond the model's bounds (13-char prefix / 7-digit
    # number) must be a loud near-miss, never silence (Morrow 3346).
    r = parse_directives(line, source="pr_body")
    assert keys(r) == []
    assert near(r) == [1]


# --- Aria (#49 review): masking must never change the line count ----------------


class TestMaskingPreservesLineCount:
    """The obligation `_mask`'s single walk takes on, pinned.

    Spans are computed once from the original token stream and applied to a
    line list earlier stages have already mutated, so every edit kind MUST
    return the number of lines it was given. If one ever doesn't, every span
    still queued points somewhere else — silently, and this module reports
    verdicts as line numbers.

    Aria found this from source during #49 and named it exactly: the old
    four-pass version got it for free by re-splitting each pass; the collected
    version depends on it. Nothing else in the suite would notice it breaking.
    """

    # One body per edit kind, then the combinations where they interact.
    BODIES = {
        "inline code span": "a `x` b\nCloses BIP-7\n",
        "multi-line-ish span": "text `<!--` tail\nCloses BIP-7\n",
        "fenced code": "```\n<!--\nstill\n```\nCloses BIP-7\n",
        "indented code": "    <!--\n    still\nCloses BIP-7\n",
        "complete comment": "intro\n<!-- a -->\nCloses BIP-7\n",
        "multi-line comment": "intro\n<!--\nhidden\n-->\nCloses BIP-7\n",
        "unterminated comment": "<div>\n<!--\nCloses BIP-7\n",
        "html block + trailer": '<img src="x">\nCloses BIP-7\n',
        "all kinds at once": ("intro `x`\n```\n<!--\n```\n<!-- a -->\n    <!--\n<div>\n<!--\nCloses BIP-7\n"),
        # markdown-it emits a MULTI-LINE inline comment as ONE html_inline
        # token carrying the newlines, so its replacement must preserve them
        # (Aria/Sia, #52). No pre-existing body reached this path.
        "multi-line inline comment": "intro <!-- one\ntwo --> tail\nCloses BIP-7\n",
        "three-line inline comment": "intro <!-- a\nb\nc --> tail\nCloses BIP-7\n",
        "crlf line endings": "intro\r\n<!-- a -->\r\nCloses BIP-7\r\n",
        "trailing blank lines": "Closes BIP-7\n\n\n",
    }

    @pytest.mark.parametrize("name", sorted(BODIES))
    def test_mask_returns_the_line_count_it_was_given(self, name):
        body = self.BODIES[name]
        tokens = grammar._MD.parse(body)
        before = len(grammar._LINE_SPLIT_RE.split(body))
        after = len(grammar._LINE_SPLIT_RE.split(grammar._mask(body, tokens)))
        assert after == before, (
            f"{name}: masking changed the line count {before} -> {after}; "
            "every span computed from the token stream is now misaligned"
        )


class TestEditRefusesToChangeLineCount:
    """The production boundary itself, not a proxy for it (Morrow 3385).

    `_mask` computes spans once from the token stream and applies them to a
    list later edits mutate, so an edit returning a different number of lines
    misaligns every span still queued. `edit()` now refuses rather than
    corrupting line numbers quietly, and this pins that refusal by driving a
    REACHED edit — one a real body actually triggers — to collapse.
    """

    #: A body whose comment span genuinely spans lines, so the comment edit is
    #: reached rather than simulated.
    BODY = "intro\n<!--\nhidden\n-->\nCloses BIP-7\n"

    def test_precondition_this_body_reaches_the_comment_edit(self):
        # Without this the next test could pass because nothing ran at all.
        tokens = grammar._MD.parse(self.BODY)
        masked = grammar._mask(self.BODY, tokens)
        assert masked != self.BODY, "body does not reach the comment edit"
        assert len(grammar._LINE_SPLIT_RE.split(masked)) == len(grammar._LINE_SPLIT_RE.split(self.BODY))

    def test_a_reached_edit_that_collapses_lines_is_refused(self, monkeypatch):
        class Collapsing:
            """Drops the line breaks the real substitution must preserve.

            Swapped in as the module attribute because a compiled pattern's
            `.sub` is read-only, and `_mask` reads this global at call time.
            """

            @staticmethod
            def sub(repl, string):
                return string.replace("\n", "")

        monkeypatch.setattr(grammar, "_COMMENT_SPAN_RE", Collapsing)
        with pytest.raises(grammar.MaskLineCountError) as excinfo:
            grammar._mask(self.BODY, grammar._MD.parse(self.BODY))
        assert "line count" in str(excinfo.value)

    def test_the_guard_is_unreachable_from_ordinary_input(self):
        # The other direction: if real bodies could trip it, the guard would be
        # a denial of service on our own parser rather than a development-time
        # signal. Every body the suite carries must pass clean.
        for name, body in TestMaskingPreservesLineCount.BODIES.items():
            grammar._mask(body, grammar._MD.parse(body))


class TestMultiLineInlineCommentStillParses:
    """A multi-line inline HTML comment must not break the parse (Aria/Sia, #52).

    markdown-it emits `<!-- one\ntwo -->` inside a paragraph as a SINGLE
    `html_inline` token whose content carries the newline. A shared closure
    once blanked it to "", which dropped a line and made `edit()` refuse a
    perfectly ordinary PR body. The two closures are separate now because they
    differ in exactly this respect.

    Asserting the DIRECTIVE still fires, not just that the count is preserved:
    the line-count test would pass on a mask that preserved the count while
    eating the trailer.
    """

    @pytest.mark.parametrize(
        "body",
        [
            "intro <!-- one\ntwo --> tail\nCloses BIP-7\n",
            "intro <!-- a\nb\nc --> tail\nCloses BIP-7\n",
            "<!-- lead\nin -->\nCloses BIP-7\n",
        ],
    )
    def test_directive_survives_a_multi_line_inline_comment(self, body):
        result = parse_directives(body, source="pr_body")
        assert keys(result) == [(COMPLETE, "BIP-7")], "the trailer must still be seen"

    def test_the_comment_text_itself_is_still_masked(self):
        # The comment must not become classifiable just because it spans lines.
        body = "intro <!-- Closes BIP-99\nstill hidden --> tail\nCloses BIP-7\n"
        result = parse_directives(body, source="pr_body")
        assert keys(result) == [(COMPLETE, "BIP-7")], "BIP-99 is inside the comment and must not fire"


class TestElementsWhoseContentTheRendererRemoves:
    """A directive inside an unclosed `script` or `style` must not be seen.

    Found by Aria against the deployed renderer, ruled into the redesign by
    Morrow: the two directive policies legitimately differ about block HTML,
    but "is this text inside an element the renderer empties" is a FACT about
    the document rather than a policy, and it was implemented twice. This
    parser had its own answer and got it wrong.

    The consequence was executable and is the exploit class BIP-54 exists to
    close: an unclosed `<style>` renders its contents invisible on Forgejo,
    and this parser extracted a COMPLETE-class directive from inside one. A
    ticket could be closed by a directive no human could see.
    """

    @pytest.mark.parametrize("body,label", [
        ("<script>\n\nrefs BIP-7", "unclosed script, later block"),
        ("<script>\nrefs BIP-7", "unclosed script, next line"),
        ("<style>\n\nresolves BIP-7", "unclosed style, and COMPLETE-class"),
        ("<style>\nfixes BIP-7", "unclosed style, next line"),
    ])
    def test_a_directive_inside_an_emptied_element_is_not_recognized(self, body, label):
        result = parse_directives(body, source="pr_body")
        assert keys(result) == [], f"a directive nobody can see was recognized: {label}"

    def test_it_is_ALSO_not_reported_as_a_near_miss(self):
        """Silence is right here, unusually. A near miss says "you nearly wrote
        a directive" to an author who wrote one they cannot see either; the
        loud-inertness rule exists for text a reader CAN see."""
        result = parse_directives("<style>\n\nresolves BIP-7", source="pr_body")
        assert result.near_misses == []

    @pytest.mark.parametrize("body,label", [
        ("<script>x</script>\n\nrefs BIP-7", "a CLOSED script suppresses only itself"),
        ("<div>\n\nrefs BIP-7", "an ordinary element empties nothing"),
        ("<!-- c -->\n\nrefs BIP-7", "nor does a closed comment"),
    ])
    def test_POSITIVE_CONTROL_ordinary_html_does_not_suppress(self, body, label):
        """Without these the class above is satisfiable by suppressing all raw
        HTML, which would be silent inertness — the BIP-33 failure this module
        exists to prevent, arriving through the fix for the opposite one."""
        result = parse_directives(body, source="pr_body")
        assert keys(result) == [(ADVANCE, "BIP-7")], label

    def test_a_script_inside_a_FENCE_opens_nothing(self):
        """The state comes from the token stream, never a raw scan. A `<script>`
        in a fence is text; every past attempt to answer this with a string pass
        got exactly that wrong."""
        result = parse_directives("```\n<script>\n```\n\nrefs BIP-7", source="pr_body")
        assert keys(result) == [(ADVANCE, "BIP-7")]


class TestBothConsumersDeriveFromTheOneProduct:
    """Morrow RC 3667's proof, kept as a test.

    An earlier revision asked the shared view only which line NUMBERS carried
    visible text and then took the verdict TEXT from `_mask`. That is two
    extractors sharing one fact, not one product: replacing every fragment's
    text changed what the unanchored matcher saw and left the canonical parser
    reading the source line unchanged.

    If this test ever needs `_mask` to pass, the refactor has come undone.
    """

    # test_replacing_fragment_text_moves_BOTH_consumers is DELETED with its
    # subject: the unanchored consumer (recognizable_text) no longer exists, so
    # there is only ONE consumer of the fragment view and nothing to prove in
    # step with it. The canonical parser's own fragment coupling is covered by
    # the masking/line-count classes below.

    def test_the_predecessor_extractors_are_gone(self):
        """Dead code that claims to be the owner is worse than no comment: a
        maintainer greps for the owner, finds it, and believes it."""
        for name in ("suppressed_line_numbers", "_visible_from_inline"):
            assert not hasattr(grammar, name), f"{name} survived the refactor"


class TestCommentOpenerInsideRawHTML:
    """Where a comment opener LIVES decides whether it suppresses prose.

    Sable's three forms, all with a complete-class trailer. `_mask`'s comment
    scan is a text scan, so `<details title="<!--">` reads as an opener; the
    HTML parser knows it is an attribute value. Exempting only html_block
    fragments closed form A and left form B — prose AFTER such a block was
    still gated on the scan that got the attribute wrong.

    The rule is not "ignore openers inside html_blocks": an opener in html_block
    TEXT genuinely does suppress onward, and the renderer proves it — the
    conformance corpus records `<div>\\n`<!--`\\n</div>` rendering as `<div>\\n``
    with everything after swallowed. So each block is asked of the parser, which
    answers by what it leaves unconsumed.
    """

    ATTR_BLOCK = '<details title="<!--">\nx\n</details>'

    def test_A_trailer_inside_the_block_is_recognized(self):
        r = parse_directives('<details title="<!--">\nCloses BIP-7\n</details>', source="pr_body")
        assert keys(r) == [(COMPLETE, "BIP-7")]

    def test_B_trailer_in_a_FOLLOWING_paragraph_is_recognized(self):
        """The form the first fix missed: the block is behind it, not around it."""
        r = parse_directives(f"{self.ATTR_BLOCK}\n\nCloses BIP-7", source="pr_body")
        assert keys(r) == [(COMPLETE, "BIP-7")], "prose after the block was gated on the bad scan"

    def test_C_CONTROL_a_benign_attribute_behaves_the_same(self):
        """Without this, form B is satisfiable by ignoring html_blocks wholesale
        — the control says the comment opener is the variable, not the layout."""
        r = parse_directives('<details title="ok">\nx\n</details>\n\nCloses BIP-7', source="pr_body")
        assert keys(r) == [(COMPLETE, "BIP-7")]

    @pytest.mark.parametrize("body,label", [
        ("> <!--\n\nCloses BIP-7", "a blockquote opener still suppresses"),
        ("- <!--\n\nCloses BIP-7", "a list-item opener still suppresses"),
    ])
    def test_openers_where_the_mask_is_the_ONLY_authority_still_suppress(self, body, label):
        """These live in contexts the view never emits, so nothing else can see
        them. Losing them would trade one silent false action for another."""
        assert keys(parse_directives(body, source="pr_body")) == [], label

    def test_an_opener_in_html_block_TEXT_still_suppresses_what_follows(self):
        """The renderer swallows everything after it, so this must too — and it
        is what made 'ignore all html_block openers' the wrong rule."""
        body = "<div>\n`<!--`\n</div>\n\nCloses BIP-7\n\n-->\n"
        assert keys(parse_directives(body, source="pr_body")) == []


class TestBenignMarkupIsNotExtraContent:
    """Morrow's §M2 ruling on BIP-64, both classes pinned as he asked.

    An attribute-free empty element renders nothing and conceals nothing, so
    the line's semantic visible text is still exactly the directive. Before
    this, ANY inline HTML after a trailer demoted it — `Closes BIP-N <br>` did
    not close the ticket, silently, with a near miss as the only trace (Aria).

    The ruling is deliberately NARROW and the second class is the half that
    keeps it so: comments can conceal source content, an attribute is source
    content a reader never sees, and `<b>x</b>` carries visible text. All
    remain disqualifying.
    """

    @pytest.mark.parametrize("body,klass", [
        ("refs BIP-7 <br>", ADVANCE),
        ("Closes BIP-7 <br>", COMPLETE),
        ("Fixes BIP-7 <br/>", COMPLETE),
        ("Resolves BIP-7 <hr>", COMPLETE),
        ("refs BIP-7 <br> <br>", ADVANCE),
    ])
    def test_attribute_free_empty_markup_does_not_disqualify(self, body, klass):
        r = parse_directives(body, source="pr_body")
        assert keys(r) == [(klass, "BIP-7")], f"{body!r} was demoted"
        assert near(r) == [], "a clean trailer must not also be reported as a near miss"

    @pytest.mark.parametrize("body,label", [
        ("Closes BIP-7 <!-- note -->", "a comment can conceal source content"),
        ('Closes BIP-7 <br class="x">', "an attribute is source content a reader never sees"),
        ("Closes BIP-7 <b>x</b>", "x is visible, so it IS extra content"),
        ("Closes BIP-7 `x`", "a code span carries content"),
        ("Closes BIP-7 once CI is green", "the case the rule was written for"),
    ])
    def test_concealing_or_visible_content_still_disqualifies(self, body, label):
        """Without these the ruling would read as 'any inline markup is fine',
        which it explicitly is not."""
        r = parse_directives(body, source="pr_body")
        assert keys(r) == [], f"{label}: {body!r} was accepted"
        assert near(r) == [1], f"{label}: and it must be LOUD, not silent"

    @pytest.mark.parametrize("sep", _UNICODE_SEPS)
    @pytest.mark.parametrize(
        "layout", ["Closes BIP-7 <br>{sep}", "Closes BIP-7 <br> {sep}"]
    )
    def test_a_benign_removal_does_not_launder_a_masked_one(self, layout, sep):
        """A benign removal accounts for ITSELF and for nothing else.

        The rule above is RECONSTRUCTION — deleting the recorded benign spans
        from the source must rebuild the residue — and not the weaker question
        "was anything concealing recorded?". Sable mutated it back to the
        weaker one and the whole bridge suite stayed green, identical to
        baseline: the rule was correct and completely unobserved.

        These are the cases that separate the two, and nothing else in this
        file does. A masking-stage removal records NOTHING, so "nothing
        concealing was recorded" is true of a line that a unicode separator
        was smuggled into; only reconstruction notices the difference it left.
        The neighbouring guards each pass the mutant — `Closes BIP-7{sep}` is
        pinned loud above, `<br>` is pinned benign above — and the hole is
        exactly one `<br>` wide, between them.
        """
        body = layout.format(sep=sep)
        r = parse_directives(body, source="pr_body")
        assert keys(r) == [], f"{body!r} was accepted: a benign span laundered a masked one"
        assert near(r) == [1], f"{body!r} must be LOUD, not silent"

    def test_an_UNRECOGNISED_removal_form_is_treated_as_concealing(self):
        """Fail closed on shapes the classifier does not know. A removal it
        cannot categorise must not be assumed harmless — that is how a narrow
        ruling becomes a wide one by accident."""
        assert grammar._removal_kind("<!-- x -->") == grammar.CONCEALING
        assert grammar._removal_kind('<span data-x="1">') == grammar.CONCEALING
        assert grammar._removal_kind("<br>") == grammar.BENIGN_MARKUP
        assert grammar._removal_kind("</b>") == grammar.BENIGN_MARKUP


class TestSelectionHasOneOwner:
    """BIP-54: the unanchored matcher and the keyword→class map lived in
    `forgejo_bridge` with their own spellings, beside this module's own. Two
    copies of one rule, and the runtime used the other one — so adding a keyword
    here changed nothing the bridge did, silently.

    Since the write-authority ruling this is SELECTION rather than authority: it
    answers which ticket an event concerns, and the class it returns is a
    proposal the write boundary may ignore.
    """

    def test_the_bridge_reads_ITS_keywords_from_this_module(self):
        """The property, not the arrangement: teach the grammar a keyword and the
        bridge must honour it.

        THIS TEST EDITS THE MAP AND NOTHING ELSE (Rowan RC 3725). Its first
        version rebuilt the compiled pattern too — which meant it passed while
        the map and the pattern were BOTH owners, hiding the exact duplication it
        claimed to disprove. A test that edits every copy cannot detect that
        there are copies.
        """
        from plane.bridge import grammar as g

        original = dict(g.KEYWORD_CLASS)
        try:
            g.KEYWORD_CLASS["squashes"] = g.COMPLETE
            # The regex is built from the map's keys; a keyword the pattern
            # does not know still cannot match, and THAT is the honest coupling
            # boundary now there is one grammar: class lookup has ONE owner,
            # and admission has one anchored pattern. Adding a keyword needs
            # the datum regeneration, and this pin holds that removing the map
            # entry alone is enough to make a keyword inert (the direction that
            # caught the original duplication).
            assert g._keyword_class("squashes") == g.COMPLETE, (
                "a keyword added to the map alone did not reach class lookup — second owner"
            )
        finally:
            g.KEYWORD_CLASS.clear()
            g.KEYWORD_CLASS.update(original)

    def test_removing_a_keyword_from_the_map_alone_makes_it_inert(self):
        """The other direction, which is what caught the duplication: a map-only
        DELETE used to raise KeyError at match time, because the pattern still
        matched a word the map no longer knew. Removing it must simply stop
        selecting, quietly and without an exception."""
        from plane.bridge import grammar as g

        original = dict(g.KEYWORD_CLASS)
        try:
            del g.KEYWORD_CLASS["closes"]
            assert g.forward_selection("closes GB-1", source="pr_body")[0] == []
        finally:
            g.KEYWORD_CLASS.clear()
            g.KEYWORD_CLASS.update(original)

    def test_a_word_that_is_not_a_keyword_selects_nothing(self):
        """The pattern matches any word before an id; the MAP decides whether
        that word is a keyword. `see BIP-7` is a reference, not a directive."""
        assert grammar.forward_selection("see GB-1", source="pr_body")[0] == []
        assert grammar.forward_selection("mentions GB-1", source="pr_body")[0] == []

    @pytest.mark.parametrize("text,expected", [
        ("close GB-1", grammar.COMPLETE),
        ("fix GB-1", grammar.COMPLETE),
        ("resolve GB-1", grammar.COMPLETE),
        ("ref GB-1", grammar.ADVANCE),
        ("closes GB-1", grammar.COMPLETE),
        ("refs GB-1", grammar.ADVANCE),
    ])
    def test_the_SINGULAR_forms_still_select(self, text, expected):
        """KEEP EVERY FEATURE. The deployed matcher accepts the singular
        spellings, and dropping them while consolidating would have silently
        deleted behaviour operators rely on — a deletion wearing a cleanup's
        clothes, which is what round three of this ticket actually was. Both
        grammars now read one shared map, so these forms are live in each."""
        assert grammar.forward_selection(text, source="pr_body")[0] == [("GB", 1, expected)]

    def test_the_longer_spelling_is_not_read_as_the_shorter_one(self):
        """The selection pattern carries no alternation at all now — it matches
        a word and asks the map — so this pins the outcome rather than the
        mechanism that used to threaten it."""
        assert grammar.forward_selection("closes GB-1", source="pr_body")[0] == [("GB", 1, grammar.COMPLETE)]

    @pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x0b", "\x0c", "\x85"])
    def test_a_unicode_separator_between_keyword_and_id_does_NOT_select(self, sep):
        """The copy this replaces used `[:\\s]+`, and Python's `\\s` matches VT,
        FF, FS, GS, RS, NEL, LS and PS — so a character the renderer treats as
        ordinary content could sit between keyword and id and still match. The
        anchored grammar has refused `\\s` since Morrow 3334; the two were simply
        inconsistent. Space and tab only, both sides."""
        assert grammar.forward_selection(f"closes{sep}GB-1", source="pr_body")[0] == []

    def test_the_weaker_class_wins_a_same_ticket_conflict(self):
        """A false completion costs more than an under-move: a stale ticket is
        visibly behind, a completed one is invisibly wrong."""
        # TWO CLEAN TRAILER LINES (anchored grammar): the same-line inline
        # form is prose now. The conflict itself is the OTHER canonical datum —
        # recorded, not just demoted (Scope A 110-112; Morrow).
        noms, _near, conflicts = grammar.forward_selection("Closes GB-1\nRefs GB-1", source="pr_body")
        assert noms == [("GB", 1, grammar.ADVANCE)]
        assert conflicts == ["GB-1"], "the demotion must be recorded, not silent"

    def test_a_directive_inside_a_code_span_is_prose_ABOUT_one(self):
        """Selection reads only what a reader sees."""
        assert grammar.forward_selection("see `closes GB-1` in the docs", source="pr_body")[0] == []

    @pytest.mark.parametrize("text", [
        "closes:GB-1",       # colon with no whitespace after it
        "closes::GB-1",      # repeated colons
        "closes ::GB-1",
        "closes :GB-1",      # space BEFORE the colon
        "closes\nGB-1",      # LF between keyword and id
        "closes\r\nGB-1",    # CRLF
        "closes\rGB-1",      # CR
        "closes:\nGB-1",     # colon then linebreak
        "closes:\r\nGB-1",
    ])
    def test_only_the_canonical_separator_selects(self, text):
        """Morrow's separator ruling: optional SINGLE colon, then required space
        or tab. The form this replaces used a colon-or-whitespace class, which
        accepted all of these — and the whitespace half crossed LINES, so a
        keyword at the end of one line could bind an id at the start of the
        next. The corpus across 85 pull requests and every reachable commit
        message contains zero compatibility-only matches of these shapes, so
        refusing them needs no operator migration note."""
        assert grammar.forward_selection(text, source="pr_body")[0] == []

    @pytest.mark.parametrize("text", ["closes GB-1", "closes: GB-1", "closes:\tGB-1", "closes\tGB-1"])
    def test_the_accepted_separators_still_select(self, text):
        """The positive controls. Refusing the malformed shapes must not make
        the matcher impotent — bare space, colon-space, colon-tab and bare tab
        are the canonical forms and all still select."""
        assert grammar.forward_selection(text, source="pr_body")[0] == [("GB", 1, grammar.COMPLETE)]

    def test_a_conventional_commit_prefix_is_not_a_near_miss(self):
        """The cost of bringing the singular forms into the ANCHORED grammar,
        found by an existing test rather than by reasoning: `fix` at line start
        is the conventional-commit prefix. `fix(api): tidy the widget (BIP-7)`
        opened a near miss on ordinary commit subject text — and a near miss is
        an UNDETERMINED OUTCOME, which is meant to reach a person, so every
        conventional-commit subject in the fleet would have been reported as a
        problem by someone who did nothing wrong. (What reaching a person means
        today: a comment on the pull request, only where the write token is set.
        There is no nudge and no notification in this release, so on a push or a
        token-less deployment it reaches nobody at all — which makes this noise
        cheaper today than it will be, not harmless.) The opener requires a
        directive separator after the keyword: `fix(` is not an attempt."""
        r = parse_directives("fix(api): tidy the widget (BIP-7)", source="commit_message")
        assert keys(r) == []
        assert near(r) == []


class TestNearMissRequiresAnAdjacentId:
    """Vex, measured on main rather than predicted. The opener tested
    CONTAINMENT — keyword at line start, id-shaped token anywhere after it —
    which was survivable while only the plural keywords were anchored. The
    shared keyword map brought the singular forms in and the noise surface grew
    the same day.

    It matters because a near miss is an undetermined outcome, and under the
    write ruling an undetermined outcome is meant to REACH A PERSON. Noise here
    is not a log line; it is a report addressed to someone who wrote ordinary
    English correctly. Today that report is a pull-request comment and only with
    a write token — there is no nudge — so the cost is currently latent rather
    than absent, and it lands in full the day the telling half ships.
    """

    @pytest.mark.parametrize("text", [
        "Fix for BIP-7 is in review",
        "Fixes for BIP-7 are in review",
        "Resolve the BIP-7 regression next sprint",
        "Refs in BIP-7 need updating",
    ])
    def test_a_sentence_that_merely_mentions_a_ticket_is_silent(self, text):
        """A word stands between the keyword and the id, so this is a sentence
        that mentions a ticket rather than a failed attempt to write a
        directive. Silent — no directive AND no near miss."""
        r = parse_directives(text, source="commit_message")
        assert keys(r) == []
        assert near(r) == []

    @pytest.mark.parametrize("text", [
        "Closes BIP-7 once CI is green",
        "Close BIP-7 after QA",
        "Closes VERYLONGPROJECT-7",
        "Closes: BIP-7 later",
    ])
    def test_an_attempt_with_the_id_in_position_is_still_LOUD(self, text):
        """The positive control, and the reason adjacency is the right line
        rather than 'anything with trailing prose is silent'. An attempt puts
        the id where a directive would; only the tail is wrong. Silencing these
        would restore the failure this grammar exists against — a directive that
        does nothing, quietly."""
        assert near(parse_directives(text, source="commit_message")) == [1]

    @pytest.mark.parametrize("sep", [" ", " ", "\x0b", "\x85"])
    def test_a_smuggled_separator_keeps_the_id_adjacent_and_LOUD(self, sep):
        """Adjacency must not become a way to smuggle: the illegitimate
        separators stay in the opener's class precisely so a character the
        renderer treats as ordinary content cannot buy silence (Morrow 3346)."""
        assert near(parse_directives(f"Closes{sep}BIP-7", source="pr_body")) == [1]
