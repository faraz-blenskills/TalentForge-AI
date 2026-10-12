"""Resume tailoring: compare a resume with a job posting and produce reviewable suggestions.

Suggestions never invent facts. Rewrites only rephrase what the resume already says; anything that would
require new information (metrics, tools) is either off by default or asks the user for input.
"""
from __future__ import annotations

import re
from collections import Counter

from app.matching import STOP, scrub_identity, text_similarity
from app.parsing import _find_skills
from app.scoring import skill_gap
from app.taxonomy import SKILLS

from .resume_score import has_metric, starts_strong, starts_weak
from .resume_struct import clone, iter_bullets, normalize, to_text

WEAK_MAP = [
    ("responsible for", "Owned"), ("duties included", "Delivered"), ("tasked with", "Delivered"),
    ("worked on", "Developed"), ("worked with", "Collaborated with"), ("helped to", "Supported"),
    ("helped", "Supported"), ("assisted with", "Supported"), ("assisted in", "Supported"),
    ("assisted", "Supported"), ("involved in", "Contributed to"), ("participated in", "Contributed to"),
    ("was part of", "Contributed to"), ("handled", "Managed"),
]
TITLE_NOISE = re.compile(r"\b(sr\.?|jr\.?|senior|junior|lead|principal|staff|intern|trainee|ii|iii|i|remote|"
                         r"\(.*?\)|hiring|urgent)\b", re.I)


DISPLAY = {
    "css": "CSS", "html": "HTML", "sql": "SQL", "aws": "AWS", "gcp": "GCP", "ci/cd": "CI/CD", "nlp": "NLP",
    "llm": "LLM", "rag": "RAG", "rest api": "REST API", "fastapi": "FastAPI", "javascript": "JavaScript",
    "typescript": "TypeScript", "postgresql": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB",
    "sqlite": "SQLite", "node.js": "Node.js", "react": "React", "vue": "Vue", "graphql": "GraphQL",
    "pytorch": "PyTorch", "tensorflow": "TensorFlow", "scikit-learn": "scikit-learn", "power bi": "Power BI",
    "langchain": "LangChain", "vector database": "Vector databases", "machine learning": "Machine learning",
    "deep learning": "Deep learning", "c++": "C++", "c#": "C#", "spring boot": "Spring Boot", "git": "Git",
    "github": "GitHub", "kubernetes": "Kubernetes", "docker": "Docker", "linux": "Linux", "python": "Python",
    "java": "Java", "pandas": "pandas", "numpy": "NumPy", "redis": "Redis", "terraform": "Terraform",
    "selenium": "Selenium", "pytest": "pytest", "cypress": "Cypress", "azure": "Azure", "excel": "Excel",
    "tableau": "Tableau", "django": "Django", "flask": "Flask", "angular": "Angular", "statistics": "Statistics",
    "testing": "Testing", "agile": "Agile", "communication": "Communication", "teamwork": "Teamwork",
    "prompt engineering": "Prompt engineering", "data visualization": "Data visualization",
    "monitoring": "Monitoring", "keras": "Keras", "go": "Go",
}


def display_skill(name: str) -> str:
    return DISPLAY.get(name.lower(), name)


def clean_title(title: str) -> str:
    t = re.sub(r"[\[(].*?[\])]", "", title or "")
    t = re.split(r"\s+[-|–—@]\s+", t)[0]
    return re.sub(r"\s+", " ", t).strip() or "the role"


def job_keywords(title: str, text: str) -> list[dict]:
    found = _find_skills(f"{title}\n{title}\n{text}")
    title_l = (title or "").lower()
    out = []
    for s in found:
        n = s["mentions"]
        in_title = s["name"] in title_l
        weight = 3 if (n >= 3 or in_title) else 2 if n >= 2 else 1
        out.append({"skill": s["name"], "mentions": n, "weight": weight})
    out.sort(key=lambda k: (-k["weight"], -k["mentions"], k["skill"]))
    return out[:18]


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z0-9+#.]{2,}", (s or "").lower()) if t not in STOP}


SOFT_SKILLS = {"communication", "teamwork", "problem solving", "agile"}


