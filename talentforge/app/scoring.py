"""Skill-gap analysis, employability scoring and what-if simulation.

Every number returned carries the factors and evidence that produced it (the XAI object).
Identity fields (name, email, institution) are never used as inputs.
"""
from __future__ import annotations

import copy

from .taxonomy import ROLES, expand_with_implied, learning_tip, related_to

WEIGHTS = {
    "skills": 0.45,
    "experience": 0.20,
    "projects": 0.15,
    "education": 0.10,
    "certifications": 0.05,
    "completeness": 0.05,
}
LABELS = {
    "skills": "Skill coverage for target role",
    "experience": "Relevant experience",
    "projects": "Projects",
    "education": "Education",
    "certifications": "Certifications",
    "completeness": "Profile completeness",
}


def skill_names(profile: dict) -> set[str]:
    return {s["name"] for s in profile.get("skills", [])}


def skill_evidence(profile: dict) -> dict[str, str]:
    return {s["name"]: s.get("evidence", "") for s in profile.get("skills", [])}


def skill_gap(profile: dict, required: dict[str, int], preferred: list[str] | None = None) -> dict:
    have = skill_names(profile)
    expanded = expand_with_implied(have)
    evidence = skill_evidence(profile)
    items = []
    total_w = sum(required.values()) or 1
    got = 0.0
    for skill, w in sorted(required.items(), key=lambda kv: (-kv[1], kv[0])):
        if skill in expanded:
            direct = skill in have
            items.append({
                "skill": skill, "weight": w, "status": "matched", "credit": 1.0,
                "via": None if direct else "implied by a more specific skill",
                "evidence": evidence.get(skill, ""), "tip": "",
            })
            got += w
            continue
        partial_via = sorted(related_to(skill) & expanded)
        if partial_via:
            items.append({
                "skill": skill, "weight": w, "status": "partial", "credit": 0.5,
                "via": f"related skill: {partial_via[0]}", "evidence": evidence.get(partial_via[0], ""),
                "tip": learning_tip(skill),
            })
            got += 0.5 * w
        else:
            items.append({
                "skill": skill, "weight": w, "status": "missing", "credit": 0.0,
                "via": None, "evidence": "", "tip": learning_tip(skill),
            })
    pref_items = []
    pref_got = 0.0
    for skill in preferred or []:
        has = skill in expanded
        pref_items.append({"skill": skill, "status": "matched" if has else "missing"})
        pref_got += 1.0 if has else 0.0
    return {
        "coverage": got / total_w,
        "preferred_coverage": (pref_got / len(preferred)) if preferred else 0.0,
        "items": items,
        "matched": [i for i in items if i["status"] == "matched"],
        "partial": [i for i in items if i["status"] == "partial"],
        "missing": [i for i in items if i["status"] == "missing"],
        "preferred": pref_items,
    }


def _education_value(profile: dict) -> tuple[float, str]:
    lvl = profile.get("highest_degree_level", 0)
    cs = profile.get("cs_related", False)
    if lvl >= 3:
        return 1.0, "Postgraduate degree detected"
    if lvl == 2:
        return (1.0, "Bachelor's degree in a relevant field") if cs else (0.7, "Bachelor's degree (field not clearly technical)")
    if lvl == 1:
        return 0.5, "Diploma level detected"
    return 0.3, "No degree detected - add your education to the profile"


def _completeness(profile: dict) -> tuple[float, str]:
    checks = {
        "name": bool(profile.get("name")),
        "email": bool(profile.get("email")),
        "education": bool(profile.get("education")),
        "5+ skills": len(profile.get("skills", [])) >= 5,
        "projects or experience": bool(profile.get("projects")) or profile.get("experience_years", 0) > 0,
    }
    miss = [k for k, v in checks.items() if not v]
    val = sum(checks.values()) / len(checks)
    return val, ("Complete" if not miss else "Missing: " + ", ".join(miss))


def employability(profile: dict, role_id: str) -> dict:
    role = ROLES[role_id]
    gap = skill_gap(profile, role["required"], role["preferred"])
    years = float(profile.get("experience_years", 0) or 0)
    n_proj = len(profile.get("projects", []))
    n_cert = len(profile.get("certifications", []))
    edu_val, edu_note = _education_value(profile)
    comp_val, comp_note = _completeness(profile)

    raw = {
        "skills": (gap["coverage"],
                   f"{len(gap['matched'])} matched, {len(gap['partial'])} partial, {len(gap['missing'])} missing "
                   f"of {len(gap['items'])} required skills"),
        "experience": (min(years / role["target_years"], 1.0),
                       f"{years:g} year(s) detected; target for this role is {role['target_years']}"),
        "projects": (min(n_proj / 3, 1.0), f"{n_proj} project(s) detected; 3 or more earns full credit"),
        "education": (edu_val, edu_note),
        "certifications": (min(n_cert / 2, 1.0), f"{n_cert} certification(s) detected; 2 or more earns full credit"),
        "completeness": (comp_val, comp_note),
    }
    tips = {
        "skills": "Close the highest-weight missing skills first (see the skill-gap report).",
        "experience": "Add internships, freelance or open-source contributions with dates.",
        "projects": "Add projects with a one-line outcome each; aim for three.",
        "education": "Make sure your degree, field and year are clearly listed.",
        "certifications": "One relevant, verifiable certification is enough to move this factor.",
        "completeness": "Fill in the missing profile fields.",
    }
    factors = []
    score = 0.0
    for key, w in WEIGHTS.items():
        val, detail = raw[key]
        pts = round(val * w * 100, 1)
        score += val * w * 100
        factors.append({
            "key": key, "label": LABELS[key], "value": round(val, 3), "weight": w,
            "points": pts, "max_points": round(w * 100, 1), "detail": detail,
            "tip": tips[key] if val < 0.999 else "",
        })
    score = round(score, 1)
    band = "Strong" if score >= 75 else "Developing" if score >= 55 else "Needs work"
    return {
        "role_id": role_id, "role_title": role["title"], "score": score, "band": band,
        "factors": factors, "gap": gap,
        "method": "Weighted factor model (weights shown); no name, gender, age, photo or institution is used.",
    }


def what_if(profile: dict, role_id: str, add_skills: list[str], add_projects: int,
            add_certs: int, add_years: float) -> dict:
    base = employability(profile, role_id)
    p = copy.deepcopy(profile)
    have = skill_names(p)
    for s in add_skills:
        if s and s not in have:
            p["skills"].append({"name": s, "evidence": "(simulated)", "mentions": 1})
    p["projects"] = list(p.get("projects", [])) + [f"(simulated project {i + 1})" for i in range(add_projects)]
    p["certifications"] = list(p.get("certifications", [])) + [f"(simulated certification {i + 1})" for i in range(add_certs)]
    p["experience_years"] = float(p.get("experience_years", 0) or 0) + add_years
    new = employability(p, role_id)
    deltas = []
    for b, n in zip(base["factors"], new["factors"]):
        d = round(n["points"] - b["points"], 1)
        if d:
            deltas.append({"label": n["label"], "delta": d})
    return {"base": base["score"], "new": new["score"], "delta": round(new["score"] - base["score"], 1),
            "deltas": deltas, "new_band": new["band"]}


def learning_suggestions(gap: dict, limit: int = 6) -> list[dict]:
    todo = [i for i in gap["items"] if i["status"] in ("missing", "partial")]
    todo.sort(key=lambda i: (-i["weight"], i["status"] != "missing", i["skill"]))
    return [{"skill": i["skill"], "weight": i["weight"], "status": i["status"], "tip": i["tip"]}
            for i in todo[:limit]]
