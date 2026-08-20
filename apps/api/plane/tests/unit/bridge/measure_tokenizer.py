# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""MEASUREMENT ONLY — how far does each candidate parser sit from the renderer?

Not a test and not shipped behaviour. This scores candidate configurations
against ``renderer_corpus.json`` (the Goldmark-derived oracle) so the
dependency question is settled by data instead of argument. Run it in a
scratch container; markdown-it-py is NOT a declared dependency at this point.

Configurations, and why each is here:

* ``current``     — grammar.py at head; the baseline to beat.
* ``mdit-para``   — markdown-it html=True, classify top-level ``paragraph``
                    only. The naive reading of "use a tokenizer".
* ``mdit-para+html`` — Vex 3353 §3: html=True and classify top-level
                    ``paragraph`` AND ``html_block``, minus comment spans.
                    His argument is that the property we need is VISIBILITY
                    AFTER FORGEJO'S SANITIZER, not html-block-ness.
* ``mdit-nohtml`` — html=False, paragraph only. Vex calls this the
                    instinctively-safe-looking setting and a security defect;
                    included to see the defect rather than take it on report.
* ``mdit+deflist``— ``mdit-para+html`` plus the deflist plugin, because the
                    renderer corpus shows Goldmark's definition lists swallow
                    directive lines and CommonMark alone cannot see them.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).parent
sys.path.insert(0, "/code")

from plane.bridge.grammar import parse_directives  # noqa: E402

_KEYWORDS = ("closes", "fixes", "resolves", "refs")
_DIRECTIVE_RE = re.compile(
    r"^(?i:(?:" + "|".join(_KEYWORDS) + r"))"
    r":?[ \t]+"
    r"(?:[A-Z][A-Z0-9]{1,11})-(?:[0-9]{1,6})"
    r"[.!]?[ \t]*$"
)
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")
_COMMENT_SPAN_RE = re.compile(r"<!--.*?-->")


def _mdit_lines(text, *, html, include_html_block, deflist=False):
    """Source line numbers markdown-it places in classifiable top-level blocks."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": html})
    md.enable("table")  # Goldmark ships GFM tables; CommonMark preset does not
    md.enable("strikethrough")
    if deflist:
        from mdit_py_plugins.deflist import deflist_plugin

        md.use(deflist_plugin)

    wanted = {"paragraph_open"}
    if include_html_block:
        wanted.add("html_block")

    lines = _LINE_SPLIT_RE.split(text)
    out = []
    for tok in md.parse(text):
        # level 0 == top level: nothing inside a quote, list, table or deflist.
        if tok.level != 0 or tok.type not in wanted or not tok.map:
            continue
        start, end = tok.map
        for idx in range(start, min(end, len(lines))):
            raw = lines[idx]
            # A directive whose line also carries a comment is not a clean
            # trailer — grammar.py's rule, kept deliberately (Vex 3353).
            if _COMMENT_SPAN_RE.sub("", raw) != raw or "<!--" in raw:
                continue
            if _DIRECTIVE_RE.match(raw.rstrip(" \t")):
                out.append(idx + 1)
    return sorted(set(out))


CONFIGS = {
    "current": lambda t: sorted(parse_directives(t, "pr_body").recognized_lines),
    "mdit-para": lambda t: _mdit_lines(t, html=True, include_html_block=False),
    "mdit-para+html": lambda t: _mdit_lines(t, html=True, include_html_block=True),
    "mdit-nohtml": lambda t: _mdit_lines(t, html=False, include_html_block=False),
    "mdit+deflist": lambda t: _mdit_lines(
        t, html=True, include_html_block=True, deflist=True
    ),
}


def main():
    cases = json.loads((_HERE / "renderer_corpus.json").read_text())["cases"]
    failures = {name: [] for name in CONFIGS}

    for case in cases:
        expected = sorted(l for l, _, _ in case["expected"])
        for name, fn in CONFIGS.items():
            try:
                got = fn(case["markdown"])
            except Exception as exc:  # a crash is a failure, not a skip
                got = f"ERROR {exc}"
            if got != expected:
                failures[name].append((case["name"], expected, got))

    total = len(cases)
    print(f"\n{total} renderer-derived cases\n" + "=" * 60)
    for name in CONFIGS:
        bad = failures[name]
        print(f"{name:18s} {total - len(bad):3d}/{total} agree   ({len(bad)} diverge)")
    print()
    for name in CONFIGS:
        if not failures[name]:
            continue
        print(f"--- {name} divergences ---")
        for case_name, exp, got in failures[name]:
            kind = "FALSE POSITIVE" if got and not exp else (
                "SILENT MISS" if exp and not got else "MISMATCH"
            )
            print(f"  {case_name:36s} want={exp} got={got}  [{kind}]")
        print()


if __name__ == "__main__":
    main()
