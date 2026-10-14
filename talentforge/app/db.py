"""Persistence.

Local development uses SQLite. Set DATABASE_URL (a Supabase / Postgres connection string) and the very same
code runs on Postgres: SQL written for SQLite is translated on the fly by `to_pg`, and the schema is
translated by `schema_to_pg`.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import load_env

load_env()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("TALENTFORGE_DB", BASE_DIR / "data" / "talentforge.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

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

# tables whose primary key is not an integer `id` (so INSERT ... RETURNING id does not apply)
_NO_ID_TABLES = {"candidate_profiles", "narrative_cache"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------- SQL / schema translation
_INSERT_RE = re.compile(r"^\s*INSERT\s+(?:OR\s+(IGNORE|REPLACE)\s+)?INTO\s+(\w+)", re.I)


def to_pg(sql: str, want_id: bool = False) -> str | None:
    """Translate one SQLite-flavoured statement to Postgres. Returns None for statements to skip."""
    s = sql.strip().rstrip(";")
    if re.match(r"(?i)^PRAGMA\b", s):
        return None
    s = s.replace("%", "%%").replace("?", "%s")
    m = _INSERT_RE.match(s)
    if m:
        mode, table = (m.group(1) or "").upper(), m.group(2).lower()
        s = _INSERT_RE.sub(f"INSERT INTO {table}", s, count=1)
        if mode == "IGNORE":
            s += " ON CONFLICT DO NOTHING"
        elif mode == "REPLACE":
            if table != "narrative_cache":
                raise ValueError(f"INSERT OR REPLACE is only translated for narrative_cache, not {table}")
            s += (" ON CONFLICT (key) DO UPDATE SET text=EXCLUDED.text, source=EXCLUDED.source, "
                  "created_at=EXCLUDED.created_at")
        if want_id and table not in _NO_ID_TABLES:
            s += " RETURNING id"
    return s


def schema_to_pg(sql: str) -> str:
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    s = re.sub(r"\bINTEGER\b", "BIGINT", s)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    return s


def table_names(*schemas: str) -> list[str]:
    out: list[str] = []
    for sch in schemas:
        out += re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sch, flags=re.I)
    return out


def rls_sql(tables: list[str]) -> str:
    """Row-level security with no policies: the public (anon) key can read nothing through Supabase's REST
    API. This server connects with the database role, which bypasses RLS."""
    return "\n".join(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;" for t in tables)


def _split_statements(script: str) -> list[str]:
    return [p.strip() for p in script.split(";") if p.strip()]


# ----------------------------------------------------------------------------- connections
class _PGCursor:
    def __init__(self, cur, returning: bool):
        self._cur = cur
        self.rowcount = cur.rowcount
        self.lastrowid = None
        self._prefetched = None
        if returning:
            row = cur.fetchone()
            self._prefetched = row
            self.lastrowid = row["id"] if row else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur.description else []

    def fetchone(self):
        return self._cur.fetchone() if self._cur.description else None


class _PGConn:
    """Gives a psycopg connection the small slice of the sqlite3 API this app uses."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, args=(), _want_id: bool = False):
        translated = to_pg(sql, _want_id)
        if translated is None:
            return _PGCursor(self._conn.execute("SELECT 1"), False)
        returning = translated.endswith(" RETURNING id")
        # Postgres text cannot hold NUL characters (SQLite can); job feeds occasionally contain them
        clean = tuple(a.replace("\x00", "") if isinstance(a, str) else a for a in args)
        return _PGCursor(self._conn.execute(translated, clean), returning)

    def executescript(self, script: str) -> None:
        for stmt in _split_statements(script):
            self._conn.execute(stmt)


_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        import psycopg
        from psycopg.rows import dict_row
        kwargs = {"row_factory": dict_row, "prepare_threshold": None, "autocommit": False}
        try:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "5")),
                                   kwargs=kwargs, open=True, timeout=20)
        except ImportError:
            _pool = False                      # no pool library: connect per call
    return _pool


@contextmanager
def connect():
    if USE_PG:
        import psycopg
        from psycopg.rows import dict_row
        pool = _get_pool()
        if pool:
            with pool.connection() as conn:
                yield _PGConn(conn)
        else:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None) as conn:
                yield _PGConn(conn)
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def backend() -> str:
    return "postgres" if USE_PG else "sqlite"


# ----------------------------------------------------------------------------- schema management
def exec_script(script: str) -> None:
    with connect() as c:
        c.executescript(schema_to_pg(script) if USE_PG else script)


def table_columns(table: str) -> set[str]:
    with connect() as c:
        if USE_PG:
            rows_ = c.execute("SELECT column_name FROM information_schema.columns "
                              "WHERE table_name=? AND table_schema=current_schema()", (table,)).fetchall()
            return {r["column_name"] for r in rows_}
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(table: str, name: str, ddl: str) -> None:
    if name in table_columns(table):
        return
    col_ddl = schema_to_pg(ddl) if USE_PG else ddl
    with connect() as c:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_ddl}")


def migrate() -> None:
    """Forward-only, idempotent changes applied after the base schema."""
    add_column_if_missing("users", "auth_uid", "TEXT")
    with connect() as c:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_auth_uid_idx ON users(auth_uid)")


def init_db() -> None:
    exec_script(SCHEMA)
    migrate()


# ----------------------------------------------------------------------------- helpers used everywhere
def rows(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    r = rows(sql, args)
    return r[0] if r else None


def run(sql: str, args: tuple = ()) -> int | None:
    with connect() as c:
        if USE_PG:
            return c.execute(sql, args, _want_id=True).lastrowid
        return c.execute(sql, args).lastrowid


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
