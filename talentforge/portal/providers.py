"""Job-source adapters.

Only sources that publish an official API/feed are used - no scraping of LinkedIn, Indeed or Naukri
(their terms forbid it). Aggregators that legitimately cover those boards (JSearch, Adzuna) are supported
when the user supplies an API key.
"""
from __future__ import annotations

import html
import os
import re
import time
from dataclasses import dataclass, field

import httpx

UA = "TalentForgeAI-research-prototype/1.0 (student project; contact via repository owner)"
TIMEOUT = 20.0
_CACHE: dict[str, tuple[float, object]] = {}
CACHE_TTL = 15 * 60


@dataclass
class JobPosting:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    remote: bool | None = None
    posted_at: str = ""
    tags: list[str] = field(default_factory=list)
    salary: str = ""
    via: str = ""          # original board when the source is an aggregator


def strip_html(s: str, limit: int = 6000) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()[:limit]


def _get_json(url: str, params: dict | None = None, headers: dict | None = None):
    key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
        r = c.get(url, params=params, headers=h)
        r.raise_for_status()
        data = r.json()
    _CACHE[key] = (time.time(), data)
    return data


def _matches(q: str, *fields: str) -> bool:
    toks = [t for t in re.findall(r"[a-z0-9+#.]+", q.lower()) if len(t) > 1]
    if not toks:
        return True
    blob = " ".join(fields).lower()
    return any(t in blob for t in toks)


class Provider:
    id = ""
    label = ""
    needs_key: str | None = None
    note = ""
    coverage = ""

    def available(self) -> bool:
        return self.needs_key is None or bool(os.environ.get(self.needs_key))

    def search(self, query: str, location: str = "", remote_only: bool = False, limit: int = 40) -> list[JobPosting]:
        raise NotImplementedError


class Remotive(Provider):
    id, label = "remotive", "Remotive"
    coverage = "Remote jobs worldwide (tech, data, design)"

    def search(self, query, location="", remote_only=False, limit=40):
        data = _get_json("https://remotive.com/api/remote-jobs", {"search": query, "limit": limit})
        out = []
        for j in data.get("jobs", [])[:limit]:
            out.append(JobPosting(
                source=self.id, external_id=str(j.get("id")), title=j.get("title", ""),
                company=j.get("company_name", ""), location=j.get("candidate_required_location") or "Remote",
                url=j.get("url", ""), description=strip_html(j.get("description", "")), remote=True,
                posted_at=(j.get("publication_date") or "")[:10], tags=list(j.get("tags") or [])[:8],
                salary=j.get("salary") or ""))
        return out


class Arbeitnow(Provider):
    id, label = "arbeitnow", "Arbeitnow"
    coverage = "Europe-focused, many English-language and remote roles"

    def search(self, query, location="", remote_only=False, limit=40):
        data = _get_json("https://www.arbeitnow.com/api/job-board-api")
        out = []
        for j in data.get("data", []):
            if not _matches(query, j.get("title", ""), " ".join(j.get("tags") or []), j.get("description", "")[:2000]):
                continue
            if remote_only and not j.get("remote"):
                continue
            ts = j.get("created_at")
            out.append(JobPosting(
                source=self.id, external_id=str(j.get("slug")), title=j.get("title", ""),
                company=j.get("company_name", ""), location=j.get("location", ""), url=j.get("url", ""),
                description=strip_html(j.get("description", "")), remote=bool(j.get("remote")),
                posted_at=time.strftime("%Y-%m-%d", time.gmtime(ts)) if isinstance(ts, (int, float)) else "",
                tags=list(j.get("tags") or [])[:8]))
            if len(out) >= limit:
                break
        return out


class RemoteOK(Provider):
    id, label = "remoteok", "RemoteOK"
    coverage = "Remote jobs worldwide (requires a link back to RemoteOK)"

    def search(self, query, location="", remote_only=False, limit=40):
        data = _get_json("https://remoteok.com/api")
        out = []
        for j in data:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            if not _matches(query, j.get("position", ""), " ".join(j.get("tags") or []), j.get("description", "")[:1500]):
                continue
            sal = ""
            if j.get("salary_min") and j.get("salary_max"):
                sal = f"${int(j['salary_min']):,} - ${int(j['salary_max']):,}"
            out.append(JobPosting(
                source=self.id, external_id=str(j.get("id")), title=j.get("position", ""),
                company=j.get("company", ""), location=j.get("location") or "Remote", url=j.get("url", ""),
                description=strip_html(j.get("description", "")), remote=True,
                posted_at=(j.get("date") or "")[:10], tags=list(j.get("tags") or [])[:8], salary=sal))
            if len(out) >= limit:
                break
        return out


class Adzuna(Provider):
    id, label = "adzuna", "Adzuna"
    needs_key = "ADZUNA_APP_KEY"
    coverage = "Country job boards incl. India (set ADZUNA_APP_ID and ADZUNA_APP_KEY)"

    def available(self):
        return bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))

    def search(self, query, location="", remote_only=False, limit=40):
        country = os.environ.get("ADZUNA_COUNTRY", "in")
        params = {"app_id": os.environ["ADZUNA_APP_ID"], "app_key": os.environ["ADZUNA_APP_KEY"],
                  "results_per_page": min(limit, 50), "what": query, "content-type": "application/json"}
        if location:
            params["where"] = location
        data = _get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params)
        out = []
        for j in data.get("results", []):
            loc = (j.get("location") or {}).get("display_name", "")
            sal = ""
            if j.get("salary_min"):
                sal = f"{int(j['salary_min']):,} - {int(j.get('salary_max') or j['salary_min']):,}"
            out.append(JobPosting(
                source=self.id, external_id=str(j.get("id")), title=strip_html(j.get("title", ""), 200),
                company=(j.get("company") or {}).get("display_name", ""), location=loc,
                url=j.get("redirect_url", ""), description=strip_html(j.get("description", "")),
                remote=None, posted_at=(j.get("created") or "")[:10], salary=sal))
        return out


class JSearch(Provider):
    id, label = "jsearch", "JSearch (LinkedIn, Indeed, Glassdoor...)"
    needs_key = "RAPIDAPI_KEY"
    coverage = "Aggregates LinkedIn, Indeed, Glassdoor and others via RapidAPI (set RAPIDAPI_KEY)"

    def search(self, query, location="", remote_only=False, limit=40):
        q = f"{query} in {location}" if location else query
        params = {"query": q, "num_pages": 1, "page": 1}
        if remote_only:
            params["remote_jobs_only"] = "true"
        data = _get_json("https://jsearch.p.rapidapi.com/search", params, headers={
            "X-RapidAPI-Key": os.environ["RAPIDAPI_KEY"], "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
        out = []
        for j in data.get("data", [])[:limit]:
            loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
            out.append(JobPosting(
                source=self.id, external_id=str(j.get("job_id")), title=j.get("job_title", ""),
                company=j.get("employer_name", ""), location=loc or "Not specified",
                url=j.get("job_apply_link", ""), description=strip_html(j.get("job_description", "")),
                remote=j.get("job_is_remote"), posted_at=(j.get("job_posted_at_datetime_utc") or "")[:10],
                via=j.get("job_publisher", "")))
        return out


REGISTRY: dict[str, Provider] = {p.id: p for p in (Remotive(), Arbeitnow(), RemoteOK(), Adzuna(), JSearch())}


def provider_status() -> list[dict]:
    return [{"id": p.id, "label": p.label, "available": p.available(), "needs_key": p.needs_key,
             "coverage": p.coverage} for p in REGISTRY.values()]
