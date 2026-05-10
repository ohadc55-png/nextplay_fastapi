"""Dataclasses returned by the Research Agent.

Verbatim port of `backend/research/models.py` (only renderer comments
trimmed to keep the module focused). Behaviour-equivalent to v1.0-flask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Confidence = Literal["high", "medium", "low"]


@dataclass
class Source:
    url: str
    tier: int
    snippet_preview: str = ""


@dataclass
class Finding:
    entity: str
    metric: str
    value: str
    source_url: str
    source_tier: int
    confidence: Confidence = "medium"
    cross_source_count: int = 1


SECTION_HEADERS = {
    "en": {
        "team_identity":    "TEAM IDENTITY",
        "team_strengths":   "Strengths",
        "team_vulns":       "Vulnerabilities",
        "differentials":    "KEY DIFFERENTIALS",
        "personnel":        "PERSONNEL",
        "matchup_prep":     "MATCHUP PREP",
        "gaps":             "GAPS — what we couldn't verify",
        "sources":          "SOURCES",
        "confidence_label": "Confidence",
    },
    "he": {
        "team_identity":    "זהות הקבוצה",
        "team_strengths":   "חוזקות",
        "team_vulns":       "נקודות תורפה",
        "differentials":    "הפרשי מפתח",
        "personnel":        "שחקנים",
        "matchup_prep":     "הכנה למשחק",
        "gaps":             "פערים — מה לא הצלחנו לאמת",
        "sources":          "מקורות",
        "confidence_label": "ביטחון",
    },
}

CONFIDENCE_LABELS = {
    "en": {"high": "High", "medium": "Medium", "low": "Low"},
    "he": {"high": "גבוה", "medium": "בינוני", "low": "נמוך"},
}


@dataclass
class ResearchResult:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    confidence_overall: Confidence = "medium"
    queries_run: list[str] = field(default_factory=list)
    urls_fetched: list[str] = field(default_factory=list)
    refinement_loops: int = 0
    elapsed_seconds: float = 0.0
    cache_hit: bool = False
    tactical_insights: dict[str, Any] = field(default_factory=dict)

    def to_text_for_agent(self) -> str:
        """Render the result as the calling agent's text payload.
        Prefers `tactical_insights` structure; falls back to `summary`."""
        if not self.findings and not self.summary and not self.tactical_insights:
            return self._render_empty()
        if self.tactical_insights:
            structured = self._render_structured()
            if structured:
                return structured
        out: list[str] = []
        if self.summary:
            out.append(self.summary.strip())
            out.append("")
        if self.sources:
            out.append("Sources used:")
            for s in self.sources:
                out.append(f"  - {s.url} (tier {s.tier})")
            out.append("")
        if self.missing:
            out.append("What I could NOT verify:")
            for m in self.missing:
                out.append(f"  - {m}")
            out.append("")
        out.append(f"[research confidence: {self.confidence_overall}]")
        return "\n".join(out)

    def _render_structured(self) -> str:
        ti = self.tactical_insights or {}
        lang = (ti.get("language") or "en").lower()
        if lang not in SECTION_HEADERS:
            lang = "en"
        H = SECTION_HEADERS[lang]
        C = CONFIDENCE_LABELS[lang]

        out: list[str] = []
        tp = ti.get("team_profile")
        if isinstance(tp, dict):
            identity_text = (tp.get("identity_text") or "").strip()
            strengths = [s.strip() for s in (tp.get("strengths") or [])
                         if isinstance(s, str) and s.strip()]
            vulns = [v.strip() for v in (tp.get("vulnerabilities") or [])
                     if isinstance(v, str) and v.strip()]
            if identity_text or strengths or vulns:
                out.append(f"━━━ {H['team_identity']} ━━━")
                if identity_text:
                    out.append(identity_text)
                    out.append("")
                if strengths:
                    out.append(f"— {H['team_strengths']} —")
                    for s in strengths:
                        out.append(f"  • {s}")
                    out.append("")
                if vulns:
                    out.append(f"— {H['team_vulns']} —")
                    for v in vulns:
                        out.append(f"  • {v}")
                    out.append("")
        elif isinstance(tp, str) and tp.strip():
            out.append(f"━━━ {H['team_identity']} ━━━")
            out.append(tp.strip())
            out.append("")

        diffs = ti.get("differentials") or []
        if diffs:
            out.append(f"━━━ {H['differentials']} ━━━")
            for d in diffs:
                if not isinstance(d, dict):
                    continue
                label = (d.get("label") or "").strip()
                value = (d.get("value") or "").strip()
                ctx = (d.get("context") or "").strip()
                line = f"• {label}: {value}"
                if ctx:
                    line += f"  ({ctx})"
                out.append(line)
            out.append("")

        people = ti.get("personnel") or []
        if people:
            out.append(f"━━━ {H['personnel']} ━━━")
            for i, p in enumerate(people, 1):
                if not isinstance(p, dict):
                    continue
                name = (p.get("name") or "").strip()
                role = (p.get("role_label") or "").strip()
                stats = (p.get("stats_line") or "").strip()
                note = (p.get("tactical_note") or "").strip()
                header = f"[{i}] {name}"
                if role:
                    header += f" — {role}"
                out.append(header)
                if stats:
                    out.append(f"    {stats}")
                if note:
                    out.append(f"    {note}")
                out.append("")

        prep = ti.get("matchup_prep") or []
        if prep:
            out.append(f"━━━ {H['matchup_prep']} ━━━")
            for j, item in enumerate(prep, 1):
                if isinstance(item, str) and item.strip():
                    out.append(f"{j}. {item.strip()}")
            out.append("")

        gaps = ti.get("missing") or self.missing or []
        if gaps:
            out.append(f"━━━ {H['gaps']} ━━━")
            for g in gaps:
                if isinstance(g, str) and g.strip():
                    out.append(f"  - {g.strip()}")
            out.append("")

        cited = ti.get("sources_cited") or [s.url for s in self.sources]
        if cited:
            out.append(f"━━━ {H['sources']} ━━━")
            for u in cited:
                out.append(f"  - {u}")
            out.append("")
        conf = (ti.get("confidence_overall") or self.confidence_overall or "medium").lower()
        out.append(f"[{H['confidence_label']}: {C.get(conf, conf)}]")

        if len(out) <= 1:
            return ""
        return "\n".join(out)

    def _render_empty(self) -> str:
        out = [
            "I couldn't find verified data for this question.",
            "",
            f"What I tried ({len(self.queries_run)} queries, "
            f"{len(self.urls_fetched)} pages fetched):",
        ]
        for q in self.queries_run[:5]:
            out.append(f"  - search: {q}")
        for u in self.urls_fetched[:5]:
            out.append(f"  - fetched: {u}")
        out.append("")
        out.append(
            "To unblock me, share one of: a screenshot, a specific URL "
            "you saw the data on, or a player name to search for directly."
        )
        return "\n".join(out)


__all__ = [
    "CONFIDENCE_LABELS",
    "SECTION_HEADERS",
    "Confidence",
    "Finding",
    "ResearchResult",
    "Source",
]
