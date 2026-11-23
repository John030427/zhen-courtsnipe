"""Which half-hours the school keeps not bookable (status 0) on each weekday, learned from the
workbench's own queries.

Status 0 is what the school shows for a class, a team or a slot not released yet. A date is only
recorded once it is open for booking (released at 20:00 two days ahead), so "not released yet"
never counts. Kept for six weeks in closed-slots.json in the data folder; only dates, times, court
numbers and statuses are stored.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from typing import Callable

from app_paths import DATA_DIR
from models.rush import CHINA

log = logging.getLogger("court")

PATH = DATA_DIR / "closed-slots.json"
RELEASE_DAYS_AHEAD = 2      # a date opens for booking at release time, this many days before
KEEP_DAYS = 42
SAVE_EVERY_SEC = 20
WEEKDAYS = "一二三四五六日"

# Weekly closures seen on the official booking page (2026-11-23, badminton): weekday (Monday = 0)
# → time ranges. They hold whatever the workbench has recorded so far.
KNOWN_WEEKLY: dict[str, dict[int, list[tuple[str, str]]]] = {
    "badminton": {0: [("10:00", "12:00"), ("14:00", "16:00")],
                  1: [("10:00", "12:00"), ("14:00", "18:00")]},
}


def _minutes(hm: str) -> int:
    h, m = str(hm).split(":")[:2]
    return int(h) * 60 + int(m)


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class ClosedSlots:
    def __init__(self, sport_for_court: Callable[[object], str], *, release_hm: Callable[[], str] | None = None,
                 path: Path = PATH, now: Callable[[], datetime] | None = None):
        self._sport_for_court = sport_for_court
        self._release_hm = release_hm or (lambda: "20:00")
        self._path = path
        self._now = now or (lambda: datetime.now(CHINA))
        self._saved_at = 0.0
        self._dirty = False
        # {sport: {date: {court_no: {time: status}}}}
        self.data: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = raw
        except (OSError, ValueError):
            pass

    # ── recording ────────────────────────────────────────
    def released(self, day: str, now: datetime | None = None) -> bool:
        """Whether `day` was open for booking at `now` (so a 0 there is not "not released yet")."""
        now = now or self._now()
        try:
            d = Date.fromisoformat(day)
            h, m = (int(x) for x in str(self._release_hm()).split(":")[:2])
        except (TypeError, ValueError):
            return False
        opens = datetime(d.year, d.month, d.day, h, m, tzinfo=CHINA) - timedelta(days=RELEASE_DAYS_AHEAD)
        return now >= opens + timedelta(seconds=30)

    def observe(self, court, day: str, blocks: list[dict]) -> None:
        """One successful court query: remember each half-hour's status, if `day` is open."""
        if not blocks or not self.released(day):
            return
        sport = self._sport_for_court(court)
        slots = {str(b.get("time")): str(b.get("status")) for b in blocks
                 if isinstance(b, dict) and b.get("time") and b.get("status") is not None}
        if not slots:
            return
        self.data.setdefault(sport, {}).setdefault(day, {})[str(court.no)] = slots
        self._dirty = True
        if time.monotonic() - self._saved_at >= SAVE_EVERY_SEC:
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        cutoff = (self._now().date() - timedelta(days=KEEP_DAYS)).isoformat()
        for days in self.data.values():
            for day in [d for d in days if d < cutoff]:
                del days[day]
        try:
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._path)
            self._dirty, self._saved_at = False, time.monotonic()
        except OSError as exc:
            log.warning("closed slots not saved: %s", type(exc).__name__)

    # ── reading ──────────────────────────────────────────
    def _closed_ranges(self, courts: dict[str, dict[str, str]]) -> list[tuple[int, int]]:
        """Half-hours where every court seen that day was 0, merged into ranges."""
        times = sorted({t for slots in courts.values() for t in slots}, key=_minutes)
        closed = [t for t in times if courts and all(slots.get(t) == "0" for slots in courts.values())]
        ranges: list[tuple[int, int]] = []
        for t in closed:
            start = _minutes(t)
            if ranges and ranges[-1][1] == start:
                ranges[-1] = (ranges[-1][0], start + 30)
            else:
                ranges.append((start, start + 30))
        return ranges

    def weekly(self, sport: str) -> list[dict]:
        """Every weekday and time range in which all courts were not bookable, with the dates."""
        found: dict[tuple[int, int, int], list[str]] = {}
        for day, courts in sorted((self.data.get(sport) or {}).items()):
            weekday = Date.fromisoformat(day).weekday()
            for start, end in self._closed_ranges(courts):
                found.setdefault((weekday, start, end), []).append(day)
        for wd, ranges in (KNOWN_WEEKLY.get(sport) or {}).items():
            for start, end in ranges:
                found.setdefault((wd, _minutes(start), _minutes(end)), [])
        return [{"weekday": WEEKDAYS[wd], "weekday_no": wd, "start": _hm(s), "end": _hm(e), "dates": dates,
                 "known": (wd, _hm(s), _hm(e)) in {(k, a, b) for k, rs in (KNOWN_WEEKLY.get(sport) or {}).items()
                                                    for a, b in rs}}
                for (wd, s, e), dates in sorted(found.items())]

    def known_overlap(self, sport: str, day: str, start: str, end: str) -> dict | None:
        """A known weekly closure on this weekday that overlaps start–end: the whole window cannot
        be booked then, since every half-hour of it has to be free."""
        try:
            weekday = Date.fromisoformat(day).weekday()
            s, e = _minutes(start), _minutes(end)
        except (TypeError, ValueError):
            return None
        for a, b in (KNOWN_WEEKLY.get(sport) or {}).get(weekday, []):
            if _minutes(a) < e and s < _minutes(b):
                return {"weekday": WEEKDAYS[weekday], "start": a, "end": b, "known": True,
                        "window_start": start, "window_end": end}
        return None

    def same_weekday_before(self, sport: str, day: str, start: str, end: str) -> dict | None:
        """What the latest recorded same weekday before `day` showed for start–end: how many
        courts were not bookable for any part of it, and which courts were bookable throughout."""
        try:
            target = Date.fromisoformat(day)
            want = list(range(_minutes(start), _minutes(end), 30))
        except (TypeError, ValueError):
            return None
        days = self.data.get(sport) or {}
        earlier = sorted((d for d in days if d < day and Date.fromisoformat(d).weekday() == target.weekday()),
                         reverse=True)
        for d in earlier:
            courts = days[d]
            seen = {no: slots for no, slots in courts.items() if all(_hm(m) in slots for m in want)}
            if not seen:
                continue
            closed = sorted((int(no) for no, slots in seen.items() if any(slots[_hm(m)] == "0" for m in want)))
            usable = sorted(int(no) for no in seen if int(no) not in closed)
            return {"date": d, "weekday": WEEKDAYS[target.weekday()], "start": start, "end": end,
                    "courts_seen": len(seen), "closed_courts": closed, "usable_courts": usable,
                    "all_closed": not usable}
        return None