def posting_match(resume: dict, title: str, description: str, company: str = "") -> dict:
    """0-100 match between a resume and a job posting, with matched/missing skills.

    - the company name is removed first (so "GitLab" is not read as a git requirement)
    - soft skills count half, and postings that name very few skills earn less coverage credit
    """
    if company and len(company) > 2:
        description = re.sub(re.escape(company), " ", description, flags=re.I)
    kws = job_keywords(title, description)
    req = {k["skill"]: (k["weight"] * 0.5 if k["skill"] in SOFT_SKILLS else k["weight"]) for k in kws}
    evidence = min(sum(req.values()) / 8.0, 1.0) if req else 0.0
    text = to_text(resume)
    resume_skills = {s for s in (x.lower() for x in resume["skills"])}
    from app.parsing import _find_skills as _fs
    prof = {"skills": [{"name": s["name"], "evidence": s["evidence"]} for s in _fs(text)]}
    # explicit skill entries that are canonical names also count
    for s in resume_skills:
        if s in SKILLS and not any(p["name"] == s for p in prof["skills"]):
            prof["skills"].append({"name": s, "evidence": "skills list"})
    gap = skill_gap(prof, req, []) if req else {"coverage": 0.0, "matched": [], "partial": [], "missing": [], "items": []}

    title_tokens = _tokens(clean_title(title))
    my_tokens = _tokens(resume["basics"]["headline"] + " " + " ".join(e["role"] for e in resume["experience"][:2]) +
                        " " + " ".join(p["name"] for p in resume["projects"][:3]))
    title_sim = (len(title_tokens & my_tokens) / len(title_tokens)) if title_tokens else 0.0

    sim, shared = text_similarity(scrub_identity(text, {"name": resume["basics"]["name"]}), f"{title} {description}")
    if req:
        score = 60 * gap["coverage"] * evidence + 15 * min(title_sim * 1.5, 1) + 25 * min(sim * 2.2, 1)
    else:
        score = 25 * min(title_sim * 1.5, 1) + 75 * min(sim * 2.2, 1)
    return {
        "score": round(min(score, 100), 1),
        "keywords": kws,
        "matched": [i["skill"] for i in gap["matched"]],
        "partial": [i["skill"] for i in gap["partial"]],
        "missing": [i["skill"] for i in gap["missing"]],
        "tech_matched": sum(1 for i in gap["matched"] if i["skill"] not in SOFT_SKILLS),
        "coverage": round(gap["coverage"], 3),
        "title_similarity": round(title_sim, 2),
        "text_similarity": round(sim, 3),
        "shared_terms": shared,
    }


def extra_terms(resume: dict, description: str, limit: int = 8) -> list[str]:
    have = _tokens(to_text(resume))
    counts = Counter(t for t in re.findall(r"[a-z][a-z\-]{3,}", description.lower()) if t not in STOP)
    skill_words = {a for _, al in SKILLS.values() for a in al}
    out = [w for w, n in counts.most_common(60) if n >= 3 and w not in have and w not in skill_words]
    return out[:limit]


def _years(resume: dict) -> float:
    total = 0.0
    for e in resume["experience"]:
        y1 = re.findall(r"(?:19|20)\d{2}", e["start"] or "")
        y2 = re.findall(r"(?:19|20)\d{2}", e["end"] or "")
        if y1:
            end = int(y2[0]) if y2 else 2026
            total += max(end - int(y1[0]), 0.3)
    return total


def _summary_suggestion(resume: dict, title: str, company: str, matched: list[str]) -> str:
    role = clean_title(title)
    base = resume["basics"]["headline"] or ("Early-career professional" if _years(resume) < 2 else "Experienced professional")
    top = [display_skill(m) for m in matched[:4]]
    if len(top) >= 2:
        skills = ", ".join(top[:-1]) + f" and {top[-1]}"
        skill_phrase = f"hands-on experience in {skills}"
    elif top:
        skill_phrase = f"hands-on experience in {top[0]}"
    else:
        skill_phrase = "a strong foundation in the core fundamentals"
    n_proj = len(resume["projects"])
    evidence = (f" Backed by {n_proj} project{'s' if n_proj != 1 else ''}" if n_proj else "")
    if resume["experience"]:
        evidence += (" and " if evidence else " Backed by ") + f"{len(resume['experience'])} professional role{'s' if len(resume['experience']) != 1 else ''}"
    evidence = (evidence + " that map directly to the role.") if evidence else ""
    target = f" Targeting {role} opportunities" + (f" at {company}" if company else "") + "."
    return f"{base} with {skill_phrase}.{evidence}{target}".replace("..", ".")


def _fix_weak(bullet: str) -> str | None:
    low = bullet.lower().lstrip()
    for phrase, verb in WEAK_MAP:
        if low.startswith(phrase):
            rest = bullet.lstrip()[len(phrase):].lstrip(" :,-")
            if not rest:
                return None
            return f"{verb} {rest[0].lower() + rest[1:] if rest[:2].upper() != rest[:2] else rest}"
    return None


