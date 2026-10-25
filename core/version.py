"""Identity of the running service for /api/health and the status bar.

Release packages write a VERSION file next to app.py; a git checkout falls back
to the short commit. Neither lookup touches the network or the school API.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP_ID = "sustech-reservation"
ROOT = Path(__file__).resolve().parents[1]

INSTANCE_ID = uuid.uuid4().hex
STARTED_AT = datetime.now(timezone.utc).isoformat()
_STARTED_MONO = time.monotonic()


def _read_version_file() -> str | None:
    try:
        text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _git_commit() -> str | None:
    if not (ROOT / ".git").exists():
        return None
    # Started windowless (pythonw), a console program such as git would flash a console window.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


VERSION = _read_version_file() or "dev"
COMMIT = _git_commit()


def service_identity() -> dict:
    return {
        "app": APP_ID,
        "version": VERSION,
        "commit": COMMIT,
        "instance_id": INSTANCE_ID,
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "uptime_sec": int(time.monotonic() - _STARTED_MONO),
    }
