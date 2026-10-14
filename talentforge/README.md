# TalentForge AI

Two apps share this folder:

| App | Start | What it is |
|---|---|---|
| **Candidate portal** (current) | `python run.py --demo` | Resume upload / builder / ATS score / AI tailoring + job-search agents. Candidate side only. |
| Phase 1 research prototype | `python run.py --app phase1` | Earlier prototype with employer ranking, XAI and identity-blind review (see below). |

## Candidate portal

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # use .venv/bin/python on macOS/Linux
.venv/Scripts/python run.py --demo                            # http://127.0.0.1:8000
```

Demo login (synthetic, local only): `demo@talentforge.example` / `Demo@12345`.
Samples to try: `samples/sample_resume_fresher.txt`, `samples/job_backend_developer.txt`.

| Area | What it does | Code |
|---|---|---|
| Sign in / register | Candidate accounts, consent, delete-all-data | `portal/main.py` |
| Resume upload | Reads the **whole** document (every page, DOCX tables/headers/text boxes, two-column PDFs, symbol-font bullets), recognises headings in many spellings, and keeps anything it cannot classify as an extra section so nothing is dropped. Shows what it read (counts, sections, original text) next to the preview | `portal/doc_extract.py`, `portal/resume_parser.py` |
| Resume builder | Build from scratch with live preview and autosave; several resumes per user; DOCX export and print-to-PDF | `portal/static/builder.js`, `portal/export.py` |
| Templates | Three ATS-friendly single-column templates: **Classic** (Georgia, centred, ruled headings), **Modern** (Calibri, accent bars), **Executive** (Arial, letter-spaced headings, strong top rule); 8 accent colours; chosen at creation and changeable any time; DOCX export follows the template | `portal/styles.py`, `portal/static/portal.css` |
| Improve score | A measured plan towards 99: quick wins we can apply, items needing one fact from you, and manual work, each with its real point gain (found by re-scoring). Apply as a new copy or in place | `portal/improve.py` |
| Resume score | 7 explained checks (contact, summary, experience, impact wording, skills, education, hygiene) with concrete fixes | `portal/resume_score.py` |
| Tailoring | Paste a posting (or pick one an agent found) -> keyword gap, role-specific summary, stronger bullet openers, skill order, optional metric prompts. Accepted changes create a **new copy**; shows match before/after | `portal/tailor.py` |
| Job agents | Plans queries, searches several sources in parallel, de-duplicates (within a run and across runs), filters by level/location/work mode, drops unrelated roles, scores against your resume, keeps a step-by-step activity log, runs on a schedule or on demand | `portal/agent.py` |
| Job sources | Remotive, Arbeitnow, RemoteOK (no key needed); Adzuna and JSearch (LinkedIn/Indeed/Glassdoor via licensed API) when keys are set | `portal/providers.py` |
| Tracker | Saved / applied / interview / offer / closed | `/applications` |

### Job sources and keys

Only official feeds and APIs are used. **LinkedIn, Indeed and Naukri are not scraped** (their terms forbid it).
To reach them legitimately, set a key for an aggregator before starting:

```bash
set ADZUNA_APP_ID=...  &  set ADZUNA_APP_KEY=...  &  set ADZUNA_COUNTRY=in     # country job boards incl. India
set RAPIDAPI_KEY=...                                                              # JSearch (LinkedIn, Indeed, Glassdoor...)
set ANTHROPIC_API_KEY=...                                                         # optional: Claude wording for tailoring + extra search queries
```

### Honest limits

- Rule-based tailoring never invents facts. Claude (when enabled) can only rephrase; any rewrite that introduces
  a number not already in the resume is discarded. Added skills and metrics are off by default and need your confirmation.
- The agent does **not** apply to jobs for you. It finds, ranks and explains; you apply on the original posting.
- Resume parsing is rule-based and works best on conventional layouts; always review the parsed sections.
- The free feeds are mostly remote/European roles. For India-specific boards use Adzuna with `ADZUNA_COUNTRY=in`.
- Match scores are heuristic (skills coverage + title + text similarity), not a validated hiring model.
- Prototype-grade security (signed cookies, PBKDF2, no CSRF tokens or rate limiting).

---

# Phase 1 research prototype

Working end-to-end MVP of the loop defined in `../TalentForge_Phase1_Scope_and_Screens.md`:

> A candidate uploads a resume and gets an **explained** employability score, skill-gap report and job matches.
> An employer posts a job and gets a **ranked, explained** shortlist, and a human makes the final decision.

## Run it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows (use .venv/bin/python on macOS/Linux)
.venv/Scripts/python run.py                                 # http://127.0.0.1:8000
```

