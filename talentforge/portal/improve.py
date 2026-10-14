"""Score-improvement plan: what to change, and how many points each change is worth.

Items are of three kinds:
  auto   - safe, factual rewrites we can apply for you (verb upgrades, skills already in the text, spelling)
  input  - need one fact only the candidate knows (a phone number, a result, a year); pre-filled where possible
  manual - need real content (another project, more bullets); we point to the builder

Gains are measured by actually re-scoring the resume with the change applied, so they are not guesses.
"""
from __future__ import annotations

import re

from app.parsing import _find_skills

from .resume_score import has_metric, score_resume, starts_strong, starts_weak
from .resume_struct import clone, iter_bullets, normalize, to_text
from .tailor import _fix_weak, display_skill

TARGET = 99
DEMO = {"phone": "+91 90000 00000", "location": "Pune, India", "year": "2026", "result": "improving results by 20%"}


def _words(s: str) -> int:
    return len(re.findall(r"\b\w+\b", s))


def _truncate_words(s: str, n: int) -> str:
    ms = list(re.finditer(r"\b\w+\b", s))
    return s[: ms[n - 1].end()] if len(ms) > n else s


def _draft_summary(r: dict) -> str:
    """A factual summary built only from what the resume already says."""
    b = r["basics"]
    role = b["headline"].split("|")[0].strip() or (r["experience"][0]["role"] if r["experience"] else "")
    role = role or "Early-career professional"
    skills = [display_skill(s) if s.lower() in {x.lower() for x in r["skills"]} else s for s in r["skills"][:4]]
    parts = [role]
    if skills:
        parts.append("with hands-on experience in " + (", ".join(skills[:-1]) + " and " + skills[-1]
                                                      if len(skills) > 1 else skills[0]))
    sentence = " ".join(parts) + "."
    facts = []
    if r["experience"]:
        facts.append(f"{len(r['experience'])} professional role{'s' if len(r['experience']) != 1 else ''}")
    if r["projects"]:
        facts.append(f"{len(r['projects'])} project{'s' if len(r['projects']) != 1 else ''}")
    if facts:
        sentence += " Backed by " + " and ".join(facts) + " that demonstrate applied, results-oriented work."
    return sentence


def _item(id_, kind, check, title, detail, fields=None, changes=None):
    return {"id": id_, "kind": kind, "check": check, "title": title, "detail": detail,
            "fields": fields or [], "changes": changes or [], "gain": 0.0, "default_on": kind == "auto"}


# ----------------------------------------------------------------------------- applying
def _apply_item(r: dict, item: dict, inputs: dict[str, str]) -> None:
    iid = item["id"]
    if iid.startswith("contact:"):
        key = iid.split(":", 1)[1]
        v = (inputs.get(iid) or "").strip()
        if v:
            if key == "links":
                r["basics"]["links"] = [x.strip() for x in re.split(r"[,\n]", v) if x.strip()][:5]
            else:
                r["basics"][key] = v[:160]
    elif iid == "summary":
        v = (inputs.get(iid) or "").strip()
        if v:
            r["summary"] = v[:1500]
    elif iid == "verbs":
        for ch in item["changes"]:
            sec, i, j = ch["ref"]
            try:
                r[sec][i]["bullets"][j] = ch["after"]
            except (IndexError, KeyError):
                pass
    elif iid.startswith("metric:"):
        _, sec, i, j = iid.split(":")
        v = (inputs.get(iid) or "").strip(" .")
        if v:
            try:
                cur = r[sec][int(i)]["bullets"][int(j)].rstrip(" .")
                r[sec][int(i)]["bullets"][int(j)] = f"{cur}, {v}"[:500]
            except (IndexError, KeyError):
                pass
    elif iid == "skills_found":
        have = {s.lower() for s in r["skills"]}
        for s in item["changes"]:
            if s["after"].lower() not in have:
                r["skills"].append(s["after"])
                have.add(s["after"].lower())
    elif iid == "skills_normalize":
        mapping = {c["before"].lower(): c["after"] for c in item["changes"]}
        r["skills"] = [mapping.get(s.lower(), s) for s in r["skills"]]
    elif iid.startswith("edu_year:"):
        i = int(iid.split(":")[1])
        v = (inputs.get(iid) or "").strip()
        if v and 0 <= i < len(r["education"]):
            r["education"][i]["year"] = v[:60]
    elif iid == "placeholders":
        r["summary"] = re.sub(r"\[[^\]]*\]", "", r["summary"]).strip()
        for sec in ("experience", "projects"):
            for e in r[sec]:
                e["bullets"] = [re.sub(r"\s*\[[^\]]*\]", "", b).strip() for b in e["bullets"]]


