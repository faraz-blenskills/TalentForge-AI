"""Test isolation: never read a developer's .env and never touch a real (Supabase) database."""
import os

os.environ["TALENTFORGE_NO_DOTENV"] = "1"
for _k in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SITE_URL"):
    os.environ.pop(_k, None)
