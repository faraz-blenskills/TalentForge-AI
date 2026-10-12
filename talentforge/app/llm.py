"""Plain-language explanations.

Scores, rankings and factors are always computed by deterministic code (scoring.py / matching.py).
The LLM, when configured, only *phrases* those facts. Without an API key - or on any error or
refusal - a template is used, so the app works fully offline.
"""
from __future__ import annotations

import hashlib
import json
import os

from . import db

MODEL = "claude-opus-5"

SYSTEM = (
    "You write short, plain-language explanations for a hiring-support tool. "
    "Use ONLY the facts in the JSON the user provides. Do not invent skills, employers or numbers. "
    "Never infer or mention gender, age, ethnicity, nationality or other personal attributes. "
    "Do not make or imply a final hiring decision; a human decides. "
    "Write 3-4 sentences, friendly and specific, no headings, no bullet points."
)

INSTRUCTIONS = {
    "score": "Explain this employability result to the candidate: what is strong, what holds the score back, and the single most useful next step.",
    "candidate": "Summarise this candidate's fit for the job for a recruiter: strongest evidence, main gaps, and what to verify in an interview.",
    "feedback": "Write constructive, respectful feedback for a candidate who was not shortlisted: which required skills were missing and what would strengthen a future application.",
    "job_match": "Explain to the candidate why this job matched their profile and what they would still need to cover.",
}


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("TALENTFORGE_LLM", "on") != "off"


def _template(kind: str, f: dict) -> str:
    if kind == "score":
        weak = [x for x in f.get("factors", []) if x["value"] < 0.6]
        strong = [x for x in f.get("factors", []) if x["value"] >= 0.8]
        parts = [f"Your employability score for {f['role']} is {f['score']} out of 100 ({f['band']})."]
        if strong:
            parts.append("Strongest areas: " + ", ".join(x["label"].lower() for x in strong[:2]) + ".")
        if weak:
            parts.append("Held back by: " + ", ".join(x["label"].lower() for x in weak[:2]) + ".")
        if f.get("top_missing"):
            parts.append(f"The most valuable next step is to learn {f['top_missing'][0]}"
                         + (f", then {f['top_missing'][1]}." if len(f['top_missing']) > 1 else "."))
        return " ".join(parts)
    if kind == "candidate":
        parts = [f"Match score {f['score']} for {f['job']} (AI suggests: {f['recommendation']})."]
        if f.get("matched"):
            parts.append("Evidence found for " + ", ".join(f["matched"][:4]) + ".")
        if f.get("missing"):
            parts.append("Not evidenced: " + ", ".join(f["missing"][:4]) + " - worth probing in an interview.")
        parts.append("This is decision support only; the final call is yours.")
        return " ".join(parts)
    if kind == "feedback":
        parts = [f"Thank you for applying for {f['job']}."]
        if f.get("missing"):
            parts.append("For this role we were looking for stronger evidence of " + ", ".join(f["missing"][:3]) + ".")
        parts.append("Adding a project or certification that shows these skills would strengthen a future application.")
        return " ".join(parts)
    if kind == "job_match":
        parts = [f"This role matched your profile at {f['score']} out of 100."]
        if f.get("matched"):
            parts.append("It lines up with your skills in " + ", ".join(f["matched"][:4]) + ".")
        if f.get("missing"):
            parts.append("You would still need to cover " + ", ".join(f["missing"][:3]) + ".")
        return " ".join(parts)
    return ""


def _call_claude(kind: str, facts: dict) -> str | None:
    import anthropic

    client = anthropic.Anthropic()
    payload = json.dumps(facts, sort_keys=True, default=str)
    kwargs = dict(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": f"{INSTRUCTIONS[kind]}\n\nFacts:\n{payload}"}],
    )
    c = client.with_options(timeout=30.0, max_retries=1)
    try:
        resp = c.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:  # older SDK without the fallbacks parameter
        resp = c.messages.create(**kwargs)
    if getattr(resp, "stop_reason", None) == "refusal":
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return text or None


def narrate(kind: str, facts: dict) -> tuple[str, str]:
    """Return (text, source) where source is 'claude' or 'template'."""
    key = hashlib.sha256((kind + json.dumps(facts, sort_keys=True, default=str)).encode()).hexdigest()
    hit = db.cache_get(key)
    if hit and (hit[1] == "claude" or not enabled()):
        return hit
    if enabled():
        try:
            text = _call_claude(kind, facts)
            if text:
                db.cache_put(key, text, "claude")
                return text, "claude"
        except Exception:  # network, auth, rate limit... never break the page
            pass
    text = _template(kind, facts)
    db.cache_put(key, text, "template")
    return text, "template"
