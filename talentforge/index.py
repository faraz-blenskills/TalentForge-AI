"""Vercel entrypoint (zero-config FastAPI): exposes the candidate portal's ASGI `app`.

The repository also contains an older prototype under app/main.py; this file makes the choice explicit.
"""
from portal.main import app  # noqa: F401
