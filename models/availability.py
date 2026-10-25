from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CellState(str, Enum):
    FREE = "free"
    BUSY = "busy"
    UNBOOKABLE = "unbookable"
    UNKNOWN = "unknown"


def status_to_cell(status: Any) -> CellState:
    if status is None:
        return CellState.UNKNOWN
    s = str(status).strip()
    if s == "1":
        return CellState.FREE
    if s in ("0", "2"):
        return CellState.BUSY
    if s in ("3", "-1"):
        return CellState.UNBOOKABLE
    return CellState.UNKNOWN


@dataclass
class CourtScanResult:
    court_no: int
    court_name: str
    ok: bool
    error: str = ""
    elapsed_ms: float = 0.0
    blocks: list[dict[str, Any]] = field(default_factory=list)
    auth_fail: bool = False
    error_kind: str = ""
    school_msg: str = ""
    code: str = ""


@dataclass
class ScanSnapshot:
    scan_id: str
    date: str
    auth_revision: int
    started_at: str
    finished_at: str
    complete: bool
    courts: list[CourtScanResult]
    matrix: dict[str, dict[str, str]]
    times: list[str] = field(default_factory=list)
    slot_ends: dict[str, str] = field(default_factory=dict)
    sport_id: str = "badminton"

    def to_public(self) -> dict[str, Any]:
        from core.dates import date_matrix_labels

        labels = date_matrix_labels(self.date)
        return {
            "sport_id": self.sport_id,
            "scan_id": self.scan_id,
            "date": self.date,
            "date_labels": labels,
            "auth_revision": self.auth_revision,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "complete": self.complete,
            "times": self.times,
            "slot_ends": self.slot_ends,
            "matrix": self.matrix,
            "courts": [
                {
                    "court": c.court_no,
                    "name": c.court_name,
                    "ok": c.ok,
                    "error": c.error,
                    "elapsed_ms": c.elapsed_ms,
                    "auth_fail": c.auth_fail,
                    "error_kind": c.error_kind,
                    "school_msg": c.school_msg,
                    "code": c.code,
                }
                for c in self.courts
            ],
        }
