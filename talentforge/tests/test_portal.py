"""Candidate portal tests: resume parsing/scoring, tailoring, job providers, agent and routes."""
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("TALENTFORGE_DB", str(Path(tempfile.mkdtemp()) / "portal-test.db"))
os.environ["PORTAL_AGENT_SCHEDULER"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from app import db
from portal import agent as agent_mod
from portal import providers, store, tailor
from portal.main import app
from portal.resume_score import score_resume, starts_weak
from portal.resume_struct import empty_resume, from_text, normalize

ROOT = Path(__file__).resolve().parent.parent
RESUME = (ROOT / "samples" / "sample_resume_fresher.txt").read_text(encoding="utf8")
JOB = (ROOT / "samples" / "job_backend_developer.txt").read_text(encoding="utf8")


# ------------------------------------------------------------------ structure + score
def test_parse_structure():
    r = from_text(RESUME)
    assert r["basics"]["name"] == "Riya Deshmukh"
    assert r["basics"]["email"] == "riya.deshmukh@demo.example"
    assert r["basics"]["headline"].lower().startswith("aspiring backend")
    assert "flask" in [s.lower() for s in r["skills"]]
    assert len(r["projects"]) == 2 and r["projects"][0]["bullets"]
    assert len(r["experience"]) == 1
    exp = r["experience"][0]
    assert "Intern" in exp["role"] and "Brightpath" in exp["company"] and exp["start"].endswith("2025")
    assert r["education"] and r["education"][0]["year"] == "2026"
    assert r["certifications"]
    assert r["basics"]["location"] == "Pune, India"


def test_normalize_is_defensive():
    n = normalize({"basics": "nope", "skills": ["a", "A", "b"], "experience": [1, {"role": "x" * 999}]})
    assert n["skills"] == ["a", "b"] and len(n["experience"]) == 1 and len(n["experience"][0]["role"]) <= 160


def test_score_blank_vs_good():
    blank = score_resume(empty_resume())
    good = score_resume(from_text(RESUME))
    assert blank["score"] < 25 < good["score"] <= 100
    assert abs(sum(c["points"] for c in good["checks"]) - good["score"]) < 0.5
    assert good["top_fixes"]
    assert starts_weak("Responsible for building APIs") and not starts_weak("Built APIs")


# ------------------------------------------------------------------ tailoring
def test_tailor_suggestions_and_apply():
    r = from_text(RESUME)
    res = tailor.analyze(r, "Backend Developer (Python)", "Acme", JOB)
    ids = {s["id"]: s for s in res["suggestions"]}
    assert res["match"]["score"] > 0
    assert "docker" in res["match"]["missing"]
    assert ids["skill:docker"]["default_on"] is False          # skills are never auto-added
    assert ids["sum"]["default_on"]
    verb_ids = [i for i in ids if i.startswith("verb:")]
    assert verb_ids, "weak openers should be flagged"
    metric_ids = [i for i in ids if i.startswith("metric:")]
    assert metric_ids and ids[metric_ids[0]]["needs_input"]

    accepted = {"sum", "skill:docker", verb_ids[0], metric_ids[0]}
    new = tailor.apply(r, res["suggestions"], accepted, {metric_ids[0]: "serving 3 user roles"})
    assert "docker" in [s.lower() for s in new["skills"]]
    assert new["summary"] == ids["sum"]["suggested"]
    assert any("serving 3 user roles" in b for e in new["projects"] + new["experience"] for b in e["bullets"])
    assert tailor.posting_match(new, "Backend Developer (Python)", JOB)["score"] > res["match"]["score"]
    assert r["summary"].startswith("Final-year")              # original untouched


def test_apply_ignores_unknown_ids_and_blank_inputs():
    r = from_text(RESUME)
    res = tailor.analyze(r, "Backend Developer", "", JOB)
    assert tailor.apply(r, res["suggestions"], {"does-not-exist"}, {}) == normalize(r)


# ------------------------------------------------------------------ providers (HTTP mocked)
def test_remotive_and_arbeitnow_normalisation(monkeypatch):
    def fake(url, params=None, headers=None):
        if "remotive" in url:
            return {"jobs": [{"id": 7, "title": "Python Developer", "company_name": "Acme", "url": "https://x/7",
                              "candidate_required_location": "Worldwide", "description": "<p>Build <b>APIs</b></p>",
                              "publication_date": "2026-10-01T10:00:00", "tags": ["python"], "salary": ""}]}
        return {"data": [
            {"slug": "py-1", "title": "Python Engineer", "company_name": "Beta", "location": "Berlin",
             "remote": True, "url": "https://y/1", "description": "<ul><li>Python</li></ul>", "tags": ["python"],
             "created_at": 1760000000},
            {"slug": "cook-1", "title": "Cook", "company_name": "Diner", "location": "Rome", "remote": False,
             "url": "https://y/2", "description": "Kitchen", "tags": [], "created_at": 1}]}

    monkeypatch.setattr(providers, "_get_json", fake)
    a = providers.Remotive().search("python")
    assert a[0].title == "Python Developer" and a[0].description == "Build APIs" and a[0].remote is True
    b = providers.Arbeitnow().search("python")
    assert [j.external_id for j in b] == ["py-1"]            # keyword filter drops the cook
    assert "- Python" in b[0].description


def test_keyed_providers_unavailable_without_keys(monkeypatch):
    for k in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "RAPIDAPI_KEY"):
        monkeypatch.delenv(k, raising=False)
    status = {s["id"]: s["available"] for s in providers.provider_status()}
    assert status["remotive"] and not status["adzuna"] and not status["jsearch"]


