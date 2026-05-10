"""Asset build script — minifier output validation.

We don't run the full glob walk here (no `frontend/static/` content yet
until Phase 8). The minifier functions themselves are pure-string
transforms that we can test directly."""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is at the repo root, not under src/ — make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


from build import minify_css, minify_js


class TestMinifyCss:
    def test_strips_block_comments(self):
        out = minify_css("body { /* a comment */ color: red; }")
        assert "/*" not in out
        assert "comment" not in out
        assert "color:red" in out

    def test_collapses_whitespace(self):
        out = minify_css("h1   {\n   color:    red ;\n}")
        # No newlines, no double-spaces
        assert "\n" not in out
        assert "  " not in out
        assert "color:red" in out

    def test_drops_trailing_semicolon_before_brace(self):
        out = minify_css("h1{color:red;}")
        assert out == "h1{color:red}"

    def test_no_space_around_punctuation(self):
        out = minify_css("a > b { margin: 0 ; }")
        # No space around `>` or `,`
        assert "a>b" in out
        # No space inside { }
        assert "{margin:0}" in out


class TestMinifyJs:
    def test_strips_block_comments(self):
        src = "/* leading comment */\nconst x = 1;\n/* trailing */"
        out = minify_js(src)
        assert "/*" not in out
        assert "const x = 1;" in out

    def test_strips_full_line_comments(self):
        src = "// banner\nconst x = 1;\n// trailing"
        out = minify_js(src)
        assert "// banner" not in out
        assert "const x = 1;" in out

    def test_preserves_string_literals(self):
        """Conservative minifier should NOT touch comment markers
        inside string literals."""
        src = 'const url = "https://example.com/x";'
        out = minify_js(src)
        # `//` inside the URL must survive
        assert "https://example.com/x" in out

    def test_collapses_blank_lines(self):
        src = "const a = 1;\n\n\n\nconst b = 2;"
        out = minify_js(src)
        # 4 newlines collapse to one blank line (\n\n)
        assert out.count("\n\n\n") == 0


class TestRoundTrip:
    def test_real_world_css(self):
        css = """
        /* Top of the file banner */
        .container {
            display: flex;
            margin: 0 auto;
            padding: 16px;
        }

        .button {
            color: #ff6b35;
            background: white;
        }
        """
        out = minify_css(css)
        assert "banner" not in out
        assert ".container{display:flex" in out
        assert ".button{color:#ff6b35" in out

    def test_real_world_js(self):
        js = """
        // Module init
        function setup() {
            const x = 1;  // assign
            return x;
        }

        /* end */
        """
        out = minify_js(js)
        assert "Module init" not in out
        assert "function setup()" in out
        assert "const x = 1;" in out
