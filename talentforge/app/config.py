"""Tiny .env loader (no dependency). Real environment variables always win over the file."""
from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_env(path: Path | None = None) -> None:
    global _LOADED
    if _LOADED or os.environ.get("TALENTFORGE_NO_DOTENV"):
        return
    _LOADED = True
    f = path or Path(__file__).resolve().parent.parent / ".env"
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