# ------------------------------------------------------------------ agent
class FakeProvider(providers.Provider):
    def __init__(self, pid, jobs, fail=False):
        self.id, self.label, self._jobs, self._fail = pid, pid.title(), jobs, fail
        self.needs_key = None

    def search(self, query, location="", remote_only=False, limit=40):
        if self._fail:
            raise RuntimeError("boom")
        return list(self._jobs)


def _jp(src, i, title, company, loc="Remote", remote=True, desc=JOB):
    return providers.JobPosting(source=src, external_id=str(i), title=title, company=company, location=loc,
                                url=f"https://example.test/{src}/{i}", description=desc, remote=remote)


def _register(c, email):
    r = c.post("/register", data={"name": "Riya Deshmukh", "email": email, "password": "longenough1",
                                  "consent": "1"}, follow_redirects=False)
    assert r.status_code == 303
    return db.one("SELECT id FROM users WHERE email=?", (email,))["id"]


def test_agent_run_dedupes_filters_scores():
    with TestClient(app) as c:
        uid = _register(c, "agent@demo.example")
        data = from_text(RESUME)
        store.create_resume(uid, "Base", data, score_resume(data), "upload")
        aid = store.save_agent(uid, None, "Backend", "Python developer", [], ["remote", "onsite", "hybrid"],
                               "entry", [], None, 0, True)
        reg = {
            "a": FakeProvider("a", [
                _jp("a", 1, "Python Developer", "Acme"),
                _jp("a", 2, "Senior Staff Engineer", "BigCo"),                          # above level
                _jp("a", 3, "Chef", "Diner", desc="Cook food in a restaurant kitchen"),  # irrelevant
                _jp("a", 4, "Account Executive", "GitLab", desc="Sell GitLab to teams. Strong communication."),
            ]),
            "b": FakeProvider("b", [_jp("b", 9, "Python Developer", "Acme")]),          # duplicate of a/1
            "c": FakeProvider("c", [], fail=True),                                      # broken source
        }
        res = agent_mod.run_agent(uid, aid, registry=reg)
        titles = [r["title"] for r in store.list_results(uid, agent_id=aid)]
        assert titles.count("Python Developer") == 1
        assert "Senior Staff Engineer" not in titles
        assert "Account Executive" not in titles and "Chef" not in titles   # unrelated roles are dropped
        top = store.list_results(uid, agent_id=aid)[0]
        assert top["title"] == "Python Developer" and top["match_score"] > 40
        steps = [s["step"] for s in res["log"]]
        assert "plan" in steps and "dedupe" in steps and "source_error" in steps and steps[-1] == "done"
        assert agent_mod.run_agent(uid, aid, registry=reg)["new"] == 0       # idempotent


def test_agent_without_resume_reports_clearly():
    with TestClient(app) as c:
        uid = _register(c, "noresume@demo.example")
        aid = store.save_agent(uid, None, "x", "python", [], ["remote"], "entry", [], None, 0, True)
        assert agent_mod.run_agent(uid, aid, registry={}).get("error") == "no_resume"


