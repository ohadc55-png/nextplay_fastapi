"""Email template renderer — Jinja2 environment scoped to
`frontend/templates/email/`. Ported from v1 `backend/email/templates.py`.

Public function:

    render(template_name, language, context) -> (subject, html, text)

Each template:
- extends `base.html` (header/footer/CTA scaffold)
- declares `<!-- SUBJECT: ... -->` and `<!-- TEXT: ... -->` markers at
  the top of the rendered output (defined via `{% block subject_tag %}`
  and `{% block text_body %}` in the template body)
- bilingual via `{% if rtl %}...{% else %}...{% endif %}`

The base template embeds a base64 logo so it shows up in every client
(Gmail, Outlook, Apple Mail) regardless of network state.
"""

from __future__ import annotations

import logging
import os
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.core.config import settings

logger = logging.getLogger(__name__)


_TEMPLATES_DIR = os.path.join("frontend", "templates", "email")


def _load_logo_data_uri() -> str:
    """Read the pre-encoded logo b64 once at startup. Returns '' if missing."""
    path = os.path.join(_TEMPLATES_DIR, "_logo_b64.txt")
    try:
        with open(path, encoding="ascii") as f:
            b64 = f.read().strip()
        return f"data:image/png;base64,{b64}" if b64 else ""
    except Exception:
        return ""


_LOGO_DATA_URI = _load_logo_data_uri()

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


_SUBJECT_RE = re.compile(r"<!--\s*SUBJECT:\s*(.+?)\s*-->", re.IGNORECASE | re.DOTALL)
_TEXT_RE = re.compile(r"<!--\s*TEXT:\s*(.+?)\s*-->", re.IGNORECASE | re.DOTALL)


def _extract_subject_and_text(rendered_html: str) -> tuple[str, str, str]:
    subject = ""
    text = ""
    m = _SUBJECT_RE.search(rendered_html)
    if m:
        subject = m.group(1).strip()
        rendered_html = rendered_html.replace(m.group(0), "", 1)
    m = _TEXT_RE.search(rendered_html)
    if m:
        text = m.group(1).strip()
        rendered_html = rendered_html.replace(m.group(0), "", 1)
    return subject, rendered_html.strip(), text


def render(
    template_name: str,
    language: str = "en",
    context: dict | None = None,
) -> tuple[str, str, str]:
    """Render the named template (with or without `.html`) for the given
    language. Returns `(subject, html, text)`.

    Raises `jinja2.TemplateNotFound` if the template doesn't exist.
    """
    if not template_name.endswith(".html"):
        template_name = template_name + ".html"
    ctx = dict(context or {})
    ctx["language"] = (language or "en").lower()
    ctx.setdefault("rtl", ctx["language"] == "he")
    ctx.setdefault("brand_name", "NEXTPLAY")
    ctx.setdefault(
        "app_url",
        (settings.APP_BASE_URL or settings.BASE_URL or "https://trynextplay.app").rstrip("/"),
    )
    ctx.setdefault("logo_data_uri", _LOGO_DATA_URI)

    template = _env.get_template(template_name)
    rendered = template.render(**ctx)
    return _extract_subject_and_text(rendered)


__all__ = ["render"]
