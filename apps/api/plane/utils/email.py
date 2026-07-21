"""
Copyright (c) 2026 The Biplane Authors
SPDX-License-Identifier: AGPL-3.0-only
See the LICENSE file for details.

Biplane reimplementation. The upstream file at this path carried a commercial
license header inconsistent with the repository's AGPL license; rather than
interpret that conflict, this utility was independently reimplemented from the
function's public interface and behavior contract by a maintainer who had seen
the original (an interface-based rewrite, not a two-team clean-room process).
Provenance record: docs/decisions/007-email-util-reimplementation.md.
"""

# Python imports
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    """Collects visible text from HTML, skipping non-content blocks."""

    _SKIP_TAGS = {"style", "script", "head", "title", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)


def generate_plain_text_from_html(html_content):
    """
    Render an HTML email body as readable plain text.

    Drops style/script/head content entirely, keeps the visible text, and
    collapses runs of blank lines to a single blank line.

    Args:
        html_content (str): The HTML content to convert to plain text

    Returns:
        str: Plain text without markup, styles, or excessive whitespace
    """
    parser = _TextExtractor()
    parser.feed(html_content or "")
    parser.close()

    lines = [line.strip() for line in "".join(parser._chunks).splitlines()]
    collapsed = []
    previous_blank = True  # also swallows leading blank lines
    for line in lines:
        if line:
            collapsed.append(line)
            previous_blank = False
        elif not previous_blank:
            collapsed.append("")
            previous_blank = True

    body = "\n".join(collapsed).strip()
    return "\n\n" + body + "\n\n"
