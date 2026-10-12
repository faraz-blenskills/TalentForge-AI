"""Optional Claude-backed suggestions (tailoring and search planning).

Everything here is best-effort: without ANTHROPIC_API_KEY, or on any error/refusal, the callers fall back to
the deterministic rules. Output is validated so the model cannot invent numbers or reference missing bullets.
"""
from __future__ import annotations

import json
import re

from app import llm

from .resume_struct import to_text

MODEL = llm.MODEL

SYSTEM_TAILOR = (
    "You help a job seeker tailor their EXISTING resume to one job posting. "
    "Rules: use only facts already present in the resume; never invent employers, tools, achievements or numbers; "
    "do not add skills the resume does not show; keep each rewritten bullet under 30 words and start it with a "
    "strong action verb; do not use first-person pronouns. Prefer rephrasing and emphasising what is relevant to "
    "the posting. Return JSON only."
)

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "bullet_rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["experience", "projects"]},
                    "entry": {"type": "integer"},
                    "bullet": {"type": "integer"},
                    "text": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["section", "entry", "bullet", "text", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "bullet_rewrites"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}


def enabled() -> bool:
    return llm.enabled()


def _complete_json(system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict | None:
    import anthropic

    client = anthropic.Anthropic().with_options(timeout=60.0, max_retries=1)
    kwargs = dict(model=MODEL, max_tokens=max_tokens, system=system,
                  output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                  messages=[{"role": "user", "content": user}])
    try:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        resp = client.messages.create(**kwargs)
    if getattr(resp, "stop_reason", None) == "refusal":
        return None
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    return json.loads(text) if text else None


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*", s))


def tailor_suggestions(resume: dict, title: str, company: str, description: str) -> list[dict]:
    if not enabled():
        return []
    try:
        payload = {"resume": resume, "job": {"title": title, "company": company, "description": description[:5000]}}
        data = _complete_json(SYSTEM_TAILOR, json.dumps(payload), TAILOR_SCHEMA)
    except Exception:
        return []
    if not data:
        return []
    out: list[dict] = []
    resume_numbers = _numbers(to_text(resume))
    summary = (data.get("summary") or "").strip()
    if summary and len(summary) <= 700 and _numbers(summary) <= resume_numbers:
        out.append({"id": "llm_sum", "type": "summary", "section": "summary", "title": "Tailor your summary (AI)",
                    "original": resume.get("summary", ""), "suggested": summary,
                    "reason": "Written by Claude from your resume facts and this posting.",
                    "impact": "high", "needs_input": False, "default_on": True, "source": "claude"})
    for i, b in enumerate(data.get("bullet_rewrites") or []):
        sec, ei, bi = b.get("section"), b.get("entry"), b.get("bullet")
        try:
            original = resume[sec][ei]["bullets"][bi]
        except (KeyError, IndexError, TypeError):
            continue
        text = (b.get("text") or "").strip()
        if not text or len(text.split()) > 40 or text == original:
            continue
        if not _numbers(text) <= _numbers(original) | resume_numbers:   # no invented metrics
            continue
        out.append({"id": f"llm_b:{sec}:{ei}:{bi}", "type": "bullet_llm", "section": sec,
                    "title": "Rephrase for this role (AI)", "original": original, "suggested": text,
                    "reason": (b.get("reason") or "Emphasises what this posting asks for.")[:200],
                    "impact": "medium", "needs_input": False, "default_on": True, "source": "claude",
                    "ref": [sec, ei, bi]})
        if len(out) >= 8:
            break
    return out


def plan_queries(query: str, skills: list[str], level: str) -> list[str]:
    if not enabled():
        return []
    try:
        data = _complete_json(
            "Suggest up to 4 alternative job-search queries (job titles only, 1-4 words each) that a recruiter "
            "might use for the same kind of role. Return JSON only.",
            json.dumps({"target": query, "skills": skills[:15], "level": level}), PLAN_SCHEMA, max_tokens=1000)
    except Exception:
        return []
    return [q.strip() for q in (data or {}).get("queries", []) if isinstance(q, str) and 2 < len(q) <= 60][:4]
