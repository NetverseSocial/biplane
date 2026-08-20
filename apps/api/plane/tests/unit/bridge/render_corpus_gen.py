# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Renderer-derived conformance corpus generator (BIP-45).

WHY THIS EXISTS
---------------
The completion grammar decides which lines of a PR body or commit message are
directives. Six rounds of hand-written CommonMark block tracking each diverged
from what the forge actually renders (Morrow RC 3333/3334/3340/3342/3346), and
Morrow's fit ruling on the tokenizer proposal is the reason this file, not a
dependency, comes first:

    Forgejo renders with **Goldmark plus extensions and configuration**, so a
    CommonMark-reference parser cannot be the authority. Proven live: a
    directive line followed by ``: body`` renders as a DEFINITION LIST, and
    ``| Closes BIP-7 |`` renders as a TABLE CELL. Neither is CommonMark.

So the authority is the deployed renderer itself. This generator asks Forgejo
to render each case, derives the ground-truth verdict from the returned HTML,
and pins it. **The generated corpus is the oracle; the parser is measured
against it.** Whether we later adopt a tokenizer is then a data question — how
far does any given parser sit from these pinned verdicts — rather than an
argument.

CONTRACT
--------
* Generation needs the network and a Forgejo token. **Tests never do.** The
  pinned JSON is committed and the test module reads it offline; a live forge
  is not a test dependency (and a corpus that silently re-derives itself would
  be an input choosing its own validation — invariant 7).
* Regenerating is a REVIEWED act: the diff of the pinned JSON is the record of
  what the renderer changed. A verdict that flips on a forge upgrade must be
  seen by a human, not absorbed silently.

GROUND TRUTH, EXACTLY
---------------------
The oracle answers ONE question, per source line: **is this line rendered as
top-level prose?** It must not answer "does the rendered text look like a
directive" — rendering erases inline markup, so ``*Closes BIP-7*`` yields the
text ``Closes BIP-7`` even though the source line is not a trailer. Comparing
rendered text against a source-line grammar silently conflates those two, and
would manufacture divergences that are really the emphasis markers doing their
job.

So the mapping is done by PROBE. For each source line that matches the
trailer grammar, the ticket number is replaced with a unique sentinel and the
whole document re-rendered. If the sentinel lands inside a TOP-LEVEL
PARAGRAPH — a ``<p>`` with no ancestor among blockquote, pre, code, li,
dl/dt/dd, table/td/th, or any heading, and not inside a ``<code>`` span —
then that source line is renderer-supported. Digits cannot change block
structure, so the probe preserves exactly what is being measured, and it
recovers the SOURCE LINE NUMBER that the HTML itself does not carry.

WHAT THE CORPUS DOES *NOT* DECIDE
---------------------------------
Everything the grammar layers on top of block context stays out of here and
stays in the hand-pinned suite: start-of-line anchoring (``  Closes BIP-7`` is
prose to the renderer and deliberately not a trailer to us), near-miss
loudness, the comment-coexistence rule, duplicate collapse, and complete/
advance conflict demotion. Those are POLICY. Mixing policy into an oracle
would let the oracle ratify our own choices back to us.

USAGE
-----
Needs Python 3 and network reach to the forge (the agent containers have one
or the other, not both; devboard.test has both)::

    FORGEJO_TOKEN=... FORGEJO_URL=http://forge.test:3000 \
        python3 render_corpus_gen.py --out renderer_corpus.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

# The grammar's own line-shape regex is deliberately reused: this corpus is an
# oracle for BLOCK CONTEXT (is this line prose?), not for line shape. Keeping
# one definition of "looks like a directive" is what makes a disagreement here
# mean "context was decided differently" and nothing else.
_KEYWORDS = ("closes", "fixes", "resolves", "refs")
_COMPLETE = ("closes", "fixes", "resolves")

_DIRECTIVE_RE = re.compile(
    r"^(?i:(?P<kw>" + "|".join(_KEYWORDS) + r"))"
    r":?[ \t]+"
    r"(?P<project>[A-Z][A-Z0-9]{1,11})-(?P<num>[0-9]{1,6})"
    r"[.!]?[ \t]*$"
)

