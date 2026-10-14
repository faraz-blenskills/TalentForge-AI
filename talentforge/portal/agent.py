"""The job-search agent: plan -> search sources -> dedupe -> filter -> score against the resume -> store.

Each run records a step-by-step activity log so the user can see exactly what the agent did.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from app import db

from . import ai, providers, store
from .tailor import posting_match

SYNONYMS = {
    "software engineer": ["software developer", "backend developer", "full stack developer", "sde"],
    "software developer": ["software engineer", "backend developer", "full stack developer"],
    "backend": ["backend developer", "python developer", "api developer"],
    "frontend": ["frontend developer", "react developer", "ui developer"],
    "full stack": ["full stack developer", "full-stack engineer"],
    "data analyst": ["business analyst", "bi analyst", "analytics analyst"],
    "data scientist": ["machine learning engineer", "ml engineer", "ai engineer"],
    "data engineer": ["etl developer", "analytics engineer", "big data engineer"],
    "machine learning": ["ml engineer", "ai engineer", "data scientist"],
    "devops": ["cloud engineer", "site reliability engineer", "platform engineer"],
    "qa": ["test engineer", "sdet", "automation engineer"],
    "python": ["python developer", "backend developer"],
    "ai": ["ai engineer", "machine learning engineer", "llm engineer"],
}
SENIOR_RE = re.compile(r"\b(senior|sr\.?|lead|principal|staff|head of|director|manager|architect|vp)\b", re.I)
ENTRY_HINT_RE = re.compile(r"entry[- ]level|fresher|graduate|junior|0\s*[-–]\s*[12]\s*years|new grad|intern", re.I)
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
MAX_QUERIES = 3
MAX_PER_SOURCE = 40


def expand_queries(query: str) -> list[str]:
    q = query.strip()
    out = [q]
    low = q.lower()
    for key, alts in SYNONYMS.items():
        if key in low:
            for a in alts:
                if a.lower() not in [x.lower() for x in out]:
                    out.append(a)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _level_ok(level: str, title: str, description: str) -> bool:
    if level in ("any", "mid"):
        return True
    if SENIOR_RE.search(title) and not ENTRY_HINT_RE.search(title):
        return False
    years = [int(y) for y in YEARS_RE.findall(description[:3000])]
    limit = 3 if level == "junior" else 2
    if years and min(years) > limit + 1 and not ENTRY_HINT_RE.search(description[:3000]):
        return False
    return True


def _location_ok(job: providers.JobPosting, locations: list[str], modes: list[str]) -> bool:
    loc = (job.location or "").lower()
    is_remote = job.remote if job.remote is not None else ("remote" in loc or "anywhere" in loc or "worldwide" in loc)
    if is_remote:
        return "remote" in modes
    if modes and set(modes) == {"remote"}:
        return False
    if not locations:
        return True
    return any(l.lower() in loc for l in locations)


GENERIC_ROLE_WORDS = {"developer", "engineer", "programmer", "analyst", "specialist", "associate", "intern",
                      "junior", "senior", "software", "engineering", "scientist", "consultant", "architect",
                      "fresher", "graduate", "trainee", "remote", "technical", "technology", "it"}


def _tok(s: str) -> set[str]:
    out = set()
    for t in re.findall(r"[a-z][a-z0-9+#.]*", (s or "").lower()):
        t = t.strip(".")
        out.add(t[:-1] if len(t) > 4 and t.endswith("s") else t)
    return out


def _is_relevant(query_tokens: set[str], title: str, tags: list[str], match: dict) -> bool:
    """A posting must be about the requested role: title/tag overlap, or clear technical-skill overlap."""
    distinctive = {t for t in query_tokens if t not in GENERIC_ROLE_WORDS} or query_tokens
    title_tok = _tok(title) | _tok(" ".join(tags))
    if distinctive & title_tok:
        return True
    tech = match.get("tech_matched", 0)
    if tech >= 4:
        return True
    return bool(GENERIC_ROLE_WORDS & _tok(title)) and tech >= 3


class Run:
    def __init__(self):
        self.steps: list[dict] = []

    def log(self, step: str, detail: str, **extra):
        self.steps.append({"step": step, "detail": detail, "at": datetime.now(timezone.utc).strftime("%H:%M:%S"), **extra})


def run_agent(user_id: int, agent_id: int, registry: dict | None = None) -> dict:
    registry = registry if registry is not None else providers.REGISTRY
    agent = store.get_agent(user_id, agent_id)
    if not agent:
        raise ValueError("agent not found")
    run = Run()
    started = db.now()
    run_id = db.run("INSERT INTO agent_runs(agent_id, started_at) VALUES (?,?)", (agent_id, started))

    resume = (store.get_resume(user_id, agent["resume_id"]) if agent["resume_id"] else None) or store.default_resume(user_id)
    if not resume:
        run.log("error", "No resume found. Upload or build a resume first so the agent can score matches.")
        _finish(run_id, agent_id, run, 0, 0)
        return {"found": 0, "new": 0, "log": run.steps, "error": "no_resume"}
    rdata = resume["data"]
    run.log("start", f"Looking for '{agent['query']}' ({agent['level']} level) matched against resume '{resume['title']}'.")

    # 1. plan
    queries = expand_queries(agent["query"])
    llm_q = ai.plan_queries(agent["query"], rdata["skills"], agent["level"])
    for q in llm_q:
        if q.lower() not in [x.lower() for x in queries]:
            queries.append(q)
    queries = queries[:MAX_QUERIES]
    run.log("plan", f"Search queries: {', '.join(queries)}" + (" (expanded with Claude)" if llm_q else ""))

    # 2. search every enabled, available source in parallel
    wanted = agent["sources"] or [p for p, prov in registry.items() if prov.available()]
    active = [registry[p] for p in wanted if p in registry and registry[p].available()]
    skipped = [p for p in wanted if p in registry and not registry[p].available()]
    if skipped:
        run.log("skip", "Skipped (no API key configured): " + ", ".join(skipped))
    remote_only = agent["work_modes"] == ["remote"]
    location = agent["locations"][0] if agent["locations"] else ""
    raw: list[providers.JobPosting] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(prov.search, q, location, remote_only, MAX_PER_SOURCE): (prov.id, q)
                for prov in active for q in queries}
        per_source: dict[str, int] = {}
        for f in as_completed(futs):
            pid, q = futs[f]
            try:
                res = f.result()
                per_source[pid] = per_source.get(pid, 0) + len(res)
                raw.extend(res)
            except Exception as e:  # one broken source must never stop the run
                run.log("source_error", f"{pid} failed for '{q}': {type(e).__name__}")
    for pid, n in sorted(per_source.items()):
        run.log("search", f"{registry[pid].label}: {n} posting(s)", source=pid, count=n)
    found = len(raw)

    # 3. dedupe - deterministic order, within this run AND against jobs already stored for this user
    raw.sort(key=lambda j: (j.source, j.external_id))
    known_exact = {(r["source"], r["external_id"]) for r in
                   db.rows("SELECT source, external_id FROM job_results WHERE user_id=?", (user_id,))}
    known_fuzzy = {(_norm(r["title"]), _norm(r["company"])) for r in
                   db.rows("SELECT title, company FROM job_results WHERE user_id=?", (user_id,))}
    seen_keys, seen_fuzzy, unique = set(), set(), []
    seen_before = 0
    for j in raw:
        k, fz = (j.source, j.external_id), (_norm(j.title), _norm(j.company))
        if k in seen_keys or fz in seen_fuzzy:
            continue
        if k not in known_exact and fz in known_fuzzy:
            seen_before += 1        # same role already stored from another source
            continue
        seen_keys.add(k)
        seen_fuzzy.add(fz)
        unique.append(j)
    run.log("dedupe", f"{len(raw)} -> {len(unique)} unique posting(s)"
                      + (f"; {seen_before} already known from another source." if seen_before else "."))

    # 4. filter
    kept = []
    dropped_level = dropped_loc = 0
    for j in unique:
        if not j.title or not j.url:
            continue
        if not _level_ok(agent["level"], j.title, j.description):
            dropped_level += 1
            continue
        if not _location_ok(j, agent["locations"], agent["work_modes"]):
            dropped_loc += 1
            continue
        kept.append(j)
    run.log("filter", f"Kept {len(kept)}; removed {dropped_level} above your level and {dropped_loc} outside your "
                      f"location/work-mode preferences.")

    # 5. score against the resume
    scored = []
    for j in kept:
        m = posting_match(rdata, j.title, j.description, j.company)
        scored.append((j, m))
    query_tokens = set()
    for q in queries:
        query_tokens |= _tok(q)
    before = len(scored)
    scored = [(j, m) for j, m in scored if _is_relevant(query_tokens, j.title, j.tags, m)]
    if before != len(scored):
        run.log("relevance", f"Removed {before - len(scored)} posting(s) that are not about this role.")
    scored = [(j, m) for j, m in scored if m["score"] >= agent["min_score"]]
    scored.sort(key=lambda t: -t[1]["score"])
    run.log("score", f"Scored {len(scored)} posting(s) against your resume"
                     + (f" (minimum {agent['min_score']:g})." if agent["min_score"] else "."))

    # 6. persist
    new = 0
    now = db.now()
    for j, m in scored:
        detail = {k: m[k] for k in ("matched", "partial", "missing", "coverage", "title_similarity",
                                    "text_similarity", "shared_terms")}
        detail["keywords"] = m["keywords"][:10]
        with db.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO job_results(user_id, agent_id, source, external_id, title, company, location, "
                "remote, url, description, posted_at, salary, via, tags_json, match_score, match_json, status, "
                "found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, agent_id, j.source, j.external_id, j.title[:200], j.company[:160], j.location[:200],
                 None if j.remote is None else int(bool(j.remote)), j.url, j.description, j.posted_at, j.salary,
                 j.via, json.dumps(j.tags), m["score"], json.dumps(detail), "new", now))
            if cur.rowcount:
                new += 1
            else:
                c.execute("UPDATE job_results SET match_score=?, match_json=?, agent_id=COALESCE(agent_id, ?) "
                          "WHERE user_id=? AND source=? AND external_id=? AND status IN ('new','seen')",
                          (m["score"], json.dumps(detail), agent_id, user_id, j.source, j.external_id))
    # prune unseen results that were fetched again this run but no longer pass the filters
    kept_keys = {(j.source, j.external_id) for j, _ in scored}
    pruned = 0
    for j in raw:
        key = (j.source, j.external_id)
        if key not in kept_keys and key in known_exact:
            pruned += db_prune(user_id, agent_id, j.source, j.external_id)
    if pruned:
        run.log("prune", f"Removed {pruned} earlier unseen result(s) that no longer pass your filters.")
    top = scored[0] if scored else None
    run.log("done", f"{new} new match(es) saved." + (f" Best: {top[0].title} at {top[0].company} "
                                                    f"({top[1]['score']:g})." if top else ""))
    _finish(run_id, agent_id, run, found, new)
    return {"found": found, "new": new, "log": run.steps, "kept": len(scored)}


def db_prune(user_id: int, agent_id: int, source: str, external_id: str) -> int:
    with db.connect() as c:
        cur = c.execute("DELETE FROM job_results WHERE user_id=? AND agent_id=? AND source=? AND external_id=? "
                        "AND status='new'", (user_id, agent_id, source, external_id))
        return cur.rowcount


def _finish(run_id: int, agent_id: int, run: Run, found: int, new: int) -> None:
    now = db.now()
    db.run("UPDATE agent_runs SET finished_at=?, found=?, new_count=?, log_json=? WHERE id=?",
           (now, found, new, json.dumps(run.steps), run_id))
    db.run("UPDATE job_agents SET last_run_at=? WHERE id=?", (now, agent_id))


def run_due_agents(limit: int = 5, min_hours: float = 12.0) -> dict:
    """Run active agents that have not run for `min_hours` (oldest first). Used by the serverless cron job."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_hours)).strftime("%Y-%m-%d %H:%M:%S")
    due = db.rows("SELECT id, user_id FROM job_agents WHERE active=1 AND (last_run_at IS NULL OR last_run_at < ?) "
                  "ORDER BY COALESCE(last_run_at, '') ASC LIMIT ?", (cutoff, int(limit)))
    ran, failed, new = 0, 0, 0
    for a in due:
        try:
            res = run_agent(a["user_id"], a["id"])
            ran += 1
            new += res.get("new", 0)
        except Exception:
            failed += 1
    return {"due": len(due), "ran": ran, "failed": failed, "new_matches": new}


# --------------------------------------------------------------------------- background scheduler
class Scheduler:
    def __init__(self, interval_minutes: int = 120):
        self.interval = timedelta(minutes=interval_minutes)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-agent-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _due(self, last: str | None) -> bool:
        if not last:
            return True
        try:
            t = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - t >= self.interval

    def tick(self):
        for a in db.rows("SELECT id, user_id, last_run_at FROM job_agents WHERE active=1"):
            if self._due(a["last_run_at"]):
                try:
                    run_agent(a["user_id"], a["id"])
                except Exception:
                    pass

    def _loop(self):
        time.sleep(30)          # let the server finish starting
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(300)   # check every 5 minutes which agents are due
