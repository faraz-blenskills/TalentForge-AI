"""Synthetic demo data. No real people, employers or resumes are used."""
from __future__ import annotations

import json

from . import db
from .matching import match_job
from .parsing import parse_resume
from .scoring import employability
from .security import hash_password

DEMO_PASSWORD = "Demo@12345"

ORGS = {
    "hr@nimbus.example": ("Nimbus Analytics", "HR Lead (Nimbus)", "Data & Analytics", "51-200"),
    "talent@orbit.example": ("Orbit Cloud Labs", "Talent Partner (Orbit)", "Cloud Software", "201-500"),
}

JOBS = [
    ("hr@nimbus.example", "Junior Data Analyst", "Bengaluru", "Full-time", 0,
     "Work with business teams to turn raw data into weekly dashboards. You will write SQL, clean data in Python and "
     "pandas, and present findings with Power BI. Strong statistics fundamentals and clear communication matter.",
     {"sql": 3, "excel": 2, "python": 2, "pandas": 2, "data visualization": 3, "statistics": 2, "power bi": 2},
     ["tableau", "communication"]),
    ("hr@nimbus.example", "Machine Learning Engineer", "Remote", "Full-time", 2,
     "Build and ship machine learning models for demand forecasting. You will use Python, scikit-learn, pandas and numpy, "
     "evaluate models with sound statistics, and work with SQL data sources. Deep learning experience is a plus.",
     {"python": 3, "machine learning": 3, "scikit-learn": 2, "pandas": 2, "numpy": 2, "statistics": 3, "sql": 2},
     ["pytorch", "tensorflow", "docker"]),
    ("hr@nimbus.example", "LLM Application Engineer", "Pune", "Full-time", 1,
     "Design retrieval-augmented generation features on top of large language models. You will build prompts, a vector "
     "database index and a FastAPI service, and evaluate answer quality. Familiarity with NLP is valued.",
     {"python": 3, "llm": 3, "rag": 3, "prompt engineering": 2, "vector database": 2, "fastapi": 2, "nlp": 1},
     ["langchain", "docker"]),
    ("hr@nimbus.example", "Business Intelligence Intern", "Mumbai", "Internship", 0,
     "Support the BI team with Excel reports and Tableau or Power BI dashboards. SQL basics and curiosity about data are "
     "expected; we will teach the rest.",
     {"excel": 3, "sql": 2, "data visualization": 2, "communication": 1},
     ["power bi", "tableau", "python"]),
    ("talent@orbit.example", "Backend Developer (Python)", "Hyderabad", "Full-time", 2,
     "Own REST APIs for our cloud control plane. You will build services in Python with FastAPI or Flask, model data in "
     "PostgreSQL, containerise with Docker and ship through CI/CD. Git and Linux fluency required.",
     {"python": 3, "rest api": 3, "sql": 3, "fastapi": 2, "docker": 2, "git": 2, "linux": 1},
     ["postgresql", "redis", "ci/cd", "aws"]),
    ("talent@orbit.example", "Frontend Engineer", "Remote", "Full-time", 2,
     "Build accessible, fast interfaces in React and TypeScript for our customer console. You will consume REST APIs, "
     "write tests, and collaborate closely with designers and backend engineers.",
     {"javascript": 3, "react": 3, "html": 2, "css": 2, "typescript": 2, "git": 1, "rest api": 2},
     ["testing", "node.js"]),
    ("talent@orbit.example", "DevOps Engineer", "Pune", "Full-time", 3,
     "Run our Kubernetes platform on AWS. You will automate delivery with CI/CD pipelines, manage infrastructure as code "
     "with Terraform, and keep Linux hosts and containers healthy and observable.",
     {"linux": 3, "docker": 3, "kubernetes": 3, "ci/cd": 3, "aws": 3, "git": 2, "terraform": 1},
     ["monitoring", "python"]),
    ("talent@orbit.example", "QA Automation Engineer", "Chennai", "Full-time", 1,
     "Design automated test suites for web and API products using Selenium and pytest. You will write test cases, "
     "integrate them into CI and report quality metrics to an agile team.",
     {"testing": 3, "selenium": 2, "python": 2, "pytest": 2, "rest api": 2, "git": 1, "agile": 1},
     ["ci/cd", "sql"]),
]