# ------------------------------------------------------------------ routes
def test_pages_require_login():
    with TestClient(app) as c:
        for path in ("/dashboard", "/resumes", "/jobs", "/applications", "/settings"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and "/login" in r.headers["location"], path


def test_full_candidate_journey(monkeypatch):
    with TestClient(app) as c:
        _register(c, "journey@demo.example")
        assert c.get("/dashboard").status_code == 200

        r = c.post("/resumes/upload", files={"resume": ("riya.txt", RESUME.encode(), "text/plain")},
                   follow_redirects=False)
        assert r.status_code == 303
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        page = c.get(f"/resumes/{rid}")
        assert page.status_code == 200 and "Riya Deshmukh" in page.text and "ATS-readiness" in page.text

        edit = c.get(f"/resumes/{rid}/edit")
        assert edit.status_code == 200 and "resume-data" in edit.text
        d = from_text(RESUME)
        d["summary"] = "Backend-focused engineering graduate who builds REST APIs with Python and Flask."
        saved = c.post(f"/api/resumes/{rid}", json={"data": d, "title": "Riya base", "template": "modern"})
        assert saved.status_code == 200 and saved.json()["score"] > 0

        r = c.post("/resumes/new", data={"title": "Scratch"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/edit")

        r = c.post(f"/resumes/{rid}/tailor", data={"title": "Backend Developer", "company": "Acme",
                                                   "description": JOB})
        assert r.status_code == 200 and "Create tailored resume" in r.text
        payload = re.search(r'name="payload" value="([^"]+)"', r.text).group(1)
        r = c.post(f"/resumes/{rid}/tailor/apply", data={"payload": payload, "accept": ["sum", "hl"]},
                   follow_redirects=False)
        assert r.status_code == 303
        new_id = int(r.headers["location"].rsplit("/", 1)[1])
        assert new_id != rid
        view = c.get(f"/resumes/{new_id}")
        assert "Tailored resume" in view.text and "Match with Backend Developer" in view.text

        docx = c.get(f"/resumes/{new_id}/download.docx")
        assert docx.status_code == 200 and docx.content[:2] == b"PK"
        assert c.get(f"/resumes/{new_id}/print").status_code == 200

        fake = {"fake": FakeProvider("fake", [_jp("fake", 1, "Python Developer", "Acme"),
                                              _jp("fake", 2, "Backend Developer", "Beta")])}
        monkeypatch.setattr(providers, "REGISTRY", fake)
        r = c.post("/jobs/agents/save", data={"query": "Python developer", "level": "entry",
                                              "work_modes": ["remote"], "sources": ["fake"], "active": "1"},
                   follow_redirects=False)
        assert r.status_code == 303
        page = c.get(r.headers["location"])
        assert page.status_code == 200 and "What the agent did" in page.text and "Python Developer" in page.text

        jobs = c.get("/jobs?tab=matches")
        assert jobs.status_code == 200 and "Backend Developer" in jobs.text
        job_id = int(re.search(r'href="/jobs/(\d+)"', jobs.text).group(1))
        detail = c.get(f"/jobs/{job_id}")
        assert detail.status_code == 200 and "Tailor my resume for this job" in detail.text
        assert c.get(f"/resumes/{rid}/tailor?job={job_id}").status_code == 200

        c.post(f"/jobs/{job_id}/status", data={"status": "applied"})
        assert "applied" in c.get("/applications").text.lower()

        r = c.post("/settings/delete", data={"confirm": "DELETE"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/dashboard", follow_redirects=False).status_code == 303


def test_other_users_cannot_touch_my_resume():
    with TestClient(app) as a, TestClient(app) as b:
        _register(a, "ownera@demo.example")
        _register(b, "ownerb@demo.example")
        r = a.post("/resumes/new", data={"title": "Private"}, follow_redirects=False)
        rid = r.headers["location"].split("/")[2]
        assert a.get(f"/resumes/{rid}").status_code == 200
        assert b.get(f"/resumes/{rid}").status_code == 404
        assert b.post(f"/api/resumes/{rid}", json={"data": {}}).status_code == 404
