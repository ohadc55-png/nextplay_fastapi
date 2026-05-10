"""CSS / JS minifier — no npm required.

Verbatim port of v1 `scripts/build.py`. Run before deploy:

    python scripts/build.py

Walks `frontend/static/**` and writes `*.min.css` / `*.min.js` next to
each source file. The Jinja2 helper (Phase 8) picks the `.min` variant
when `RAILWAY_ENVIRONMENT` is set.
"""

from __future__ import annotations

import glob
import os
import re

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend", "static",
)


def minify_css(content: str) -> str:
    """Basic CSS minification: remove comments, collapse whitespace."""
    content = re.sub(r"/\*[\s\S]*?\*/", "", content)
    content = re.sub(r"^\s*//.*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s+", " ", content)
    content = re.sub(r"\s*([{}:;,>~+])\s*", r"\1", content)
    content = re.sub(r";}", "}", content)
    return content.strip()


def minify_js(content: str) -> str:
    """Conservative JS minification — only blank lines + trailing
    whitespace + block/line comments. Avoids the risk of mangling
    string-literal content that a full minifier would catch."""
    content = re.sub(r"/\*[\s\S]*?\*/", "", content)
    content = re.sub(r"^\s*//.*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"\n\s*\n\s*\n", "\n\n", content)
    content = re.sub(r"[ \t]+$", "", content, flags=re.MULTILINE)
    return content.strip()


def process_files() -> tuple[int, int]:
    """Returns (count_processed, total_bytes_saved)."""
    total_saved = 0
    count = 0
    for ext, minifier in (("css", minify_css), ("js", minify_js)):
        pattern = os.path.join(STATIC_DIR, "**", f"*.{ext}")
        for filepath in glob.glob(pattern, recursive=True):
            if ".min." in filepath:
                continue
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            minified = minifier(content)
            out_path = filepath.replace(f".{ext}", f".min.{ext}")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(minified)
            orig_size = len(content.encode("utf-8"))
            min_size = len(minified.encode("utf-8"))
            saved = orig_size - min_size
            total_saved += saved
            count += 1
            pct = (saved / orig_size * 100) if orig_size else 0
            name = os.path.relpath(filepath, STATIC_DIR)
            print(f"  {name}: {orig_size // 1024}KB -> {min_size // 1024}KB ({pct:.0f}% saved)")
    print(f"\n  Total: {count} files, {total_saved // 1024}KB saved")
    return count, total_saved


if __name__ == "__main__":
    print("Building minified assets...")
    process_files()
    print("Done.")
