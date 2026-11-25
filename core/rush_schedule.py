"""Timed start of Rushes: set up earlier in the day, started a few minutes before release.

A Rush started hours ahead keeps its login alive the whole time, and a login that lapses meanwhile
is refreshed again and again. A timed start instead waits quietly; at the chosen time (19:55 by
default) the workbench checks the capture proxy (and restarts it when its port is down), checks the
login, then starts each planned sport's Rush exactly as the start button would. Pending starts are
saved, so a restart of the workbench in between keeps them.

The booking page has to be open in WeCom: a lapsed login is renewed by refreshing it. When a start
is set while it is not open, the reply says so; 10 minutes before the start it is checked once more,
and a Windows message box asks for it if it is still not open. Nothing is opened or clicked for you.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from app_paths import DATA_DIR
from core.sports import SPORT_LABELS, is_sport
from models.rush import CHINA

log = logging.getLogger("court")

PATH = DATA_DIR / "rush-schedule.json"
LOGIN_CHECK_TIMEOUT_SEC = 30
KEEP_DONE_HOURS = 20  # a finished timed start stays listed until the next day
PAGE_CHECK_MINUTES_BEFORE = 10
PAGE_MISSING = "企业微信里的场地预约页没有打开。登录失效时要靠刷新它拿新登录，请现在打开它，开着别关。"


def _page_open() -> bool | None:
    from utils import reservation_page_open
    return reservation_page_open()


def _alert(text: str) -> bool:
    from utils import show_reminder
    return show_reminder(text)


def _parse_hm(text: str) -> tuple[int, int] | None:
    try:
        h, m = (int(x) for x in str(text).strip().split(":")[:2])
    except (TypeError, ValueError):
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


class RushSchedule:
    def __init__(self, engine, *, path: Path = PATH, now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], Any] | None = None, page_open: Callable[[], bool | None] = _page_open,
                 alert: Callable[[str], bool] = _alert):
        self.engine = engine
        self._page_open = page_open
        self._alert = alert
        self._path = path
        self._now = now or (lambda: datetime.now(CHINA))
        self._sleep = sleep or asyncio.sleep
        self.items: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ── storage ──────────────────────────────────────────
    def _save(self) -> None:
        try:
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(list(self.items.values()), ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self._path)
        except OSError as exc:
            log.warning("rush schedule not saved: %s", type(exc).__name__)

    def load(self) -> None:
        """Restore saved starts; re-arm the pending ones that are still ahead."""
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = self._now()
        for item in saved if isinstance(saved, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                at = datetime.fromisoformat(item["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if item.get("status") == "pending" and at <= now:
                item["status"] = "missed"
                item["steps"] = [{"ok": False, "text": "工作台在设定时间没有运行，这次没有自动开始"}]
            if item.get("status") != "pending" and now - at > timedelta(hours=KEEP_DONE_HOURS):
                continue
            self.items[item["id"]] = item
            if item["status"] == "pending":
                self._arm(item)
        self._save()

    # ── public ───────────────────────────────────────────
    def add(self, at_hm: str, plans: list[dict]) -> dict:
        """Plan one timed start for today. Returns {"ok", "msg", "item"}."""
        hm = _parse_hm(at_hm)
        if hm is None:
            return {"ok": False, "msg": "时间格式不对，例如 19:55"}
        now = self._now()
        at = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if at <= now:
            return {"ok": False, "msg": f"{at_hm} 已经过了，请选一个今天还没到的时间"}
        clean = []
        for plan in plans or []:
            sid = str(plan.get("sport_id") or "")
            if not is_sport(sid):
                return {"ok": False, "msg": "不支持的运动"}
            if not isinstance(plan.get("confirmed_dry_run"), bool):
                return {"ok": False, "msg": "没有带上你确认过的预约模式，请刷新页面后再设"}
            release = _parse_hm(plan.get("release_at") or "20:00:00")
            if release and (release[0], release[1]) <= hm:
                return {"ok": False, "msg": f"定时开始要早于放号时间（{plan.get('release_at')}）"}
            if any(sid == p["sport_id"] for p in clean):
                return {"ok": False, "msg": f"{SPORT_LABELS.get(sid, sid)}重复了"}
            clean.append({k: plan[k] for k in ("sport_id", "date", "slot_start", "slot_end", "release_at",
                                               "court_priority", "confirmed_dry_run", "request_id") if k in plan})
        if not clean:
            return {"ok": False, "msg": "没有要开始的运动"}
        taken = {p["sport_id"] for item in self.items.values() if item["status"] == "pending" for p in item["plans"]}
        clash = [p["sport_id"] for p in clean if p["sport_id"] in taken]
        if clash:
            return {"ok": False, "msg": f"{SPORT_LABELS.get(clash[0], clash[0])}已经设了定时开始，先取消原来的"}
        item = {"id": uuid.uuid4().hex[:12], "at": at.isoformat(), "plans": clean, "status": "pending",
                "created_at": now.isoformat(), "steps": []}
        self.items[item["id"]] = item
        self._save()
        self._arm(item)
        out = {"ok": True, "item": item}
        if self._page_open() is False:
            out["warning"] = PAGE_MISSING
        return out

    def cancel(self, item_id: str) -> bool:
        item = self.items.get(item_id)
        if not item or item["status"] != "pending":
            return False
        task = self._tasks.pop(item_id, None)
        if task:
            task.cancel()
        item["status"] = "cancelled"
        self._save()
        return True

    def state(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda i: i["at"])

    def pending_sports(self) -> set[str]:
        return {p["sport_id"] for i in self.items.values() if i["status"] == "pending" for p in i["plans"]}

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ── running ──────────────────────────────────────────
    def _arm(self, item: dict) -> None:
        self._tasks[item["id"]] = asyncio.create_task(self._wait_and_fire(item), name=f"rush-schedule-{item['id']}")

    async def _wait_and_fire(self, item: dict) -> None:
        at = datetime.fromisoformat(item["at"])
        # one look at the booking page; none when the start was set closer than that
        check = at - timedelta(minutes=PAGE_CHECK_MINUTES_BEFORE)
        check = check if check > self._now() else None
        while True:
            now = self._now()
            if check is not None and now >= check:
                check = None
                if self._page_open() is False:
                    self._alert(PAGE_MISSING)
            left = (at - now).total_seconds()
            if left <= 0:
                break
            nap = min(left, 30) if check is None else min(left, 30, (check - now).total_seconds())
            await self._sleep(nap)  # short naps, so a clock change is followed
        await self.fire(item)

    def _step(self, item: dict, ok: bool | None, text: str) -> None:
        item["steps"].append({"at": self._now().strftime("%H:%M:%S"), "ok": ok, "text": text})
        log.info("[定时开始] %s", text)
        self._save()

    async def fire(self, item: dict) -> None:
        e = self.engine
        item["status"] = "preparing"
        self._tasks.pop(item["id"], None)
        try:
            await self._check_capture(item)
            await self._check_login(item)
            started = 0
            for plan in item["plans"]:
                label = SPORT_LABELS.get(plan["sport_id"], plan["sport_id"])
                result = await e.rush.start_from_config(sport_id=plan["sport_id"], settings=dict(plan))
                if result.get("ok"):
                    started += 1
                    self._step(item, True, f"{label}抢场已开始")
                else:
                    self._step(item, False, f"{label}没有开始：{result.get('msg') or '原因未知'}")
            item["status"] = "started" if started == len(item["plans"]) else "partly" if started else "failed"
        except Exception as exc:  # never leave a timed start silently half done
            log.exception("timed rush start failed")
            self._step(item, False, f"定时开始出错：{type(exc).__name__}")
            item["status"] = "failed"
        self._save()

    async def _check_capture(self, item: dict) -> None:
        e = self.engine
        enabled = getattr(e, "mitm_capture_enabled", None)
        if not callable(enabled) or not enabled():
            self._step(item, None, "没有开启抓取代理，跳过检查")
            return
        state = e.mitm_state()
        if state.get("port_listening") and state.get("proc_alive"):
            self._step(item, True, f"抓取代理正常（端口 {state.get('port')}）")
            return
        if state.get("port_conflict"):
            self._step(item, False, f"端口 {state.get('port')} 被别的程序占着，没有重启它；抢场照常开始")
            return
        ok = await e.restart_mitm()
        self._step(item, ok, f"抓取代理端口 {state.get('port')} 没在监听，已重启" + ("" if ok else "，但没有起来"))

    async def _check_login(self, item: dict) -> None:
        e = self.engine
        keeper = getattr(e, "auth_keeper", None)
        try:
            check = keeper.check if keeper is not None else e.tokens.validate_current
            ok = await asyncio.wait_for(check(), timeout=LOGIN_CHECK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            self._step(item, False, "登录检查超时（抢场开始后，放号前 2 分钟还会再检查一次）")
            return
        self._step(item, bool(ok), "登录有效" if ok else
                   "登录没通过检查（抢场开始后，放号前 2 分钟还会再检查；请留意状态线「学校验证」）")
