"""Optional demo candidate for trying the portal quickly (synthetic data, local only)."""
from __future__ import annotations

from pathlib import Path

from app import db
from app.security import hash_password

from . import store
from .resume_score import score_resume
from .resume_struct import from_text

DEMO_EMAIL = "demo@talentforge.example"
DEMO_PASSWORD = "Demo@12345"
SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample_resume_fresher.txt"


def ensure_demo_user() -> bool:
    db.init_db()
    store.init()
    if db.one("SELECT 1 AS x FROM users WHERE email=?", (DEMO_EMAIL,)):
        return False
    uid = db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
                 (DEMO_EMAIL, "Riya Deshmukh", hash_password(DEMO_PASSWORD), "candidate", db.now(), db.now()))
    data = from_text(SAMPLE.read_text(encoding="utf8"), fallback_name="Riya Deshmukh")
    store.create_resume(uid, "Riya - base resume", data, score_resume(data), source="upload")
    return True
