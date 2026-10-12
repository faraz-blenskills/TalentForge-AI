"""ATS-style resume quality score. Deterministic; every point is explained."""
from __future__ import annotations

import re

from .resume_struct import detected_skills, iter_bullets, to_text

STRONG_VERBS = set("""
achieved architected automated analysed analyzed built championed collaborated completed configured
conducted created cut debugged decreased delivered deployed designed developed drove enabled engineered
established evaluated executed expanded extended founded generated implemented improved increased
initiated integrated introduced launched led managed mentored migrated modelled modeled optimised optimized
orchestrated organised organized owned partnered planned presented produced reduced refactored released
researched resolved scaled secured shipped simplified solved spearheaded standardised streamlined supported
tested trained transformed tuned upgraded validated wrote
""".split())
WEAK_STARTERS = ("responsible for", "worked on", "worked with", "helped", "assisted", "involved in",
                 "participated in", "duties included", "tasked with", "was part of", "handled")
METRIC_RE = re.compile(r"(\d[\d,.]*\s?(%|x\b|k\b|m\b|\+|percent|users|ms\b|seconds|hours|days|customers|clients|"
                       r"requests|records|rows|tests|apis?|models?|students))|[$₹€£]\s?\d|\b\d{2,}\b", re.I)
FIRST_PERSON_RE = re.compile(r"\b(i|my|me|myself)\b", re.I)


def _words(s: str) -> int:
    return len(re.findall(r"\b\w+\b", s))


def _first_word(s: str) -> str:
    m = re.match(r"\W*([A-Za-z\-]+)", s)
    return m.group(1).lower() if m else ""


def starts_weak(bullet: str) -> bool:
    b = bullet.strip().lower()
    return any(b.startswith(w) for w in WEAK_STARTERS)


def starts_strong(bullet: str) -> bool:
    fw = _first_word(bullet)
    return fw in STRONG_VERBS or (fw.endswith("ed") and len(fw) > 4 and not starts_weak(bullet))


def has_metric(bullet: str) -> bool:
    return bool(METRIC_RE.search(bullet))


def _check(id_: str, label: str, max_pts: int) -> dict:
    return {"id": id_, "label": label, "max": max_pts, "points": 0.0, "findings": [], "fixes": []}


