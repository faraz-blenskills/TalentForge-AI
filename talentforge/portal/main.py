"""TalentForge candidate portal (resume upload / builder / tailoring + job-search agents)."""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.security import hash_password, session_secret, verify_password

from . import agent as agent_mod
from . import ai, improve, providers, store, styles, tailor
from .doc_extract import extract_text
from .export import resume_to_docx
from .resume_score import score_resume
from .resume_struct import empty_resume, normalize, parse_with_report

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
MAX_UPLOAD = 5 * 1024 * 1024
SCHEDULER = agent_mod.Scheduler(int(os.environ.get("PORTAL_AGENT_INTERVAL_MIN", "120")))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    store.init()
    if os.environ.get("PORTAL_AGENT_SCHEDULER", "1") != "0":
        SCHEDULER.start()
    yield
    SCHEDULER.stop()


app = FastAPI(title="TalentForge Portal", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=session_secret(ROOT), same_site="lax", max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _fmt_dt(v: str | None) -> str:
    return (v or "")[:16].replace("T", " ")


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["score_class"] = lambda s: "good" if s >= 70 else "mid" if s >= 50 else "low"


# --------------------------------------------------------------------------- helpers
class NeedLogin(Exception):
    def __init__(self, next_url: str = "/dashboard"):
        self.next_url = next_url


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)


def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    return db.one("SELECT id, email, name, role FROM users WHERE id=?", (uid,)) if uid else None


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise NeedLogin(str(request.url.path))
    if u["role"] != "candidate":
        raise HTTPException(403, "This portal is for candidates. Employer tools are not part of this release.")
    return u


def flash(request: Request, msg: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", current_user(request))
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["llm_on"] = ai.enabled()
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _own_resume(user: dict, resume_id: int) -> dict:
    r = store.get_resume(user["id"], resume_id)
    if not r:
        raise HTTPException(404, "Resume not found")
    return r


def _save_resume_data(resume: dict, data: dict, title: str | None = None, template: str | None = None,
                      accent: str | None = None) -> dict:
    data = normalize(data)
    prev = resume["score_detail"].get("tailoring") if resume.get("score_detail") else None
    score = score_resume(data)
    if prev:
        score["tailoring"] = prev
    store.update_resume(resume["id"], data, score, title, template, accent)
    return score


@app.get("/healthz", response_class=Response)
def healthz():
    return Response("ok", media_type="text/plain")


# --------------------------------------------------------------------------- landing + auth
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return render(request, "landing.html", sources=providers.provider_status())


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return render(request, "register.html")


@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
             consent: str = Form("")):
    email = email.strip().lower()
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors.append("Please enter a valid email address.")
    if not consent:
        errors.append("Please accept the data-processing notice to continue.")
    if db.one("SELECT 1 AS x FROM users WHERE email=?", (email,)):
        errors.append("An account with this email already exists.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return RedirectResponse("/register", status_code=303)
    uid = db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
                 (email, name.strip(), hash_password(password), "candidate", db.now(), db.now()))
    db.audit(uid, "register", f"user:{uid}", "candidate")
    request.session["uid"] = uid
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", next=request.query_params.get("next", ""))


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("")):
    u = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not u or not verify_password(password, u["password_hash"]):
        flash(request, "Incorrect email or password.", "error")
        return RedirectResponse("/login", status_code=303)
    if u["role"] != "candidate":
        flash(request, "This portal is for candidate accounts.", "error")
        return RedirectResponse("/login", status_code=303)
    request.session["uid"] = u["id"]
    db.audit(u["id"], "login", f"user:{u['id']}")
    dest = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
    return RedirectResponse(dest, status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- dashboard
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(require_user)):
    resumes = store.list_resumes(user["id"])
    best = max(resumes, key=lambda r: r["score"]) if resumes else None
    default = next((r for r in resumes if r["is_default"]), resumes[0] if resumes else None)
    agents = store.list_agents(user["id"])
    top_jobs = store.list_results(user["id"], limit=5)
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    return render(request, "dashboard.html", resumes=resumes, best=best, default=default, agents=agents,
                  top_jobs=top_jobs, counts=store.counts(user["id"]), greeting=greeting)


