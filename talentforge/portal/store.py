"""Portal persistence: resumes, job agents, runs and results (shares app.db's SQLite file)."""
from __future__ import annotations

import json

from app import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS resumes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'builder',
  is_default INTEGER NOT NULL DEFAULT 0,
  parent_id INTEGER,
  job_title TEXT NOT NULL DEFAULT '',
  job_company TEXT NOT NULL DEFAULT '',
  template TEXT NOT NULL DEFAULT 'classic',
  data_json TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  score_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  locations_json TEXT NOT NULL DEFAULT '[]',
  work_modes_json TEXT NOT NULL DEFAULT '["remote","onsite","hybrid"]',
  level TEXT NOT NULL DEFAULT 'entry',
  sources_json TEXT NOT NULL DEFAULT '[]',
  resume_id INTEGER,
  min_score REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL REFERENCES job_agents(id),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  found INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  log_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS job_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  agent_id INTEGER,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  remote INTEGER,
  url TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  posted_at TEXT NOT NULL DEFAULT '',
  salary TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]',
  match_score REAL NOT NULL DEFAULT 0,
  match_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'new',
  found_at TEXT NOT NULL,
  UNIQUE(user_id, source, external_id)
);
"""


_RESUME_MIGRATIONS = (
    ("accent", "TEXT NOT NULL DEFAULT ''"),
    ("raw_text", "TEXT NOT NULL DEFAULT ''"),
    ("parse_json", "TEXT NOT NULL DEFAULT '{}'"),
)


def init() -> None:
    with db.connect() as c:
        c.executescript(SCHEMA)
        have = {row[1] for row in c.execute("PRAGMA table_info(resumes)")}
        for name, ddl in _RESUME_MIGRATIONS:          # forward-only, idempotent
            if name not in have:
                c.execute(f"ALTER TABLE resumes ADD COLUMN {name} {ddl}")


# ----------------------------------------------------------------------------- resumes
def _resume(r: dict | None) -> dict | None:
    if not r:
        return None
    r["data"] = json.loads(r["data_json"])
    r["score_detail"] = json.loads(r["score_json"] or "{}")
    r["parse"] = json.loads(r.get("parse_json") or "{}")
    return r


def list_resumes(user_id: int) -> list[dict]:
    return [_resume(r) for r in db.rows(
        "SELECT * FROM resumes WHERE user_id=? ORDER BY is_default DESC, updated_at DESC, id DESC", (user_id,))]


def get_resume(user_id: int, resume_id: int) -> dict | None:
    return _resume(db.one("SELECT * FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id)))


def default_resume(user_id: int) -> dict | None:
    return _resume(db.one(
        "SELECT * FROM resumes WHERE user_id=? ORDER BY is_default DESC, score DESC, id DESC LIMIT 1", (user_id,)))


def create_resume(user_id: int, title: str, data: dict, score: dict, source: str, parent_id: int | None = None,
                  job_title: str = "", job_company: str = "", template: str = "modern", accent: str = "",
                  raw_text: str = "", parse: dict | None = None) -> int:
    first = not db.one("SELECT 1 AS x FROM resumes WHERE user_id=?", (user_id,))
    now = db.now()
    return db.run(
        "INSERT INTO resumes(user_id, title, source, is_default, parent_id, job_title, job_company, template, "
        "accent, raw_text, parse_json, data_json, score, score_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, title[:120], source, 1 if first else 0, parent_id, job_title[:200], job_company[:200], template,
         accent, raw_text[:60000], json.dumps(parse or {}), json.dumps(data), score["score"], json.dumps(score),
         now, now))


def update_resume(resume_id: int, data: dict, score: dict, title: str | None = None, template: str | None = None,
                  accent: str | None = None) -> None:
    cur = db.one("SELECT title, template, accent FROM resumes WHERE id=?", (resume_id,))
    db.run("UPDATE resumes SET data_json=?, score=?, score_json=?, title=?, template=?, accent=?, updated_at=? "
           "WHERE id=?",
           (json.dumps(data), score["score"], json.dumps(score), (title or cur["title"])[:120],
            template or cur["template"], accent if accent is not None else cur["accent"], db.now(), resume_id))


def set_style(resume_id: int, template: str, accent: str) -> None:
    db.run("UPDATE resumes SET template=?, accent=?, updated_at=? WHERE id=?", (template, accent, db.now(), resume_id))


def set_default(user_id: int, resume_id: int) -> None:
    db.run("UPDATE resumes SET is_default=0 WHERE user_id=?", (user_id,))
    db.run("UPDATE resumes SET is_default=1 WHERE id=? AND user_id=?", (resume_id, user_id))


def delete_resume(user_id: int, resume_id: int) -> None:
    was_default = db.one("SELECT is_default FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id))
    db.run("DELETE FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id))
    db.run("UPDATE job_agents SET resume_id=NULL WHERE resume_id=?", (resume_id,))
    if was_default and was_default["is_default"]:
        nxt = db.one("SELECT id FROM resumes WHERE user_id=? ORDER BY score DESC, id DESC LIMIT 1", (user_id,))
        if nxt:
            set_default(user_id, nxt["id"])


# ----------------------------------------------------------------------------- agents
def _agent(r: dict | None) -> dict | None:
    if not r:
        return None
    r["locations"] = json.loads(r["locations_json"])
    r["work_modes"] = json.loads(r["work_modes_json"])
    r["sources"] = json.loads(r["sources_json"])
    return r


def list_agents(user_id: int) -> list[dict]:
    out = []
    for r in db.rows("SELECT * FROM job_agents WHERE user_id=? ORDER BY id DESC", (user_id,)):
        a = _agent(r)
        s = db.one("SELECT COUNT(*) AS total, "
                   "SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) AS unseen, "
                   "SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) AS applied "
                   "FROM job_results WHERE agent_id=?", (a["id"],))
        a["stats"] = {"total": s["total"] or 0, "unseen": s["unseen"] or 0, "applied": s["applied"] or 0}
        out.append(a)
    return out


def get_agent(user_id: int, agent_id: int) -> dict | None:
    return _agent(db.one("SELECT * FROM job_agents WHERE id=? AND user_id=?", (agent_id, user_id)))


def save_agent(user_id: int, agent_id: int | None, name: str, query: str, locations: list[str],
               work_modes: list[str], level: str, sources: list[str], resume_id: int | None,
               min_score: float, active: bool) -> int:
    vals = (name[:80], query[:160], json.dumps(locations), json.dumps(work_modes), level, json.dumps(sources),
            resume_id, min_score, 1 if active else 0)
    if agent_id:
        db.run("UPDATE job_agents SET name=?, query=?, locations_json=?, work_modes_json=?, level=?, sources_json=?, "
               "resume_id=?, min_score=?, active=? WHERE id=? AND user_id=?", vals + (agent_id, user_id))
        return agent_id
    return db.run("INSERT INTO job_agents(name, query, locations_json, work_modes_json, level, sources_json, "
                  "resume_id, min_score, active, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  vals + (user_id, db.now()))


def delete_agent(user_id: int, agent_id: int) -> None:
    db.run("DELETE FROM job_results WHERE agent_id=? AND user_id=? AND status IN ('new','seen')", (agent_id, user_id))
    db.run("UPDATE job_results SET agent_id=NULL WHERE agent_id=? AND user_id=?", (agent_id, user_id))
    db.run("DELETE FROM agent_runs WHERE agent_id=?", (agent_id,))
    db.run("DELETE FROM job_agents WHERE id=? AND user_id=?", (agent_id, user_id))


def last_runs(agent_id: int, n: int = 5) -> list[dict]:
    out = db.rows("SELECT * FROM agent_runs WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, n))
    for r in out:
        r["log"] = json.loads(r["log_json"])
    return out


# ----------------------------------------------------------------------------- results
def _result(r: dict | None) -> dict | None:
    if not r:
        return None
    r["tags"] = json.loads(r["tags_json"])
    r["match"] = json.loads(r["match_json"] or "{}")
    return r


def list_results(user_id: int, agent_id: int | None = None, status: str | None = None,
                 min_score: float = 0, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM job_results WHERE user_id=? AND match_score>=? AND status!='dismissed'"
    args: list = [user_id, min_score]
    if agent_id:
        sql += " AND agent_id=?"
        args.append(agent_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY match_score DESC, id DESC LIMIT ?"
    args.append(limit)
    return [_result(r) for r in db.rows(sql, tuple(args))]


def get_result(user_id: int, result_id: int) -> dict | None:
    return _result(db.one("SELECT * FROM job_results WHERE id=? AND user_id=?", (result_id, user_id)))


def set_result_status(user_id: int, result_id: int, status: str) -> None:
    db.run("UPDATE job_results SET status=? WHERE id=? AND user_id=?", (status, result_id, user_id))


def counts(user_id: int) -> dict:
    with db.connect() as c:
        row = c.execute(
            "SELECT (SELECT COUNT(*) FROM resumes WHERE user_id=?1) AS resumes, "
            "(SELECT COALESCE(MAX(score),0) FROM resumes WHERE user_id=?1) AS best_score, "
            "(SELECT COUNT(*) FROM job_agents WHERE user_id=?1 AND active=1) AS agents, "
            "(SELECT COUNT(*) FROM job_results WHERE user_id=?1 AND status='new') AS new_matches, "
            "(SELECT COUNT(*) FROM job_results WHERE user_id=?1 AND status IN "
            "('saved','applied','interview','offer')) AS tracked, "
            "(SELECT COUNT(*) FROM job_results WHERE user_id=?1 AND status!='dismissed') AS total_matches",
            (user_id,)).fetchone()
        return dict(row)
