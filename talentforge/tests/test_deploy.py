"""Serverless / hosted deployment behaviour."""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("TALENTFORGE_DB", str(Path(tempfile.mkdtemp()) / "deploy-test.db"))
os.environ["PORTAL_AGENT_SCHEDULER"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from app import db
from app.security import session_secret
from portal import agent as agent_mod
from portal import providers, store
from portal.main import _site_url, app
from portal.resume_score import score_resume
from portal.resume_struct import empty_resume


class _Req:
    def __init__(self, headers, base="http://127.0.0.1:8000/"):
        self.headers, self.base_url = headers, base


def test_site_url_uses_the_address_the_user_actually_visited(monkeypatch):
    monkeypatch.delenv("SITE_URL_FORCE", raising=False)
    assert _site_url(_Req({})) == "http://127.0.0.1:8000"
    assert _site_url(_Req({"x-forwarded-host": "talentforge.vercel.app", "x-forwarded-proto": "https"})) \
        == "https://talentforge.vercel.app"
    # a proxy chain: first value wins
    assert _site_url(_Req({"x-forwarded-host": "a.example, b.example", "x-forwarded-proto": "https, http"})) \
        == "https://a.example"
    monkeypatch.setenv("SITE_URL", "https://forced.example")
    assert _site_url(_Req({})) == "http://127.0.0.1:8000"                 # SITE_URL alone does not override
    monkeypatch.setenv("SITE_URL_FORCE", "1")
    assert _site_url(_Req({})) == "https://forced.example"


def test_production_refuses_to_guess_the_session_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("TALENTFORGE_SECRET", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="TALENTFORGE_SECRET"):
        session_secret(tmp_path)
    monkeypatch.setenv("TALENTFORGE_SECRET", "x" * 40)
    assert session_secret(tmp_path) == "x" * 40
    assert not (tmp_path / "data").exists()            # nothing was written to the (read-only) disk


def test_cron_endpoint_requires_the_secret(monkeypatch):
    with TestClient(app) as c:
        monkeypatch.delenv("CRON_SECRET", raising=False)
        assert c.get("/internal/cron/agents").status_code == 401                     # no secret configured
        monkeypatch.setenv("CRON_SECRET", "s3cret-value")
        assert c.get("/internal/cron/agents").status_code == 401
        assert c.get("/internal/cron/agents", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = c.get("/internal/cron/agents", headers={"Authorization": "Bearer s3cret-value"})
        assert ok.status_code == 200 and set(ok.json()) == {"due", "ran", "failed", "new_matches"}


class _Fake(providers.Provider):
    id, label, needs_key = "fake", "Fake", None

    def search(self, query, location="", remote_only=False, limit=40):
        return [providers.JobPosting(source="fake", external_id="1", title="Python Developer", company="Acme",
                                     location="Remote", url="https://example.test/1", remote=True,
                                     description="Python developer building REST APIs with FastAPI and SQL.")]


def test_run_due_agents_only_runs_stale_active_agents(monkeypatch):
    monkeypatch.setattr(providers, "REGISTRY", {"fake": _Fake()})
    with TestClient(app) as c:
        email = "cron-user@demo.example"
        c.post("/register", data={"name": "Cron User", "email": email, "password": "longenough1", "consent": "1"})
        uid = db.one("SELECT id FROM users WHERE email=?", (email,))["id"]
        d = empty_resume("Cron User", email)
        d["skills"] = ["Python", "SQL", "FastAPI"]
        store.create_resume(uid, "base", d, score_resume(d), "builder")
        fresh = store.save_agent(uid, None, "fresh", "python developer", [], ["remote"], "entry", ["fake"], None, 0, True)
        stale = store.save_agent(uid, None, "stale", "python developer", [], ["remote"], "entry", ["fake"], None, 0, True)
        paused = store.save_agent(uid, None, "paused", "python developer", [], ["remote"], "entry", ["fake"], None, 0, False)
        db.run("UPDATE job_agents SET last_run_at=? WHERE id=?", (db.now(), fresh))            # ran just now
        db.run("UPDATE job_agents SET last_run_at='2020-01-01 00:00:00' WHERE id=?", (stale,))
        res = agent_mod.run_due_agents(limit=5)
        assert res["ran"] >= 1 and res["failed"] == 0
        assert db.one("SELECT last_run_at FROM job_agents WHERE id=?", (stale,))["last_run_at"] > "2021"
        assert db.one("SELECT last_run_at FROM job_agents WHERE id=?", (paused,))["last_run_at"] is None


def test_vercel_config_is_valid_and_complete():
    import tomllib
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "vercel.json").read_text())
    assert cfg["functions"]["index.py"]["maxDuration"] >= 30
    assert any(c["path"] == "/internal/cron/agents" for c in cfg["crons"])
    assert "rewrites" not in cfg                      # the FastAPI preset routes everything to the one function
    entry = (root / "index.py").read_text()
    assert "from portal.main import app" in entry     # not the older prototype in app/main.py
    py = tomllib.loads((root / "pyproject.toml").read_text())
    assert py["project"]["scripts"]["app"] == "portal.main:app"
    assert py["tool"]["uv"]["package"] is False
    runtime = (root / "requirements.txt").read_text().lower()
    for needed in ("fastapi", "psycopg", "pdfplumber", "python-docx", "python-multipart", "itsdangerous"):
        assert needed in runtime
    assert "pytest" not in runtime and "matplotlib" not in runtime        # test-only, keeps the bundle small

    def names(lines):
        import re
        return {re.split(r"[<>=\[ ]", l.strip().lower())[0] for l in lines if l.strip() and not l.startswith("#")}
    assert names(py["project"]["dependencies"]) == names(runtime.splitlines())   # the two lists must not drift
