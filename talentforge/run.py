"""Start TalentForge AI locally.

  python run.py                 # candidate portal (resume + tailoring + job agents) on :8000
  python run.py --demo          # ...and create demo@talentforge.example with a sample resume
  python run.py --app phase1    # the earlier research prototype (candidate + employer)
"""
import argparse
import os
import sys

import uvicorn

from app import db, seed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", choices=["portal", "phase1"], default="portal")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--demo", action="store_true", help="portal: create a demo candidate with a sample resume")
    ap.add_argument("--reset", action="store_true", help="delete the local database first")
    ap.add_argument("--no-demo", action="store_true", help="phase1: do not load synthetic demo data")
    args = ap.parse_args()

    if args.reset and db.DB_PATH.exists():
        db.DB_PATH.unlink()
        print(f"Deleted {db.DB_PATH}")
    if db.USE_PG and args.reset:
        raise SystemExit("--reset only deletes the local SQLite file; it will not touch your cloud database.")
    db.init_db()
    print("Database:", "Supabase / Postgres (cloud)" if db.USE_PG else f"local SQLite file ({db.DB_PATH.name})")
    if db.USE_PG and args.demo:
        print("Note: --demo is ignored on the cloud database so no known-password demo account is created there.")
        args.demo = False

    if args.app == "portal":
        from portal import providers, store
        store.init()
        if args.demo:
            from portal import demo
            if demo.ensure_demo_user():
                print(f"Demo account: {demo.DEMO_EMAIL} / {demo.DEMO_PASSWORD}")
        ready = [p["label"] for p in providers.provider_status() if p["available"]]
        print("Job sources ready:", ", ".join(ready) or "none")
        target = "portal.main:app"
    else:
        if not args.no_demo and seed.seed():
            print(f"Loaded synthetic demo data (password for every demo account: {seed.DEMO_PASSWORD})")
        target = "app.main:app"

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set: AI wording uses rule-based suggestions (everything else is unchanged).")
    uvicorn.run(target, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    sys.exit(main())
