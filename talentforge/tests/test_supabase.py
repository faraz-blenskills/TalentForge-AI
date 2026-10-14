"""Supabase/Postgres support: SQL translation, env loading, Google + email-code sign-in and persistence."""
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("TALENTFORGE_DB", str(Path(tempfile.mkdtemp()) / "supabase-test.db"))
os.environ["PORTAL_AGENT_SCHEDULER"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.security import hash_password
from portal import dbtool, store, supa_auth
from portal import main as portal_main
from portal.main import app
from portal.resume_struct import empty_resume
from portal.resume_score import score_resume


# ------------------------------------------------------------------------------ SQL translation
def test_placeholders_and_percent_signs_are_translated():
    assert db.to_pg("SELECT * FROM users WHERE email=? AND name LIKE '%a%'") == \
        "SELECT * FROM users WHERE email=%s AND name LIKE '%%a%%'"


def test_insert_or_ignore_and_returning():
    s = db.to_pg("INSERT OR IGNORE INTO job_results(user_id, source) VALUES (?,?)")
    assert s == "INSERT INTO job_results(user_id, source) VALUES (%s,%s) ON CONFLICT DO NOTHING"
    assert db.to_pg("INSERT INTO users(email) VALUES (?)", want_id=True).endswith(" RETURNING id")
    # tables keyed by something other than `id` must not ask for RETURNING id
    assert "RETURNING" not in db.to_pg("INSERT INTO candidate_profiles(user_id) VALUES (?)", want_id=True)
    both = db.to_pg("INSERT OR IGNORE INTO resumes(user_id) VALUES (?)", want_id=True)
    assert both.endswith("ON CONFLICT DO NOTHING RETURNING id")


def test_insert_or_replace_becomes_an_upsert_for_the_cache_only():
    s = db.to_pg("INSERT OR REPLACE INTO narrative_cache(key, text, source, created_at) VALUES (?,?,?,?)")
    assert "ON CONFLICT (key) DO UPDATE SET text=EXCLUDED.text" in s
    with pytest.raises(ValueError):
        db.to_pg("INSERT OR REPLACE INTO users(email) VALUES (?)")


def test_pragma_is_skipped():
    assert db.to_pg("PRAGMA table_info(resumes)") is None


def test_schema_translation_is_complete():
    pg = db.schema_to_pg(db.SCHEMA + store.SCHEMA)
    assert "AUTOINCREMENT" not in pg and not re.search(r"\bINTEGER\b", pg) and not re.search(r"\bREAL\b", pg)
    assert "BIGSERIAL PRIMARY KEY" in pg and "DOUBLE PRECISION" in pg


def test_generated_schema_covers_every_table_with_rls():
    sql = dbtool.schema_sql()
    tables = dbtool.all_tables()
    assert len(tables) >= 13 and len(set(tables)) == len(tables)
    for t in tables:
        assert f"CREATE TABLE IF NOT EXISTS {t}" in sql
        assert f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;" in sql
    assert "auth_uid" in sql and "raw_text" in sql and "parse_json" in sql
    assert set(dbtool.COPY_ORDER) == set(tables)          # migration copies everything we define


def test_database_url_is_percent_encoded_and_env_file_updated(tmp_path):
    from urllib.parse import unquote, urlparse
    pw = "p@ss:w/rd#1 %"
    url = dbtool.build_database_url("abcdref", pw)
    parsed = urlparse(url)
    assert parsed.username == "postgres.abcdref" and unquote(parsed.password) == pw
    assert parsed.hostname.endswith("pooler.supabase.com") and parsed.port == 6543 and parsed.path == "/postgres"

    f = tmp_path / ".env"
    f.write_text("SUPABASE_URL=https://x.supabase.co\n# DATABASE_URL=postgresql://template\nDATABASE_URL=\nOTHER=1\n")
    dbtool.update_env_file(f, "DATABASE_URL", url)
    text = f.read_text()
    assert text.count("DATABASE_URL=") == 1 and "template" not in text
    assert "SUPABASE_URL=https://x.supabase.co" in text and "OTHER=1" in text
    dbtool.update_env_file(f, "NEW_KEY", "v")           # appends when missing
    assert "NEW_KEY=v" in f.read_text()


def test_dbtool_refuses_to_run_without_a_postgres_url():
    with pytest.raises(SystemExit):
        dbtool.cmd_check(None)


def test_env_file_never_overrides_real_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nFOO_TEST_A=from-file\nFOO_TEST_B='quoted value'\nFOO_TEST_C = spaced \n\nbad line\n")
    monkeypatch.setenv("FOO_TEST_A", "from-env")
    monkeypatch.delenv("FOO_TEST_B", raising=False)
    monkeypatch.delenv("FOO_TEST_C", raising=False)
    monkeypatch.delenv("TALENTFORGE_NO_DOTENV", raising=False)
    monkeypatch.setattr(config, "_LOADED", False)
    config.load_env(f)
    assert os.environ["FOO_TEST_A"] == "from-env"
    assert os.environ["FOO_TEST_B"] == "quoted value" and os.environ["FOO_TEST_C"] == "spaced"
    for k in ("FOO_TEST_B", "FOO_TEST_C"):
        os.environ.pop(k, None)


# ------------------------------------------------------------------------------ Supabase auth client
def _client(handler):
    return supa_auth.SupabaseAuth("https://proj.supabase.co", "anon-key", httpx.MockTransport(handler))


def _session_payload(uid="u-1", email="Asha@Demo.example", name="Asha Rao", verified=True):
    return {"access_token": "t", "user": {"id": uid, "email": email, "email_confirmed_at": "2026-10-01T00:00:00Z" if verified else None,
                                           "user_metadata": {"full_name": name}}}


def test_pkce_pair_and_google_url():
    v, c = supa_auth.new_pkce()
    assert 43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_\-]+", c)
    url = _client(lambda r: httpx.Response(200)).google_url("http://x/auth/callback", c)
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://proj.supabase.co/auth/v1/authorize?")
    assert q["provider"] == ["google"] and q["code_challenge"] == [c] and q["code_challenge_method"] == ["s256"]