def analyze(resume: dict, title: str, company: str = "", description: str = "", llm_suggestions=None) -> dict:
    resume = normalize(resume)
    m = posting_match(resume, title, description, company)
    kw_names = [k["skill"] for k in m["keywords"]]
    sugg: list[dict] = []

    # headline
    role = clean_title(title)
    if role != "the role" and not (_tokens(role) & _tokens(resume["basics"]["headline"])):
        top = [display_skill(s) for s in m["matched"][:3]]
        new_hl = role + (" | " + " · ".join(top) if top else "")
        sugg.append({"id": "hl", "type": "headline", "section": "basics", "title": "Align your headline with the role",
                     "original": resume["basics"]["headline"], "suggested": new_hl,
                     "reason": f"Recruiters scan the headline first; yours does not mention '{role}'.",
                     "impact": "medium", "needs_input": False, "default_on": True, "source": "rules"})

    # summary
    new_sum = _summary_suggestion(resume, title, company, m["matched"])
    sugg.append({"id": "sum", "type": "summary", "section": "summary", "title": "Tailor your summary to this job",
                 "original": resume["summary"], "suggested": new_sum,
                 "reason": "Opens with the skills this posting emphasises that you already have.",
                 "impact": "high", "needs_input": False, "default_on": True, "source": "rules"})

    # skills order
    present = {s.lower(): s for s in resume["skills"]}
    lead = [present[k] for k in kw_names if k in present]
    rest = [s for s in resume["skills"] if s not in lead]
    if lead and (lead + rest) != resume["skills"]:
        sugg.append({"id": "skills_order", "type": "skills_order", "section": "skills",
                     "title": "Put the most relevant skills first",
                     "original": ", ".join(resume["skills"]), "suggested": ", ".join(lead + rest),
                     "reason": "ATS filters and recruiters weigh the first skills most.",
                     "impact": "medium", "needs_input": False, "default_on": True, "source": "rules"})

    # missing keywords (never on by default)
    for k in m["keywords"]:
        if k["skill"] in m["missing"] and k["skill"] not in present and (k["weight"] >= 2 or len(sugg) < 8):
            sugg.append({"id": f"skill:{k['skill']}", "type": "skill_add", "section": "skills",
                         "title": f"Add keyword: {display_skill(k['skill'])}", "original": "",
                         "suggested": display_skill(k["skill"]),
                         "reason": f"Mentioned {k['mentions']}x in the posting. Add it ONLY if you have genuinely used it.",
                         "impact": "high" if k["weight"] == 3 else "medium", "needs_input": False,
                         "default_on": False, "source": "rules"})

    # bullets
    seen_metric = 0
    for section, (i, j), text in iter_bullets(resume):
        if starts_weak(text):
            fixed = _fix_weak(text)
            if fixed:
                sugg.append({"id": f"verb:{section}:{i}:{j}", "type": "bullet_verb", "section": section,
                             "title": "Use a stronger opening verb", "original": text, "suggested": fixed,
                             "reason": "Weak openers hide ownership; action verbs signal impact.",
                             "impact": "medium", "needs_input": False, "default_on": True, "source": "rules",
                             "ref": [section, i, j]})
        elif not has_metric(text) and seen_metric < 3:
            seen_metric += 1
            sugg.append({"id": f"metric:{section}:{i}:{j}", "type": "bullet_metric", "section": section,
                         "title": "Add a measurable result", "original": text, "suggested": "",
                         "reason": "Bullets with numbers are far more persuasive. Only add a result that is true.",
                         "impact": "high", "needs_input": True,
                         "input_hint": "e.g. cut report time by 40%, served 2,000 users", "default_on": False,
                         "source": "rules", "ref": [section, i, j]})

    # LLM suggestions (optional)
    if llm_suggestions:
        if any(s["type"] == "summary" for s in llm_suggestions):
            sugg = [s for s in sugg if s["type"] != "summary"]
        replaced = {tuple(s.get("ref", [])) for s in llm_suggestions if s["type"] == "bullet_llm"}
        sugg = [s for s in sugg if not (s["type"] == "bullet_verb" and tuple(s.get("ref", [])) in replaced)]
        sugg = llm_suggestions + sugg

    order = {"high": 0, "medium": 1, "low": 2}
    sugg.sort(key=lambda s: (order[s["impact"]], not s["default_on"]))
    return {"match": m, "suggestions": sugg, "extra_terms": extra_terms(resume, description),
            "job": {"title": title, "company": company}}


def apply(resume: dict, suggestions: list[dict], accepted: set[str], inputs: dict[str, str] | None = None) -> dict:
    """Return a new resume with the accepted suggestions applied."""
    inputs = inputs or {}
    out = clone(normalize(resume))
    by_id = {s["id"]: s for s in suggestions}
    for sid in accepted:
        s = by_id.get(sid)
        if not s:
            continue
        t = s["type"]
        if t == "headline":
            out["basics"]["headline"] = s["suggested"][:160]
        elif t == "summary":
            out["summary"] = s["suggested"][:1200]
        elif t == "skills_order":
            order = [x.strip() for x in s["suggested"].split(",") if x.strip()]
            if set(map(str.lower, order)) == set(x.lower() for x in out["skills"]):
                out["skills"] = order
        elif t == "skill_add":
            if s["suggested"].lower() not in {x.lower() for x in out["skills"]}:
                out["skills"].append(s["suggested"])
        elif t in ("bullet_verb", "bullet_llm"):
            sec, i, j = s["ref"]
            try:
                out[sec][i]["bullets"][j] = s["suggested"][:500]
            except (IndexError, KeyError):
                pass
        elif t == "bullet_metric":
            extra = (inputs.get(sid) or "").strip()
            if extra:
                sec, i, j = s["ref"]
                try:
                    cur = out[sec][i]["bullets"][j].rstrip(" .")
                    out[sec][i]["bullets"][j] = f"{cur}, {extra.strip(' .')}"[:500]
                except (IndexError, KeyError):
                    pass
    return normalize(out)
