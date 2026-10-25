"""Persistent order journal — dedup, overlap, and account scoping."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app_paths import DATA_DIR

log = logging.getLogger("court")

ORDER_JOURNAL_PATH = DATA_DIR / "watch-orders.json"
BLOCKING_STATES = frozenset({"pending", "success", "uncertain"})


class JournalUnavailable(RuntimeError):
    pass


def _minutes(hm: str) -> int:
    h, m = map(int, hm.split(":"))
    return h * 60 + m


def ranges_overlap(
    date_a: str, start_a: str, end_a: str,
    date_b: str, start_b: str, end_b: str,
) -> bool:
    """Same calendar date and half-open [start, end) intervals intersect."""
    if date_a != date_b:
        return False
    sa, ea = _minutes(start_a), _minutes(end_a)
    sb, eb = _minutes(start_b), _minutes(end_b)
    return sa < eb and sb < ea


def lock_key(ground_id: str, date: str, start: str, end: str) -> str:
    return f"{ground_id}|{date}|{start}|{end}"


class OrderJournal:
    def __init__(self, path: Path = ORDER_JOURNAL_PATH):
        self._path = path

    def load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
                raise ValueError("invalid journal structure")
            return data
        except (OSError, ValueError, TypeError) as e:
            log.warning("journal read failed: %s", e)
            raise JournalUnavailable("Order journal is unreadable") from e

    def persist_record(self, record: dict[str, Any]) -> bool:
        """Atomic upsert by (account_id, date, court, start, end)."""
        records = self.load()
        key_fields = ("account_id", "date", "ground_id", "court", "start", "end")
        key = {k: record.get(k) for k in key_fields}
        records = [r for r in records if not all(r.get(k) == v for k, v in key.items())]
        records.append(record)
        temp = self._path.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self._path)
            return True
        except OSError as e:
            log.error("journal persist failed: %s", e)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def has_blocking_on_dates(self, account_id: str, dates: list[str]) -> bool:
        date_set = set(dates)
        for r in self.load():
            if r.get("account_id") != account_id:
                continue
            if r.get("date") in date_set and r.get("state") in BLOCKING_STATES:
                return True
        return False

    def find_blockers(
        self,
        account_id: str,
        *,
        date: str,
        start: str,
        end: str,
        ground_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Block when this account already has pending/success/uncertain on overlapping time.

        Semantics: one active order per account per overlapping time window on a given
        date, regardless of court/groundId — prevents cross-venue double booking for the
        same person at the same time. Exact same ground+slot is also blocked via ground_id.
        Records for other account_id values are ignored (account switch).
        """
        out: list[dict[str, Any]] = []
        for r in self.load():
            if r.get("account_id") != account_id:
                continue
            if r.get("state") not in BLOCKING_STATES:
                continue
            if ground_id and r.get("ground_id") == ground_id:
                if (
                    r.get("date") == date
                    and r.get("start") == start
                    and r.get("end") == end
                ):
                    out.append(r)
                    continue
            if ranges_overlap(
                date, start, end,
                str(r.get("date", "")), str(r.get("start", "")), str(r.get("end", "")),
            ):
                out.append(r)
        return out
