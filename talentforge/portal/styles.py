"""Resume templates (all ATS-friendly) and accent colours.

ATS rules every template follows: single column, real selectable text, standard section headings, standard
fonts, no tables, icons, photos or text boxes. Templates differ only in typography, spacing and colour.
"""
from __future__ import annotations

import re

TEMPLATES: dict[str, dict] = {
    "classic": {
        "name": "Classic",
        "tagline": "Traditional serif, centred header, ruled headings",
        "best_for": "Corporate, finance, consulting, academia and government applications",
        "font": "Georgia",
        "css_font": 'Georgia, "Times New Roman", serif',
        "default_accent": "#141413",
        "accents": [("Ink", "#141413"), ("Navy", "#1f3a5f"), ("Maroon", "#7a1f2b"), ("Forest", "#1f5c3f")],
    },
    "modern": {
        "name": "Modern",
        "tagline": "Clean sans-serif, coloured headings with an accent bar",
        "best_for": "Tech, startups, product, data and design-adjacent roles",
        "font": "Calibri",
        "css_font": 'Calibri, Carlito, "Segoe UI", Roboto, Arial, sans-serif',
        "default_accent": "#2b5fb8",
        "accents": [("Blue", "#2b5fb8"), ("Teal", "#0f766e"), ("Orange", "#cf4500"), ("Violet", "#5b3fa0")],
    },
    "executive": {
        "name": "Executive",
        "tagline": "Compact Arial, letter-spaced headings, strong top rule",
        "best_for": "Experienced candidates who need to fit a lot on one page",
        "font": "Arial",
        "css_font": 'Arial, Helvetica, "Liberation Sans", sans-serif',
        "default_accent": "#334155",
        "accents": [("Slate", "#334155"), ("Burgundy", "#8a1c3d"), ("Steel", "#1d4e89"), ("Charcoal", "#1f2933")],
    },
}
DEFAULT_TEMPLATE = "modern"
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def resolve(template_id: str | None, accent: str | None) -> tuple[str, str]:
    """Return a valid (template_id, accent_hex), falling back to defaults."""
    tid = template_id if template_id in TEMPLATES else DEFAULT_TEMPLATE
    acc = accent if accent and _HEX_RE.match(accent) else TEMPLATES[tid]["default_accent"]
    return tid, acc.lower()


def catalogue() -> list[dict]:
    return [{"id": k, **v} for k, v in TEMPLATES.items()]


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# A small neutral sample used for template thumbnails before the user has any content.
SAMPLE_RESUME = {
    "basics": {"name": "Your Name", "headline": "Target role | Key skills", "email": "you@example.com",
               "phone": "+91 90000 00000", "location": "Pune, India", "links": ["linkedin.com/in/you"]},
    "summary": "Motivated professional with hands-on project experience and a record of delivering measurable results.",
    "skills": ["Python", "SQL", "Git", "REST API", "Docker", "Excel"],
    "experience": [{"role": "Software Intern", "company": "Company Name", "location": "Pune", "start": "Jun 2025",
                    "end": "Aug 2025",
                    "bullets": ["Built an internal tool that cut reporting time by 40%", "Wrote tests covering 85% of modules"]}],
    "projects": [{"name": "Project name", "description": "Python, Flask",
                  "bullets": ["Designed a REST API serving 3 user roles"]}],
    "education": [{"degree": "B.Tech in Computer Science", "institution": "University Name", "year": "2026",
                   "details": ""}],
    "certifications": ["Relevant certification"],
    "extras": [],
}