def test_exchange_code_success_and_failure():
    seen = {}

    def handler(req):
        seen["url"], seen["body"], seen["apikey"] = str(req.url), json.loads(req.content), req.headers.get("apikey")
        return httpx.Response(200, json=_session_payload())

    au = _client(handler).exchange_code("abc", "verifier123")
    assert "grant_type=pkce" in seen["url"] and seen["body"] == {"auth_code": "abc", "code_verifier": "verifier123"}
    assert seen["apikey"] == "anon-key"
    assert au.email == "asha@demo.example" and au.name == "Asha Rao" and au.email_verified

    with pytest.raises(supa_auth.AuthError):
        _client(lambda r: httpx.Response(400, json={"msg": "bad"})).exchange_code("abc", "v")
    with pytest.raises(supa_auth.AuthError):
        _client(lambda r: httpx.Response(200)).exchange_code("", "v")


def test_otp_request_and_verify_with_type_fallback():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append((req.url.path, body.get("type")))
        if req.url.path.endswith("/otp"):
            return httpx.Response(200, json={})
        # an older server that only knows "magiclink"
        return httpx.Response(200, json=_session_payload()) if body["type"] == "magiclink" \
            else httpx.Response(422, json={"msg": "unsupported type"})

    c = _client(handler)
    c.otp_request("asha@demo.example", "Asha")
    au = c.otp_verify("asha@demo.example", "123 456")
    assert au.uid == "u-1"
    assert [t for p, t in calls if p.endswith("/verify")] == ["email", "magiclink"]


def test_otp_errors_are_friendly():
    with pytest.raises(supa_auth.AuthError, match="6-digit"):
        _client(lambda r: httpx.Response(200)).otp_verify("a@b.co", "12")
    with pytest.raises(supa_auth.AuthError, match="invalid or has expired"):
        _client(lambda r: httpx.Response(403, json={"msg": "Token has expired or is invalid"})).otp_verify("a@b.co", "123456")
    with pytest.raises(supa_auth.AuthError, match="Too many"):
        _client(lambda r: httpx.Response(429)).otp_request("a@b.co")

    def boom(req):
        raise httpx.ConnectError("down")
    with pytest.raises(supa_auth.AuthError, match="Could not reach"):
        _client(boom).otp_request("a@b.co")


def test_unconfigured_client_is_disabled():
    c = supa_auth.SupabaseAuth("", "")
    assert not c.enabled
    with pytest.raises(supa_auth.AuthError):
        c.otp_request("a@b.co")


