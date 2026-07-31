"""
Copyright (c) 2023-present Plane Software, Inc. and contributors
SPDX-License-Identifier: AGPL-3.0-only
See the LICENSE file for details.

Tests for the plain-text email renderer (Morrow PR #8 blocker 2, seeded from
Sable's OLD-vs-NEW differential in the same review round). This function has
twelve email/background-task callers and had no test; the differential proved
the previous implementation shipped an entity-mangled password-reset URL, so
that case is pinned first and hardest.
"""

import pytest

from plane.utils.email import generate_plain_text_from_html


class TestResetUrlEntityHandling:
    """The bug the reimplementation fixed: entity refs must be DECODED in
    text/plain output. Django's strip_tags preserves them (right for HTML,
    wrong for plain text), which mailed users a parameter named `amp;token`."""

    def test_ampersand_entities_are_decoded_in_urls(self):
        html = (
            '<p>If the button doesn\'t work, copy and paste this link:</p>'
            "<p>https://example.com/reset-password/?uidb64=UID123&amp;token=TOK456&amp;email=a%40b.co</p>"
        )
        out = generate_plain_text_from_html(html)
        assert "uidb64=UID123&token=TOK456&email=a%40b.co" in out
        assert "&amp;" not in out

    def test_common_entities_are_decoded(self):
        out = generate_plain_text_from_html("<p>a &lt;b&gt; &quot;c&quot; &#39;d&#39;</p>")
        assert "a <b> \"c\" 'd'" in out

    def test_nbsp_becomes_character_not_literal_entity(self):
        out = generate_plain_text_from_html("<p>a&nbsp;&nbsp;b</p>")
        assert "&nbsp;" not in out
        assert "a\xa0\xa0b" in out


class TestNonContentBlocksAreExcluded:
    """The differential showed the old implementation leaking <script> bodies
    and <title> text into email bodies. These pin the exclusion set."""

    @pytest.mark.parametrize(
        "block",
        [
            "<style>p { color: red; }</style>",
            '<style type="text/css">.a{display:none}</style>',
            "<script>alert('X')</script>",
            "<title>TITLE-LEAK</title>",
            "<template><p>TEMPLATE-LEAK</p></template>",
        ],
    )
    def test_block_content_never_reaches_output(self, block):
        out = generate_plain_text_from_html(f"<p>hi</p>{block}<p>bye</p>")
        assert "hi" in out and "bye" in out
        for marker in ("color", "alert", "TITLE-LEAK", "TEMPLATE-LEAK", "display:none"):
            assert marker not in out

    def test_uppercase_and_nested_blocks(self):
        out = generate_plain_text_from_html(
            "<HEAD><STYLE>h{}</STYLE><title>T</title></HEAD><p>body</p>"
        )
        assert "body" in out
        assert "h{}" not in out and "T\n" not in out.replace("body", "")

    def test_unclosed_skip_block_does_not_leak_or_crash(self):
        # A skip tag that never closes swallows the rest — it must not crash,
        # and the style body must not appear as text.
        out = generate_plain_text_from_html("<p>hi</p><style>p{color:red}")
        assert "hi" in out
        assert "color" not in out

    def test_stray_close_tag_does_not_underflow(self):
        # Kills the mutant that removes the `> 0` underflow guard: a stray
        # </style> before any <style> must not put the parser into skip mode.
        out = generate_plain_text_from_html("</style><p>visible</p>")
        assert "visible" in out


class TestWhitespaceContract:
    """Exact output framing the twelve callers inherit."""

    def test_blank_runs_collapse_to_single_blank_line(self):
        # Kills the mutant that removes the blank-collapse loop.
        out = generate_plain_text_from_html("<p>one</p>\n\n\n\n\n<p>two</p>")
        assert "one\n\ntwo" in out
        assert "\n\n\n" not in out  # nowhere: not in the body, not in the frame

    def test_leading_and_trailing_wrapper_is_exactly_two_newlines(self):
        out = generate_plain_text_from_html("<p>solo</p>")
        assert out.startswith("\n\n") and not out.startswith("\n\n\n")
        assert out.endswith("\n\n") and not out.endswith("\n\n\n")
        assert out.strip() == "solo"

    def test_template_indentation_is_stripped(self):
        out = generate_plain_text_from_html("<div>\n      indented line\n   </div>")
        assert "\n      indented" not in out
        assert "indented line" in out


class TestDegenerateInputs:
    def test_empty_string(self):
        assert generate_plain_text_from_html("") == "\n\n\n\n"

    def test_none_returns_empty_frame_not_exception(self):
        # Deliberate fail-open (Sable RC 3050): a mail task should not crash
        # on a missing body. Direction change from the old implementation.
        assert generate_plain_text_from_html(None) == "\n\n\n\n"

    def test_html_comments_do_not_leak(self):
        out = generate_plain_text_from_html("<p>hi</p><!-- SECRET -->")
        assert "SECRET" not in out

    def test_deep_nesting_survives(self):
        html = "<div>" * 300 + "deep" + "</div>" * 300
        assert "deep" in generate_plain_text_from_html(html)


class TestRepresentativeEmailTemplate:
    """Shape of the real templates/emails/*.html files: head with style,
    preheader, button + copy-paste fallback link."""

    HTML = """
    <html>
      <head><style>.btn { background: #123; }</style><title>Reset your password</title></head>
      <body>
        <span style="display:none">Preheader text</span>
        <p>Hi Cond,</p>
        <p><a class="btn" href="https://x.co/reset/?uidb64=U&amp;token=T">Reset password</a></p>
        <p>If the button doesn't work, copy and paste this link into your browser.</p>
        <p>https://x.co/reset/?uidb64=U&amp;token=T&amp;email=a%40b.co</p>
      </body>
    </html>
    """

    def test_full_template_renders_usable_plain_text(self):
        out = generate_plain_text_from_html(self.HTML)
        assert "Hi Cond," in out
        assert "https://x.co/reset/?uidb64=U&token=T&email=a%40b.co" in out
        assert "&amp;" not in out
        assert ".btn" not in out and "background" not in out
        assert "Reset your password" not in out  # title dropped by design
