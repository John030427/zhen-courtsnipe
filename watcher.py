"""Bounded venue monitoring — facade over WatchEngine (Phase 4)."""
from __future__ import annotations

from core.watch_engine import (
    JOURNAL,
    WatchEngine,
    candidates,
    detect_busy_free_changes,
    find_available_windows,
)
from models.plans import CHINA, Target, WatchPlan, minutes, now


class Watcher(WatchEngine):
    """Backward-compatible alias; tests import Watcher from watcher."""

    pass


__all__ = [
    "CHINA",
    "JOURNAL",
    "Target",
    "WatchPlan",
    "Watcher",
    "candidates",
    "detect_busy_free_changes",
    "find_available_windows",
    "minutes",
    "now",
]