# ------------------------------------------------------------------------------ login flows
@pytest.fixture()
def fake_supabase(monkeypatch):
    """Replace the module-level Supabase client with one served by an in-memory handler."""
    state = {"users": {}, "codes": {}, "sent": []}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        path = req.url.path
        if path.endswith("/token"):
            code = body.get("auth_code")
            if code in state["users"]:
                return httpx.Response(200, json=state["users"][code])
            return httpx.Response(400, json={"msg": "invalid grant"})
        if path.endswith("/otp"):
            email = body["email"].lower()
            state["sent"].append(email)
            state.setdefault("otp_bodies", []).append((body, dict(req.url.params)))
            state["codes"][email] = "424242"
            return httpx.Response(200, json={})
        if path.endswith("/verify"):
            email = body["email"].lower()
            if body.get("token") == state["codes"].get(email):
                return httpx.Response(200, json=_session_payload(uid=f"otp-{email}", email=email,
                                                                 name=email.split("@")[0]))
            return httpx.Response(403, json={"msg": "Token has expired or is invalid"})
        return httpx.Response(404)

    client = supa_auth.SupabaseAuth("https://proj.supabase.co", "anon-key", httpx.MockTransport(handler))
    monkeypatch.setattr(portal_main, "supa", client)
    portal_main._OTP_BY_EMAIL.clear()
    portal_main._OTP_BY_IP.clear()
    return state


def _user(email):
    return db.one("SELECT * FROM users WHERE email=?", (email,))


def test_login_page_offers_google_and_code_only_when_configured(fake_supabase, monkeypatch):
    with TestClient(app) as c:
        page = c.get("/login")
        assert "Continue with Google" in page.text and "Email me a sign-in link" in page.text
        monkeypatch.setattr(portal_main, "supa", supa_auth.SupabaseAuth("", ""))
        page = c.get("/login")
        assert "Continue with Google" not in page.text and 'action="/login"' in page.text


def test_google_flow_creates_account_then_reuses_it(fake_supabase):
    fake_supabase["users"]["code-1"] = _session_payload(uid="g-1", email="google.user@demo.example", name="Gita Rao")
    with TestClient(app) as c:
        r = c.get("/auth/google", follow_redirects=False)
        assert r.status_code == 303
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["provider"] == ["google"] and q["redirect_to"][0].endswith("/auth/callback")
        assert q["code_challenge"] and q["code_challenge_method"] == ["s256"]

        r = c.get("/auth/callback?code=code-1", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard"
        u = _user("google.user@demo.example")
        assert u and u["auth_uid"] == "g-1" and u["role"] == "candidate" and u["consent_at"]
        assert u["password_hash"] == "!external"
        assert c.get("/dashboard").status_code == 200

        # the PKCE verifier is single-use: replaying the callback must fail
        c.post("/logout")
        r = c.get("/auth/callback?code=code-1", follow_redirects=False)
        assert r.headers["location"] == "/login"


def test_google_error_and_missing_verifier_are_handled(fake_supabase):
    with TestClient(app) as c:
        r = c.get("/auth/callback?error=access_denied&error_description=nope", follow_redirects=False)
        assert r.headers["location"] == "/login"
        r = c.get("/auth/callback?code=whatever", follow_redirects=False)      # no verifier in the session
        assert r.headers["location"] == "/login"
        assert c.get("/dashboard", follow_redirects=False).status_code == 303


def test_existing_password_account_is_linked_by_verified_email(fake_supabase):
    uid = db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
                 ("linked@demo.example", "Old Account", hash_password("longenough1"), "candidate", db.now(), db.now()))
    fake_supabase["users"]["c-link"] = _session_payload(uid="g-link", email="linked@demo.example")
    fake_supabase["users"]["c-unv"] = _session_payload(uid="g-unv", email="linked@demo.example", verified=False)
    with TestClient(app) as c:
        c.get("/auth/google")
        r = c.get("/auth/callback?code=c-unv", follow_redirects=False)           # unverified email: refuse to link
        assert r.headers["location"] == "/login" and _user("linked@demo.example")["auth_uid"] is None
        c.get("/auth/google")
        r = c.get("/auth/callback?code=c-link", follow_redirects=False)
        assert r.headers["location"] == "/dashboard"
        assert _user("linked@demo.example")["id"] == uid and _user("linked@demo.example")["auth_uid"] == "g-link"