CANDIDATES = [
    ("Aarav Menon", "aarav.menon@demo.example", "data_analyst", """Aarav Menon
aarav.menon@demo.example

SUMMARY
Final-year student who enjoys turning messy data into clear dashboards.

EDUCATION
B.Tech in Computer Science and Engineering, Greenfield Institute of Technology, 2026

SKILLS
Python, SQL, pandas, numpy, Excel, Power BI, data visualization, statistics, communication

PROJECTS
Retail sales dashboard
- Built a Power BI dashboard from a SQL database of 200k orders; cut weekly reporting time for the club by half
Customer churn analysis
- Cleaned data with pandas and used statistics and matplotlib to find the drivers of churn
Campus survey analysis
- Analysed 1,200 survey responses in Excel and Python

EXPERIENCE
Data Analytics Intern, Brightpath Foods, Jun 2025 - Aug 2025
- Automated a weekly Excel report with Python

CERTIFICATIONS
Google Data Analytics Certificate
SQL for Data Analysis (online course)
""", ["Junior Data Analyst", "Business Intelligence Intern", "Machine Learning Engineer"]),
    ("Diya Nair", "diya.nair@demo.example", "data_scientist", """Diya Nair
diya.nair@demo.example

EDUCATION
M.Tech in Artificial Intelligence, Lakeview University, 2025

SKILLS
Python, machine learning, deep learning, scikit-learn, pandas, numpy, pytorch, SQL, statistics, nlp, docker, git

EXPERIENCE
Machine Learning Engineer, Quantus Retail, Jul 2025 - Present
- Built demand forecasting models in scikit-learn and pytorch
- Deployed model services with Docker

PROJECTS
Resume screening classifier
- Fine-tuned an NLP model and reported precision and recall
Image defect detection
- CNN built with pytorch for a manufacturing dataset

CERTIFICATIONS
Deep Learning Specialisation
""", ["Machine Learning Engineer", "LLM Application Engineer"]),
    ("Rohan Iyer", "rohan.iyer@demo.example", "backend", """Rohan Iyer
rohan.iyer@demo.example

EDUCATION
B.E. in Information Technology, Riverbend College of Engineering, 2024

SKILLS
Python, Flask, REST API, SQL, PostgreSQL, Git, Linux, Docker

EXPERIENCE
Software Engineer, Stackline Systems, Aug 2024 - Present
- Built and maintained Flask REST APIs backed by PostgreSQL
- Containerised services with Docker and wrote unit testing in pytest

PROJECTS
Inventory API
- REST API with authentication and role-based access
Task tracker
- Flask app with PostgreSQL and Docker compose

CERTIFICATIONS
AWS Cloud Practitioner
""", ["Backend Developer (Python)", "DevOps Engineer"]),
    ("Sara Kulkarni", "sara.kulkarni@demo.example", "frontend", """Sara Kulkarni
sara.kulkarni@demo.example

EDUCATION
BCA, Hillcrest College, 2025

SKILLS
JavaScript, React, HTML, CSS, TypeScript, Git, REST API

PROJECTS
Portfolio site
- Responsive site with React and Tailwind CSS
Weather dashboard
- Consumed a public REST API with React and TypeScript
Expense tracker
- React app with local storage and charts
""", ["Frontend Engineer"]),
    ("Kabir Shah", "kabir.shah@demo.example", "llm_engineer", """Kabir Shah
kabir.shah@demo.example

EDUCATION
B.Tech in Computer Science, Summit Institute of Technology, 2026

SKILLS
Python, LLM, RAG, prompt engineering, FastAPI, vector database, langchain, NLP, Git, REST API

PROJECTS
Campus FAQ assistant
- Retrieval-augmented generation over college documents using chroma as the vector database and FastAPI
Resume summariser
- Prompt engineering and evaluation of LLM summaries
Study planner agent
- LangChain agent that schedules revision

EXPERIENCE
AI Intern, Nova Labs, May 2025 - Jul 2025
- Built a RAG prototype for support tickets

CERTIFICATIONS
Generative AI with LLMs (online course)
""", ["LLM Application Engineer", "Machine Learning Engineer"]),
    ("Meera Joshi", "meera.joshi@demo.example", "qa", """Meera Joshi
meera.joshi@demo.example

EDUCATION
B.Sc in Computer Science, Maple Grove College, 2024

SKILLS
Testing, Selenium, Python, SQL, Agile, communication

EXPERIENCE
QA Engineer, Pixelwave Studios, Jul 2024 - Present
- Wrote manual testing and test cases for web releases
- Automated regression tests with Selenium and Python

PROJECTS
Login flow test suite
- Selenium suite covering 40 scenarios
""", ["QA Automation Engineer"]),
]