# --------------------------------------------------------------------------- resumes
ACCENT_PALETTE = [("Ink", "#141413"), ("Navy", "#1f3a5f"), ("Blue", "#2b5fb8"), ("Teal", "#0f766e"),
                  ("Forest", "#1f5c3f"), ("Orange", "#cf4500"), ("Burgundy", "#8a1c3d"), ("Violet", "#5b3fa0")]


def _style_ctx() -> dict:
    return {"tpls": styles.catalogue(), "palette": ACCENT_PALETTE}


@app.get("/resumes", response_class=HTMLResponse)
def resumes_page(request: Request, user: dict = Depends(require_user)):
    tid, acc = styles.resolve(None, None)
    return render(request, "resumes.html", resumes=store.list_resumes(user["id"]), sample=styles.SAMPLE_RESUME,
                  default_template=tid, default_accent=acc, **_style_ctx())


@app.post("/resumes/upload")
async def resume_upload(request: Request, resume: UploadFile = File(None), pasted: str = Form(""),
                        template: str = Form(""), accent: str = Form(""), user: dict = Depends(require_user)):
    text, filename = "", ""
    if resume is not None and resume.filename:
        if not resume.filename.lower().endswith((".pdf", ".docx", ".txt")):
            flash(request, "Please upload a PDF, DOCX or TXT file.", "error")
            return RedirectResponse("/resumes", status_code=303)
        data = await resume.read()
        if len(data) > MAX_UPLOAD:
            flash(request, "That file is larger than 5 MB.", "error")
            return RedirectResponse("/resumes", status_code=303)
        try:
            text = extract_text(data, resume.filename)
        except Exception:
            flash(request, "We could not read that file. Try another format or paste the text.", "error")
            return RedirectResponse("/resumes", status_code=303)
        filename = resume.filename
    elif pasted.strip():
        text, filename = pasted, "Pasted resume"
    if len(text.strip()) < 40:
        flash(request, "We could not find enough text in that resume. It may be a scanned image: paste the text "
                       "instead, or upload a PDF/DOCX that has selectable text.", "error")
        return RedirectResponse("/resumes", status_code=303)

    tid, acc = styles.resolve(template, accent)
    data, report = parse_with_report(text, fallback_name=user["name"])
    score = score_resume(data)
    title = Path(filename).stem[:60] if filename else "Uploaded resume"
    rid = store.create_resume(user["id"], title or "Uploaded resume", data, score, source="upload",
                              template=tid, accent=acc, raw_text=text, parse=report)
    db.audit(user["id"], "resume_uploaded", f"resume:{rid}", filename)
    c = report["counts"]
    flash(request, f"Scanned {c['lines_read']} lines: {c['experience']} experience, {c['projects']} projects, "
                   f"{c['education']} education, {c['skills']} skills, {c['certifications']} certifications. "
                   "Review the preview, then follow the improvement plan.", "ok")
    return RedirectResponse(f"/resumes/{rid}", status_code=303)


@app.post("/resumes/new")
def resume_new(request: Request, title: str = Form("My resume"), template: str = Form(""),
               accent: str = Form(""), user: dict = Depends(require_user)):
    tid, acc = styles.resolve(template, accent)
    data = empty_resume(user["name"], user["email"])
    rid = store.create_resume(user["id"], title.strip() or "My resume", data, score_resume(data), source="builder",
                              template=tid, accent=acc)
    return RedirectResponse(f"/resumes/{rid}/edit", status_code=303)


