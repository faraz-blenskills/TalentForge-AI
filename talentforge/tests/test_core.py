"""Core behaviour tests. Run from the talentforge folder:  python -m pytest -q"""
import copy
import os
import tempfile
from pathlib import Path

os.environ["TALENTFORGE_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.main import app
from app.matching import anonymise, identity_blind_check, match_job, rank_candidates
from app.parsing import parse_resume
from app.scoring import employability, skill_gap, what_if

RESUME = """Jane Example
jane@example.com

EDUCATION
B.Tech in Computer Science, Test Institute of Technology, 2025

SKILLS
Python, SQL, FastAPI, Docker, Git, REST API

EXPERIENCE
Backend Intern, Acme Corp, Jan 2025 - Jun 2025
- Built REST APIs with FastAPI

PROJECTS
Todo API
- FastAPI and PostgreSQL service
Blog engine
- Flask app

CERTIFICATIONS
AWS Cloud Practitioner
"""


def test_parser_extracts_structure():
    p = parse_resume(RESUME)
    names = {s["name"] for s in p["skills"]}
    assert {"python", "sql", "fastapi", "docker", "git", "rest api"} <= names
    assert p["email"] == "jane@example.com"
    assert p["highest_degree_level"] == 2 and p["cs_related"]
    assert 0.4 <= p["experience_years"] <= 0.6
    assert len(p["projects"]) == 2
    assert len(p["certifications"]) == 1


def test_skill_gap_matched_partial_missing():
    p = parse_resume(RESUME)
    gap = skill_gap(p, {"python": 3, "django": 2, "kubernetes": 2, "react": 1})
    status = {i["skill"]: i["status"] for i in gap["items"]}
    assert status["python"] == "matched"
    assert status["django"] == "partial"     # fastapi is a related framework
    assert status["kubernetes"] == "partial"  # docker is related
    assert status["react"] == "missing"
    assert 0 < gap["coverage"] < 1


def test_employability_is_bounded_and_explained():
    p = parse_resume(RESUME)
    res = employability(p, "backend")
    assert 0 <= res["score"] <= 100
    assert abs(sum(f["weight"] for f in res["factors"]) - 1.0) < 1e-9
    assert all(f["detail"] for f in res["factors"])
    assert round(sum(f["points"] for f in res["factors"]), 1) == pytest.approx(res["score"], abs=0.2)


def test_identity_is_not_a_scoring_input():
    p = parse_resume(RESUME)
    q = copy.deepcopy(p)
    q["name"] = "Someone Else Entirely"
    q["email"] = "other@else.org"
    q["education"][0]["text"] = "B.Tech in Computer Science, A Different University, 2025"
    assert employability(p, "backend")["score"] == employability(q, "backend")["score"]


def test_what_if_adding_missing_skill_never_lowers_score():
    p = parse_resume(RESUME)
    sim = what_if(p, "backend", ["linux"], 1, 0, 0)
    assert sim["new"] >= sim["base"]


def test_ranking_stable_when_identities_removed():
    job = {"title": "Backend Developer", "description": "Python REST APIs with FastAPI and SQL",
           "required": {"python": 3, "sql": 3, "fastapi": 2}, "preferred": ["docker"], "min_years": 0}
    cands = []
    for i, text in enumerate([RESUME, RESUME.replace("FastAPI", "Flask"), "Plain Person\nSKILLS\nExcel"], 1):
        cands.append({"application_id": i, "profile": parse_resume(text), "resume_text": text})
    assert identity_blind_check(job, cands)["identical"]
    ranked = rank_candidates(job, cands)
    assert ranked[0]["application_id"] == 1 and ranked[0]["rank"] == 1
    assert anonymise(cands[0]["profile"], "Candidate 1")["email"] == ""


@pytest.fixture(scope="module")
def client():
    seed.seed(force=True)
    with TestClient(app) as c:
        yield c


def _login(c, email):
    r = c.post("/login", data={"email": email, "password": seed.DEMO_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303


def test_role_protection(client):
    r = client.get("/employer", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    _login(client, "aarav.menon@demo.example")
    assert client.get("/employer").status_code == 403
    assert client.get("/admin").status_code == 403
    client.post("/logout")


def test_candidate_journey(client):
    _login(client, "aarav.menon@demo.example")
    for path in ["/candidate/dashboard", "/candidate/gap", "/candidate/score", "/candidate/jobs",
                 "/candidate/applications", "/candidate/profile", "/candidate/target"]:
        r = client.get(path)
        assert r.status_code == 200, path
    r = client.get("/candidate/score?simulate=1&skills=docker&projects=1&certs=0&years=0")
    assert "Simulated result" in r.text
    r = client.post("/candidate/jobs/3/apply", follow_redirects=False)  # LLM role: new application
    assert r.status_code == 303
    client.post("/logout")


def test_employer_flow_and_human_decision(client):
    _login(client, "hr@nimbus.example")
    assert client.get("/employer").status_code == 200
    r = client.get("/employer/jobs/1/candidates")
    assert r.status_code == 200 and "Identity-blind check passed" in r.text
    blind = client.get("/employer/jobs/1/candidates?blind=1")
    assert "Candidate #" in blind.text and "Aarav Menon" not in blind.text
    # a decision without a reason is refused
    r = client.post("/employer/applications/1/decision", data={"decision": "reject", "reason": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/employer/applications/1/decision",
                    data={"decision": "shortlist", "reason": "Strong SQL and dashboard projects"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Strong SQL" in client.get("/employer/applications/1").text
    csv = client.get("/employer/jobs/1/shortlist.csv")
    assert csv.status_code == 200 and "match_score" in csv.text
    # employer cannot see another organisation's job
    assert client.get("/employer/jobs/6/candidates").status_code == 404
    client.post("/logout")


def test_registration_requires_consent(client):
    r = client.post("/register", data={"name": "X", "email": "x@demo.example", "password": "longenough1",
                                       "role": "candidate"}, follow_redirects=False)
    assert r.status_code == 303 and "register" in r.headers["location"]
