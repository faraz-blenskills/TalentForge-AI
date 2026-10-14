"""Structured resume model: normalise, parse (via resume_parser), render to plain text."""
from __future__ import annotations

import copy

MAX_ITEMS = 25


def empty_resume(name: str = "", email: str = "") -> dict:
    return {
        "basics": {"name": name, "headline": "", "email": email, "phone": "", "location": "", "links": []},
        "summary": "",
        "experience": [],
        "education": [],
        "skills": [],
        "projects": [],
        "certifications": [],
        "extras": [],
    }


def _s(v, n: int = 400) -> str:
    return str(v).strip()[:n] if v is not None else ""


def _list(v) -> list:
    return v if isinstance(v, list) else []


def normalize(data: dict | None) -> dict:
    """Coerce arbitrary (client-supplied) JSON into the resume schema. Never raises on bad shapes."""
    d = data if isinstance(data, dict) else {}
    b = d.get("basics") if isinstance(d.get("basics"), dict) else {}
    out = empty_resume()
    out["basics"] = {
        "name": _s(b.get("name"), 120), "headline": _s(b.get("headline"), 160),
        "email": _s(b.get("email"), 160), "phone": _s(b.get("phone"), 40),
        "location": _s(b.get("location"), 120),
        "links": [_s(x, 200) for x in _list(b.get("links")) if _s(x)][:6],
    }
    out["summary"] = _s(d.get("summary"), 1500)
    for e in _list(d.get("experience"))[:MAX_ITEMS]:
        if not isinstance(e, dict):
            continue
        out["experience"].append({
            "role": _s(e.get("role"), 160), "company": _s(e.get("company"), 160),
            "location": _s(e.get("location"), 120), "start": _s(e.get("start"), 40), "end": _s(e.get("end"), 40),
            "bullets": [_s(x, 500) for x in _list(e.get("bullets")) if _s(x)][:14],
        })
    for e in _list(d.get("education"))[:MAX_ITEMS]:
        if isinstance(e, dict):
            out["education"].append({
                "degree": _s(e.get("degree"), 200), "institution": _s(e.get("institution"), 200),
                "year": _s(e.get("year"), 60), "details": _s(e.get("details"), 300)})
    seen = set()
    for x in _list(d.get("skills"))[:100]:
        t = _s(x, 60)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out["skills"].append(t)
    for p in _list(d.get("projects"))[:MAX_ITEMS]:
        if isinstance(p, dict):
            out["projects"].append({
                "name": _s(p.get("name"), 160), "description": _s(p.get("description"), 400),
                "bullets": [_s(x, 500) for x in _list(p.get("bullets")) if _s(x)][:10]})
    out["certifications"] = [_s(x, 240) for x in _list(d.get("certifications")) if _s(x)][:30]
    for ex in _list(d.get("extras"))[:10]:
        if isinstance(ex, dict):
            items = [_s(x, 300) for x in _list(ex.get("items")) if _s(x)][:30]
            if items or _s(ex.get("title")):
                out["extras"].append({"title": _s(ex.get("title"), 80) or "Additional", "items": items})
    return out


# --------------------------------------------------------------------------- parsing
def from_text(text: str, fallback_name: str = "") -> dict:
    from .resume_parser import parse_document
    r, _ = parse_document(text, fallback_name)
    return normalize(r)


def parse_with_report(text: str, fallback_name: str = "") -> tuple[dict, dict]:
    from .resume_parser import parse_document
    r, report = parse_document(text, fallback_name)
    return normalize(r), report


# --------------------------------------------------------------------------- helpers
def iter_bullets(r: dict):
    """Yield (section, index_path, text) for every bullet so suggestions can reference them."""
    for i, e in enumerate(r.get("experience", [])):
        for j, b in enumerate(e.get("bullets", [])):
            yield "experience", (i, j), b
    for i, p in enumerate(r.get("projects", [])):
        for j, b in enumerate(p.get("bullets", [])):
            yield "projects", (i, j), b


def to_text(r: dict) -> str:
    b = r["basics"]
    lines = [b["name"], b["headline"], " | ".join(x for x in (b["email"], b["phone"], b["location"]) if x)]
    lines += b["links"]
    if r["summary"]:
        lines += ["SUMMARY", r["summary"]]
    if r["skills"]:
        lines += ["SKILLS", ", ".join(r["skills"])]
    if r["experience"]:
        lines.append("EXPERIENCE")
        for e in r["experience"]:
            lines.append(" - ".join(x for x in (e["role"], e["company"], f"{e['start']} {e['end']}".strip()) if x))
            lines += [f"- {x}" for x in e["bullets"]]
    if r["projects"]:
        lines.append("PROJECTS")
        for p in r["projects"]:
            lines.append(" - ".join(x for x in (p["name"], p["description"]) if x))
            lines += [f"- {x}" for x in p["bullets"]]
    if r["education"]:
        lines.append("EDUCATION")
        for e in r["education"]:
            lines.append(", ".join(x for x in (e["degree"], e["institution"], e["year"], e["details"]) if x))
    if r["certifications"]:
        lines += ["CERTIFICATIONS"] + r["certifications"]
    for ex in r.get("extras", []):
        lines.append(ex["title"].upper())
        lines += ex["items"]
    return "\n".join(l for l in lines if l is not None)


def detected_skills(r: dict) -> set[str]:
    """Canonical taxonomy skills present anywhere in the resume text."""
    from app.parsing import _find_skills
    return {s["name"] for s in _find_skills(to_text(r))}


def clone(r: dict) -> dict:
    return copy.deepcopy(r)
