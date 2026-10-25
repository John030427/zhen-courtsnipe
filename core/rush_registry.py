"""One Rush per sport, sharing account order guards and a bounded VID pool."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime

from core.rush_engine import RushEngine
from models.rush import CHINA, RushPlan

LABELS = {"badminton": "羽毛球", "tennis": "网球"}


class SportRushRegistry:
    def __init__(self, engine):
        self.engine = engine
        self.slots = {sid: RushEngine(engine) for sid in ("badminton", "tennis")}
        self._gate = asyncio.Lock()
        self._last_sport = "badminton"
        self._pool_gate = asyncio.Lock()
        self._pool_owners = set()

    def _selected(self):
        return next((slot for slot in self.slots.values() if slot.running), self.slots[self._last_sport])

    def __getattr__(self, name):
        if name in ("phase", "plan", "run_id", "msg", "reason", "outcome", "task", "applied",
                    "started_at", "finished_at"):
            return getattr(self._selected(), name)
        raise AttributeError(name)

    @property
    def running(self):
        return any(slot.running for slot in self.slots.values())

    def is_running(self, sport_id):
        return bool(sport_id in self.slots and self.slots[sport_id].running)

    def state_dict(self, sport_id=None):
        selected = self.slots[sport_id] if sport_id in self.slots else self._selected()
        state = selected.state_dict()
        state["by_sport"] = {sid: slot.state_dict() for sid, slot in self.slots.items()}
        state["any_running"] = self.running
        if sport_id is None:
            state["running"] = self.running
        return state

    async def start_from_config(self, sport_id="badminton", settings=None):
        if sport_id not in self.slots:
            return {"ok": False, "msg": "不支持的运动"}
        data = deepcopy(self.engine.config_data)
        settings = settings or {}
        rush = data.setdefault("rush", {})
        for key in ("slot_start", "slot_end", "release_at", "court_priority"):
            if settings.get(key) is not None:
                rush[key] = settings[key]
        try:
            plan = RushPlan.from_config(data, target_date=settings.get("date") or self.engine.target_date,
                                        now=datetime.now(CHINA), sport_id=sport_id)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "msg": str(exc)}
        return await self.start(plan)

    async def start(self, plan):
        async with self._gate:
            sid = plan.sport_id
            if sid not in self.slots:
                return {"ok": False, "msg": "不支持的运动"}
            e = self.engine
            if not e.token:
                return {"ok": False, "msg": "未配置 Token"}
            if e.watcher.is_running(sid) or e._reserve_inflight:
                return {"ok": False, "msg": "请先停止该运动监控或等待手动预约结束"}
            for other_sid, slot in self.slots.items():
                if not slot.running or other_sid == sid or slot.plan.get("date") != plan.date:
                    continue
                clash = next((old for w in plan.windows for old in slot.plan.get("windows", [])
                              if w.start < old["end"] and old["start"] < w.end), None)
                if clash:
                    return {"ok": False, "msg": f"和正在运行的{LABELS.get(other_sid, other_sid)}抢场时段重叠"
                                                f"（{clash['start']}–{clash['end']}）；同一天两个运动的时段不能重叠"}
            result = await self.slots[sid].start(plan)
            if result.get("ok"):
                self._last_sport = sid
            return {**result, "sport_id": sid}

    async def stop(self, sport_id=None):
        slots = [self.slots[sport_id]] if sport_id in self.slots else list(self.slots.values())
        for slot in slots:
            await slot.stop()

    async def acquire_pool(self, run_id):
        async with self._pool_gate:
            result = await self.engine.start_vid_pool(rush_owner=run_id)
            if result.get("ok"):
                self._pool_owners.add(run_id)
            return result

    async def release_pool(self, run_id):
        async with self._pool_gate:
            if run_id not in self._pool_owners:
                return
            self._pool_owners.remove(run_id)
            if not self._pool_owners:
                await self.engine.stop_vid_pool()