def _simulate(r: dict, items: list[dict], inputs: dict[str, str]) -> float:
    clone_r = clone(r)
    for it in items:
        _apply_item(clone_r, it, inputs)
    return score_resume(normalize(clone_r))["score"]


# ----------------------------------------------------------------------------- plan
def build_plan(resume: dict) -> dict:
    r = normalize(resume)
    base = score_resume(r)
    b = r["basics"]
    items: list[dict] = []

    # --- contact details
    for key, label, ph in (("name", "Full name", "Your full name"), ("email", "Email address", "name@example.com"),
                           ("phone", "Phone number", "+91 90000 00000")):
        if not b.get(key):
            items.append(_item(f"contact:{key}", "input", "basics", f"Add your {label.lower()}",
                               "Recruiters and ATS systems need this to reach you.",
                               [{"id": f"contact:{key}", "label": label, "placeholder": ph, "value": ""}]))
    if not b.get("location") and not b.get("links"):
        items.append(_item("contact:location", "input", "basics", "Add your city and a professional link",
                           "A location and a LinkedIn/GitHub link complete the contact block.",
                           [{"id": "contact:location", "label": "City, Country", "placeholder": "Pune, India",
                             "value": ""},
                            {"id": "contact:links", "label": "LinkedIn / GitHub", "placeholder":
                             "linkedin.com/in/your-name", "value": ""}]))

    # --- summary
    wc = _words(r["summary"])
    if wc == 0 or not (20 <= wc <= 80):
        why = ("You have no summary." if wc == 0 else
               f"Your summary is {wc} words; 20-80 reads best.")
        title = "Add a focused 2-3 sentence summary" if wc == 0 else (
            "Tighten your summary to 2-3 sentences" if wc > 80 else "Expand your summary to 2-3 sentences")
        items.append(_item("summary", "input", "summary", title,
                           why + " This draft uses only facts already on your resume - edit it freely.",
                           [{"id": "summary", "label": "Summary", "textarea": True, "value": _draft_summary(r),
                             "placeholder": ""}]))

    # --- impact: weak openers (auto) and missing results (input)
    changes = []
    for sec, (i, j), t in iter_bullets(r):
        if starts_weak(t):
            fixed = _fix_weak(t)
            if fixed:
                changes.append({"ref": [sec, i, j], "before": t, "after": fixed})
    if changes:
        items.append(_item("verbs", "auto", "impact", f"Strengthen {len(changes)} weak bullet opener"
                           f"{'s' if len(changes) != 1 else ''}",
                           "Replaces phrases like 'Responsible for' or 'Worked on' with action verbs.",
                           changes=changes))
    bullets = list(iter_bullets(r))
    need_metrics = max(0, int(0.4 * len(bullets) + 0.999) - sum(1 for _, _, t in bullets if has_metric(t)))
    shown = 0
    for sec, (i, j), t in bullets:
        if shown >= max(need_metrics, 0):
            break
        if not has_metric(t):
            shown += 1
            items.append(_item(f"metric:{sec}:{i}:{j}", "input", "impact", "Add a measurable result",
                               f"\"{t[:110]}{'...' if len(t) > 110 else ''}\" - add a number: %, time saved, "
                               "users, size of data. Only add what is true.",
                               [{"id": f"metric:{sec}:{i}:{j}", "label": "Result", "placeholder":
                                 "e.g. cutting report time by 40%", "value": ""}]))
    long_b = [t for _, _, t in bullets if _words(t) > 30]
    if long_b:
        items.append(_item("long_bullets", "manual", "hygiene", f"Shorten {len(long_b)} long bullet"
                           f"{'s' if len(long_b) != 1 else ''}",
                           "Bullets over 30 words are skimmed. Keep one idea and one result per bullet."))

    # --- skills
    found = {s["name"] for s in _find_skills(to_text(r))}
    listed = {s.lower() for s in r["skills"]}
    addable = [display_skill(s) for s in sorted(found) if s not in listed and display_skill(s).lower() not in listed]
    if addable:
        items.append(_item("skills_found", "auto", "skills", f"Add {len(addable)} skill"
                           f"{'s' if len(addable) != 1 else ''} already mentioned in your resume",
                           "These appear in your experience or projects but not in your Skills list, so ATS "
                           "keyword filters miss them: " + ", ".join(addable[:8]) + ".",
                           changes=[{"before": "", "after": s} for s in addable]))
    norm = []
    for s in r["skills"]:
        d = display_skill(s)
        if d != s and d.lower() == s.lower():
            norm.append({"before": s, "after": d})
    if norm:
        items.append(_item("skills_normalize", "auto", "skills", "Use standard spellings for skills",
                           "ATS filters match exact keywords: " + ", ".join(f"{c['before']} -> {c['after']}"
                                                                          for c in norm[:6]) + ".",
                           changes=norm))
    if len(r["skills"]) + len(addable) < 8:
        items.append(_item("skills_more", "manual", "skills", "List at least 8 relevant skills",
                           "Add tools, languages and frameworks you have actually used."))

    # --- experience / projects depth
    for i, e in enumerate(r["experience"]):
        if len(e["bullets"]) < 2:
            label = e["role"] or e["company"] or f"role {i + 1}"
            items.append(_item(f"more_bullets:exp:{i}", "manual", "experience",
                               f"Add bullets to '{label}'", "Describe what you did and the result; two to four "
                               "bullets per role is ideal."))
    if r["projects"]:
        thin = [p for p in r["projects"] if len(p["bullets"]) < 2]
        if thin:
            items.append(_item("more_bullets:proj", "manual", "experience",
                               f"Give {len(thin)} project{'s' if len(thin) != 1 else ''} two bullets each",
                               "State what you built and the outcome: " + "; ".join(
                                   (p["name"] or "project")[:40] for p in thin[:4]) + "."))
    if len(r["projects"]) < 3 and not (r["experience"] and len(r["experience"]) >= 2):
        n = 3 - len(r["projects"])
        items.append(_item("more_projects", "manual", "experience",
                           f"Add {n} more project{'s' if n != 1 else ''}",
                           "Three well-described projects (or two roles) earn full credit in this section."))
    if not r["experience"] and not r["projects"]:
        items.append(_item("no_content", "manual", "experience", "Add experience or projects",
                           "Recruiters need evidence of what you built. Add an internship, a project or a job."))
    if any(not e["start"] for e in r["experience"]):
        items.append(_item("exp_dates", "manual", "experience", "Add dates to every role",
                           "Use 'Mon YYYY - Mon YYYY' or 'Present'."))

    # --- education
    for i, e in enumerate(r["education"]):
        if not e["year"]:
            items.append(_item(f"edu_year:{i}", "input", "education",
                               f"Add the year for '{(e['degree'] or e['institution'])[:50]}'", "",
                               [{"id": f"edu_year:{i}", "label": "Year or date range", "placeholder": "2026",
                                 "value": ""}]))
    if not r["education"]:
        items.append(_item("no_edu", "manual", "education", "Add your education",
                           "Degree, institution and year."))

    # --- hygiene
    if re.search(r"\[[^\]]*\]|lorem ipsum|\(simulated", to_text(r), re.I):
        items.append(_item("placeholders", "auto", "hygiene", "Remove placeholder text",
                           "Brackets such as [add metric] must not reach a recruiter."))
    fp = [t for _, _, t in bullets if re.search(r"\b(i|my|me)\b", t, re.I)]
    if fp or re.search(r"\b(i|my|me)\b", r["summary"], re.I):
        items.append(_item("first_person", "manual", "hygiene", "Remove first-person words (I, my)",
                           "Write bullets and the summary without personal pronouns."))
    total_words = _words(to_text(r))
    if total_words < 250:
        items.append(_item("short", "manual", "hygiene", "Add more detail",
                           f"The resume is only {total_words} words; 250-900 words reads as complete."))

    # --- measure each item's real gain by re-scoring with it applied
    demo_inputs: dict[str, str] = {}
    for it in items:
        for f in it["fields"]:
            key = f["id"]
            if key.startswith("contact:phone"):
                demo_inputs[key] = DEMO["phone"]
            elif key == "contact:location":
                demo_inputs[key] = DEMO["location"]
            elif key == "contact:links":
                demo_inputs[key] = "linkedin.com/in/your-name"
            elif key.startswith("contact:"):
                demo_inputs[key] = "Your Name" if key.endswith("name") else "name@example.com"
            elif key.startswith("metric:"):
                demo_inputs[key] = DEMO["result"]
            elif key.startswith("edu_year:"):
                demo_inputs[key] = DEMO["year"]
            elif key == "summary":
                demo_inputs[key] = f["value"]
    for it in items:
        if it["kind"] == "manual":
            continue
        it["gain"] = round(max(_simulate(r, [it], demo_inputs) - base["score"], 0.0), 1)

    # manual items: measure each by simulating the content being added/changed
    def apply_manual(sim: dict, iid: str) -> None:
        filler = "Delivered measurable improvements, increasing efficiency by 25%"
        if iid == "more_projects":
            while len(sim["projects"]) < 3:
                sim["projects"].append({"name": "Project", "description": "Python", "bullets": [
                    "Built a working application, serving 3 user roles", "Reduced manual effort by 30% with automation"]})
        elif iid.startswith("more_bullets:exp"):
            for e in sim["experience"]:
                while len(e["bullets"]) < 2:
                    e["bullets"].append(filler)
        elif iid == "more_bullets:proj":
            for p_ in sim["projects"]:
                while len(p_["bullets"]) < 2:
                    p_["bullets"].append(filler)
        elif iid == "exp_dates":
            for e in sim["experience"]:
                e["start"] = e["start"] or "Jan 2025"
        elif iid == "long_bullets":
            for sec in ("experience", "projects"):
                for e in sim[sec]:
                    e["bullets"] = [_truncate_words(b_, 28) if _words(b_) > 30 else b_ for b_ in e["bullets"]]
        elif iid == "skills_more":
            for sk in ["Python", "SQL", "Git", "Excel", "Communication", "Teamwork", "Docker", "Linux"]:
                if len(sim["skills"]) >= 8:
                    break
                if sk.lower() not in {x.lower() for x in sim["skills"]}:
                    sim["skills"].append(sk)
        elif iid == "first_person":
            pron = re.compile(r"\b(?:I|my|me)\b\s*", re.I)
            sim["summary"] = pron.sub("", sim["summary"])
            for sec in ("experience", "projects"):
                for e in sim[sec]:
                    e["bullets"] = [pron.sub("", b_) for b_ in e["bullets"]]
        elif iid == "short":
            for e in sim["experience"][:1] or sim["projects"][:1]:
                e["bullets"] += [filler] * 3

    manual_items = [i for i in items if i["kind"] == "manual"]
    for it in manual_items:
        sim = clone(r)
        apply_manual(sim, it["id"])
        it["gain"] = round(max(score_resume(normalize(sim))["score"] - base["score"], 0.0), 1)

    # manual guidance that cannot change the score (its section is already full) is just noise
    items = [i for i in items if i["kind"] != "manual" or i["gain"] > 0]
    auto_items = [i for i in items if i["kind"] == "auto"]
    projected_auto = _simulate(r, auto_items, {})
    nonmanual = [i for i in items if i["kind"] != "manual"]
    all_sim = clone(r)
    for it in nonmanual:
        _apply_item(all_sim, it, demo_inputs)
    for it in manual_items:
        apply_manual(all_sim, it["id"])
    projected_all = score_resume(normalize(all_sim))["score"]

    order = {"auto": 0, "input": 1, "manual": 2}
    items.sort(key=lambda i: (order[i["kind"]], -i["gain"]))
    return {
        "score": base["score"], "grade": base["grade"], "target": TARGET, "items": items,
        "projected_auto": round(max(projected_auto, base["score"]), 1),
        "projected_all": round(max(projected_all, base["score"]), 1),
        "counts": {k: sum(1 for i in items if i["kind"] == k) for k in ("auto", "input", "manual")},
        "checks": base["checks"],
    }


def apply_plan(resume: dict, plan: dict, accepted: set[str], inputs: dict[str, str]) -> dict:
    out = clone(normalize(resume))
    for it in plan["items"]:
        if it["id"] in accepted and it["kind"] != "manual":
            _apply_item(out, it, inputs)
    return normalize(out)
