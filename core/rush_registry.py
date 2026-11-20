"""One Rush per sport, sharing account order guards and a bounded VID pool."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime

from core.rush_engine import RushEngine
from core.safety import dry_run_orders_enabled
from models.rush import CHINA, RushPlan

from core.sports import ALL_SPORTS, SPORT_LABELS as LABELS


class SportRushRegistry:
    def __init__(self, engine):
        self.engine = engine
        self.slots = {sid: RushEngine(engine) for sid in ALL_SPORTS}
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
        settings = settings or {}
        # The person confirmed one mode; never run in the other. A start without it is refused.
        confirmed = settings.get("confirmed_dry_run")
        if not isinstance(confirmed, bool):
            return {"ok": False, "msg": "启动请求没有带上你确认过的预约模式，请刷新页面后再点开始"}
        if confirmed != dry_run_orders_enabled():
            now = "测试模式（不会提交订单）" if dry_run_orders_enabled() else "真实预约（会提交订单）"
            return {"ok": False, "mode_changed": True,
                    "msg": f"工作台现在是{now}，和你确认时看到的不一样，没有启动。请刷新页面后重新确认"}
        data = deepcopy(self.engine.config_data)
        rush = data.setdefault("rush", {})
        for key in ("slot_start", "slot_end", "release_at", "court_priority", "prefer_first_scan"):
            if settings.get(key) is not None:
                rush[key] = settings[key]
        try:
            plan = RushPlan.from_config(data, target_date=settings.get("date") or self.engine.target_date,
                                        now=datetime.now(CHINA), sport_id=sport_id)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "msg": str(exc)}
        plan.request_id = str(settings.get("request_id") or "")[:64]
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
            # Badminton and tennis may run for the same day and hours: both may book.
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