@app.get("/resumes/{resume_id}", response_class=HTMLResponse)
def resume_view(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    tid, acc = styles.resolve(r["template"], r["accent"])
    r["template"], r["accent"] = tid, acc
    return render(request, "resume_view.html", r=r, d=r["data"], s=r["score_detail"],
                  plan=improve.build_plan(r["data"]), **_style_ctx())


@app.get("/resumes/{resume_id}/edit", response_class=HTMLResponse)
def resume_edit(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    r["template"], r["accent"] = styles.resolve(r["template"], r["accent"])
    return render(request, "resume_edit.html", r=r, d=r["data"], s=r["score_detail"], **_style_ctx())


@app.post("/api/resumes/{resume_id}")
async def resume_save(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    tid, acc = styles.resolve(payload.get("template") or r["template"], payload.get("accent") or r["accent"])
    score = _save_resume_data(r, payload.get("data") or {}, payload.get("title"), tid, acc)
    return {"score": score["score"], "grade": score["grade"], "checks": score["checks"], "top_fixes": score["top_fixes"]}


@app.post("/resumes/{resume_id}/style")
def resume_style(resume_id: int, request: Request, template: str = Form(""), accent: str = Form(""),
                 user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    tid, acc = styles.resolve(template or r["template"], accent or r["accent"])
    store.set_style(resume_id, tid, acc)
    flash(request, f"Template set to {styles.TEMPLATES[tid]['name']}.", "ok")
    return RedirectResponse(f"/resumes/{resume_id}", status_code=303)


@app.get("/resumes/{resume_id}/improve", response_class=HTMLResponse)
def resume_improve(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    return render(request, "improve.html", r=r, plan=improve.build_plan(r["data"]))


@app.post("/resumes/{resume_id}/improve")
async def resume_improve_apply(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    form = await request.form()
    plan = improve.build_plan(r["data"])
    accepted = {str(v) for v in form.getlist("accept")}
    inputs = {k[len("input_"):]: str(v) for k, v in form.multi_items() if k.startswith("input_")}
    new_data = improve.apply_plan(r["data"], plan, accepted, inputs)
    score = score_resume(new_data)
    if form.get("copy"):
        rid = store.create_resume(user["id"], f"{r['title']} (improved)"[:120], new_data, score, source=r["source"],
                                  parent_id=r["id"], template=r["template"], accent=r["accent"])
    else:
        rid = r["id"]
        store.update_resume(rid, new_data, score)
    db.audit(user["id"], "resume_improved", f"resume:{rid}", f"{plan['score']} -> {score['score']}")
    flash(request, f"Improvements applied: score {plan['score']:g} -> {score['score']:g}.", "ok")
    return RedirectResponse(f"/resumes/{rid}/improve", status_code=303)


@app.post("/resumes/{resume_id}/default")
def resume_default(resume_id: int, request: Request, user: dict = Depends(require_user)):
    _own_resume(user, resume_id)
    store.set_default(user["id"], resume_id)
    flash(request, "Default resume updated. Job agents use it unless you choose another.", "ok")
    return RedirectResponse("/resumes", status_code=303)


@app.post("/resumes/{resume_id}/duplicate")
def resume_duplicate(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    rid = store.create_resume(user["id"], f"{r['title']} (copy)", r["data"], score_resume(r["data"]),
                              source=r["source"], parent_id=r["id"], template=r["template"], accent=r["accent"])
    return RedirectResponse(f"/resumes/{rid}/edit", status_code=303)


@app.post("/resumes/{resume_id}/delete")
def resume_delete(resume_id: int, request: Request, user: dict = Depends(require_user)):
    _own_resume(user, resume_id)
    store.delete_resume(user["id"], resume_id)
    flash(request, "Resume deleted.", "ok")
    return RedirectResponse("/resumes", status_code=303)


@app.get("/resumes/{resume_id}/download.docx")
def resume_docx(resume_id: int, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", r["title"]) or "resume"
    return Response(resume_to_docx(r["data"], r["template"], r["accent"]),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{name}.docx"'})


@app.get("/resumes/{resume_id}/print", response_class=HTMLResponse)
def resume_print(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    r["template"], r["accent"] = styles.resolve(r["template"], r["accent"])
    return render(request, "resume_print.html", r=r, d=r["data"])


# --------------------------------------------------------------------------- tailoring
def _job_input(user: dict, request: Request) -> dict:
    q = request.query_params
    job = {"title": "", "company": "", "description": "", "result_id": None, "url": ""}
    if q.get("job"):
        try:
            res = store.get_result(user["id"], int(q["job"]))
        except ValueError:
            res = None
        if res:
            job.update(title=res["title"], company=res["company"], description=res["description"],
                       result_id=res["id"], url=res["url"])
    return job


@app.get("/resumes/{resume_id}/tailor", response_class=HTMLResponse)
def tailor_form(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    return render(request, "tailor.html", r=r, job=_job_input(user, request), result=None,
                  resumes=store.list_resumes(user["id"]))


@app.post("/resumes/{resume_id}/tailor", response_class=HTMLResponse)
def tailor_analyze(resume_id: int, request: Request, title: str = Form(""), company: str = Form(""),
                   description: str = Form(""), result_id: str = Form(""), user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    job = {"title": title.strip(), "company": company.strip(), "description": description.strip(),
           "result_id": int(result_id) if result_id.isdigit() else None, "url": ""}
    if not job["title"] or len(job["description"]) < 40:
        flash(request, "Add the job title and paste the job description (at least a few sentences).", "error")
        return render(request, "tailor.html", r=r, job=job, result=None, resumes=store.list_resumes(user["id"]))
    llm_sugg = ai.tailor_suggestions(r["data"], job["title"], job["company"], job["description"])
    result = tailor.analyze(r["data"], job["title"], job["company"], job["description"], llm_sugg)
    payload = base64.urlsafe_b64encode(json.dumps({
        "suggestions": result["suggestions"], "job": job}).encode()).decode()
    return render(request, "tailor.html", r=r, job=job, result=result, payload=payload,
                  before=result["match"]["score"], ats=score_resume(r["data"]),
                  resumes=store.list_resumes(user["id"]))


@app.post("/resumes/{resume_id}/tailor/apply")
async def tailor_apply(resume_id: int, request: Request, user: dict = Depends(require_user)):
    r = _own_resume(user, resume_id)
    form = await request.form()
    try:
        blob = json.loads(base64.urlsafe_b64decode(str(form.get("payload", "")).encode()).decode())
        suggestions = blob["suggestions"]
        job = blob["job"]
    except Exception:
        flash(request, "That tailoring session expired. Please analyse the job again.", "error")
        return RedirectResponse(f"/resumes/{resume_id}/tailor", status_code=303)
    accepted = {str(v) for v in form.getlist("accept")}
    inputs = {s["id"]: str(form.get(f"input_{s['id']}", "")) for s in suggestions if s.get("needs_input")}
    new_data = tailor.apply(r["data"], suggestions, accepted, inputs)
    before = tailor.posting_match(r["data"], job["title"], job["description"])["score"]
    after = tailor.posting_match(new_data, job["title"], job["description"])["score"]
    score = score_resume(new_data)
    applied_titles = [s["title"] for s in suggestions if s["id"] in accepted]
    score["tailoring"] = {"job_title": job["title"], "company": job.get("company", ""), "before": before,
                          "after": after, "applied": applied_titles[:12], "source_resume": r["title"]}
    title = f"{r['title'].split(' - ')[0]} - {tailor.clean_title(job['title'])}"[:120]
    rid = store.create_resume(user["id"], title, new_data, score, source="tailored", parent_id=r["id"],
                              job_title=job["title"], job_company=job.get("company", ""), template=r["template"],
                              accent=r["accent"])
    db.audit(user["id"], "resume_tailored", f"resume:{rid}", f"{job['title']} before={before} after={after}")
    flash(request, f"Tailored resume created. Match with this job: {before:g} -> {after:g}.", "ok")
    return RedirectResponse(f"/resumes/{rid}", status_code=303)


# --------------------------------------------------------------------------- jobs + agents
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, user: dict = Depends(require_user)):
    q = request.query_params
    tab = q.get("tab", "agents")
    try:
        min_score = float(q.get("min", 0) or 0)
    except ValueError:
        min_score = 0.0
    status = q.get("status") or None
    results = store.list_results(user["id"], status=status, min_score=min_score, limit=150) if tab == "matches" else []
    return render(request, "jobs.html", tab=tab, agents=store.list_agents(user["id"]), results=results,
                  min_score=min_score, status=status or "", sources=providers.provider_status(),
                  counts=store.counts(user["id"]), has_resume=bool(store.default_resume(user["id"])))


def _agent_form_ctx(user: dict, agent: dict | None) -> dict:
    return {"agent": agent, "resumes": store.list_resumes(user["id"]), "sources": providers.provider_status()}


@app.get("/jobs/agents/new", response_class=HTMLResponse)
def agent_new(request: Request, user: dict = Depends(require_user)):
    return render(request, "agent_form.html", **_agent_form_ctx(user, None))


@app.get("/jobs/agents/{agent_id}/edit", response_class=HTMLResponse)
def agent_edit(agent_id: int, request: Request, user: dict = Depends(require_user)):
    a = store.get_agent(user["id"], agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    return render(request, "agent_form.html", **_agent_form_ctx(user, a))


@app.post("/jobs/agents/save")
async def agent_save(request: Request, user: dict = Depends(require_user)):
    f = await request.form()
    aid = int(f["agent_id"]) if str(f.get("agent_id", "")).isdigit() else None
    if aid and not store.get_agent(user["id"], aid):
        raise HTTPException(404, "Agent not found")
    query = str(f.get("query", "")).strip()
    if len(query) < 2:
        flash(request, "Tell the agent what role to look for (for example 'Python developer').", "error")
        return RedirectResponse(f"/jobs/agents/{aid}/edit" if aid else "/jobs/agents/new", status_code=303)
    locs = [l.strip() for l in re.split(r"[,\n;]", str(f.get("locations", ""))) if l.strip()][:5]
    modes = [m for m in f.getlist("work_modes") if m in ("remote", "onsite", "hybrid")] or ["remote", "onsite", "hybrid"]
    level = str(f.get("level", "entry"))
    level = level if level in ("entry", "junior", "mid", "any") else "entry"
    valid_sources = set(providers.REGISTRY)
    sources = [s for s in f.getlist("sources") if s in valid_sources]
    rid = int(f["resume_id"]) if str(f.get("resume_id", "")).isdigit() else None
    if rid and not store.get_resume(user["id"], rid):
        rid = None
    try:
        min_score = max(0.0, min(float(f.get("min_score", 0) or 0), 100.0))
    except ValueError:
        min_score = 0.0
    name = str(f.get("name", "")).strip() or query
    new_id = store.save_agent(user["id"], aid, name, query, locs, modes, level, sources, rid, min_score,
                              bool(f.get("active", "1")))
    db.audit(user["id"], "agent_saved", f"agent:{new_id}", query)
    if not aid:
        try:
            res = agent_mod.run_agent(user["id"], new_id)
            flash(request, f"Agent created and ran once: {res['found']} postings found, {res['new']} new matches.", "ok")
        except Exception:
            flash(request, "Agent created. The first search will run shortly.", "info")
    else:
        flash(request, "Agent updated.", "ok")
    return RedirectResponse(f"/jobs/agents/{new_id}", status_code=303)


@app.get("/jobs/agents/{agent_id}", response_class=HTMLResponse)
def agent_detail(agent_id: int, request: Request, user: dict = Depends(require_user)):
    a = store.get_agent(user["id"], agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    status = request.query_params.get("status") or None
    return render(request, "agent_detail.html", agent=a, results=store.list_results(user["id"], agent_id=agent_id,
                  status=status, limit=150), runs=store.last_runs(agent_id, 3), status=status or "",
                  resume=(store.get_resume(user["id"], a["resume_id"]) if a["resume_id"] else store.default_resume(user["id"])))


@app.post("/jobs/agents/{agent_id}/run")
def agent_run(agent_id: int, request: Request, user: dict = Depends(require_user)):
    if not store.get_agent(user["id"], agent_id):
        raise HTTPException(404, "Agent not found")
    res = agent_mod.run_agent(user["id"], agent_id)
    if res.get("error") == "no_resume":
        flash(request, "Add a resume first so the agent can score matches.", "error")
    else:
        flash(request, f"Search finished: {res['found']} postings checked, {res['new']} new match(es).", "ok")
    return RedirectResponse(f"/jobs/agents/{agent_id}", status_code=303)


@app.post("/jobs/agents/{agent_id}/toggle")
def agent_toggle(agent_id: int, request: Request, user: dict = Depends(require_user)):
    a = store.get_agent(user["id"], agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    db.run("UPDATE job_agents SET active=? WHERE id=? AND user_id=?", (0 if a["active"] else 1, agent_id, user["id"]))
    return RedirectResponse(request.headers.get("referer") or "/jobs", status_code=303)


@app.post("/jobs/agents/{agent_id}/delete")
def agent_delete(agent_id: int, request: Request, user: dict = Depends(require_user)):
    if not store.get_agent(user["id"], agent_id):
        raise HTTPException(404, "Agent not found")
    store.delete_agent(user["id"], agent_id)
    flash(request, "Agent deleted. Jobs you saved or applied to are kept.", "ok")
    return RedirectResponse("/jobs", status_code=303)


@app.get("/jobs/{result_id}", response_class=HTMLResponse)
def job_detail(result_id: int, request: Request, user: dict = Depends(require_user)):
    j = store.get_result(user["id"], result_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if j["status"] == "new":
        store.set_result_status(user["id"], result_id, "seen")
        j["status"] = "seen"
    return render(request, "job_detail.html", j=j, m=j["match"], default=store.default_resume(user["id"]),
                  resumes=store.list_resumes(user["id"]))


@app.post("/jobs/{result_id}/status")
def job_status(result_id: int, request: Request, status: str = Form(...), user: dict = Depends(require_user)):
    if status not in ("seen", "saved", "applied", "interview", "offer", "rejected", "dismissed"):
        raise HTTPException(400, "Invalid status")
    if not store.get_result(user["id"], result_id):
        raise HTTPException(404, "Job not found")
    store.set_result_status(user["id"], result_id, status)
    if status == "dismissed":
        flash(request, "Hidden from your matches.", "ok")
        return RedirectResponse(request.headers.get("referer") or "/jobs?tab=matches", status_code=303)
    return RedirectResponse(request.headers.get("referer") or f"/jobs/{result_id}", status_code=303)


@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request, user: dict = Depends(require_user)):
    cols = {}
    for st in ("saved", "applied", "interview", "offer", "rejected"):
        cols[st] = store.list_results(user["id"], status=st, limit=100)
    return render(request, "applications.html", cols=cols)


# --------------------------------------------------------------------------- settings
@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, user: dict = Depends(require_user)):
    return render(request, "settings.html", sources=providers.provider_status())


@app.post("/settings/delete")
def delete_account(request: Request, confirm: str = Form(""), user: dict = Depends(require_user)):
    if confirm.strip().upper() != "DELETE":
        flash(request, "Type DELETE to confirm.", "error")
        return RedirectResponse("/settings", status_code=303)
    uid = user["id"]
    for sql in ("DELETE FROM job_results WHERE user_id=?",
                "DELETE FROM agent_runs WHERE agent_id IN (SELECT id FROM job_agents WHERE user_id=?)",
                "DELETE FROM job_agents WHERE user_id=?", "DELETE FROM resumes WHERE user_id=?",
                "DELETE FROM decisions WHERE application_id IN (SELECT id FROM applications WHERE candidate_id=?)",
                "DELETE FROM applications WHERE candidate_id=?", "DELETE FROM candidate_profiles WHERE user_id=?",
                "DELETE FROM users WHERE id=?"):
        db.run(sql, (uid,))
    db.audit(None, "account_deleted", f"user:{uid}", "candidate requested deletion of all data")
    request.session.clear()
    flash(request, "Your account and all data have been deleted.", "ok")
    return RedirectResponse("/", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, message=exc.detail)
