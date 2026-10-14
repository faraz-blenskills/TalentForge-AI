"""TalentForge candidate portal (resume upload / builder / tailoring + job-search agents)."""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import time
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
from . import ai, improve, providers, store, styles, supa_auth, tailor
from .doc_extract import extract_text
from .export import resume_to_docx
from .resume_score import score_resume
from .resume_struct import empty_resume, normalize, parse_with_report
from .supa_auth import supa

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel rejects request bodies over 4.5 MB, so keep a safety margin there
MAX_UPLOAD = (4 if ON_VERCEL else 5) * 1024 * 1024
SCHEDULER = agent_mod.Scheduler(int(os.environ.get("PORTAL_AGENT_INTERVAL_MIN", "120")))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Serverless: tables are created once with `python -m portal.dbtool init`, not on every cold start,
    # and there is no long-lived process for a background scheduler (Vercel Cron calls /internal/cron/agents).
    if not ON_VERCEL or os.environ.get("RUN_DB_INIT") == "1":
        db.init_db()
        store.init()
    if not ON_VERCEL and os.environ.get("PORTAL_AGENT_SCHEDULER", "1") != "0":
        SCHEDULER.start()
    yield
    SCHEDULER.stop()


_HTTPS = ON_VERCEL or os.environ.get("SITE_URL", "").startswith("https://")
app = FastAPI(title="TalentForge Portal", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=session_secret(ROOT), same_site="lax", max_age=60 * 60 * 8,
                   https_only=_HTTPS)
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


def _password_login_allowed() -> bool:
    return os.environ.get("ALLOW_PASSWORD_LOGIN", "1") != "0"


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", current_user(request))
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["llm_on"] = ai.enabled()
    ctx["supa_on"] = supa.enabled
    ctx["allow_password"] = _password_login_allowed()
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


@app.get("/internal/cron/agents", response_class=JSONResponse)
def cron_run_agents(request: Request):
    """Called by Vercel Cron (which sends `Authorization: Bearer $CRON_SECRET`). Runs the agents that are due."""
    secret = os.environ.get("CRON_SECRET", "")
    got = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(got.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(401, "Unauthorized")
    return agent_mod.run_due_agents(limit=int(os.environ.get("CRON_AGENT_LIMIT", "5")))


@app.get("/healthz", response_class=Response)
def healthz():
    return Response("ok", media_type="text/plain")


# --------------------------------------------------------------------------- landing + auth
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return render(request, "landing.html", sources=providers.provider_status())


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if not _password_login_allowed():
        return RedirectResponse("/login", status_code=303)
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
    step = request.query_params.get("step", "")
    otp_email = request.session.get("otp_email", "") if step == "code" else ""
    return render(request, "login.html", next=request.query_params.get("next", ""), step=step, otp_email=otp_email)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("")):
    if not _password_login_allowed():
        flash(request, "Password sign-in is turned off. Use Google or an email code.", "error")
        return RedirectResponse("/login", status_code=303)
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


# --------------------------------------------------------------------------- Supabase sign-in (Google + email code)
_OTP_BY_EMAIL: dict[str, list[float]] = {}
_OTP_BY_IP: dict[str, list[float]] = {}


def _allow(bucket: dict, key: str, limit: int, window: float = 600.0) -> bool:
    """Sliding-window limiter: at most `limit` events per `window` seconds for `key`."""
    now = time.time()
    hits = [t for t in bucket.get(key, []) if now - t < window]
    allowed = len(hits) < limit
    if allowed:
        hits.append(now)
    bucket[key] = hits
    return allowed


def _site_url(request: Request) -> str:
    """Public origin of this request, so sign-in returns to whichever address the user actually used
    (localhost, a LAN address, a tunnel or the Vercel domain). Supabase only honours redirect URLs that are
    on its allow-list, so a spoofed Host header cannot send anyone elsewhere."""
    if os.environ.get("SITE_URL_FORCE") and os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/")
    host = request.headers.get("x-forwarded-host")
    if host:
        scheme = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
        return f"{scheme}://{host.split(',')[0].strip()}"
    return str(request.base_url).rstrip("/")


