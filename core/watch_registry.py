"""Per-sport Watch task slots — badminton and tennis run independently."""
from __future__ import annotations

from typing import Any

from core.sports import SPORT_BADMINTON, SPORT_TENNIS, normalize_sport_id, public_sports_capabilities
from core.watch_engine import WatchEngine
from models.plans import WatchPlan


class SportWatchRegistry:
    """One WatchEngine per sport_id; shared engine, shared rate limiter."""

    _SLOTS = (SPORT_BADMINTON, SPORT_TENNIS)

    def __init__(self, engine):
        self.engine = engine
        self._engines: dict[str, WatchEngine] = {}

    def _slot(self, sport_id: str) -> WatchEngine:
        sid = normalize_sport_id(sport_id)
        if sid not in self._engines:
            self._engines[sid] = WatchEngine(self.engine)
        return self._engines[sid]

    @property
    def state(self) -> dict[str, Any]:
        """Backward-compatible: badminton slot state."""
        return self.state_for(SPORT_BADMINTON)

    def state_for(self, sport_id: str) -> dict[str, Any]:
        st = dict(self._slot(sport_id).state)
        st["sport_id"] = normalize_sport_id(sport_id)
        return st

    def all_states(self) -> dict[str, dict[str, Any]]:
        return {sid: self.state_for(sid) for sid in self._SLOTS}

    def is_running(self, sport_id: str) -> bool:
        return bool(self._slot(sport_id).state.get("running"))

    def any_running(self) -> bool:
        return any(self.is_running(sid) for sid in self._SLOTS)

    async def start(self, plan: WatchPlan) -> dict:
        sid = normalize_sport_id(plan.sport_id)
        caps = public_sports_capabilities().get(sid) or {}
        if not caps.get("watch"):
            msg = caps.get("stub_message") or f"{sid} 尚未接入监控"
            return {"ok": False, "msg": msg, "sport_id": sid}

        return await self._slot(sid).start(plan)

    async def stop(self, sport_id: str = SPORT_BADMINTON) -> None:
        await self._slot(sport_id).stop()

    async def stop_all(self) -> None:
        for sid in self._SLOTS:
            if self.is_running(sid):
                await self._slot(sid).stop()