# Ancestors that disqualify a paragraph from being top-level prose.
_BLOCKING_ANCESTORS = frozenset(
    {
        "blockquote",
        "pre",
        "code",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

_VOID = frozenset({"br", "hr", "img", "input", "meta", "link"})


class _ParagraphLineExtractor(HTMLParser):
    """Collect the visible text lines of every TOP-LEVEL paragraph.

    Rendered code spans are dropped; anchor text is kept. ``<br>`` ends a line
    inside a paragraph, which is how Goldmark renders a soft source newline in
    the hard-break configuration Forgejo ships.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.lines: list[str] = []
        self._buf: list[str] = []

    # -- helpers ---------------------------------------------------------
    def _in_top_level_paragraph(self) -> bool:
        if "p" not in self.stack:
            return False
        idx = self.stack.index("p")
        return not any(t in _BLOCKING_ANCESTORS for t in self.stack[:idx])

    def _in_code(self) -> bool:
        return "code" in self.stack

    def _flush(self) -> None:
        text = "".join(self._buf)
        self._buf = []
        if text.strip():
            self.lines.append(text)

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if tag == "br" and self._in_top_level_paragraph():
                self._flush()
            return
        if tag == "p" and self._in_top_level_paragraph():
            # Nested <p> is not valid here, but never silently merge blocks.
            self._flush()
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag == "p" and self._in_top_level_paragraph():
            self._flush()
        # Tolerate unbalanced markup rather than desync the whole document.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_top_level_paragraph() and not self._in_code():
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


# Only these line terminators create lines — matching the grammar. Python's
# str.splitlines() also splits on VT/FF/FS/GS/RS/NEL/LS/PS, synthetic anchors
# an attacker can smuggle inside what renders as ONE line (Morrow 3333).
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

# A sentinel that cannot occur in the source text and survives rendering as
# ordinary digits. Six digits keeps it inside the grammar's number bound, so
# probing never changes whether the line matches.
_SENTINEL_BASE = 900000


class _AllTextExtractor(HTMLParser):
    """Every character a reader can see, in any container.

    Attribute values are excluded on purpose: a sentinel that survives only
    inside an ``href`` is not text anyone reads.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data):
        self.chunks.append(data)


def _paragraph_text(html: str) -> str:
    """All visible text of TOP-LEVEL paragraphs, code spans excluded."""
    parser = _ParagraphLineExtractor()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.lines)


def _all_text(html: str) -> str:
    """All visible text ANYWHERE — the safety floor's input.

    Vex 3354: the one property that must never be violated is that a line no
    human can see never acts. Whether a visible line sits in a paragraph, a
    definition term or a table cell is POLICY; whether it is visible at all is
    SAFETY. Recording both lets every divergence be triaged mechanically
    instead of argued case by case.
    """
    parser = _AllTextExtractor()
    parser.feed(html)
    parser.close()
    return "".join(parser.chunks)


def candidate_lines(text: str) -> list[tuple[int, str, str]]:
    """Source lines that match the trailer grammar: (lineno, class, key)."""
    out = []
    for lineno, raw in enumerate(_LINE_SPLIT_RE.split(text), start=1):
        m = _DIRECTIVE_RE.match(raw.rstrip(" \t"))
        if not m:
            continue
        klass = "complete" if m.group("kw").lower() in _COMPLETE else "advance"
        out.append((lineno, klass, f"{m.group('project')}-{m.group('num')}"))
    return out


def expected_for_case(text: str, base_url: str, token: str) -> tuple[list, str]:
    """Probe each candidate line; return the renderer-supported ones.

    Returns ``(expected, html)`` where ``html`` is the UNPROBED rendering, kept
    in the corpus purely as human-auditable evidence.
    """
    html = render(text, base_url, token)
    # Keep the ORIGINAL separators: a CRLF or bare-CR case must be re-rendered
    # with its own line endings, or the probe measures a different document
    # than the one under test.
    parts = re.split(r"(\r\n|\r|\n)", text)
    expected = []

    for idx, (lineno, klass, key) in enumerate(candidate_lines(text)):
        sentinel = str(_SENTINEL_BASE + idx)
        probed = list(parts)
        pos = (lineno - 1) * 2
        # Swap only the ticket NUMBER on this one line. Digit-for-digit, so
        # neither the line's grammar match nor any block boundary can move.
        probed[pos] = re.sub(
            r"([A-Z][A-Z0-9]{1,11})-[0-9]{1,6}",
            r"\g<1>-" + sentinel,
            probed[pos],
            count=1,
        )
        probed_html = render("".join(probed), base_url, token)
        expected.append(
            {
                "line": lineno,
                "class": klass,
                "key": key,
                # POLICY: does the renderer put this line in top-level prose?
                "in_prose": sentinel in _paragraph_text(probed_html),
                # SAFETY: can a human see this line at all, in any container?
                "visible": sentinel in _all_text(probed_html),
            }
        )

    return expected, html