def _login_with_auth_user(request: Request, au: supa_auth.AuthUser) -> bool:
    """Map a verified Supabase identity to our own users row (creating or linking it) and start a session."""
    u = db.one("SELECT * FROM users WHERE auth_uid=?", (au.uid,))
    if not u:
        u = db.one("SELECT * FROM users WHERE email=?", (au.email,))
        if u:
            if not au.email_verified:
                flash(request, "Verify your email before linking it to an existing account.", "error")
                return False
            db.run("UPDATE users SET auth_uid=? WHERE id=?", (au.uid, u["id"]))
        else:
            uid = db.run(
                "INSERT INTO users(email, name, password_hash, role, consent_at, created_at, auth_uid) "
                "VALUES (?,?,?,?,?,?,?)", (au.email, au.name, "!external", "candidate", db.now(), db.now(), au.uid))
            u = {"id": uid, "role": "candidate"}
            db.audit(uid, "register", f"user:{uid}", "candidate via supabase")
    if u["role"] != "candidate":
        flash(request, "This portal is for candidate accounts.", "error")
        return False
    request.session.clear()                    # new session on privilege change
    request.session["uid"] = u["id"]
    db.audit(u["id"], "login", f"user:{u['id']}", "supabase")
    return True


@app.get("/auth/google")
def auth_google(request: Request):
    if not supa.enabled:
        flash(request, "Google sign-in is not configured on this server yet.", "error")
        return RedirectResponse("/login", status_code=303)
    verifier, challenge = supa_auth.new_pkce()
    request.session["pkce"] = verifier
    return RedirectResponse(supa.google_url(f"{_site_url(request)}/auth/callback", challenge), status_code=303)


@app.get("/auth/callback")
def auth_callback(request: Request):
    q = request.query_params
    verifier = request.session.pop("pkce", "")
    if q.get("error") or q.get("error_description"):
        flash(request, "Google sign-in was cancelled or refused. You can try again.", "error")
        return RedirectResponse("/login", status_code=303)
    try:
        au = supa.exchange_code(q.get("code", ""), verifier)
    except supa_auth.AuthError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/login", status_code=303)
    if not _login_with_auth_user(request, au):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/auth/otp/request")
def otp_request(request: Request, email: str = Form(...), name: str = Form("")):
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        flash(request, "Please enter a valid email address.", "error")
        return RedirectResponse("/login", status_code=303)
    ip = request.client.host if request.client else "unknown"
    if not _allow(_OTP_BY_EMAIL, email, 3) or not _allow(_OTP_BY_IP, ip, 10):
        flash(request, "Too many code requests. Please wait a few minutes and try again.", "error")
        return RedirectResponse("/login", status_code=303)
    verifier, challenge = supa_auth.new_pkce()
    try:
        supa.otp_request(email, name.strip()[:120], challenge=challenge,
                         redirect_to=f"{_site_url(request)}/auth/callback")
    except supa_auth.AuthError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/login", status_code=303)
    request.session["pkce"] = verifier             # lets the emailed link finish signing in on this browser
    request.session["otp_email"] = email
    request.session["otp_tries"] = 0
    flash(request, f"We emailed {email} a sign-in link. If the email also shows a 6-digit code you can type it "
                   "below instead. It expires in a few minutes.", "ok")
    return RedirectResponse("/login?step=code", status_code=303)


@app.post("/auth/otp/verify")
def otp_verify(request: Request, code: str = Form(...)):
    email = request.session.get("otp_email")
    if not email:
        flash(request, "Request a code first.", "error")
        return RedirectResponse("/login", status_code=303)
    tries = int(request.session.get("otp_tries", 0)) + 1
    if tries > 5:
        request.session.pop("otp_email", None)
        flash(request, "Too many wrong codes. Please request a new one.", "error")
        return RedirectResponse("/login", status_code=303)
    request.session["otp_tries"] = tries
    try:
        au = supa.otp_verify(email, code)
    except supa_auth.AuthError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/login?step=code", status_code=303)
    if au.email != email:
        flash(request, "That code does not belong to this email address.", "error")
        return RedirectResponse("/login", status_code=303)
    if not _login_with_auth_user(request, au):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


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
