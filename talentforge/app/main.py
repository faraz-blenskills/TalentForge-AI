"""TalentForge AI - Phase 1 web application (FastAPI + Jinja2 + SQLite)."""
from __future__ import annotations

import csv
import io
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import db, llm
from .db import BASE_DIR
from .matching import identity_blind_check, match_job, rank_candidates, recommendation
from .parsing import _find_skills, _parse_education, extract_text, parse_resume
from .scoring import employability, learning_suggestions, what_if
from .security import hash_password, session_secret, verify_password
from .taxonomy import ROLES, SKILLS

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="TalentForge AI", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=session_secret(BASE_DIR),
                   same_site="lax", max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

ALIAS: dict[str, str] = {}
for _canon, (_cat, _aliases) in SKILLS.items():
    ALIAS[_canon] = _canon
    for _a in _aliases:
        ALIAS[_a.lower()] = _canon

MAX_UPLOAD = 5 * 1024 * 1024
ALLOWED_EXT = (".pdf", ".docx", ".txt")


# --------------------------------------------------------------------------- helpers
class NeedLogin(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)


def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    return db.one("SELECT id, email, name, role FROM users WHERE id=?", (uid,)) if uid else None


def require_role(*roles: str):
    def dep(request: Request) -> dict:
        user = current_user(request)
        if not user:
            raise NeedLogin(str(request.url.path))
        if user["role"] not in roles:
            raise HTTPException(403, "You do not have access to this page.")
        return user
    return dep


