"""Structured resume model: parse from text, normalise, render to plain text."""
from __future__ import annotations

import copy
import re

from app.parsing import BULLET_RE, DEGREES, EMAIL_RE, RANGE_RE, _split_sections
from app.taxonomy import SKILLS

PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{8,}\d")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|gitlab\.com|behance\.net)/[^\s,|;]+", re.I)
INSTITUTION_RE = re.compile(r"universit|institut|college|school|academy|iit\b|nit\b", re.I)
ROLE_WORDS = re.compile(
    r"engineer|developer|analyst|scientist|intern|student|designer|manager|consultant|architect|"
    r"administrator|specialist|associate|trainee|lead|researcher", re.I)

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
    out["summary"] = _s(d.get("summary"), 1200)
    for e in _list(d.get("experience"))[:MAX_ITEMS]:
        if not isinstance(e, dict):
            continue
        out["experience"].append({
            "role": _s(e.get("role"), 160), "company": _s(e.get("company"), 160),
            "location": _s(e.get("location"), 120), "start": _s(e.get("start"), 40), "end": _s(e.get("end"), 40),
            "bullets": [_s(x, 500) for x in _list(e.get("bullets")) if _s(x)][:12],
        })
    for e in _list(d.get("education"))[:MAX_ITEMS]:
        if isinstance(e, dict):
            out["education"].append({
                "degree": _s(e.get("degree"), 200), "institution": _s(e.get("institution"), 200),
                "year": _s(e.get("year"), 40), "details": _s(e.get("details"), 300)})
    out["skills"] = []
    seen = set()
    for x in _list(d.get("skills"))[:80]:
        t = _s(x, 60)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out["skills"].append(t)
    for p in _list(d.get("projects"))[:MAX_ITEMS]:
        if isinstance(p, dict):
            out["projects"].append({
                "name": _s(p.get("name"), 160), "description": _s(p.get("description"), 400),
                "bullets": [_s(x, 500) for x in _list(p.get("bullets")) if _s(x)][:8]})
    out["certifications"] = [_s(x, 200) for x in _list(d.get("certifications")) if _s(x)][:20]
    return out


# --------------------------------------------------------------------------- parsing
def _strip_bullet(s: str) -> str:
    return BULLET_RE.sub("", s).strip()