def test_employer_accounts_cannot_use_the_candidate_portal(fake_supabase):
    db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
           ("boss@demo.example", "Boss", hash_password("longenough1"), "employer", db.now(), db.now()))
    fake_supabase["users"]["c-emp"] = _session_payload(uid="g-emp", email="boss@demo.example")
    with TestClient(app) as c:
        c.get("/auth/google")
        r = c.get("/auth/callback?code=c-emp", follow_redirects=False)
        assert r.headers["location"] == "/login"
        assert c.get("/dashboard", follow_redirects=False).status_code == 303


def test_email_code_flow_and_attempt_limit(fake_supabase):
    with TestClient(app) as c:
        r = c.post("/auth/otp/request", data={"email": "Otp.User@Demo.example"}, follow_redirects=False)
        assert r.headers["location"] == "/login?step=code"
        assert fake_supabase["sent"] == ["otp.user@demo.example"]
        body, params = fake_supabase["otp_bodies"][0]
        assert body["code_challenge"] and body["code_challenge_method"] == "s256"   # emailed link is PKCE-bound
        assert params["redirect_to"].endswith("/auth/callback")
        assert "otp.user@demo.example" in c.get("/login?step=code").text

        for _ in range(5):                                   # five wrong guesses allowed...
            r = c.post("/auth/otp/verify", data={"code": "000000"}, follow_redirects=False)
            assert r.headers["location"] == "/login?step=code"
        r = c.post("/auth/otp/verify", data={"code": "424242"}, follow_redirects=False)
        assert r.headers["location"] == "/login"              # ...the sixth attempt is refused even if correct

        r = c.post("/auth/otp/request", data={"email": "otp.user@demo.example"}, follow_redirects=False)
        r = c.post("/auth/otp/verify", data={"code": "424242"}, follow_redirects=False)
        assert r.headers["location"] == "/dashboard"
        assert _user("otp.user@demo.example")["auth_uid"] == "otp-otp.user@demo.example"


def test_code_requests_are_rate_limited(fake_supabase):
    with TestClient(app) as c:
        results = [c.post("/auth/otp/request", data={"email": "limit@demo.example"}, follow_redirects=False)
                   .headers["location"] for _ in range(4)]
        assert results[:3] == ["/login?step=code"] * 3 and results[3] == "/login"
        assert len(fake_supabase["sent"]) == 3
        bad = c.post("/auth/otp/request", data={"email": "not-an-email"}, follow_redirects=False)
        assert bad.headers["location"] == "/login"


def test_data_follows_the_login_identity_across_sessions(fake_supabase):
    """Same Google/email identity -> same resumes, from a brand-new browser session."""
    fake_supabase["users"]["c-p"] = _session_payload(uid="g-persist", email="persist@demo.example", name="Pia Rao")
    with TestClient(app) as first:
        first.get("/auth/google")
        assert first.get("/auth/callback?code=c-p", follow_redirects=False).headers["location"] == "/dashboard"
        r = first.post("/resumes/new", data={"title": "Saved in the cloud"}, follow_redirects=False)
        rid = int(r.headers["location"].split("/")[2])
        d = empty_resume("Pia Rao", "persist@demo.example")
        d["summary"] = "Persistent data check."
        assert first.post(f"/api/resumes/{rid}", json={"data": d}).status_code == 200

    # a different browser/device, signing in with an email code for the same address
    with TestClient(app) as second:
        second.post("/auth/otp/request", data={"email": "persist@demo.example"})
        second.post("/auth/otp/verify", data={"code": "424242"})
        page = second.get(f"/resumes/{rid}")
        assert page.status_code == 200 and "Persistent data check." in page.text
        assert "Saved in the cloud" in second.get("/resumes").text


def test_password_login_can_be_switched_off(fake_supabase, monkeypatch):
    db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
           ("pw@demo.example", "Pw", hash_password("longenough1"), "candidate", db.now(), db.now()))
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "0")
    with TestClient(app) as c:
        r = c.post("/login", data={"email": "pw@demo.example", "password": "longenough1"}, follow_redirects=False)
        assert r.headers["location"] == "/login"
        assert c.get("/dashboard", follow_redirects=False).status_code == 303
        assert c.get("/register", follow_redirects=False).headers["location"] == "/login"
        assert 'action="/login"' not in c.get("/login").text