def flash(request: Request, msg: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", current_user(request))
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["llm_on"] = llm.enabled()
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def home_for(user: dict) -> str:
    return {"candidate": "/candidate", "employer": "/employer", "admin": "/admin"}[user["role"]]


def parse_skill_weights(text: str, default_weight: int = 2) -> tuple[dict[str, int], list[str]]:
    out: dict[str, int] = {}
    unknown: list[str] = []
    for tok in re.split(r"[,\n;]+", text or ""):
        tok = tok.strip()
        if not tok:
            continue
        w = default_weight
        if ":" in tok:
            tok, ws = tok.rsplit(":", 1)
            try:
                w = max(1, min(3, int(ws.strip())))
            except ValueError:
                pass
        canon = ALIAS.get(tok.strip().lower())
        if canon:
            out[canon] = w
        else:
            unknown.append(tok.strip())
    return out, unknown


def _to_float(value: str, lo: float, hi: float) -> float:
    try:
        return max(lo, min(float(value), hi))
    except (TypeError, ValueError):
        return lo


def skills_to_text(req: dict[str, int]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in req.items())


def job_dict(row: dict) -> dict:
    return {
        **row,
        "required": json.loads(row["required_json"]),
        "preferred": json.loads(row["preferred_json"]),
    }


def get_candidate(user_id: int) -> dict | None:
    r = db.one("SELECT * FROM candidate_profiles WHERE user_id=?", (user_id,))
    if not r:
        return None
    return {"profile": json.loads(r["profile_json"]), "resume_text": r["resume_text"],
            "filename": r["resume_filename"], "target_role": r["target_role"]}


def save_candidate(user_id: int, profile: dict, resume_text: str | None = None,
                   filename: str | None = None, target_role: str | None = None) -> None:
    existing = db.one("SELECT * FROM candidate_profiles WHERE user_id=?", (user_id,))
    if existing:
        db.run(
            "UPDATE candidate_profiles SET profile_json=?, resume_text=?, resume_filename=?, "
            "target_role=?, updated_at=? WHERE user_id=?",
            (json.dumps(profile),
             resume_text if resume_text is not None else existing["resume_text"],
             filename if filename is not None else existing["resume_filename"],
             target_role if target_role is not None else existing["target_role"],
             db.now(), user_id))
    else:
        db.run(
            "INSERT INTO candidate_profiles(user_id, profile_json, resume_text, resume_filename, "
            "target_role, updated_at) VALUES (?,?,?,?,?,?)",
            (user_id, json.dumps(profile), resume_text or "", filename or "", target_role, db.now()))


def refresh_score(user_id: int) -> dict | None:
    c = get_candidate(user_id)
    if not c or not c["target_role"]:
        return None
    result = employability(c["profile"], c["target_role"])
    db.record_ai("score", f"user:{user_id}", {"score": result["score"], "role": c["target_role"],
                                                "factors": result["factors"]})
    return result


def get_org(user_id: int) -> dict | None:
    return db.one("SELECT * FROM organisations WHERE owner_id=?", (user_id,))


def load_candidates(job_id: int) -> list[dict]:
    out = []
    for r in db.rows(
        "SELECT a.id AS application_id, a.candidate_id, a.status, a.applied_at, a.match_score, "
        "p.profile_json, p.resume_text, u.email AS account_email "
        "FROM applications a JOIN candidate_profiles p ON p.user_id=a.candidate_id "
        "JOIN users u ON u.id=a.candidate_id WHERE a.job_id=?", (job_id,)):
        out.append({
            "application_id": r["application_id"], "candidate_id": r["candidate_id"],
            "status": r["status"], "applied_at": r["applied_at"], "snapshot_score": r["match_score"],
            "profile": json.loads(r["profile_json"]), "resume_text": r["resume_text"],
        })
    return out


def display_name(profile: dict, application_id: int, blind: bool) -> str:
    return f"Candidate #{application_id}" if blind else (profile.get("name") or f"Candidate #{application_id}")


@app.get("/healthz", response_class=Response)
def healthz():
    return Response("ok", media_type="text/plain")


# --------------------------------------------------------------------------- public (P1-P4)
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return render(request, "landing.html", roles=ROLES)


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return render(request, "register.html", role=request.query_params.get("role", "candidate"))


@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...),
             password: str = Form(...), role: str = Form(...), consent: str = Form("")):
    email = email.strip().lower()
    if role not in ("candidate", "employer"):
        raise HTTPException(400, "Invalid role")
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors.append("Please enter a valid email address.")
    if not consent:
        errors.append("You must accept the data-processing notice to continue.")
    if db.one("SELECT 1 AS x FROM users WHERE email=?", (email,)):
        errors.append("An account with this email already exists.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return RedirectResponse(f"/register?role={role}", status_code=303)
    uid = db.run(
        "INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
        (email, name.strip(), hash_password(password), role, db.now(), db.now()))
    db.audit(uid, "register", f"user:{uid}", role)
    request.session["uid"] = uid
    return RedirectResponse("/onboarding", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", next=request.query_params.get("next", ""))


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("")):
    u = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not u or not verify_password(password, u["password_hash"]):
        flash(request, "Incorrect email or password.", "error")
        return RedirectResponse("/login", status_code=303)
    request.session["uid"] = u["id"]
    db.audit(u["id"], "login", f"user:{u['id']}")
    if next.startswith("/") and not next.startswith("//"):
        return RedirectResponse(next, status_code=303)
    return RedirectResponse("/onboarding", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/onboarding")
def onboarding(request: Request, user: dict = Depends(require_role("candidate", "employer", "admin"))):
    return RedirectResponse(home_for(user), status_code=303)


# --------------------------------------------------------------------------- candidate (C1-C10)
def _candidate_gate(request: Request, user: dict, need_target: bool = True):
    c = get_candidate(user["id"])
    if not c:
        return None, RedirectResponse("/candidate/upload", status_code=303)
    if need_target and not c["target_role"]:
        return None, RedirectResponse("/candidate/target", status_code=303)
    return c, None


@app.get("/candidate")
def candidate_home(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    return redirect or RedirectResponse("/candidate/dashboard", status_code=303)


@app.get("/candidate/upload", response_class=HTMLResponse)
def upload_form(request: Request, user: dict = Depends(require_role("candidate"))):
    return render(request, "c_upload.html", existing=get_candidate(user["id"]))


@app.post("/candidate/upload")
async def upload(request: Request, resume: UploadFile = File(None), pasted: str = Form(""),
                 user: dict = Depends(require_role("candidate"))):
    text, filename = "", ""
    if resume is not None and resume.filename:
        if not resume.filename.lower().endswith(ALLOWED_EXT):
            flash(request, "Please upload a PDF, DOCX or TXT file.", "error")
            return RedirectResponse("/candidate/upload", status_code=303)
        data = await resume.read()
        if len(data) > MAX_UPLOAD:
            flash(request, "File is larger than 5 MB.", "error")
            return RedirectResponse("/candidate/upload", status_code=303)
        try:
            text = extract_text(data, resume.filename)
        except Exception:
            flash(request, "We could not read that file. Try another format or paste the text.", "error")
            return RedirectResponse("/candidate/upload", status_code=303)
        filename = resume.filename
    elif pasted.strip():
        text, filename = pasted, "pasted-text"
    if len(text.strip()) < 40:
        flash(request, "We could not find enough text in the resume. Paste it as text or try another file.", "error")
        return RedirectResponse("/candidate/upload", status_code=303)

    profile = parse_resume(text)
    if not profile["name"]:
        profile["name"] = user["name"]
    save_candidate(user["id"], profile, resume_text=text, filename=filename)
    db.record_ai("parse", f"user:{user['id']}", {k: profile[k] for k in ("skills", "experience_years", "warnings")})
    db.audit(user["id"], "resume_uploaded", f"user:{user['id']}", filename)
    refresh_score(user["id"])
    return render(request, "c_processing.html", target="/candidate/profile")


@app.get("/candidate/profile", response_class=HTMLResponse)
def profile_form(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user, need_target=False)
    if redirect:
        return redirect
    p = c["profile"]
    return render(request, "c_profile.html", p=p, filename=c["filename"],
                  skills_text=", ".join(s["name"] for s in p["skills"]),
                  edu_text="\n".join(e["text"] for e in p["education"]),
                  proj_text="\n".join(p["projects"]), cert_text="\n".join(p["certifications"]),
                  all_skills=sorted(SKILLS))


@app.post("/candidate/profile")
def profile_save(request: Request, name: str = Form(""), email: str = Form(""), skills: str = Form(""),
                 education: str = Form(""), experience_years: str = Form("0"),
                 projects: str = Form(""), certifications: str = Form(""),
                 user: dict = Depends(require_role("candidate"))):
    c = get_candidate(user["id"])
    if not c:
        return RedirectResponse("/candidate/upload", status_code=303)
    p = c["profile"]
    old = {s["name"]: s for s in p["skills"]}
    new_skills, unknown = parse_skill_weights(skills)
    p["skills"] = [old.get(k) or {"name": k, "evidence": "(added manually)", "mentions": 1}
                   for k in new_skills]
    if unknown:
        flash(request, "Not in the Phase 1 skill catalogue (ignored): " + ", ".join(unknown), "warn")
    edu_lines = [l.strip() for l in education.splitlines() if l.strip()]
    entries, level, cs = _parse_education(edu_lines)
    if edu_lines and not entries:
        entries = [{"text": l, "level": 0, "year": "", "cs_related": False} for l in edu_lines]
    p.update({
        "name": name.strip() or p.get("name", ""), "email": email.strip(),
        "education": entries, "highest_degree_level": level, "cs_related": cs,
        "experience_years": _to_float(experience_years, 0.0, 40.0),
        "projects": [l.strip() for l in projects.splitlines() if l.strip()],
        "certifications": [l.strip() for l in certifications.splitlines() if l.strip()],
    })
    save_candidate(user["id"], p)
    db.audit(user["id"], "profile_edited", f"user:{user['id']}")
    refresh_score(user["id"])
    flash(request, "Profile saved.", "ok")
    return RedirectResponse("/candidate/target" if not c["target_role"] else "/candidate/dashboard",
                            status_code=303)


@app.get("/candidate/target", response_class=HTMLResponse)
def target_form(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user, need_target=False)
    if redirect:
        return redirect
    return render(request, "c_target.html", roles=ROLES, current=c["target_role"])


@app.post("/candidate/target")
def target_save(request: Request, role_id: str = Form(...), user: dict = Depends(require_role("candidate"))):
    if role_id not in ROLES:
        raise HTTPException(400, "Unknown role")
    c = get_candidate(user["id"])
    if not c:
        return RedirectResponse("/candidate/upload", status_code=303)
    save_candidate(user["id"], c["profile"], target_role=role_id)
    db.audit(user["id"], "target_role_set", f"user:{user['id']}", role_id)
    refresh_score(user["id"])
    return RedirectResponse("/candidate/dashboard", status_code=303)


def _score_view(c: dict) -> dict:
    return employability(c["profile"], c["target_role"])


@app.get("/candidate/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    if redirect:
        return redirect
    res = _score_view(c)
    apps = db.rows("SELECT status FROM applications WHERE candidate_id=?", (user["id"],))
    return render(request, "c_dashboard.html", res=res, p=c["profile"], n_apps=len(apps),
                  suggestions=learning_suggestions(res["gap"], 3))


@app.get("/candidate/gap", response_class=HTMLResponse)
def gap_report(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    if redirect:
        return redirect
    res = _score_view(c)
    return render(request, "c_gap.html", res=res, role=ROLES[c["target_role"]],
                  suggestions=learning_suggestions(res["gap"], 8))


@app.get("/candidate/score", response_class=HTMLResponse)
def score_explain(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    if redirect:
        return redirect
    res = _score_view(c)
    facts = {"role": res["role_title"], "score": res["score"], "band": res["band"],
             "factors": [{"label": f["label"], "value": f["value"], "detail": f["detail"]} for f in res["factors"]],
             "top_missing": [i["skill"] for i in res["gap"]["missing"]][:3]}
    narrative, source = llm.narrate("score", facts)

    q = request.query_params
    sim = None
    if q.get("simulate"):
        add_skills, _ = parse_skill_weights(q.get("skills", ""))
        try:
            projects = max(0, min(int(q.get("projects", 0) or 0), 5))
            certs = max(0, min(int(q.get("certs", 0) or 0), 5))
            years = max(0.0, min(float(q.get("years", 0) or 0), 5.0))
        except ValueError:
            projects = certs = 0
            years = 0.0
        sim = what_if(c["profile"], c["target_role"], list(add_skills), projects, certs, years)
        sim["inputs"] = {"skills": list(add_skills), "projects": projects, "certs": certs, "years": years}
    missing_names = [i["skill"] for i in res["gap"]["items"] if i["status"] != "matched"]
    return render(request, "c_score.html", res=res, narrative=narrative, source=source,
                  sim=sim, missing_names=missing_names, q=q)


@app.get("/candidate/jobs", response_class=HTMLResponse)
def job_feed(request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    if redirect:
        return redirect
    q = request.query_params
    term = (q.get("q") or "").lower().strip()
    loc = (q.get("loc") or "").lower().strip()
    applied = {r["job_id"] for r in db.rows(
        "SELECT job_id FROM applications WHERE candidate_id=?", (user["id"],))}
    cards = []
    for r in db.rows("SELECT j.*, o.name AS org_name FROM jobs j JOIN organisations o ON o.id=j.org_id "
                     "WHERE j.status='open'"):
        job = job_dict(r)
        if term and term not in (job["title"] + " " + job["description"]).lower():
            continue
        if loc and loc not in job["location"].lower():
            continue
        m = match_job(c["profile"], c["resume_text"], job)
        cards.append({"job": job, "m": m, "applied": job["id"] in applied})
    cards.sort(key=lambda x: -x["m"]["score"])
    return render(request, "c_jobs.html", cards=cards, q=q)


@app.get("/candidate/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    if redirect:
        return redirect
    r = db.one("SELECT j.*, o.name AS org_name, o.industry FROM jobs j JOIN organisations o ON o.id=j.org_id "
               "WHERE j.id=?", (job_id,))
    if not r or r["status"] == "draft":
        raise HTTPException(404, "Job not found")
    job = job_dict(r)
    m = match_job(c["profile"], c["resume_text"], job)
    facts = {"job": job["title"], "score": m["score"],
             "matched": [i["skill"] for i in m["matched"]], "missing": [i["skill"] for i in m["missing"]]}
    narrative, source = llm.narrate("job_match", facts)
    already = db.one("SELECT id, status FROM applications WHERE job_id=? AND candidate_id=?",
                     (job_id, user["id"]))
    return render(request, "c_job.html", job=job, m=m, narrative=narrative, source=source, already=already)


@app.post("/candidate/jobs/{job_id}/apply")
def apply(job_id: int, request: Request, user: dict = Depends(require_role("candidate"))):
    c, redirect = _candidate_gate(request, user)
    if redirect:
        return redirect
    r = db.one("SELECT * FROM jobs WHERE id=? AND status='open'", (job_id,))
    if not r:
        flash(request, "This job is no longer open.", "error")
        return RedirectResponse("/candidate/jobs", status_code=303)
    if db.one("SELECT 1 AS x FROM applications WHERE job_id=? AND candidate_id=?", (job_id, user["id"])):
        flash(request, "You have already applied to this job.", "warn")
        return RedirectResponse("/candidate/applications", status_code=303)
    m = match_job(c["profile"], c["resume_text"], job_dict(r))
    app_id = db.run(
        "INSERT INTO applications(job_id, candidate_id, status, match_score, match_json, applied_at) "
        "VALUES (?,?,?,?,?,?)",
        (job_id, user["id"], "applied", m["score"], json.dumps(m, default=str), db.now()))
    db.record_ai("match", f"application:{app_id}", {"score": m["score"], "factors": m["factors"],
                                                     "recommendation": m["recommendation"]})
    db.audit(user["id"], "applied", f"application:{app_id}", f"job:{job_id} score={m['score']}")
    flash(request, "Application submitted.", "ok")
    return RedirectResponse("/candidate/applications", status_code=303)


@app.get("/candidate/applications", response_class=HTMLResponse)
def my_applications(request: Request, user: dict = Depends(require_role("candidate"))):
    items = db.rows(
        "SELECT a.id, a.status, a.match_score, a.applied_at, j.title, j.location, o.name AS org_name, "
        "d.decision, d.feedback, d.share_feedback, d.decided_at "
        "FROM applications a JOIN jobs j ON j.id=a.job_id JOIN organisations o ON o.id=j.org_id "
        "LEFT JOIN decisions d ON d.application_id=a.id AND d.id=("
        "  SELECT MAX(id) FROM decisions WHERE application_id=a.id) "
        "WHERE a.candidate_id=? ORDER BY a.applied_at DESC", (user["id"],))
    return render(request, "c_applications.html", items=items)


@app.get("/candidate/settings", response_class=HTMLResponse)
def candidate_settings(request: Request, user: dict = Depends(require_role("candidate"))):
    return render(request, "c_settings.html")


@app.post("/candidate/delete")
def delete_my_data(request: Request, confirm: str = Form(""), user: dict = Depends(require_role("candidate"))):
    if confirm.strip().upper() != "DELETE":
        flash(request, "Type DELETE to confirm.", "error")
        return RedirectResponse("/candidate/settings", status_code=303)
    uid = user["id"]
    db.run("DELETE FROM decisions WHERE application_id IN (SELECT id FROM applications WHERE candidate_id=?)", (uid,))
    db.run("DELETE FROM applications WHERE candidate_id=?", (uid,))
    db.run("DELETE FROM candidate_profiles WHERE user_id=?", (uid,))
    db.run("DELETE FROM users WHERE id=?", (uid,))
    db.audit(None, "account_deleted", f"user:{uid}", "candidate requested deletion of all personal data")
    request.session.clear()
    flash(request, "Your account and data have been deleted.", "ok")
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- employer (E1-E8)
def _org_gate(user: dict):
    org = get_org(user["id"])
    return org, (None if org else RedirectResponse("/employer/setup", status_code=303))


def _own_job(job_id: int, org: dict) -> dict:
    r = db.one("SELECT * FROM jobs WHERE id=? AND org_id=?", (job_id, org["id"]))
    if not r:
        raise HTTPException(404, "Job not found")
    return job_dict(r)


@app.get("/employer/setup", response_class=HTMLResponse)
def org_form(request: Request, user: dict = Depends(require_role("employer"))):
    return render(request, "e_setup.html", org=get_org(user["id"]))


@app.post("/employer/setup")
def org_save(request: Request, name: str = Form(...), industry: str = Form(""), size: str = Form(""),
             website: str = Form(""), user: dict = Depends(require_role("employer"))):
    org = get_org(user["id"])
    if org:
        db.run("UPDATE organisations SET name=?, industry=?, size=?, website=? WHERE id=?",
               (name.strip(), industry.strip(), size.strip(), website.strip(), org["id"]))
    else:
        db.run("INSERT INTO organisations(owner_id, name, industry, size, website) VALUES (?,?,?,?,?)",
               (user["id"], name.strip(), industry.strip(), size.strip(), website.strip()))
    db.audit(user["id"], "org_saved", f"user:{user['id']}", name)
    return RedirectResponse("/employer", status_code=303)


@app.get("/employer", response_class=HTMLResponse)
def employer_dashboard(request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    jobs = db.rows(
        "SELECT j.*, (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS n_apps, "
        "(SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id AND a.status='shortlisted') AS n_short "
        "FROM jobs j WHERE j.org_id=? ORDER BY j.created_at DESC", (org["id"],))
    stats = {
        "open": sum(1 for j in jobs if j["status"] == "open"),
        "apps": sum(j["n_apps"] for j in jobs),
        "short": sum(j["n_short"] for j in jobs),
        "pending": db.one(
            "SELECT COUNT(*) AS n FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE j.org_id=? AND a.status='applied'", (org["id"],))["n"],
    }
    return render(request, "e_dashboard.html", org=org, jobs=jobs, stats=stats)


@app.get("/employer/jobs", response_class=HTMLResponse)
def employer_jobs(request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    jobs = db.rows(
        "SELECT j.*, (SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS n_apps "
        "FROM jobs j WHERE j.org_id=? ORDER BY j.created_at DESC", (org["id"],))
    return render(request, "e_jobs.html", org=org, jobs=jobs)


def _job_form_ctx(job: dict | None, form: dict | None = None, notice: str = "") -> dict:
    j = job or {}
    f = form or {}
    return {
        "job": job, "notice": notice,
        "title": f.get("title", j.get("title", "")),
        "description": f.get("description", j.get("description", "")),
        "location": f.get("location", j.get("location", "")),
        "job_type": f.get("job_type", j.get("job_type", "Full-time")),
        "required_text": f.get("required", skills_to_text(j.get("required", {}))),
        "preferred_text": f.get("preferred", ", ".join(j.get("preferred", []))),
        "min_years": f.get("min_years", j.get("min_years", 0)),
        "status": f.get("status", j.get("status", "open")),
        "all_skills": sorted(SKILLS),
    }


@app.get("/employer/jobs/new", response_class=HTMLResponse)
def job_new(request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    return redirect or render(request, "e_job_form.html", org=org, **_job_form_ctx(None))


@app.get("/employer/jobs/{job_id}/edit", response_class=HTMLResponse)
def job_edit(job_id: int, request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    return render(request, "e_job_form.html", org=org, **_job_form_ctx(_own_job(job_id, org)))


@app.post("/employer/jobs/save")
def job_save(request: Request, job_id: str = Form(""), action: str = Form("save"),
             title: str = Form(""), description: str = Form(""), location: str = Form(""),
             job_type: str = Form("Full-time"), required: str = Form(""), preferred: str = Form(""),
             min_years: str = Form("0"), status: str = Form("open"),
             user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    form = dict(title=title, description=description, location=location, job_type=job_type,
                required=required, preferred=preferred, min_years=min_years, status=status)
    existing = _own_job(int(job_id), org) if job_id else None

    if action == "extract":
        found = _find_skills(f"{title}\n{description}")
        merged, _ = parse_skill_weights(required)
        for s in found:
            merged.setdefault(s["name"], 2)
        form["required"] = skills_to_text(merged)
        note = f"Extracted {len(found)} skill(s) from the description. Review the weights (1-3) before saving."
        return render(request, "e_job_form.html", org=org, **_job_form_ctx(existing, form, note))

    req, unknown_r = parse_skill_weights(required)
    pref_dict, unknown_p = parse_skill_weights(preferred, 1)
    errors = []
    if not title.strip():
        errors.append("Job title is required.")
    if not req:
        errors.append("Add at least one required skill from the catalogue (e.g. python:3, sql:2).")
    if status not in ("draft", "open", "closed"):
        status = "open"
    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(request, "e_job_form.html", org=org, **_job_form_ctx(existing, form))
    unknown = unknown_r + unknown_p
    if unknown:
        flash(request, "Not in the Phase 1 skill catalogue (ignored): " + ", ".join(unknown), "warn")
    vals = (title.strip(), description.strip(), location.strip(), job_type, json.dumps(req),
            json.dumps(list(pref_dict)), _to_float(min_years, 0.0, 20.0), status)
    if existing:
        db.run("UPDATE jobs SET title=?, description=?, location=?, job_type=?, required_json=?, "
               "preferred_json=?, min_years=?, status=? WHERE id=?", vals + (existing["id"],))
        jid = existing["id"]
        db.audit(user["id"], "job_updated", f"job:{jid}", title)
    else:
        jid = db.run("INSERT INTO jobs(title, description, location, job_type, required_json, preferred_json, "
                     "min_years, status, org_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     vals + (org["id"], db.now()))
        db.audit(user["id"], "job_created", f"job:{jid}", title)
    flash(request, "Job saved.", "ok")
    return RedirectResponse(f"/employer/jobs/{jid}/candidates", status_code=303)


@app.post("/employer/jobs/{job_id}/close")
def job_close(job_id: int, request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    _own_job(job_id, org)
    db.run("UPDATE jobs SET status='closed' WHERE id=?", (job_id,))
    db.audit(user["id"], "job_closed", f"job:{job_id}")
    return RedirectResponse("/employer/jobs", status_code=303)


@app.get("/employer/jobs/{job_id}/candidates", response_class=HTMLResponse)
def ranked_candidates(job_id: int, request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    job = _own_job(job_id, org)
    blind = request.query_params.get("blind") == "1"
    status_filter = request.query_params.get("status", "")
    try:
        min_score = float(request.query_params.get("min", 0) or 0)
    except ValueError:
        min_score = 0.0
    cands = load_candidates(job_id)
    ranked = rank_candidates(job, cands)
    check = identity_blind_check(job, cands) if cands else {"identical": True, "n": 0}
    rows_ = []
    for r in ranked:
        if r["match"]["score"] < min_score:
            continue
        if status_filter and r["status"] != status_filter:
            continue
        rows_.append({**r, "label": display_name(r["profile"], r["application_id"], blind)})
    db.audit(user["id"], "ranking_viewed", f"job:{job_id}", f"{len(ranked)} candidates, blind={blind}")
    return render(request, "e_candidates.html", org=org, job=job, rows=rows_, blind=blind, check=check,
                  total=len(ranked), status_filter=status_filter, min_score=min_score)


@app.get("/employer/applications/{app_id}", response_class=HTMLResponse)
def candidate_detail(app_id: int, request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    a = db.one("SELECT a.* FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND j.org_id=?",
               (app_id, org["id"]))
    if not a:
        raise HTTPException(404, "Application not found")
    job = _own_job(a["job_id"], org)
    cand = next(c for c in load_candidates(job["id"]) if c["application_id"] == app_id)
    blind = request.query_params.get("blind") == "1"
    m = match_job(cand["profile"], cand["resume_text"], job)
    facts = {"job": job["title"], "score": m["score"], "recommendation": m["recommendation"],
             "matched": [i["skill"] for i in m["matched"]], "missing": [i["skill"] for i in m["missing"]]}
    narrative, source = llm.narrate("candidate", facts)
    decisions = db.rows("SELECT d.*, u.name AS decider FROM decisions d JOIN users u ON u.id=d.decided_by "
                        "WHERE d.application_id=? ORDER BY d.id DESC", (app_id,))
    p = cand["profile"]
    view_profile = {
        "name": display_name(p, app_id, blind),
        "email": "" if blind else p.get("email", ""),
        "education": [e["text"] if not blind else
                      {4: "Doctorate", 3: "Postgraduate degree", 2: "Bachelor's degree", 1: "Diploma"}.get(e["level"], "Degree")
                      + (f" ({e['year']})" if e.get("year") else "")
                      for e in p.get("education", [])],
        "experience_years": p.get("experience_years", 0),
        "projects": p.get("projects", []), "certifications": p.get("certifications", []),
        "skills": p.get("skills", []),
    }
    return render(request, "e_candidate.html", org=org, job=job, application=a, m=m, vp=view_profile,
                  blind=blind, narrative=narrative, source=source, decisions=decisions)


@app.post("/employer/applications/{app_id}/decision")
def record_decision(app_id: int, request: Request, decision: str = Form(...), reason: str = Form(""),
                    share_feedback: str = Form(""), blind: str = Form(""),
                    user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    a = db.one("SELECT a.* FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=? AND j.org_id=?",
               (app_id, org["id"]))
    if not a:
        raise HTTPException(404, "Application not found")
    back = f"/employer/applications/{app_id}" + ("?blind=1" if blind else "")
    if decision not in ("shortlist", "reject", "hold"):
        raise HTTPException(400, "Invalid decision")
    if len(reason.strip()) < 5:
        flash(request, "A reason is required (at least a short sentence).", "error")
        return RedirectResponse(back, status_code=303)
    job = _own_job(a["job_id"], org)
    cand = next(c for c in load_candidates(job["id"]) if c["application_id"] == app_id)
    m = match_job(cand["profile"], cand["resume_text"], job)
    feedback = ""
    if decision == "reject":
        feedback, _ = llm.narrate("feedback", {"job": job["title"], "score": m["score"],
                                               "missing": [i["skill"] for i in m["missing"]]})
    db.run("INSERT INTO decisions(application_id, decision, reason, ai_recommendation, feedback, "
           "share_feedback, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?)",
           (app_id, decision, reason.strip(), recommendation(m["score"]), feedback,
            1 if share_feedback else 0, user["id"], db.now()))
    new_status = {"shortlist": "shortlisted", "reject": "rejected", "hold": "on_hold"}[decision]
    db.run("UPDATE applications SET status=? WHERE id=?", (new_status, app_id))
    db.audit(user["id"], f"decision_{decision}", f"application:{app_id}",
             f"ai={recommendation(m['score'])} score={m['score']} reason={reason.strip()[:120]}")
    flash(request, f"Decision recorded: {decision}.", "ok")
    return RedirectResponse(f"/employer/jobs/{job['id']}/candidates" + ("?blind=1" if blind else ""),
                            status_code=303)


@app.get("/employer/jobs/{job_id}/shortlist", response_class=HTMLResponse)
def shortlist(job_id: int, request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    job = _own_job(job_id, org)
    items = _shortlist_rows(job)
    return render(request, "e_shortlist.html", org=org, job=job, items=items)


def _shortlist_rows(job: dict) -> list[dict]:
    ranked = rank_candidates(job, [c for c in load_candidates(job["id"]) if c["status"] == "shortlisted"])
    out = []
    for r in ranked:
        d = db.one("SELECT reason, decided_at FROM decisions WHERE application_id=? ORDER BY id DESC",
                   (r["application_id"],)) or {}
        out.append({**r, "reason": d.get("reason", ""), "decided_at": d.get("decided_at", "")})
    return out


@app.get("/employer/jobs/{job_id}/shortlist.csv")
def shortlist_csv(job_id: int, request: Request, user: dict = Depends(require_role("employer"))):
    org, redirect = _org_gate(user)
    if redirect:
        return redirect
    job = _own_job(job_id, org)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "name", "email", "match_score", "ai_recommendation", "decision_reason", "decided_at"])
    for r in _shortlist_rows(job):
        w.writerow([r["rank"], r["profile"].get("name", ""), r["profile"].get("email", ""),
                    r["match"]["score"], r["match"]["recommendation"], r["reason"], r["decided_at"]])
    db.audit(user["id"], "shortlist_exported", f"job:{job_id}")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="shortlist-job-{job_id}.csv"'})


# --------------------------------------------------------------------------- admin (A1-A2)
@app.get("/admin", response_class=HTMLResponse)
def admin_overview(request: Request, user: dict = Depends(require_role("admin"))):
    users = db.rows("SELECT id, email, name, role, created_at FROM users ORDER BY id")
    jobs = db.rows("SELECT j.id, j.title, j.status, o.name AS org_name, "
                   "(SELECT COUNT(*) FROM applications a WHERE a.job_id=j.id) AS n_apps "
                   "FROM jobs j JOIN organisations o ON o.id=j.org_id ORDER BY j.id")
    counts = {
        "users": len(users), "jobs": len(jobs),
        "apps": db.one("SELECT COUNT(*) AS n FROM applications")["n"],
        "decisions": db.one("SELECT COUNT(*) AS n FROM decisions")["n"],
        "ai": db.one("SELECT COUNT(*) AS n FROM ai_results")["n"],
    }
    return render(request, "a_overview.html", users=users, jobs=jobs, counts=counts)


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request, user: dict = Depends(require_role("admin"))):
    events = db.rows("SELECT e.*, u.email AS actor FROM audit_events e LEFT JOIN users u ON u.id=e.actor_id "
                     "ORDER BY e.id DESC LIMIT 300")
    ai = db.rows("SELECT * FROM ai_results ORDER BY id DESC LIMIT 50")
    return render(request, "a_audit.html", events=events, ai=ai)


# --------------------------------------------------------------------------- errors
@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code,
                  message=exc.detail)