def _parse_experience(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        is_bullet = bool(BULLET_RE.match(ln))
        has_range = bool(RANGE_RE.search(s))
        starts_new = False
        if not is_bullet:
            if cur is None or has_range or cur["bullets"]:
                starts_new = len(s) <= 110 and not s.endswith(".")
            elif not (cur["role"] and cur["company"]):
                starts_new = False
        if starts_new:
            start = end = ""
            m = RANGE_RE.search(s)
            head = s
            if m:
                start = f"{(m.group('m1') or '').title()} {m.group('y1')}".strip()
                e = m.group("y2")
                end = "Present" if not e.isdigit() else f"{(m.group('m2') or '').title()} {e}".strip()
                head = (s[:m.start()] + " " + s[m.end():]).strip(" -–—|,()")
            parts = [p.strip() for p in re.split(r"\s+at\s+|\s*[|•·@]\s*|\s+[-–—]\s+|,\s+", head) if p.strip()]
            role = parts[0] if parts else head
            company = parts[1] if len(parts) > 1 else ""
            location = parts[2] if len(parts) > 2 else ""
            if role and not ROLE_WORDS.search(role) and company and ROLE_WORDS.search(company):
                role, company = company, role
            cur = {"role": role[:160], "company": company[:160], "location": location[:120],
                   "start": start, "end": end, "bullets": []}
            entries.append(cur)
        elif cur is not None:
            cur["bullets"].append(_strip_bullet(s)[:500])
        else:
            cur = {"role": "", "company": "", "location": "", "start": "", "end": "", "bullets": [_strip_bullet(s)]}
            entries.append(cur)
    return entries[:MAX_ITEMS]


def _parse_education(lines: list[str]) -> list[dict]:
    degree_rx = re.compile("|".join(rx for _, rx in DEGREES), re.I)
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        s = _strip_bullet(ln)
        if not s:
            continue
        year = re.findall(r"(?:19|20)\d{2}", s)
        parts = [p.strip() for p in re.split(r",|\|| - | – | — ", s) if p.strip()]
        deg = next((p for p in parts if degree_rx.search(p)), "")
        inst = next((p for p in parts if INSTITUTION_RE.search(p)), "")
        if deg:
            cur = {"degree": re.sub(r"\(?\b(?:19|20)\d{2}\b\)?", "", deg).strip(" -,"),
                   "institution": inst, "year": year[-1] if year else "", "details": ""}
            out.append(cur)
        elif inst and cur is not None and not cur["institution"]:
            cur["institution"] = re.sub(r"\(?\b(?:19|20)\d{2}\b\)?", "", inst).strip(" -,")
            if year and not cur["year"]:
                cur["year"] = year[-1]
        elif inst:
            cur = {"degree": "", "institution": inst, "year": year[-1] if year else "", "details": ""}
            out.append(cur)
        elif cur is not None:
            cur["details"] = (cur["details"] + " " + s).strip()[:300]
    return out[:MAX_ITEMS]


def _parse_projects(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if BULLET_RE.match(ln):
            if cur is None:
                cur = {"name": "", "description": "", "bullets": []}
                out.append(cur)
            cur["bullets"].append(_strip_bullet(s)[:500])
        else:
            if " - " in s and len(s) > 40:
                name, desc = s.split(" - ", 1)
            else:
                name, desc = s, ""
            cur = {"name": name.strip()[:160], "description": desc.strip()[:400], "bullets": []}
            out.append(cur)
    return out[:MAX_ITEMS]


def _split_skill_tokens(lines: list[str]) -> list[str]:
    toks: list[str] = []
    for ln in lines:
        s = _strip_bullet(ln)
        if ":" in s and len(s.split(":", 1)[0]) <= 30:
            s = s.split(":", 1)[1]
        for t in re.split(r"[,;|•·/]|\s{2,}", s):
            t = t.strip(" .-")
            if 1 < len(t) <= 40:
                toks.append(t)
    return toks


def from_text(text: str, fallback_name: str = "") -> dict:
    from app.parsing import _find_skills

    sections = _split_sections(text.splitlines())
    r = empty_resume()
    header = [l.strip() for l in sections.get("header", []) if l.strip()]

    name = ""
    for l in header[:3]:
        words = l.split()
        if 1 < len(words) <= 4 and not EMAIL_RE.search(l) and all(re.match(r"^[A-Za-z.'\-]+$", w) for w in words):
            name = l.title() if l.isupper() else l
            break
    head_blob = "\n".join(header[:6])
    email = EMAIL_RE.search(text)
    phone = PHONE_RE.search(head_blob)
    links = [u.rstrip(".") for u in URL_RE.findall(text)]
    headline = ""
    for l in header[:5]:
        if l != name and not EMAIL_RE.search(l) and not PHONE_RE.search(l) and ROLE_WORDS.search(l) and len(l) <= 100:
            headline = l
            break
    location = ""
    loc_rx = re.compile(r"^[A-Z][a-zA-Z.]+(?: [A-Z][a-zA-Z.]+)?,\s*[A-Z][a-zA-Z.]+(?: [A-Z][a-zA-Z.]+)?$")
    for l in header[:6]:
        if l == name or l == headline:
            continue
        for seg in (x.strip() for x in l.split("|")):
            if seg and not EMAIL_RE.search(seg) and not PHONE_RE.search(seg) and loc_rx.match(seg):
                location = seg
                break
        if location:
            break

    r["basics"] = {"name": name or fallback_name, "headline": headline, "email": email.group(0) if email else "",
                   "phone": phone.group(0).strip() if phone else "", "location": location,
                   "links": list(dict.fromkeys(links))[:4]}
    r["summary"] = " ".join(_strip_bullet(l) for l in sections.get("summary", []) if l.strip())[:1200]
    r["experience"] = _parse_experience(sections.get("experience", []))
    r["education"] = _parse_education(sections.get("education", []) or [])
    r["projects"] = _parse_projects(sections.get("projects", []))
    r["certifications"] = [_strip_bullet(l)[:200] for l in sections.get("certifications", [])
                           if len(_strip_bullet(l)) >= 4][:20]

    skills = _split_skill_tokens(sections.get("skills", []))
    if not skills:
        skills = [s["name"] for s in _find_skills(text)]
    r["skills"] = skills
    return normalize(r)


# --------------------------------------------------------------------------- rendering helpers
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
            lines.append(", ".join(x for x in (e["degree"], e["institution"], e["year"]) if x))
    if r["certifications"]:
        lines += ["CERTIFICATIONS"] + r["certifications"]
    return "\n".join(l for l in lines if l is not None)


def detected_skills(r: dict) -> set[str]:
    """Canonical taxonomy skills present anywhere in the resume text."""
    from app.parsing import _find_skills
    return {s["name"] for s in _find_skills(to_text(r))}


def clone(r: dict) -> dict:
    return copy.deepcopy(r)


def known_skill_names() -> set[str]:
    return set(SKILLS)