def score_resume(r: dict) -> dict:
    checks: list[dict] = []
    b = r["basics"]

    # 1. contact & basics (12)
    c = _check("basics", "Contact details", 12)
    for key, pts, fix in (("name", 3, "Add your full name."), ("email", 3, "Add a professional email address."),
                          ("phone", 3, "Add a phone number recruiters can call.")):
        if b.get(key):
            c["points"] += pts
        else:
            c["fixes"].append(fix)
    if b.get("location") or b.get("links"):
        c["points"] += 3
    else:
        c["fixes"].append("Add your city and a LinkedIn or GitHub link.")
    c["findings"].append("Contact details look complete." if not c["fixes"] else "Some contact details are missing.")
    checks.append(c)

    # 2. summary (8)
    c = _check("summary", "Professional summary", 8)
    wc = _words(r["summary"])
    if wc:
        c["points"] += 4
        if 20 <= wc <= 80:
            c["points"] += 4
            c["findings"].append(f"Summary length is good ({wc} words).")
        else:
            c["findings"].append(f"Summary is {wc} words; 20-80 reads best.")
            c["fixes"].append("Rewrite the summary in 2-3 sentences (20-80 words).")
    else:
        c["findings"].append("No summary found.")
        c["fixes"].append("Add a 2-3 sentence summary naming your target role and strongest skills.")
    checks.append(c)

    # 3. experience / projects (20)
    c = _check("experience", "Experience and projects", 20)
    exp, proj = r["experience"], r["projects"]
    exp_pts = 0.0
    if exp:
        with_bullets = sum(1 for e in exp if len(e["bullets"]) >= 2)
        exp_pts = 8 + 6 * (with_bullets / len(exp))
        exp_pts += 3 if all(e["start"] for e in exp) else 0
        exp_pts += 3 if all(e["role"] and e["company"] for e in exp) else 0
        if with_bullets < len(exp):
            c["fixes"].append("Give each role at least two bullets that describe what you did and the result.")
        if not all(e["start"] for e in exp):
            c["fixes"].append("Add start and end dates to every role.")
    proj_pts = 0.0
    if proj:
        proj_pts = 6 * min(len(proj), 3) / 3 + 8 * (sum(1 for p in proj if p["bullets"] or p["description"]) / len(proj))
        if len(proj) < 2:
            c["fixes"].append("Add at least two projects that show applied skills.")
    c["points"] = round(min(max(exp_pts, proj_pts, (exp_pts + proj_pts) * 0.8), 20), 1)
    c["findings"].append(f"{len(exp)} role(s) and {len(proj)} project(s) found.")
    if not exp and not proj:
        c["fixes"].append("Add work experience, internships or projects - recruiters need evidence of what you built.")
    checks.append(c)

    # 4. impact (20): action verbs + quantified results
    c = _check("impact", "Impact and wording", 20)
    bullets = [(s, p, t) for s, p, t in iter_bullets(r)]
    if bullets:
        strong = sum(1 for _, _, t in bullets if starts_strong(t))
        weak = sum(1 for _, _, t in bullets if starts_weak(t))
        metric = sum(1 for _, _, t in bullets if has_metric(t))
        n = len(bullets)
        c["points"] = round(10 * min(strong / n / 0.7, 1) + 10 * min(metric / n / 0.4, 1), 1)
        c["findings"].append(f"{strong}/{n} bullets open with a strong action verb; {metric}/{n} include a number or result.")
        if weak:
            c["fixes"].append(f"Replace weak openers like 'Responsible for' or 'Worked on' ({weak} bullet(s)).")
        if metric / n < 0.4:
            c["fixes"].append("Quantify results: add numbers (%, time saved, users, size of data) to at least 40% of bullets.")
        if strong / n < 0.7:
            c["fixes"].append("Start bullets with verbs such as Built, Reduced, Led, Automated, Designed.")
    else:
        c["findings"].append("No bullet points to evaluate.")
        c["fixes"].append("Describe your work as short bullets that start with an action verb.")
    checks.append(c)

    # 5. skills (15)
    c = _check("skills", "Skills", 15)
    sk = r["skills"]
    known = detected_skills(r)
    if len(sk) >= 8:
        c["points"] += 10
    elif len(sk) >= 5:
        c["points"] += 7
    elif sk:
        c["points"] += 3
    c["points"] += 5 * min(len(known) / 8, 1)
    c["findings"].append(f"{len(sk)} skills listed; {len(known)} recognised industry keywords found in the text.")
    if len(sk) < 8:
        c["fixes"].append("List at least 8 relevant skills (tools, languages, frameworks).")
    if len(known) < 5:
        c["fixes"].append("Use standard keyword spellings (e.g. 'JavaScript', 'PostgreSQL') so ATS filters match them.")
    c["points"] = round(c["points"], 1)
    checks.append(c)

    # 6. education (10)
    c = _check("education", "Education", 10)
    ed = r["education"]
    if ed:
        c["points"] += 6
        c["points"] += 2 if any(e["year"] for e in ed) else 0
        c["points"] += 2 if any(e["degree"] for e in ed) else 0
        if not any(e["year"] for e in ed):
            c["fixes"].append("Add the graduation year.")
        c["findings"].append(f"{len(ed)} education entr{'y' if len(ed) == 1 else 'ies'} found.")
    else:
        c["findings"].append("No education found.")
        c["fixes"].append("Add your degree, institution and year.")
    checks.append(c)

    # 7. hygiene (15)
    c = _check("hygiene", "Length and hygiene", 15)
    text = to_text(r)
    total_words = _words(text)
    if 250 <= total_words <= 900:
        c["points"] += 5
        c["findings"].append(f"Length is appropriate ({total_words} words).")
    else:
        c["findings"].append(f"Resume is {total_words} words; aim for 250-900.")
        c["fixes"].append("Aim for one page for under 5 years of experience (about 300-700 words).")
    if not FIRST_PERSON_RE.search(" ".join(t for _, _, t in bullets) + " " + r["summary"]):
        c["points"] += 3
    else:
        c["fixes"].append("Remove first-person words (I, my) from bullets and summary.")
    long_b = [t for _, _, t in bullets if _words(t) > 30]
    if bullets and not long_b:
        c["points"] += 4
    elif long_b:
        c["points"] += 1
        c["fixes"].append(f"Shorten {len(long_b)} bullet(s) longer than 30 words.")
    else:
        c["points"] += 2
    if not re.search(r"\[[^\]]*\]|\(simulated|lorem ipsum|todo", text, re.I):
        c["points"] += 3
    else:
        c["fixes"].append("Remove placeholder text such as [add metric].")
    checks.append(c)

    total = round(sum(c["points"] for c in checks), 1)
    grade = ("Excellent" if total >= 85 else "Good" if total >= 70 else "Fair" if total >= 50 else "Needs work")
    for c in checks:
        c["points"] = round(c["points"], 1)
        c["status"] = "good" if c["points"] >= c["max"] * 0.8 else "ok" if c["points"] >= c["max"] * 0.5 else "low"
    fixes = [f for c in sorted(checks, key=lambda x: x["points"] / x["max"]) for f in c["fixes"]]
    return {"score": total, "grade": grade, "checks": checks, "top_fixes": fixes[:6]}