`run.py --reset` deletes the local database and reloads the synthetic demo data.
`python -m pytest -q` runs the test suite.

### Demo accounts (synthetic data, local only)

All demo accounts use the password `Demo@12345`.

| Role | Email | What to look at |
|---|---|---|
| Candidate | `aarav.menon@demo.example` | Data analyst, strong profile, 3 applications |
| Candidate | `sara.kulkarni@demo.example` | Frontend, no experience: see the gaps and what-if simulator |
| Employer | `hr@nimbus.example` | Nimbus Analytics: ranked candidates, anonymised mode, decisions |
| Employer | `talent@orbit.example` | Orbit Cloud Labs |
| Admin | `admin@talentforge.example` | Audit log of AI outputs and human decisions |

## Optional: LLM explanations

Scores, ranks and factors are **always computed by deterministic code**. If `ANTHROPIC_API_KEY` is set,
the plain-language explanations are phrased by Claude (`claude-opus-5`, low effort, server-side fallbacks on)
from the computed facts only; otherwise built-in templates are used. Every explanation shows which one wrote it.

```bash
set ANTHROPIC_API_KEY=...        # Windows cmd;  export ANTHROPIC_API_KEY=... elsewhere
set TALENTFORGE_LLM=off          # force templates even when a key exists
```

## What is in Phase 1

| Feature | Where |
|---|---|
| Register / login / roles, consent, delete-my-data | `app/main.py` |
| Resume upload (PDF, DOCX, TXT) and rule-based parsing → editable profile | `app/parsing.py` |
| Skill taxonomy + 8-role catalogue | `app/taxonomy.py` |
| Skill-gap analysis (matched / partial / missing, with resume evidence) | `app/scoring.py` |
| Employability score with factor breakdown + what-if simulator | `app/scoring.py` |
| Job matching with "why matched", candidate ranking | `app/matching.py` |
| Identity-blind review mode + automatic "ranking unchanged without identities" check | `app/matching.py` |
| Human decision with mandatory reason, optional feedback to rejected candidates | `app/main.py` |
| Audit log of AI results and decisions | `app/db.py`, `/admin/audit` |

## Honest limits of Phase 1

- **Parsing is rule-based**, not LLM-based. It works on conventionally structured resumes and shows warnings
  and an edit screen when it is unsure. LLM parsing is a Phase 2 upgrade.
- **Matching is taxonomy-aware skill coverage plus TF-IDF text similarity**, not neural embeddings yet.
  The taxonomy has ~60 skills and 8 roles; skills outside it are ignored.
- **The "orchestrator" is a plain router.** Autonomous multi-agent planning, interview agents and scheduling are Phase 2.
- **Fairness claim is narrow:** name, email, phone and institution are not used for scoring, and a test proves the
  ranking is unchanged without them. This does *not* prove the absence of bias in the skill taxonomy or weights.
- Demo data is synthetic. The weights in `app/scoring.py` are design choices to be validated, not trained values.
- Security is prototype-grade (signed session cookie, PBKDF2 passwords, no CSRF tokens, no rate limiting).

## Layout

```
app/
  main.py        routes (public, candidate, employer, admin)
  parsing.py     resume text extraction + structured parse
  taxonomy.py    skills, implied/related skills, role catalogue
  scoring.py     skill gap, employability, what-if
  matching.py    job match, ranking, anonymisation, blind check
  llm.py         optional Claude phrasing with template fallback
  db.py          SQLite schema and helpers
  seed.py        synthetic demo data
  templates/     Jinja2 pages (26)
  static/        CSS
tests/           pytest suite
```
