"""Job matching, candidate ranking and identity-blind (anonymised) views."""
from __future__ import annotations

import copy
import math
import re
from collections import Counter

from .scoring import skill_gap

STOP = set(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with "
    "you your we our they their i he she not but if so than then into over under per using use used work "
    "working experience years year team teams strong good ability skills skill knowledge looking role job".split()
)
WORD_RE = re.compile(r"[a-z][a-z0-9+#.\-]{1,}")


def _tokens(text: str) -> list[str]:
    return [t.strip(".-") for t in WORD_RE.findall(text.lower()) if t.strip(".-") not in STOP]


def text_similarity(a: str, b: str) -> tuple[float, list[str]]:
    """Cosine similarity of sub-linear term-frequency vectors, plus top shared terms."""
    ca, cb = Counter(_tokens(a)), Counter(_tokens(b))
    if not ca or not cb:
        return 0.0, []
    va = {t: 1 + math.log(c) for t, c in ca.items()}
    vb = {t: 1 + math.log(c) for t, c in cb.items()}
    dot = sum(va[t] * vb[t] for t in va.keys() & vb.keys())
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    sim = dot / (na * nb) if na and nb else 0.0
    shared = sorted(va.keys() & vb.keys(), key=lambda t: -(va[t] * vb[t]))[:6]
    return sim, shared


_INSTITUTION_RE = re.compile(r"universit|institute|college|school of|academy", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")


def scrub_identity(text: str, profile: dict) -> str:
    """Remove name, email, phone and institution lines so they cannot influence text similarity."""
    name = (profile.get("name") or "").strip()
    out = []
    for ln in (text or "").splitlines():
        if _INSTITUTION_RE.search(ln):
            continue
        ln = _EMAIL_RE.sub(" ", ln)
        ln = _PHONE_RE.sub(" ", ln)
        if name:
            ln = re.sub(re.escape(name), " ", ln, flags=re.I)
        out.append(ln)
    return "\n".join(out)


def recommendation(score: float) -> str:
    if score >= 70:
        return "shortlist"
    if score >= 50:
        return "review"
    return "reject"


def match_job(profile: dict, resume_text: str, job: dict) -> dict:
    """job: {title, description, required: {skill: weight}, preferred: [..], min_years}"""
    gap = skill_gap(profile, job["required"], job.get("preferred", []))
    sim, shared = text_similarity(scrub_identity(resume_text or "", profile),
                                  f"{job['title']} {job.get('description', '')}")
    min_years = float(job.get("min_years", 0) or 0)
    years = float(profile.get("experience_years", 0) or 0)
    exp_fit = 1.0 if min_years <= 0 else min(years / min_years, 1.0)

    parts = [
        ("Required skills", 0.60, gap["coverage"],
         f"{len(gap['matched'])} matched, {len(gap['partial'])} partial, {len(gap['missing'])} missing"),
        ("Preferred skills", 0.15, gap["preferred_coverage"],
         f"{sum(1 for p in gap['preferred'] if p['status'] == 'matched')} of {len(gap['preferred'])} present"),
        ("Resume-to-job text similarity", 0.15, min(sim * 2.0, 1.0),
         "Shared terms: " + (", ".join(shared) if shared else "none")),
        ("Experience fit", 0.10, exp_fit,
         f"{years:g} year(s) vs {min_years:g} requested" if min_years else "No minimum experience requested"),
    ]
    factors, score = [], 0.0
    for label, w, val, detail in parts:
        pts = round(val * w * 100, 1)
        score += val * w * 100
        factors.append({"label": label, "weight": w, "value": round(val, 3), "points": pts,
                        "max_points": round(w * 100, 1), "detail": detail})
    score = round(score, 1)
    return {
        "score": score,
        "recommendation": recommendation(score),
        "factors": factors,
        "matched": gap["matched"],
        "partial": gap["partial"],
        "missing": gap["missing"],
        "preferred": gap["preferred"],
        "shared_terms": shared,
        "method": "Skill coverage (taxonomy-aware) + text similarity; identity fields are not used.",
    }


def anonymise(profile: dict, label: str) -> dict:
    """Return a copy with identity fields removed. Scoring never reads them anyway."""
    p = copy.deepcopy(profile)
    p["name"] = label
    p["email"] = ""
    level_names = {4: "Doctorate", 3: "Postgraduate degree", 2: "Bachelor's degree", 1: "Diploma"}
    p["education"] = [
        {"text": level_names.get(e.get("level", 0), "Degree"), "level": e.get("level", 0),
         "year": e.get("year", ""), "cs_related": e.get("cs_related", False)}
        for e in p.get("education", [])
    ]
    return p


def rank_candidates(job: dict, candidates: list[dict]) -> list[dict]:
    """candidates: [{application_id, profile, resume_text}] -> sorted with rank + match."""
    out = []
    for c in candidates:
        m = match_job(c["profile"], c.get("resume_text", ""), job)
        out.append({**c, "match": m})
    out.sort(key=lambda r: (-r["match"]["score"], r["application_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def identity_blind_check(job: dict, candidates: list[dict]) -> dict:
    """Re-run ranking on anonymised profiles and compare order with the normal ranking."""
    normal = [r["application_id"] for r in rank_candidates(job, candidates)]
    blind_in = [
        {**c, "profile": anonymise(c["profile"], f"Candidate {c['application_id']}"),
         "resume_text": c.get("resume_text", "")}
        for c in candidates
    ]
    blind = [r["application_id"] for r in rank_candidates(job, blind_in)]
    return {"identical": normal == blind, "n": len(normal)}
