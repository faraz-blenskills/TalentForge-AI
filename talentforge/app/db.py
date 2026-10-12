"""SQLite persistence (stdlib only)."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("TALENTFORGE_DB", BASE_DIR / "data" / "talentforge.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('candidate','employer','admin')),
  consent_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_profiles (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  profile_json TEXT NOT NULL,
  resume_text TEXT NOT NULL DEFAULT '',
  resume_filename TEXT NOT NULL DEFAULT '',
  target_role TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organisations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  industry TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  website TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id INTEGER NOT NULL REFERENCES organisations(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  job_type TEXT NOT NULL DEFAULT 'Full-time',
  required_json TEXT NOT NULL,
  preferred_json TEXT NOT NULL DEFAULT '[]',
  min_years REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('draft','open','closed')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  candidate_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'applied',
  match_score REAL NOT NULL DEFAULT 0,
  match_json TEXT NOT NULL DEFAULT '{}',
  applied_at TEXT NOT NULL,
  UNIQUE(job_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  application_id INTEGER NOT NULL REFERENCES applications(id),
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  ai_recommendation TEXT NOT NULL,
  feedback TEXT NOT NULL DEFAULT '',
  share_feedback INTEGER NOT NULL DEFAULT 0,
  decided_by INTEGER NOT NULL REFERENCES users(id),
  decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  subject TEXT NOT NULL,
  output_json TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER,
  action TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS narrative_cache (
  key TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

MODEL_VERSION = "tf-phase1-rules-v1"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    r = rows(sql, args)
    return r[0] if r else None


def run(sql: str, args: tuple = ()) -> int:
    with connect() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def audit(actor_id: int | None, action: str, ref: str = "", detail: str = "") -> None:
    run("INSERT INTO audit_events(actor_id, action, ref, detail, created_at) VALUES (?,?,?,?,?)",
        (actor_id, action, ref, detail[:500], now()))


def record_ai(type_: str, subject: str, output: dict, model: str = MODEL_VERSION) -> None:
    run("INSERT INTO ai_results(type, subject, output_json, model, created_at) VALUES (?,?,?,?,?)",
        (type_, subject, json.dumps(output, default=str), model, now()))


def cache_get(key: str) -> tuple[str, str] | None:
    r = one("SELECT text, source FROM narrative_cache WHERE key=?", (key,))
    return (r["text"], r["source"]) if r else None


def cache_put(key: str, text: str, source: str) -> None:
    run("INSERT OR REPLACE INTO narrative_cache(key, text, source, created_at) VALUES (?,?,?,?)",
        (key, text, source, now()))