def seed(force: bool = False) -> bool:
    db.init_db()
    if not force and db.one("SELECT 1 AS x FROM users LIMIT 1"):
        return False
    pw = hash_password(DEMO_PASSWORD)
    ts = db.now()

    db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
           ("admin@talentforge.example", "Platform Admin", pw, "admin", ts, ts))

    org_ids: dict[str, int] = {}
    for email, (org, person, industry, size) in ORGS.items():
        uid = db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
                     (email, person, pw, "employer", ts, ts))
        org_ids[email] = db.run("INSERT INTO organisations(owner_id, name, industry, size, website) VALUES (?,?,?,?,?)",
                                (uid, org, industry, size, ""))

    job_ids: dict[str, int] = {}
    for email, title, loc, jtype, min_y, desc, req, pref in JOBS:
        job_ids[title] = db.run(
            "INSERT INTO jobs(org_id, title, description, location, job_type, required_json, preferred_json, "
            "min_years, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (org_ids[email], title, desc, loc, jtype, json.dumps(req), json.dumps(pref), min_y, "open", ts))

    for name, email, target, text, applied in CANDIDATES:
        uid = db.run("INSERT INTO users(email, name, password_hash, role, consent_at, created_at) VALUES (?,?,?,?,?,?)",
                     (email, name, pw, "candidate", ts, ts))
        profile = parse_resume(text)
        if not profile["name"]:
            profile["name"] = name
        db.run("INSERT INTO candidate_profiles(user_id, profile_json, resume_text, resume_filename, target_role, "
               "updated_at) VALUES (?,?,?,?,?,?)",
               (uid, json.dumps(profile), text, "demo-resume.txt", target, ts))
        res = employability(profile, target)
        db.record_ai("score", f"user:{uid}", {"score": res["score"], "role": target, "factors": res["factors"]})
        for title in applied:
            jrow = db.one("SELECT * FROM jobs WHERE id=?", (job_ids[title],))
            job = {**jrow, "required": json.loads(jrow["required_json"]), "preferred": json.loads(jrow["preferred_json"])}
            m = match_job(profile, text, job)
            aid = db.run("INSERT INTO applications(job_id, candidate_id, status, match_score, match_json, applied_at) "
                         "VALUES (?,?,?,?,?,?)", (job["id"], uid, "applied", m["score"], json.dumps(m, default=str), ts))
            db.record_ai("match", f"application:{aid}", {"score": m["score"], "factors": m["factors"]})
    db.audit(None, "seed", "", "synthetic demo data loaded")
    return True


if __name__ == "__main__":
    print("seeded" if seed() else "database already has data (use --force via run.py)")
