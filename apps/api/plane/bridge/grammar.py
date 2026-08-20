# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The completion grammar — lexically closed directive parsing (BIP-45).

Normative source: the Scope-A design, architecture/biplane-a-scope-design.md, §M2. The
shape, in one paragraph: a directive is an ANCHORED TRAILER LINE — start of
line, entire line (a single terminal ``.`` or ``!`` tolerated), colon optional
— carrying exactly one keyword and one ticket id. Directives are recognized in
PR bodies and commit message bodies, nowhere else. A line that BEGINS like a
directive but fails the full-line match is a NEAR MISS and is reported loudly
under the durable key ``ignored.near_misses`` — silent inertness is the failure
class this module exists to kill, in both directions (BIP-33).

Keyword classes (John's rulings, 2026-08-11), all EIGHT spellings, singular and
plural, no past tense: ``close``/``closes``, ``fix``/``fixes``,
``resolve``/``resolves`` are complete-class; ``ref``/``refs`` are the
advance-class keywords.

**A class is a PROPOSAL, not an outcome.** Nothing completes and nothing
advances (BIP-67): the write boundary refuses every board write, so a
complete-class directive selects a ticket and proposes a completion that is then
declined and recorded. This module only classifies; what the class is used for
is the boundary's business.

WHY A TOKENIZER, AND WHY A CORPUS AS WELL
-----------------------------------------
Block context used to be tracked by hand here. Six adversarial rounds (Morrow
RC 3333/3334/3340/3342/3346) each found a NEW class of divergence between this
parser and what the forge actually shows a human: crafted fences, lazy
blockquote continuation, unicode line separators, paragraph-vs-leaf-block
state, autolink and inline-span quote paragraphs, empty list markers,
code-span comment openers. The seventh class — setext headings — was found
twice independently (Vex 3353 by tokenizer diff, Aria by renderer probing) and
is the one that ended the approach: this module was single-pass with no
lookahead, so it could not see a setext underline BY CONSTRUCTION. Not a case
it missed; a case its shape could not reach.

So block structure is now ``markdown-it-py``'s job. Two constraints on that,
both established by measurement rather than argument:

* **The tokenizer is not the authority.** Forgejo renders with Goldmark plus
  extensions, so no CommonMark parser can be exactly right (Morrow's
  conditional-fit ruling). Definition lists are the proof: ``Closes BIP-7``
  followed by ``: body`` renders ``<dl><dt>``, which plain markdown-it calls a
  paragraph. The deflist plugin closes most of it and still differs on a
  whitespace-only body. The renderer-derived corpus in
  ``tests/unit/bridge/renderer_corpus.json`` is therefore PERMANENT
  infrastructure, not a one-time check.
* **``html=True``, and classify ``html_block`` too** (Vex 3353 §3). The
  property this grammar needs is VISIBILITY AFTER FORGEJO'S SANITIZER, not
  html-block-ness, and the two settings fail in opposite directions:
  ``html=False`` lets an unterminated HTML comment — invisible on Forgejo —
  carry a line that then CLOSES A TICKET; ``html=True`` with paragraphs only
  makes a plain ``<img>`` badge at the top of a PR body swallow the trailer
  under it, which is silent inertness. Comments are keyed on specifically,
  which is the distinction that gets both right.

Invariant 7 note: nothing in the parsed text chooses how it is parsed. The
caller names the source; ``pr_title`` is a contract error by design (Morrow
3288 — titles are inert, the BridgeEvent source enum is two-member). The
conformance corpus is generated OFFLINE and committed; the renderer is never
in the runtime path, or the input would be choosing its own validation.

Ruling carried (Morrow, 2026-08-12): a no-blank ``Closes BIP-7`` above ``---``
or ``===`` is HEADING CONTEXT — zero directives AND zero near-misses. Unmerged
parser behaviour has no compatibility entitlement; already-processed outcomes
are protected at the durable-event and grammar-version boundary, not by
retaining the divergence here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from markdown_it import MarkdownIt

COMPLETE = "complete"
ADVANCE = "advance"

VALID_SOURCES = ("pr_body", "commit_message")

# ONE KEYWORD→CLASS MAP, FOR THE ONE GRAMMAR (Morrow's ruling, 2026-08-14).
#
# HISTORY, and it is only history: the anchored trailer grammar below and an
# unanchored selection matcher once carried separate keyword sets — plural-only
# here, plural-and-singular in the bridge's copy. His ruling then was that
# admission policy might differ between the two but spelling and class might
# not, because what the word `closes` MEANS is one fact and two spellings of it
# is how they drift apart silently.
#
# THAT SPLIT NO LONGER EXISTS. The compatibility matcher was deleted with its
# last caller; `forward_selection` delegates to `parse_directives`, so there is
# ONE admission policy — the anchored trailer — on every path. Nothing below
# reconciles two consumers, and prose describing a live contrast between them
# would be describing code that is gone.
#
# Singular and plural, no past tense: `close`/`closes`, never `closed`.
KEYWORD_CLASS = {
    "ref": "advance", "refs": "advance",
    "close": "complete", "closes": "complete",
    "fix": "complete", "fixes": "complete",
    "resolve": "complete", "resolves": "complete",
}
# Longest-first, because regex alternation is first-match rather than
# longest-match: without it `closes` matches `close` and leaves a stray `s`.
_KEYWORDS_LONGEST_FIRST = sorted(KEYWORD_CLASS, key=len, reverse=True)

_COMPLETE_KEYWORDS = tuple(k for k, v in KEYWORD_CLASS.items() if v == "complete")
_ADVANCE_KEYWORDS = tuple(k for k, v in KEYWORD_CLASS.items() if v == "advance")
_ALL_KEYWORDS = tuple(_KEYWORDS_LONGEST_FIRST)

# The whole line, or it is not a directive: optional colon after the keyword,
# one ticket id, optional SINGLE terminal . or !, trailing whitespace only.
# The KEYWORD is case-insensitive (doc §M2); the TICKET ID is UPPERCASE-only,
# matching the DEPLOYED matcher — widening id case here would make lowercase
# refs newly complete tickets the day this module is wired (7of9 3327).
# Lexical whitespace is SPACE/TAB ONLY throughout this grammar — never \s.
# Python's \s matches VT/FF/FS/GS/RS/NEL/LS/PS, so a U+2028 between keyword
# and id would alter grammar through characters the renderer treats as
# ordinary content (Morrow 3334).
_DIRECTIVE_RE = re.compile(
    r"^(?i:(?P<kw>" + "|".join(_ALL_KEYWORDS) + r"))"
    r":?[ \t]+"
    r"(?P<project>[A-Z][A-Z0-9]{1,11})-(?P<num>[0-9]{1,6})"
    r"[.!]?[ \t]*$"
)

# Near-miss requires directive SHAPE, not merely a keyword opener: a keyword
# at line start plus an id-like token anywhere after it. "Fixes the race
# condition." is prose and stays silent (7of9 3327); an oversized project
# prefix or a unicode-separator smuggle is id-shaped and LOUD (Morrow 3346).
#
# THE KEYWORD MUST BE FOLLOWED BY A DIRECTIVE SEPARATOR — colon, space or tab.
# Required the moment the shared map brought the SINGULAR forms into the
# anchored grammar, because `fix` at line start is the conventional-commit
# prefix: `fix(api): tidy the widget (BIP-7)` opened a near miss on a line that
# is ordinary commit subject text. That matters more than it reads — a near
# miss is an undetermined outcome, and an undetermined outcome is meant to
# reach a person, so every conventional-commit subject in the fleet would have
# interrupted someone who did nothing wrong. (The notification half is cut from
# this release; the reply still speaks where a pull request exists.) `fix(` is not a
# directive attempt; `fix ` and `fix:` are.
# The separator set here is WIDER than the directive's on purpose: colon, space
# and tab are the legitimate ones, and the smuggling characters are included
# precisely BECAUSE they are illegitimate — `Closes BIP-7` must stay LOUD
# (Morrow 3346), and requiring only the legitimate separators silenced it, which
# an existing test caught. What is excluded is `fix(`: a keyword welded to
# ordinary text, which is prose rather than a failed attempt at a directive.
_OPENER_SEPARATOR = r"[: \t\x0b\x0c\x1c\x1d\x1e\x85  ]"
# THE ID MUST BE ADJACENT, NOT MERELY PRESENT ON THE LINE (Vex, measured on
# main rather than predicted). This tested CONTAINMENT — keyword at line start,
# id-shaped token anywhere after it — which was survivable while only the plural
# keywords were anchored. The shared map brought the singular forms in, and the
# noise surface grew the same day.
#
# THE LINE IS WHERE THE ID SITS, NOT WHETHER PROSE FOLLOWS IT (Morrow — an
# earlier version of this comment listed four examples as interruptions when
# three of them stay loud by design, which made the comment claim more than the
# change does):
#
#     Fix for BIP-7 is in review     SILENT   a word stands between keyword and
#                                             id, so this is a sentence that
#                                             mentions a ticket
#     Close BIP-7 after QA           LOUD     the id is in directive position;
#                                             only the tail is wrong
#     Closes BIP-7 once CI is green  LOUD     same shape, and loud since long
#                                             before the singular forms existed
#
# A near miss is a FAILED ATTEMPT at a directive, and an attempt puts the id
# where a directive would. So adjacency silences the sentences and leaves the
# attempts alone — it does not decide whether a trailing-prose attempt should
# be loud at all, which is a separate question and the grammar owner's.
#
# It is not tidiness either way: under the write ruling a near miss is an
# undetermined outcome, and an undetermined outcome is meant to REACH A PERSON.
# A false one is an interruption addressed to someone who did nothing wrong,
# which is the difference between a diagnostic and a notification.
# (How it reaches them today: a comment on the pull request, and only where the
# write token is set — there is no nudge and no notification in this release,
# so on a token-less deployment it reaches nobody. That makes a false near miss
# cheaper today than it will be, not harmless.)
#
# The smuggling separators stay in the class, so `Closes<U+2028>BIP-7` is still
# adjacent and still loud (Morrow 3346) — adjacency must not be purchasable
# with a character the renderer treats as ordinary content.
_OPENER_RE = re.compile(
    r"^(?:" + "|".join(_ALL_KEYWORDS) + r")\b" + _OPENER_SEPARATOR + r"+"
    r"(?=\b[A-Za-z][A-Za-z0-9]{0,30}-(?:[0-9]|[^A-Za-z \t]|[ \t]*$))",
    re.IGNORECASE,
)

# Only the line endings the forge Markdown contract recognizes create lines —
# the same set markdown-it normalizes on, so token line maps and these indices
# cannot drift. str.splitlines() would also split on VT/FF/FS/GS/RS/NEL/LS/PS,
# synthetic anchors smuggled inside what renders as ONE line (Morrow 3333).
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

_COMMENT_SPAN_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class MaskLineCountError(RuntimeError):
    """An edit returned a different number of lines than it was given.

    A programming error, not a runtime condition. ``_mask`` computes every span
    once from the token stream and applies them to a list that later edits
    mutate, so a count change silently misaligns every span still queued — and
    this module reports its verdicts as LINE NUMBERS.

    Raising aborts the parse, which fails toward "no directive fired, loudly"
    rather than "a directive fired against the wrong line". Enforced at the one
    application point (Morrow 3385, Rowan 3391) instead of approximated by a
    corpus: every masking test already reaches ``edit()``, so a new edit kind
    that breaks the invariant reddens the existing suite at development time
    and cannot reach a delivery without shipping a change that detonates it
    (Aria — the argument that decided this).
    """


# Blocks whose lines carry text a human reads as prose. Headings, list items,
# table cells, definition terms, quotes and code are all NOT this — a trailer
# rendered inside them is not a trailer.
_CLASSIFIABLE = ("paragraph_open", "html_block")

# Internal marker for "this line carried a comment alongside directive-shaped
# text". A NUL cannot occur in a masked source line, so it cannot be forged by
# the input — the sentinel is ours, not the author's (invariant 7).
_NEAR_MISS = "\0near-miss"


def _build_parser() -> MarkdownIt:
    """Tokenizer configured to track the forge's dialect as closely as any
    CommonMark parser can. ``maxNesting`` is left at the preset default: it is
    the DoS control, and raising it turns a 500-byte body of nested quotes
    into a RecursionError (Vex 3353). Over-depth content is silently dropped
    from the token stream, which is why near misses are computed from lines
    blocks POSITIVELY claim, never from "lines no token claimed"."""
    md = MarkdownIt("commonmark", {"html": True})
    md.enable("table")  # Goldmark ships GFM tables
    md.enable("strikethrough")  # ...and strikethrough
    # NO deflist plugin, and the reason is a ruling rather than an omission
    # (Vex 3354): chasing Goldmark's extension set is unbounded and, measured,
    # the deflist plugin makes this WORSE. Forgejo renders `Closes BIP-7`
    # above `: why` as a VISIBLE <dt>; plain markdown-it calls it a paragraph
    # and fires, which is correct. With the plugin the token stream becomes
    # dl_open/dl_close, no paragraph, and a directive a human can plainly read
    # is silently ignored — the BIP-33 class, traded in for tidier structure.
    return md


_MD = _build_parser()


@dataclass(frozen=True)
class Directive:
    keyword_class: str  # COMPLETE | ADVANCE
    ticket_key: str  # e.g. "BIP-7"
    line: int  # 1-based line number in the source text
    source: str = ""  # pr_body | commit_message — self-describing for the
    # event-matrix merge across sources (7of9 3327)


@dataclass(frozen=True)
class NearMiss:
    line: int
    text: str


@dataclass
class ParseResult:
    directives: list[Directive] = field(default_factory=list)
    near_misses: list[NearMiss] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # ticket keys demoted on conflict
    # Every line judged a live trailer, BEFORE duplicate collapse and conflict
    # demotion. This is the only field comparable to the renderer-derived
    # corpus: that oracle answers "is this source line top-level prose?", and
    # dedup/demotion are our policy layered on top. Keeping them apart stops
    # the oracle from ratifying our own choices back to us.
    recognized_lines: list[int] = field(default_factory=list)


def _keyword_class(keyword: str):
    """Class from the LIVE map — the map is the only owner.

    The previous body consulted _COMPLETE_KEYWORDS, a tuple FROZEN AT IMPORT:
    a second owner derived from the map, which is exactly the duplication the
    one-owner tests exist to catch — a runtime map edit reached the pattern's
    idea of keywords but not the class lookup. Returns None for a word the map
    does not know, and the parse treats that line as a non-directive: removing
    a keyword from the map alone makes it inert, without exceptions.
    """
    return KEYWORD_CLASS.get(keyword.lower())


def _blank_preserving_lines(match: re.Match) -> str:
    """Replace a matched span with the SAME number of line breaks.

    Masking must never move a line number: every verdict this module emits is
    reported against a source line, and a collapsed multi-line comment would
    silently renumber every trailer below it.
    """
    return "\n" * match.group(0).count("\n")


def _mask(text: str, tokens) -> str:
    """Blank the spans that are not prose, keeping line numbering exact.

    ONE walk of the token stream collects the work; one helper applies it.
    Everything a token contributes is a (line range, edit) pair, so the four
    rules below differ only in which tokens they read and what they do to the
    lines those tokens own — they are not four algorithms.

    The ORDER the collected work is applied in is load-bearing, and it is the
    only reason these are separate stages at all:

    1. **Code spans**, from the tokenizer's own inline children, scoped to the
       lines their parent covers. A comment opener written as ``` `<!--` ``` is
       neutralized BEFORE comment scanning and cannot suppress the line after
       it (Morrow 3346 item 2). Scoping is what makes the replace safe: a
       whole-text replace masks the first occurrence of the reconstructed
       literal anywhere in the document, so a shadow copy inside a fence
       absorbs it and the real span survives into the comment scan (Vex 3358).
    2. **Fenced and indented code**, whole lines. A line is inside an HTML
       comment IFF THE RENDERER PARSES IT AS ONE, and Forgejo does not treat
       ``<!--`` in a code context as an opener — it displays the characters.
       Leaving code in place lets a shown ``<!--`` pair with a ``-->`` further
       down and silently eat a real trailer (Vex 3354/3355).
    3. **Complete HTML comments**, sourced from tokens, never string search —
       only what markdown-it itself emits as HTML may open one, masked within
       the lines of the token that owns it. A regex over the whole document
       treats any ``<!--`` as an opener, including one the renderer escapes to
       visible prose, which then pairs with a later ``-->`` and blanks a
       visible trailer between them (Vex 3360). Indent an opener four columns
       and it can no longer start a type-2 block, so it stays visible text:
       leaving it visible is correct, leaving it SCANNABLE was the defect.
    4. **Unterminated comments**, last, because it relies on every complete
       span already being gone — an opener still standing has no closer
       anywhere after it. CommonMark runs an unterminated type-2 block to end
       of input and Forgejo renders it to nothing, so those lines are masked
       whatever encloses them (Vex 3354). An unclosed ``<!--`` in PARAGRAPH
       text is the opposite case and is left alone: the renderer shows the
       literal ``&lt;!--`` and the line is merely an unclean trailer.

    Known residual, recorded rather than hidden: ``markup + content + markup``
    does not reconstruct a code span containing a line break, because
    CommonMark converts interior line endings to spaces, so the replacement
    silently matches nothing. Deriving masks from token extents would remove
    the class; no case was found where it matters AND the text is visible.
    """
    lines = _LINE_SPLIT_RE.split(text)

    def edit(span, fn) -> None:
        """Apply ``fn`` to the joined lines of a token's half-open range.

        LOAD-BEARING INVARIANT — every ``fn`` MUST return the same number of
        lines it was given (Aria, #49 review).

        Spans are computed ONCE from the original token stream and applied to
        ``lines``, which earlier stages have already mutated. That is only
        sound while every edit preserves line count: one line that vanishes
        and every span still queued points somewhere else, silently, and the
        verdicts this module reports are line numbers.

        The four-pass version got this for free — each pass re-split the whole
        text, so a count change could only corrupt that pass. Collecting first
        buys one walk and takes on this obligation instead. All three edit
        kinds honour it today, each for its own reason:

        * ``drop_code_spans`` removes a literal that cannot contain a break;
        * ``blank_comment_spans`` replaces one with ``"\\n" * count``;
        * fence blanking assigns ``""`` per line and never resizes;
        * the comment-span sub uses :func:`_blank_preserving_lines`.

        Enforced here rather than approximated by a corpus (Morrow 3385,
        Rowan 3391). An earlier revision documented the obligation and pinned
        it with eleven representative bodies, then admitted the limit: a fourth
        edit kind with no body to reach it would not have gone red. The check
        below removes the limit instead of describing it — it holds for every
        edit, present and future, whenever one is exercised.

        Aria's is the argument that settled it: **every masking test already
        reaches this function**, so a new kind that breaks the count reddens
        the whole existing suite at development time. Broken code cannot get
        to a delivery without shipping a change that detonates the suite
        first, which makes the live-path objection — that a violation aborts
        parsing a PR body — much smaller than it looks. The corpus stays, now
        as ordinary coverage rather than as the guard.
        """
        start, end = span[0], min(span[1], len(lines))
        if start >= end:
            return
        replacement = _LINE_SPLIT_RE.split(fn("\n".join(lines[start:end])))
        if len(replacement) != end - start:
            raise MaskLineCountError(
                f"masking changed the line count {end - start} -> {len(replacement)}; "
                "every span still queued is now misaligned"
            )
        lines[start:end] = replacement

    # TWO closures, not one shared helper, and the split is the fix for a
    # defect this file introduced (Aria/Sia, #52). They differ in exactly one
    # respect — whether the literal can contain a line break — and that
    # difference is the whole line-count invariant.
    #
    # A single `blank_literals` served both callers. Removing its
    # newline-preserving replacement was justified on the CODE SPAN, where the
    # reasoning is sound: CommonMark converts a code span's interior newlines
    # to spaces, so the count is always zero. Three reviewers checked that
    # reasoning and it was true. Nobody checked the second caller, where it is
    # false — markdown-it emits a multi-line inline HTML comment as ONE
    # `html_inline` token whose content carries the newlines. Blanking that to
    # "" drops lines, and `edit()` then refuses a legitimate PR body.
    #
    # Keeping them apart means a true statement about one can no longer
    # silently govern the other.

    def drop_code_spans(literals):
        """Code spans cannot span lines, so the replacement need not."""

        def apply(segment: str) -> str:
            for literal in literals:
                segment = segment.replace(literal, "", 1)
            return segment

        return apply

    def blank_comment_spans(literals):
        """Inline comments CAN span lines; preserve every break they carry."""

        def apply(segment: str) -> str:
            for literal in literals:
                segment = segment.replace(literal, "\n" * literal.count("\n"), 1)
            return segment

        return apply

    # ONE walk. Each stage is a list of (span, edit) closures, applied below.
    code_spans, fences, comments, html_lines = [], [], [], set()
    for tok in tokens:
        if not tok.map:
            continue
        if tok.type in ("fence", "code_block"):
            fences.append(tok.map)
        elif tok.type == "html_block":
            html_lines.update(range(tok.map[0], tok.map[1]))
            comments.append((tok.map, lambda seg: _COMMENT_SPAN_RE.sub(_blank_preserving_lines, seg)))
        elif tok.type == "inline" and tok.children:
            literals = [
                f"{c.markup}{c.content}{c.markup}" for c in tok.children if c.type == "code_inline" and c.markup
            ]
            if literals:
                code_spans.append((tok.map, drop_code_spans(literals)))
            inline_comments = [
                c.content for c in tok.children if c.type == "html_inline" and c.content.startswith("<!--")
            ]
            if inline_comments:
                comments.append((tok.map, blank_comment_spans(inline_comments)))

    for span, fn in code_spans:
        edit(span, fn)
    for span in fences:
        for i in range(span[0], min(span[1], len(lines))):
            lines[i] = ""
    for span, fn in comments:
        edit(span, fn)

    # Unterminated openers, in HTML-block lines only, to end of document.
    if html_lines:
        masking = False
        for i, line in enumerate(lines):
            if masking:
                lines[i] = ""
            elif i in html_lines and "<!--" in line:
                lines[i] = line[: line.index("<!--")]
                masking = True

    return "\n".join(lines)


def _classifiable_lines(text: str, lines: list[str]) -> dict[int, str]:
    """Map 1-based line number -> the line's VISIBLE text, for every line a
    top-level paragraph or HTML block claims.

    HTML blocks are included on purpose (Vex 3353 §3): a badge ``<img>``,
    ``<details>`` or ``<kbd>`` at the top of a PR body folds the trailer under
    it into one html_block, and Forgejo renders BOTH — dropping those lines
    would be silent inertness, the BIP-33 failure class.

    DERIVED ENTIRELY FROM ``_fragments``, including the verdict TEXT. An
    earlier revision asked the view only which line NUMBERS carried visible
    text and then took the text itself from ``_mask``, so the two consumers
    still had two extractors and only the suppression fact was shared —
    replacing every fragment's text changed what the unanchored matcher saw
    and left this parser reading the source line (Morrow RC 3667). One
    product, or it is not one product.
    """
    # THE POLICY, and the whole of this consumer's contribution: BOTH block
    # contexts are eligible. This is now the ONLY admission policy — the
    # compatibility matcher that accepted `paragraph_open` alone is deleted, so
    # what was once a deliberate difference between two consumers is simply
    # what the grammar does. The reason it was safe to be the wider of the two
    # still holds: an anchored trailer cannot form inside link metadata, so
    # nothing here ever needed to exclude block HTML.
    # The predicate is vacuous today because the view emits no other context;
    # it is kept so that adding one forces a decision, and it is labelled
    # rather than left to read as protection.
    eligible: dict = {}
    benign: dict = {}
    for frag in _fragments(text):
        if frag.context not in _CLASSIFIABLE:
            continue
        eligible.setdefault(frag.line, []).append(frag.text)
        if frag.removed == BENIGN_MARKUP:
            benign.setdefault(frag.line, []).append(frag.removed_text)

    visible: dict[int, str] = {}
    for lineno, parts in eligible.items():
        if lineno > len(lines):
            continue
        residue = "".join(parts)
        if residue == lines[lineno - 1]:
            visible[lineno] = residue
            continue
        # BENIGN MARKUP IS NOT EXTRA CONTENT (Morrow's §M2 ruling on BIP-64).
        # `Closes GB-1 <br>` differs from its source line, but the difference
        # is an attribute-free empty element: it renders nothing and conceals
        # nothing, so the line's semantic visible text is still exactly the
        # directive.
        #
        # THE DIFFERENCE MUST BE ENTIRELY ACCOUNTED FOR, which is what keeps
        # the ruling as narrow as it was written. Deleting the recorded benign
        # spans from the source must RECONSTRUCT the residue. Asking only
        # "was anything concealing recorded?" is not the same question and is
        # wrong, but NOT for the reason this comment used to give. The split is
        # not "comments and separators versus everything else" — it is WHETHER
        # THE FRAGMENT VIEW EMITS THE BLOCK AT ALL. When it does, the inline
        # walk records the removal WITH its raw span, and an inline comment is
        # recorded exactly like a code span. When the block is masked away
        # entirely, NOTHING is recorded. Unicode separators and comments that
        # span or open blocks fall on that second side, so they leave a
        # difference nothing accounted for — and would have been silently
        # accepted. Comments fall on BOTH sides depending on shape, which is
        # why naming them as a class was wrong (Aria, 2026-08-14).
        rebuilt = lines[lineno - 1]
        for raw in benign.get(lineno, ()):
            rebuilt = rebuilt.replace(raw, "", 1)
        if benign.get(lineno) and rebuilt == residue:
            visible[lineno] = residue
            continue
        # Something on this line was masked — a comment, a code span. Whatever
        # remains visible can no longer be a clean trailer, so if it still
        # LOOKS like a directive attempt that is disqualifying extra content
        # and must be loud rather than silent (Morrow 3333 blocker 4).
        candidate = residue.strip()
        if candidate and (_DIRECTIVE_RE.match(candidate) or _OPENER_RE.match(candidate)):
            visible[lineno] = _NEAR_MISS

    return visible


#: Inline tokens whose text a human never reads: a code span renders as code,
#: raw HTML renders as markup, and an image's alt text is a fallback the reader
#: does not see. Each is replaced by a SPACE, never deleted, so removing a
#: hidden span can never push two words together into a new match.
_HIDDEN_INLINE = ("code_inline", "html_inline", "image")

#: Elements whose CONTENT is not text a reader sees. Forgejo's sanitizer
#: removes `script` and `style` outright, so their bodies render as nothing;
#: `HTMLParser` hands them over as ordinary data because it is a parser, not a
#: sanitizer, and cannot know which elements a renderer will drop.
#: `textarea` is deliberately NOT here — its content IS displayed.
_INVISIBLE_ELEMENT_CONTENT = frozenset({"script", "style"})

#: Directive-bearing bodies and commit messages use the CommonMark block
#: renderer. Pull-request titles are inert under Scope A and never enter this
#: module; retaining a title renderer would preserve a second recognition site
#: that the normative source list explicitly excludes.


class _ElementState:
    """Whether a reader can see text right now, decided by ELEMENT.

    THE ONE OWNER of that rule. It has to be consulted from two places, because
    markdown-it hands raw HTML over in two different shapes — a whole
    ``html_block``, or ``html_inline`` tags interleaved with ``text`` children
    inside a paragraph. The first version of this lived only inside the block
    parser, so the block path was correct while the inline path kept the text
    between `<script>` and `</script>` and matched on it (Morrow, round four).
    Two implementations of one rule is what produced that, so there is now one.

    Unbalanced input fails toward HIDDEN: an unclosed `<script>` suppresses to
    the end of the field, which is what a sanitizer does with it.
    """

    def __init__(self):
        # A STACK OF TAG NAMES, not a counter (Morrow's cold pass). A scalar
        # depth lets one hidden element be closed by the OTHER one's end tag:
        #
        #     "prefix <script>x</style> refs GB-1"   depth: 1 then 0 -> exposed
        #
        # and the deployed renderer emits only `<p>prefix ` for that, and for
        # the style/script inverse — a mismatched close does NOT end the
        # element, so the directive is invisible and the counter produced a
        # FALSE ACTION. Control, matched pair: `<script>x</script> refs GB-1`
        # renders `prefix  refs GB-1`, visible, and still matches.
        self._open = []

    def start(self, tag):
        if tag in _INVISIBLE_ELEMENT_CONTENT:
            self._open.append(tag)

    def end(self, tag):
        # Only a matching close ends it. An unmatched close is ignored rather
        # than popping the stack, which is the fail-toward-hidden direction.
        if tag in _INVISIBLE_ELEMENT_CONTENT and tag in self._open:
            for i in range(len(self._open) - 1, -1, -1):
                if self._open[i] == tag:
                    del self._open[i:]
                    break

    @property
    def hidden(self) -> bool:
        return bool(self._open)



class _ElementStateFeeder(HTMLParser):
    """Drives an `_ElementState` from raw HTML and reports no text.

    `close()` is never called by callers: an unterminated construct stays
    buffered and unreported, which is both what hides an unterminated comment
    and what `_block_leaves_comment_open` reads to detect one.
    """

    def __init__(self, state: "_ElementState"):
        super().__init__(convert_charrefs=True)
        self._state = state

    def handle_starttag(self, tag, attrs):
        self._state.start(tag)

    def handle_endtag(self, tag):
        self._state.end(tag)



def _block_leaves_comment_open(raw: str) -> bool:
    """Did this raw HTML block end inside an unterminated comment?

    Asked of the PARSER rather than a pattern, because the difference is
    exactly the one a pattern cannot see: `<!--` inside a quoted attribute is
    an attribute value, and `<!--` in text opens a comment. Measured on the
    interpreter this ships on — an unterminated construct is left unconsumed
    in `rawdata`, while a fully-parsed block leaves it empty:

        '<details title="<!--">…'   rawdata ''                  -> False
        '<div>\n`<!--`\n</div>'     rawdata '<!--`\n</div>'      -> True

    Relies on `HTMLParser.rawdata`; if a future runtime changes that, the
    renderer-conformance corpus reds rather than this failing silently.
    """
    parser = _ElementStateFeeder(_ElementState())
    try:
        parser.feed(raw)  # never close(): closing flushes the buffer
    except Exception:
        return True  # unparseable raw HTML: fail toward suppressing
    return "<!--" in getattr(parser, "rawdata", "")


class _InlineTagKind(HTMLParser):
    """Classify ONE raw inline tag as (kind, name), using the HTML parser
    rather than a pattern — same reason attributes are its job and not a
    regex's."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.kind = None
        self.name = None

    def _first(self, kind, name):
        if self.kind is None:
            self.kind, self.name = kind, name

    def handle_starttag(self, tag, attrs):
        self._first("start", tag)

    def handle_startendtag(self, tag, attrs):
        self._first("startend", tag)

    def handle_endtag(self, tag):
        self._first("end", tag)


def _inline_tag_kind(raw: str):
    scanner = _InlineTagKind()
    try:
        scanner.feed(raw)
    except Exception:
        return None, None
    return scanner.kind, scanner.name



#: A removal that renders nothing and can conceal nothing: an attribute-free
#: empty element. `<br>`, `<hr>`, `<br/>`, `</b>`.
BENIGN_MARKUP = "benign-markup"
#: A removal that COULD hide source content from a reader — a comment, an
#: element carrying attributes, a code span, an image's alt text. Morrow's
#: ruling is explicitly narrow: it does not authorise arbitrary hidden
#: attributes or markup, only empty attribute-free elements.
CONCEALING = "concealing"

#: An attribute-free tag: `<br>`, `<br/>`, `</b>`, `<b>`. Anything carrying an
#: attribute is CONCEALING, because an attribute is source content a reader
#: never sees — which is the whole of the `<details title="<!--">` family.
_BARE_TAG_RE = re.compile(r"^</?[a-zA-Z][a-zA-Z0-9]*\s*/?>$")


def _removal_kind(raw: str) -> str:
    """Classify one removed inline span. Defaults to CONCEALING: a form this
    does not recognise must not be treated as harmless."""
    if _BARE_TAG_RE.match(raw.strip()):
        return BENIGN_MARKUP
    return CONCEALING


@dataclass(frozen=True)
class Fragment:
    """One run of text a reader sees, with where it came from.

    `context` is the PARSER'S node kind for the top-level block that produced
    the fragment — `paragraph_open`, `html_block`, and so on — not a vocabulary
    invented here (Morrow's ruling). A second hand-built classifier would be a
    second owner of exactly the question this view exists to answer once.
    """

    text: str
    line: int  # 1-based source line
    context: str  # markdown-it block token type
    #: What this fragment RECORDS RATHER THAN CARRIES, when it carries no text
    #: (BIP-64). `None` for ordinary text. `BENIGN_MARKUP` for an attribute-free
    #: empty element such as `<br>`, which renders nothing and can conceal
    #: nothing. `CONCEALING` for anything that could hide source content from a
    #: reader — a comment, an element with attributes, a code span, an image.
    #:
    #: The distinction is the whole of Morrow's §M2 ruling: `Closes GB-1 <br>`
    #: has a semantic visible text of exactly the directive, so it is not
    #: "extra content"; `Closes GB-1 <!-- x -->` and `Closes GB-1 <b>x</b>` are.
    removed: str = None
    #: The raw source of a removed span, so a consumer can check that the
    #: difference between a source line and its residue is ENTIRELY accounted
    #: for by removals it accepts. Without it, "no concealing removal was
    #: recorded" reads as "the difference was benign" — and a removal is only
    #: recorded when the fragment view EMITS the block it lives in. What the
    #: masking stage takes out entirely is recorded nowhere, so it leaves a
    #: difference nothing accounted for. That is the case reconstruction
    #: exists to catch, and it is why the raw span has to be kept rather than
    #: just a flag.
    removed_text: str = ""


def _fragments(text: str) -> list:
    """THE shared product: every fragment of reader-visible text, per source
    line, tagged with the parser node kind that produced it.

    WHAT THIS OWNS, so that no consumer re-derives it: source spans, the
    element tag stack, suppression of elements the renderer empties, and
    provenance. What it does NOT own is eligibility — whether a given context
    may carry a directive is the consumer's policy predicate, kept separate so
    this stays a description of the text rather than a decision about it.
    (Historically there were two consumers with two policies, canonical
    classifying block HTML where the unanchored compatibility matcher excluded
    it. That matcher is deleted; one policy remains, and the separation is kept
    for the structural reason, not to reconcile a disagreement.)

    PER LINE, NOT PER LINE RANGE. A line can carry hidden and visible text at
    once — `prefix <script>x</script> tail` — so suppressing whole lines would
    lose the visible half while keeping whole lines would leak the hidden half.
    Fragments carry their own source line, so both halves are placed exactly.

    Hidden content reaches NEITHER consumer: script/style bodies are never
    emitted as fragments at all, which is why the element rule cannot diverge
    between the two paths again.
    """
    state = _ElementState()
    out: list = []

    tokens = _MD.parse(text or "")
    # PROSE lines come from `_mask`, which is this view's masking stage rather
    # than a second extractor: it is the only thing that gets comment scanning
    # right ACROSS blocks this view never emits — an unterminated `<!--` inside
    # a blockquote or a list item suppresses what follows it, and a walk that
    # visits only emitted contexts cannot see that. Six review rounds of
    # comment and code-span behaviour live in it and are not reimplemented
    # here. Both consumers still read only fragments; `_mask` has no consumer
    # of its own.
    # …AND IT IS RUN OVER A SOURCE WITH RAW HTML BLANKED OUT, so that an
    # opener INSIDE an html_block cannot suppress prose after it. That is
    # Sable's form B: `<details title="<!--">` blanks a trailer in a FOLLOWING
    # paragraph, because `_mask`'s comment scan reads the quoted attribute as
    # an opener. Exempting html_block fragments from the blanked set — the
    # first fix — closed only the trailer INSIDE the block; the prose after it
    # was still gated on the scan that got the attribute wrong.
    #
    # The distinguishing fact is not "inside an html_block" — it is WHETHER THE
    # HTML PARSER SEES A COMMENT THERE, and the two differ:
    #
    #   <details title="<!--">        an ATTRIBUTE value. Renders normally, and
    #                                 prose after it is visible.
    #   <div>\n`<!--`\n</div>         TEXT content. The renderer emits `<div>\n`
    #                                 and swallows everything after — measured,
    #                                 it is in the conformance corpus.
    #
    # Blanking every html_block line got the first right and the second wrong.
    # So each block is asked, and the parser answers by what it leaves
    # UNCONSUMED: an unterminated comment stays buffered in `rawdata`, an
    # attribute does not. Blocks that end mid-comment keep an opener so `_mask`
    # suppresses onward; the rest are blanked. Openers in a blockquote or list
    # item — contexts this view never emits — are untouched, and `_mask`
    # remains their only authority.
    source_lines = _LINE_SPLIT_RE.split(text or "")
    prose_lines = list(source_lines)
    for tok in tokens:
        if tok.level != 0 or tok.type != "html_block" or not tok.map:
            continue
        start, end = tok.map
        for i in range(start, min(end, len(prose_lines))):
            prose_lines[i] = ""
        if _block_leaves_comment_open(tok.content):
            last = min(end, len(prose_lines)) - 1
            if last >= start:
                prose_lines[last] = "<!--"
    prose_source = "\n".join(prose_lines)
    masked = _LINE_SPLIT_RE.split(_mask(prose_source, _MD.parse(prose_source)))

    for i, tok in enumerate(tokens):
        if tok.level != 0 or not tok.map:
            continue
        if tok.type == "html_block":
            # RAW HTML is read by an HTML PARSER instead, because `_mask`'s
            # comment scan is a text scan and cannot see attribute quoting:
            # `<details title="<!--">` reads as an unterminated comment and
            # eats the trailer under it (Rowan RC 3669).
            out.extend(_html_block_fragments(tok, state))
            continue
        if tok.type != "paragraph_open":
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and nxt.type == "inline":
            out.extend(_inline_fragments(nxt, state, line=tok.map[0] + 1,
                                         context="paragraph_open"))

    # `_mask` contributes ONE fact, not text: which lines it blanks entirely.
    # That is the only way to see a comment opened in a block this view never
    # emits — an unterminated `<!--` inside a blockquote or list item
    # suppresses what follows, and a walk visiting only emitted contexts is
    # blind to it. The TEXT still comes from the token walk, which is the only
    # thing with sub-line precision: link destinations, image alt and a
    # mid-line `<script>` all have to be excluded WITHIN a line, and a
    # line-oriented mask cannot express that.
    #
    # APPLIED TO PROSE ONLY. Inside raw HTML the HTML parser is the authority
    # and `_mask` is actively wrong: its comment scan is a text scan, so
    # `<details title="<!--">` reads as an unterminated comment and blanks the
    # trailer under it, which is Rowan's RC 3669 witness. Gating html_block
    # fragments on it would reintroduce exactly that defect one layer up.
    blanked = {
        i + 1
        for i in range(min(len(masked), len(source_lines)))
        if source_lines[i].strip() and not masked[i].strip()
    }
    return [f for f in out if f.context == "html_block" or f.line not in blanked]


def _inline_fragments(inline_token, state: "_ElementState", line: int, context: str) -> list:
    """Fragments from one inline run, advancing the source line on each break.

    Raw HTML arrives here as `html_inline` TAGS interleaved with the text
    between them, so the element state has to be applied over the sequence:
    dropping the tags alone keeps a `<script>` body's text.
    """
    out: list = []
    for child in inline_token.children or []:
        if child.type == "html_inline":
            kind, name = _inline_tag_kind(child.content)
            # A self-closing form does NOT open the element — measured, not
            # reasoned. `<script/>x refs GB-1` renders `x refs GB-1` on the
            # deployed instance: the sanitizer drops the empty element and
            # keeps what follows.
            if kind == "start":
                state.start(name)
            elif kind == "end":
                state.end(name)
            if not state.hidden:
                # Recorded, not dropped: the canonical policy needs to know
                # WHAT was removed to decide whether the line is still a clean
                # trailer (BIP-64).
                out.append(Fragment("", line, context, _removal_kind(child.content),
                                    child.content))
            # A RAW HTML TOKEN CAN SPAN LINES WITHOUT A SOFTBREAK: markdown-it
            # keeps `<!-- one\ntwo -->` as one token with the newline inside
            # it. Counting only softbreaks under-counts the source line, and a
            # trailer after the comment then lands on the wrong one.
            line += child.content.count("\n")
            continue
        if child.type in ("softbreak", "hardbreak"):
            line += 1
            continue
        if state.hidden:
            line += child.content.count("\n") if child.content else 0
            continue
        if child.type == "text":
            for offset, piece in enumerate(_LINE_SPLIT_RE.split(child.content)):
                if piece:
                    out.append(Fragment(piece, line + offset, context))
            line += child.content.count("\n")
        elif child.type in _HIDDEN_INLINE:
            # A code span or an image's alt text: emit a SEPARATOR rather than
            # nothing, so removing it cannot push two words together into a
            # match that neither of them made. Always CONCEALING — both carry
            # source content a reader does not see as written.
            out.append(Fragment(" ", line, context, CONCEALING, child.content or ""))
    return out


class _HTMLBlockFragments(HTMLParser):
    """Fragments from one raw HTML block, placed by the parser's own position.

    `getpos()` is what makes per-line placement exact inside a block: text on
    the same line as a `<script>` open tag is attributed to that line and
    suppressed, while text after the matching close on that same line is
    attributed to the same line and kept.

    `close()` is deliberately never called: an unclosed construct stays
    buffered and unreported, which is what makes an unterminated comment hide
    rather than leak. Measured on the interpreter this ships on — it differs on
    3.13, so the probe has to run in the target container.
    """

    def __init__(self, state: "_ElementState", first_line: int):
        super().__init__(convert_charrefs=True)
        self._state = state
        self._first_line = first_line
        self.fragments: list = []

    def handle_starttag(self, tag, attrs):
        self._state.start(tag)

    def handle_endtag(self, tag):
        self._state.end(tag)

    def handle_data(self, data):
        if self._state.hidden or not data:
            return
        # `getpos()` reports where the run STARTS, and a run crosses lines —
        # `<img src="x">\nCloses GB-1` arrives as one datum beginning with a
        # newline. Attributing all of it to the start line puts a trailer on
        # the wrong line, and the consumer that reports LINE NUMBERS would
        # then report the wrong one. Split and place each piece.
        start_line, _ = self.getpos()
        for offset, piece in enumerate(_LINE_SPLIT_RE.split(data)):
            if piece:
                self.fragments.append(
                    Fragment(piece, self._first_line + start_line - 1 + offset, "html_block")
                )


def _html_block_fragments(tok, state: "_ElementState") -> list:
    """Fragments of an HTML block. Fails toward FEWER fragments: an
    unparseable block contributes nothing, which can narrow but can never
    invent a directive."""
    parser = _HTMLBlockFragments(state, tok.map[0] + 1)
    try:
        parser.feed(tok.content)  # never close(); see _HTMLBlockFragments
    except Exception:
        return []
    return parser.fragments


def forward_selection(text: str, source: str):
    """THE one selection entry for push and merged-PR paths: returns
    ``(nominations, near_miss_lines, conflict_ticket_keys)`` from a SINGLE
    canonical parse. ``source`` is MANDATORY so no caller can silently choose
    pr_body for a commit message.

    Two rulings folded here (Morrow, candidate blockers):

    1. The parse produces more than tickets and the caller must be handed all
       of it — dropping near misses meant `Closes BIP-7 after QA` was answered
       "no ticket was named", confidently false, and a push near miss vanished
       from the durable record (Scope A 108-109). ``no-ticket`` means no
       nomination AND no near miss.

    2. ONE ADMISSION POLICY, the anchored trailer (Scope A 86-104). The forward
       paths used the unanchored compatibility matcher while review used the
       canonical grammar, so `please Closes BIP-7` selected nothing on a
       changes-requested review and then selected BIP-7 on the merge — two
       policies in one module, and a wrong-ticket ask waiting to happen. The
       compatibility matcher is DELETED with its last caller; mid-line
       references are prose now, on every path.

    Nominations are ``[(identifier, sequence, keyword_class), …]``; the parse
    already owns dedup and weaker-class-wins on conflict — and the DEMOTION IS
    A DATUM, not just an outcome (Scope A 110-112; Morrow): ``conflicts`` is
    the third member, the ticket keys whose Closes+Refs pair was demoted to
    advance, so the delivery result can record the conflict rather than
    silently under-moving.

    Returns ``(nominations, near_miss_lines, conflict_ticket_keys)``.
    """
    parsed = parse_directives(text or "", source=source)
    nominations = [
        (*_split_key(d.ticket_key), d.keyword_class) for d in parsed.directives
    ]
    return nominations, [n.text for n in parsed.near_misses], list(parsed.conflicts)


def _split_key(key: str):
    identifier, sequence = key.rsplit("-", 1)
    return identifier, int(sequence)


def parse_directives(text: str, source: str) -> ParseResult:
    """Parse directive trailer lines out of ``text``.

    ``source`` must be ``pr_body`` or ``commit_message`` — the one-location
    rule is enforced here so no caller can quietly widen it.
    """
    if source not in VALID_SOURCES:
        raise ValueError(
            f"directives are recognized in {VALID_SOURCES} only; {source!r} is a contract error (titles are inert)"
        )

    result = ParseResult()
    seen: dict[str, Directive] = {}
    demoted: set[str] = set()

    lines = _LINE_SPLIT_RE.split(text)
    visible = _classifiable_lines(text, lines)

    for lineno in sorted(visible):
        raw = visible[lineno]

        if raw == _NEAR_MISS:
            result.near_misses.append(NearMiss(lineno, lines[lineno - 1]))
            continue

        # Space/tab trim only: rstrip()'s unicode set would delete the very
        # separators the directive-end anchor must refuse (Morrow 3334).
        # Anchoring is at start of LINE, not start of stripped content — an
        # indented keyword is not a trailer.
        line = raw.rstrip(" \t")

        m = _DIRECTIVE_RE.match(line)
        if m:
            key = f"{m.group('project')}-{m.group('num')}"
            klass = _keyword_class(m.group("kw"))
            if klass is None:
                # The pattern matched a word the map no longer owns: not a
                # directive. (One-owner rule: map-only removal must be enough.)
                continue
            result.recognized_lines.append(lineno)
            if key in seen:
                prior = seen[key]
                if prior.keyword_class != klass and key not in demoted:
                    # Conflict: the weaker (advance) wins, loudly.
                    demoted.add(key)
                    result.conflicts.append(key)
                    seen[key] = Directive(ADVANCE, key, prior.line, source)
                # Duplicates are idempotent.
                continue
            seen[key] = Directive(klass, key, lineno, source)
            continue

        if _OPENER_RE.match(line):
            result.near_misses.append(NearMiss(lineno, line))

    result.directives = list(seen.values())
    return result