def render(text: str, base_url: str, token: str) -> str:
    payload = json.dumps({"Text": text, "Mode": "markdown"}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/markdown",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# The case space.
#
# Grouped by the block family that decides the verdict. Every family that has
# already produced a divergence gets controls in BOTH directions: a form that
# must be recognized and a form that must be suppressed. Silent inertness and
# silent firing are both failures (BIP-33).
# ---------------------------------------------------------------------------
CASES: list[tuple[str, str]] = [
    # -- baseline -----------------------------------------------------------
    ("plain", "Closes BIP-7\n"),
    ("plain-colon", "Closes: BIP-7\n"),
    ("plain-terminal-dot", "Closes BIP-7.\n"),
    ("plain-advance", "Refs BIP-7\n"),
    ("plain-mixed-case-kw", "CLOSES BIP-7\n"),
    ("after-prose", "Some body text.\n\nCloses BIP-7\n"),
    ("prose-negative", "Fixes the race condition.\n"),
    ("lowercase-id-negative", "Closes bip-7\n"),
    ("mid-line-negative", "This Closes BIP-7 inline.\n"),
    ("indented-keyword-negative", "  Closes BIP-7\n"),
    # -- setext headings (found 2026-08-12; the parser has NO setext state) --
    ("setext-h1", "Closes BIP-7\n===\n"),
    ("setext-h2", "Closes BIP-7\n---\n"),
    ("setext-h2-long", "Closes BIP-7\n-------\n"),
    ("setext-then-real", "Closes BIP-7\n===\n\nRefs BIP-8\n"),
    # -- Goldmark definition lists (Morrow's fit proof) ----------------------
    ("deflist", "Closes BIP-7\n: definition body\n"),
    ("deflist-empty-body", "Closes BIP-7\n:  \n"),
    ("deflist-then-real", "Closes BIP-7\n: body\n\nRefs BIP-8\n"),
    # -- GFM tables ---------------------------------------------------------
    ("table-cell", "| a |\n| - |\n| Closes BIP-7 |\n"),
    ("table-then-real", "| a |\n| - |\n| x |\n\nCloses BIP-7\n"),
    # -- task lists / list items -------------------------------------------
    ("tasklist-item", "- [ ] Closes BIP-7\n"),
    ("list-item", "- Closes BIP-7\n"),
    ("ordered-list-item", "1. Closes BIP-7\n"),
    ("empty-marker-then-real", "- \nCloses BIP-7\n"),
    # -- blockquotes, incl. lazy continuation -------------------------------
    ("quote-direct", "> Closes BIP-7\n"),
    ("quote-lazy-continuation", "> quoted paragraph\nCloses BIP-7\n"),
    ("quote-empty-list-marker-then-real", "> -\nCloses BIP-7\n"),
    ("quote-blank-then-real", "> quoted\n\nCloses BIP-7\n"),
    ("quote-thematic-break-then-real", "> quoted\n\n---\n\nCloses BIP-7\n"),
    ("quote-nested", "> > Closes BIP-7\n"),
    ("quote-tab-marker", ">\tCloses BIP-7\n"),
    # -- fenced code --------------------------------------------------------
    ("fence-backtick", "```\nCloses BIP-7\n```\n"),
    ("fence-tilde", "~~~\nCloses BIP-7\n~~~\n"),
    ("fence-info-string", "```python\nCloses BIP-7\n```\n"),
    ("fence-unclosed", "```\nCloses BIP-7\n"),
    ("fence-short-closer", "````\nCloses BIP-7\n```\n"),
    ("fence-mismatched-char", "```\nCloses BIP-7\n~~~\n"),
    ("fence-info-backtick", "``` bad`info\nCloses BIP-7\n"),
    ("fence-indented-opener", "   ```\nCloses BIP-7\n   ```\n"),
    ("fence-then-real", "```\nx\n```\n\nCloses BIP-7\n"),
    # -- indented code ------------------------------------------------------
    ("indented-code", "    Closes BIP-7\n"),
    ("indented-code-after-blank", "para\n\n    Closes BIP-7\n"),
    # -- HTML comments ------------------------------------------------------
    ("comment-single-line", "<!-- Closes BIP-7 -->\n"),
    ("comment-multi-line", "<!--\nCloses BIP-7\n-->\n"),
    ("comment-closed-then-real", "<!-- x -->\n\nCloses BIP-7\n"),
    ("comment-opener-in-code-span", "`<!--`\nCloses BIP-7\n"),
    ("comment-opener-escaped", "\\<!--\nCloses BIP-7\n"),
    ("comment-trailing-on-line", "Closes BIP-7 <!-- note -->\n"),
    # -- inline spans -------------------------------------------------------
    ("code-span-whole-line", "`Closes BIP-7`\n"),
    ("emphasis-wrapped", "*Closes BIP-7*\n"),
    ("autolink-in-quote-para", "> <https://example.invalid>\nCloses BIP-7\n"),
    # -- headings / breaks --------------------------------------------------
    ("atx-heading", "# Closes BIP-7\n"),
    ("thematic-break-then-real", "***\n\nCloses BIP-7\n"),
    # -- id shape -----------------------------------------------------------
    ("oversize-project", "Closes ABCDEFGHIJKLM-1\n"),
    ("oversize-number", "Closes BIP-1234567\n"),
    ("two-ids-one-line", "Closes BIP-7 BIP-8\n"),
    ("duplicate-same-key", "Closes BIP-7\nCloses BIP-7\n"),
    ("conflict-classes", "Closes BIP-7\nRefs BIP-7\n"),
    ("two-distinct", "Closes BIP-7\nRefs BIP-8\n"),
    # -- line endings -------------------------------------------------------
    ("crlf", "para\r\n\r\nCloses BIP-7\r\n"),
    ("cr-only", "para\r\rCloses BIP-7\r"),
    ("no-trailing-newline", "Closes BIP-7"),
    # -- invisibility: the SAFETY class, where a fire is unauthorized --------
    # An unterminated comment renders to an EMPTY document, and an enclosing
    # element does not change that. Fires here are ticket completions no human
    # reading the PR could ever have seen (Vex 3354).
    ("comment-unterminated", "<!--\nCloses BIP-7\n"),
    ("comment-unterminated-in-div", "<div>\n<!--\nCloses BIP-7\n"),
    ("comment-closed-in-div", "<div>\n<!--\nCloses BIP-7\n-->\n</div>\n"),
    ("comment-then-text-same-line", "<!-- note --> Closes BIP-7\n"),
    ("comment-multiline-then-text", "<!-- a\nb --> Closes BIP-7\n"),
    # A comment opener shown INSIDE a code context is displayed literally and
    # opens nothing. These exist because the fix for the invisible-comment
    # class above can reach through a fence and eat a real trailer: the mask
    # pairs the fenced `<!--` with a `-->` further down the body. A PR body
    # documenting this very grammar is the likeliest body in the repo to hit
    # it, and mutation testing caught that the suite could not see it.
    ("comment-opener-in-fence", "```\n<!--\n```\n\nCloses BIP-7\n"),
    ("comment-opener-in-fence-no-blank", "```\n<!--\n```\nCloses BIP-7\n"),
    ("comment-opener-in-fence-later-comment", "```\n<!--\n```\n\nCloses BIP-7\n\n<!-- x -->\n"),
    ("comment-opener-in-fence-bare-closer", "```\n<!--\n```\n\nCloses BIP-7\n\n-->\n"),
    ("comment-opener-in-indented-code", "    <!--\n\nCloses BIP-7\n"),
    ("comment-opener-in-code-span-then-blank", "`<!--`\n\nCloses BIP-7\n"),
    # SHADOWED CODE SPAN (Vex 3358). A copy of the same code-span literal
    # earlier in the document used to absorb a position-blind str.replace, so
    # the REAL span survived into the comment scan and paired with the `-->`
    # below — blanking a visible trailer, silently. One case per shadow
    # source, plus the two controls that prove it is the pairing and not the
    # mask itself.
    ("code-span-shadowed-by-fence", "```\n`<!--`\n```\n\n`<!--`\n\nCloses BIP-7\n\n-->\n"),
    ("code-span-shadowed-by-indented-code", "    `<!--`\n\n`<!--`\n\nCloses BIP-7\n\n-->\n"),
    ("code-span-shadowed-by-html-block", "<div>\n`<!--`\n</div>\n\n`<!--`\n\nCloses BIP-7\n\n-->\n"),
    ("code-span-unshadowed-control", "`<!--`\n\nCloses BIP-7\n\n-->\n"),
    ("code-span-shadowed-no-closer-control", "```\n`<!--`\n```\n\n`<!--`\n\nCloses BIP-7\n"),
    # COMMENTS ARE NOT SCOPED BY BLOCK CONTAINER — pinned because the
    # opposite is the natural next refactor and it would be a REGRESSION.
    # Vex attacked _mask_unterminated_comments' end-of-text reach expecting
    # the container-crossing class we had just closed for code, and the
    # renderer refuted all four: Goldmark runs an unterminated comment to END
    # OF DOCUMENT even from inside a quote or a list, and returns a truncated
    # tree to prove it. Masking to end of text READS like over-reach and is
    # exactly what the forge does.
    # A VISIBLE `<!--` IN PROSE MUST NOT OPEN A SPAN (Vex 3360, found by
    # renderer-differential fuzzing). The indent is the whole point: an
    # unindented `<!--` opens an HTML block (CommonMark type 2 interrupts a
    # paragraph) and the renderer shows nothing, so suppression is right —
    # that is the `comment-unterminated-*` family below. Indented, it cannot
    # open a block, so it stays inline text the renderer escapes to
    # `&lt;!--` and DISPLAYS, while a whole-document regex still read it as
    # an opener and paired it with a `-->` further down.
    ("comment-opener-tab-indented-prose", "intro\n\t<!--\nCloses BIP-7\n\n<!-- y -->\n"),
    ("comment-opener-space-indented-prose", "intro\n    <!--\nCloses BIP-7\n\n<!-- y -->\n"),
    ("comment-opener-indented-bare-closer", "intro\n\t<!--\nCloses BIP-7\n\n-->\n"),
    ("comment-opener-indented-no-closer-control", "intro\n\t<!--\nCloses BIP-7\n"),
    ("comment-opener-unindented-control", "intro\n<!-- x\n\nCloses BIP-7\n\n<!-- y -->\n"),
    # Scoping the code-span mask to a line RANGE still leaves a
    # first-occurrence replace inside that range (Vex 3360 §1).
    ("two-identical-code-spans-one-line", "`<!--` and `<!--`\n\nCloses BIP-7\n\n-->\n"),
    ("code-span-shadowed-same-paragraph", "`<!--`\n`<!--`\n\nCloses BIP-7\n\n-->\n"),
    ("comment-unterminated-in-multiline-code-span", "`a\n<!--`\n\nCloses BIP-7\n"),
    ("comment-unterminated-in-quote", "> <!--\n\nCloses BIP-7\n"),
    ("comment-in-quote-pairs-later", "> <!--\n\nCloses BIP-7\n\n-->\n"),
    ("comment-in-list-item-pairs-later", "- <!--\n\nCloses BIP-7\n\n-->\n"),
    # -- html blocks a human DOES see: paired positive controls (Vex cond. 9)
    # An over-tight comment filter eats real directives, and a corpus of
    # negatives structurally cannot detect that.
    ("html-img-badge-then-trailer", '<img src="badge.png">\nCloses BIP-7\n'),
    ("html-details-then-trailer", "<details>\n<summary>s</summary>\nCloses BIP-7\n"),
    # -- visible but not prose: the POLICY class, over-fires and suppressions
    ("deflist-visible-dt", "Closes BIP-7\n: why\n"),
    ("list-lazy-continuation", "- item\nCloses BIP-7\n"),
    ("quote-then-list-lazy", "> quoted paragraph\n- item\nCloses BIP-7\n"),
    # -- setext controls (Morrow's ruling: heading context, silent both ways)
    ("setext-blank-line-control", "Closes BIP-7\n\n---\n"),
]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="renderer_corpus.json")
    ap.add_argument("--url", default=os.environ.get("FORGEJO_URL", "http://forge.test:3000"))
    args = ap.parse_args(argv)

    token = os.environ.get("FORGEJO_TOKEN") or os.environ.get("FORGEJO_GIT_TOKEN")
    if not token:
        print("FORGEJO_TOKEN is required to reach the renderer", file=sys.stderr)
        return 2

    records = []
    for name, text in CASES:
        try:
            expected, html = expected_for_case(text, args.url, token)
        except urllib.error.URLError as exc:  # network/forge failure is fatal
            print(f"{name}: render failed: {exc}", file=sys.stderr)
            return 1
        records.append(
            {
                "name": name,
                "markdown": text,
                "html": html,
                "candidates": [list(c) for c in candidate_lines(text)],
                "expected": expected,
            }
        )
        print(
            f"{name:38s} cand={len(records[-1]['candidates'])} -> {expected}",
            file=sys.stderr,
        )

    payload = {
        "_note": (
            "GENERATED by render_corpus_gen.py against a live Forgejo. Do not "
            "hand-edit. 'expected' is the renderer's verdict, derived from "
            "'html' — the parser is measured against it, never the reverse."
        ),
        "_source_url": "redacted \u2014 generated against a private Forgejo instance",
        "cases": records,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwrote {len(records)} cases -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
