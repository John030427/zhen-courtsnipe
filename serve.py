"""Run the workbench in-process so /api/admin/shutdown can stop it cleanly.

launcher.py starts this file; run-local.ps1 -Console uses uvicorn for development.
Only ever binds 127.0.0.1.
"""
from __future__ import annotations

import os
import sys

import uvicorn

from app_paths import LOG_DIR, ensure_data_dir

HOST = "127.0.0.1"


def app_port() -> int:
    return int(os.environ.get("SUSTECH_APP_PORT") or 8766)


def main() -> None:
    ensure_data_dir()
    # pythonw has no console streams unless the parent redirected them.
    if sys.stdout is None or sys.stderr is None:
        stream = open(LOG_DIR / "server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    port = app_port()
    # The capture IPC posts back to this port; keep both in step.
    os.environ["SUSTECH_APP_PORT"] = str(port)
    from app import app  # after the data folder exists: the engine reads config at import

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="info"))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
