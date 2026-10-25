"""Where the workbench keeps user data (config, order journal, audit, logs).

A git checkout keeps the existing layout: everything next to app.py. A release package
(``PACKAGED`` marker next to app.py) keeps data per user under %LOCALAPPDATA%, so replacing
the package folder on update never touches config, the order journal or audit logs.
``SUSTECH_DATA_DIR`` overrides both (tests, portable use).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
PACKAGED_MARKER = APP_ROOT / "PACKAGED"


def resolve_data_dir() -> Path:
    override = os.environ.get("SUSTECH_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if PACKAGED_MARKER.exists():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SustechReservation" / "data"
    return APP_ROOT


DATA_DIR = resolve_data_dir()
LOG_DIR = DATA_DIR / "logs"


def ensure_data_dir(template: Path = APP_ROOT / "config.example.toml") -> Path:
    """Create the data folder and a config from the clean template; never overwrite."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    config = DATA_DIR / "config.toml"
    if not config.exists() and template.exists():
        shutil.copyfile(template, config)
    return DATA_DIR


IMPORTABLE = ("config.toml", "watch-orders.json")


def import_from(source: Path) -> list[str]:
    """Copy config and order journal from an older install. Existing files are never replaced,
    so a journal's pending/uncertain records cannot be lost or overwritten here."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in IMPORTABLE:
        src, dst = Path(source) / name, DATA_DIR / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            copied.append(name)
    return copied
