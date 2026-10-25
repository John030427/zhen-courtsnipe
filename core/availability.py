from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from models.availability import CellState, CourtScanResult, ScanSnapshot, status_to_cell
from network.reservation_client import query_court
from utils import norm_hm

if TYPE_CHECKING:
    from engine import Engine


def _sanitize_blocks(blocks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for blk in blocks:
        out.append({
            "time": blk.get("time"),
            "endTime": blk.get("endTime"),
            "status": blk.get("status"),
        })
    return out


def build_matrix(courts: list[CourtScanResult]) -> tuple[list[str], dict[str, dict[str, str]]]:
    times: set[str] = set()
    per_court: dict[int, dict[str, CellState]] = {}

    for c in courts:
        if not c.ok:
            continue
        per_court[c.court_no] = {}
        for blk in c.blocks:
            t = norm_hm(str(blk.get("time") or ""))
            if not t:
                continue
            times.add(t)
            per_court[c.court_no][t] = status_to_cell(blk.get("status"))

    ordered = sorted(times)
    matrix: dict[str, dict[str, str]] = {}
    for t in ordered:
        row: dict[str, str] = {}
        for c in courts:
            key = str(c.court_no)
            if not c.ok:
                row[key] = CellState.UNKNOWN.value
                continue
            row[key] = per_court.get(c.court_no, {}).get(t, CellState.UNKNOWN).value
        matrix[t] = row
    slot_ends: dict[str, str] = {}
    for c in courts:
        if not c.ok:
            continue
        for blk in c.blocks:
            t = norm_hm(str(blk.get("time") or ""))
            et = norm_hm(str(blk.get("endTime") or ""))
            if t and et and t not in slot_ends:
                slot_ends[t] = et
    return ordered, matrix, slot_ends


async def scan_all_courts(
    engine: Engine,
    date: str,
    *,
    courts: list | None = None,
    sport_id: str = "badminton",
    concurrency: int = 3,
) -> ScanSnapshot:
    sem = asyncio.Semaphore(max(1, min(3, int(concurrency))))
    scan_id = uuid.uuid4().hex[:12]
    started = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    auth_revision = engine.tokens.auth_revision
    token = engine.token
    user_id = engine.user_id

    async def one(court) -> CourtScanResult:
        async with sem:
            # HTTP 1s pacing already applied in utils.get_client event hook — do not wait twice.
            t0 = time.monotonic()
            res = await query_court(court, date, token, user_id)
            elapsed = (time.monotonic() - t0) * 1000
            if res.get("ok"):
                return CourtScanResult(
                    court_no=court.no,
                    court_name=court.name,
                    ok=True,
                    elapsed_ms=round(elapsed, 1),
                    blocks=_sanitize_blocks(res.get("blocks") or []),
                )
            return CourtScanResult(
                court_no=court.no,
                court_name=court.name,
                ok=False,
                error=str(res.get("error") or "查询失败"),
                elapsed_ms=round(elapsed, 1),
                auth_fail=bool(res.get("auth_fail")),
                error_kind=str(res.get('error_kind') or ('auth' if res.get('auth_fail') else 'query')),
                school_msg=str(res.get("school_msg") or ""),
                code=str(res.get("code") or ""),
            )

    court_list = list(courts if courts is not None else engine.courts)
    results = await asyncio.gather(*(one(c) for c in court_list))
    times, matrix, slot_ends = build_matrix(list(results))
    finished = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    complete = all(r.ok for r in results) and bool(court_list)
    return ScanSnapshot(
        scan_id=scan_id,
        date=date,
        auth_revision=auth_revision,
        started_at=started,
        finished_at=finished,
        complete=complete,
        courts=list(results),
        matrix=matrix,
        times=times,
        slot_ends=slot_ends,
        sport_id=sport_id,
    )
