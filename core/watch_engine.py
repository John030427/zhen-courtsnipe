"""Watch monitoring engine — read-only scans, structured busy→free, Phase 4 notify-only auto."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

log = logging.getLogger("court")

from core.booking_service import BookingSource, BookingTarget
from core.order_journal import ORDER_JOURNAL_PATH, OrderJournal
from core.safety import dry_run_orders_enabled
from models.plans import CHINA, Target, WatchPlan, minutes, now
from utils import get_order_once

JOURNAL = ORDER_JOURNAL_PATH

ClockNow = Callable[[], datetime]
ClockMono = Callable[[], float]
Sleep = Callable[[float], Any]


def _default_sleep(seconds: float):
    return asyncio.sleep(seconds)


def block_is_free(block: dict) -> bool:
    try:
        start, end = minutes(block["time"]), minutes(block["endTime"])
    except (KeyError, ValueError, TypeError):
        return False
    return str(block.get("status")) == "1" and end - start == 30


def find_available_windows(blocks: list[dict]) -> list[tuple[str, str]]:
    """Merge adjacent 30-minute free blocks into maximal intervals."""
    starts: list[int] = []
    for block in blocks:
        if not block_is_free(block):
            continue
        starts.append(minutes(block["time"]))
    if not starts:
        return []
    starts.sort()
    windows: list[tuple[int, int]] = []
    seg_start = starts[0]
    seg_end = seg_start + 30
    for t in starts[1:]:
        if t == seg_end:
            seg_end += 30
        else:
            windows.append((seg_start, seg_end))
            seg_start, seg_end = t, t + 30
    windows.append((seg_start, seg_end))
    return [
        (f"{s // 60:02d}:{s % 60:02d}", f"{e // 60:02d}:{e % 60:02d}")
        for s, e in windows
    ]


def detect_busy_free_changes(
    previous_blocks: list[dict] | None, current_blocks: list[dict],
) -> list[dict[str, str]]:
    if not previous_blocks:
        return []

    def slot_status(blocks: list[dict]) -> dict[str, str]:
        out: dict[str, str] = {}
        for b in blocks:
            t = b.get("time")
            if not t:
                continue
            out[str(t)] = str(b.get("status", ""))
        return out

    prev = slot_status(previous_blocks)
    curr = slot_status(current_blocks)
    changes: list[dict[str, str]] = []
    for t, st in curr.items():
        if st != "1":
            continue
        old = prev.get(t)
        if old is not None and old != "1":
            changes.append({
                "time": t,
                "from_status": old,
                "to_status": "1",
                "kind": "busy_to_free",
            })
    return changes


def candidates(blocks: list[dict], target: Target) -> list[tuple[str, str]]:
    """Windows inside target range that satisfy min_duration (merged free blocks)."""
    t0, t1 = minutes(target.start), minutes(target.end)
    out: list[tuple[str, str]] = []
    for ws, we in find_available_windows(blocks):
        s, e = minutes(ws), minutes(we)
        s = max(s, t0)
        e = min(e, t1)
        if e - s < target.min_duration:
            continue
        # Maximal window inside target; also emit aligned sub-slots for longer windows.
        pos = s
        while pos + target.min_duration <= e:
            out.append(
                (
                    f"{pos // 60:02d}:{pos % 60:02d}",
                    f"{(pos + target.min_duration) // 60:02d}:{(pos + target.min_duration) % 60:02d}",
                )
            )
            pos += 30
    return out


def _target_exactness(item: dict, target: Target) -> int:
    if item["start"] == target.start and item["end"] == target.end:
        return 0
    return 1


def sort_matches(matches: list[dict], plan: WatchPlan) -> list[dict]:
    court_rank = {c: i for i, c in enumerate(plan.courts)}
    targets = plan.targets

    def key(item: dict):
        duration = minutes(item["end"]) - minutes(item["start"])
        best_exact = min(_target_exactness(item, t) for t in targets)
        earliest = minutes(item["start"])
        return (best_exact, -duration, earliest, court_rank.get(item["court"], 99))

    return sorted(matches, key=key)


class WatchEngine:
    # Login lost and not recovered: queries pause, recovery is retried this often, and the
    # watch carries on as soon as login works again (a refreshed booking page counts).
    AUTH_RETRY_SEC = 180
    AUTH_POLL_SEC = 5

    def __init__(
        self,
        engine,
        *,
        clock_now: ClockNow | None = None,
        clock_mono: ClockMono | None = None,
        sleep: Sleep | None = None,
    ):
        self.engine = engine
        self._now = clock_now or now
        self._mono = clock_mono or time.monotonic
        self._sleep = sleep or _default_sleep
        self.task: asyncio.Task | None = None
        self.control_lock = asyncio.Lock()
        self.state: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "scans": 0,
            "attempts": 0,
            "snapshots": {},
            "matches": [],
            "events": [],
            "changes": [],
        }
        self._notified_keys: set[str] = set()

    def _sport_id(self, plan: WatchPlan) -> str:
        from core.sports import normalize_sport_id

        return normalize_sport_id(plan.sport_id)

    def _court_by_no(self, plan: WatchPlan) -> dict[int, Any]:
        getter = getattr(self.engine, "courts_for_sport", None)
        courts = list(getter(self._sport_id(plan))) if getter else []
        if not courts:
            legacy = getattr(self.engine, "court_by_no", None)
            if legacy:
                return legacy
        return {c.no: c for c in courts}

    @staticmethod
    def _vacancy_dedupe_key(
        sport_id: str, date: str, court: int, start: str, end: str,
    ) -> str:
        return f"{sport_id}|{date}|{court}|{start}-{end}"

    async def _emit_vacancy_notify(
        self,
        plan: WatchPlan,
        *,
        date: str,
        court: int,
        start: str,
        end: str,
        kind: str,
        run_id: str | None,
        scan_id: str | None,
    ) -> None:
        sid = self._sport_id(plan)
        key = self._vacancy_dedupe_key(sid, date, court, start, end)
        if key in self._notified_keys:
            return
        self._notified_keys.add(key)
        payload = {
            "sport_id": sid,
            "date": date,
            "court": court,
            "start": start,
            "end": end,
            "detected_at": self._now().isoformat(),
            "kind": kind,
            "dedupe_key": key,
        }
        try:
            await self.engine.bus.publish(
                "watch.notify",
                payload,
                run_id=run_id,
                scan_id=scan_id,
                date=date,
                court=court,
            )
        except Exception as exc:
            log.warning("watch.notify publish failed (%s): %s", key, exc)

    async def publish(self, phase: str | None = None, message: str | None = None):
        if phase:
            self.state["phase"] = phase
        if message:
            self.state["message"] = message
            self.state["events"] = (self.state["events"] + [
                {"time": self._now().isoformat(), "message": message},
            ])[-50:]
        await self.engine.bus.publish("watch.state", self.state.copy())

    async def _recover_login(self) -> bool:
        e = self.engine
        mode = str((e.config_data.get("connection") or {}).get("mode") or "")
        if mode == "page":
            check = e.auth_keeper.check if hasattr(e, 'auth_keeper') else e.tokens.validate_current
            ok = await check()
            e._sync_from_token_manager()
            return bool(ok)
        return bool((await e.fetch_token()).get("ok"))

    def _login_ready(self) -> bool:
        """Login works again without a request from here, e.g. a new capture was verified."""
        from models.auth import TokenState

        tokens = getattr(self.engine, "tokens", None)
        return (getattr(tokens, "state", None) == TokenState.VALID
                and not tokens.is_degraded_for_orders())

    async def _pause_for_login(self, deadline: float) -> bool:
        """No queries and no orders until login works; False if the watch ran out of time."""
        self.state["auth_paused_since"] = self._now().isoformat()
        await self.publish(
            "auth_paused",
            "登录失效，已暂停查询；每 3 分钟自动尝试恢复，也可以在企业微信里刷新预约页",
        )
        retry_at = self._mono() + self.AUTH_RETRY_SEC
        try:
            while self._mono() < deadline:
                if self._login_ready():
                    break
                if self._mono() >= retry_at:
                    retry_at = self._mono() + self.AUTH_RETRY_SEC
                    if await self._recover_login():
                        break
                    await self.publish(message="登录仍未恢复，3 分钟后再试")
                self.state["auth_retry_in_sec"] = max(0, round(retry_at - self._mono()))
                await self._sleep(max(0, min(self.AUTH_POLL_SEC, deadline - self._mono())))
            else:
                return False
        finally:
            self.state["auth_paused_since"] = None
            self.state["auth_retry_in_sec"] = None
        await self.publish("watching", "登录已恢复，继续监控")
        return True

    async def start(self, plan: WatchPlan) -> dict:
        async with self.control_lock:
            return await self._start(plan)

    async def _start(self, plan: WatchPlan) -> dict:
        from core.sports import SPORT_BADMINTON

        e = self.engine
        sid = self._sport_id(plan)
        court_by_no = self._court_by_no(plan)
        if self.state["running"]:
            return {"ok": False, "msg": "该运动监控已在运行", "sport_id": sid}
        if hasattr(e.auto, "is_running"):
            if e.auto.is_running(sid) or e._reserve_inflight or (e.vid_running and not e.auto.running):
                return {"ok": False, "msg": "请先停止该运动抢场、手动预约或独立 VID 池"}
        elif sid == SPORT_BADMINTON:
            if e.auto.running or e.vid_running or e._reserve_inflight:
                return {"ok": False, "msg": "请先停止当前监控、预约或 VID 池"}
        elif e._reserve_inflight:
            return {"ok": False, "msg": "有预约请求进行中，请稍后再开监控"}
        if not e.token or not e.user_id or e.user_id.startswith("YOUR_"):
            return {"ok": False, "msg": "请先配置账号并获取 Token"}
        if any(c not in court_by_no for c in plan.courts):
            return {"ok": False, "msg": "包含未配置的场地"}
        journal = OrderJournal()
        if plan.auto_reserve and journal.has_blocking_on_dates(e.user_id, plan.dates):
            return {
                "ok": False,
                "msg": "目标日期已有成功或待核实订单，请先核对官方订单和 watch-orders.json",
            }
        run_id = e.booking.new_run_id("watch") if plan.auto_reserve else None
        self._notified_keys = set()
        self.state = {
            "running": True,
            "phase": "waiting",
            "scans": 0,
            "attempts": 0,
            "snapshots": {},
            "matches": [],
            "events": [],
            "changes": [],
            "plan": plan.model_dump(),
            "sport_id": sid,
            "last_scan": None,
            "run_id": run_id,
            "started_at": None,
            "ends_at": None,
            "next_scan_at": None,
            "found_rounds": 0,
            "orders_sent": 0,
            "auth_paused_since": None,
            "auth_retry_in_sec": None,
        }
        await e.stop_refresh()
        await e.bus.publish(
            "watch.started",
            {
                "sport_id": sid,
                "dates": plan.dates,
                "courts": plan.courts,
                "auto_reserve": plan.auto_reserve,
            },
            run_id=run_id,
        )
        self.task = asyncio.create_task(self.run(plan, run_id=run_id))
        return {"ok": True}

    async def stop(self):
        async with self.control_lock:
            await self._stop()

    async def _stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.state["running"] = False
        await self.publish("stopped", "监控已停止")
        await self.engine.bus.publish(
            "watch.stopped",
            {
                "sport_id": self.state.get("sport_id"),
                "phase": self.state.get("phase", "stopped"),
            },
            run_id=self.state.get("run_id"),
        )

    async def run(self, plan: WatchPlan, run_id: str | None = None):
        e = self.engine
        court_by_no = self._court_by_no(plan)
        failures = 0
        try:
            if plan.start_at:
                await self.publish("waiting", "等待定时开始")
                while (
                    delay := (
                        datetime.fromisoformat(plan.start_at) - self._now()
                    ).total_seconds()
                ) > 0:
                    await self._sleep(min(delay, 1))

            started = self._now()
            target_end = max(datetime.fromisoformat(f"{date}T{target.end}").replace(tzinfo=CHINA)
                             for date in plan.dates for target in plan.targets)
            # A finite run (up to 24 h) still stops once the last target window is over.
            ends_at = target_end if plan.continuous else min(
                target_end, started + timedelta(minutes=plan.duration_minutes))
            deadline = self._mono() + max(0, (ends_at - started).total_seconds())
            self.state['started_at'] = started.isoformat()
            self.state['ends_at'] = ends_at.isoformat()
            while self._mono() < deadline:
                scan_id = f"watch-{self.state['scans'] + 1}"
                await self.publish("scanning")
                await e.bus.publish(
                    "watch.scan",
                    {"scan_id": scan_id, "scans": self.state["scans"] + 1},
                    run_id=run_id,
                    scan_id=scan_id,
                )
                matches: list[dict] = []
                round_failed = False
                auth_pause = False
                login_lost = False

                for date in plan.dates:
                    if datetime.fromisoformat(
                        f"{date}T{max(t.end for t in plan.targets)}").replace(tzinfo=CHINA) <= self._now():
                        continue
                    for number in plan.courts:
                        if self._mono() >= deadline:
                            break
                        court = court_by_no[number]
                        token, user_id = e.token, e.user_id
                        result = await get_order_once(
                            court, date, token, user_id,
                        )
                        key = f"{date}/{number}"
                        previous = self.state["snapshots"].get(key)
                        snapshot = {
                            "date": date,
                            "court": number,
                            "ok": result.get("ok", False),
                            "time": self._now().isoformat(),
                            "blocks": [
                                {k: b.get(k) for k in ("time", "endTime", "status")}
                                for b in result.get("blocks", [])
                            ],
                            "error": result.get("error", ""),
                        }
                        self.state["snapshots"][key] = snapshot

                        if not result.get("ok"):
                            round_failed = True
                            if result.get("auth_fail"):
                                if hasattr(e, 'tokens') and hasattr(e.tokens, 'mark_expired'):
                                    if token != e.token or user_id != e.user_id:
                                        continue
                                    e.tokens.mark_expired('监控查询鉴权失败')
                                await self.publish(
                                    "refreshing_token", "鉴权失败，暂停扫描并恢复登录",
                                )
                                auth_pause = True
                                if await self._recover_login():
                                    await self.publish(
                                        "watching", "登录已恢复，将继续本轮监控扫描",
                                    )
                                else:
                                    login_lost = True
                            break

                        if previous and previous.get("ok"):
                            changes = detect_busy_free_changes(
                                previous.get("blocks", []), snapshot["blocks"],
                            )
                            for ch in changes:
                                ch_full = {
                                    **ch,
                                    "date": date,
                                    "court": number,
                                }
                                self.state["changes"] = (
                                    self.state.get("changes", []) + [ch_full]
                                )[-100:]
                                await e.bus.publish(
                                    "watch.change",
                                    {**ch_full, "sport_id": self._sport_id(plan)},
                                    run_id=run_id,
                                    scan_id=scan_id,
                                    date=date,
                                    court=number,
                                )
                                t0 = minutes(ch["time"])
                                await self._emit_vacancy_notify(
                                    plan,
                                    date=date,
                                    court=number,
                                    start=ch["time"],
                                    end=f"{(t0 + 30) // 60:02d}:{(t0 + 30) % 60:02d}",
                                    kind="busy_to_free",
                                    run_id=run_id,
                                    scan_id=scan_id,
                                )
                                await self.publish(
                                    message=(
                                        f"{date} {number}号场 {ch['time']} "
                                        f"由占用变为空闲"
                                    ),
                                )
                            if changes and previous["blocks"] != snapshot["blocks"]:
                                await self.publish(
                                    message=f"{date} {number}号场状态变化",
                                )

                        for target in plan.targets:
                            for start, end in candidates(snapshot["blocks"], target):
                                slot_time = datetime.fromisoformat(
                                    f"{date}T{start}",
                                ).replace(tzinfo=CHINA)
                                if slot_time > self._now():
                                    item = {
                                        "date": date,
                                        "court": number,
                                        "start": start,
                                        "end": end,
                                    }
                                    if item not in matches:
                                        matches.append(item)

                    if auth_pause or round_failed:
                        break

                matches = sort_matches(matches, plan)
                self.state["matches"] = matches
                self.state["scans"] += 1
                self.state["found_rounds"] = self.state.get("found_rounds", 0) + bool(matches)
                self.state["last_scan"] = self._now().isoformat()
                await self.publish(
                    message=f"扫描完成，发现 {len(matches)} 个目标时段",
                )
                if matches:
                    await e.bus.publish(
                        "watch.match",
                        {
                            "sport_id": self._sport_id(plan),
                            "count": len(matches),
                            "top": matches[0],
                        },
                        run_id=run_id,
                        scan_id=scan_id,
                        date=matches[0].get("date"),
                        court=matches[0].get("court"),
                    )

                if matches and not round_failed and not plan.auto_reserve:
                    for item in matches:
                        await self._emit_vacancy_notify(
                            plan,
                            date=item["date"],
                            court=item["court"],
                            start=item["start"],
                            end=item["end"],
                            kind="new_match",
                            run_id=run_id,
                            scan_id=scan_id,
                        )

                if matches and plan.auto_reserve and not round_failed:
                    self.state["attempts"] += 1
                    top = matches[0]
                    court = court_by_no[top["court"]]
                    if dry_run_orders_enabled():
                        await self.publish(
                            "notify",
                            f"发现可预约时段 {top['date']} {top['court']}号 "
                            f"{top['start']}-{top['end']}（DRY RUN：仅通知，未提交订单）",
                        )
                        if plan.stop_after_success:
                            await self.publish("completed", "已通知匹配时段，监控结束")
                            return
                    else:
                        target = BookingTarget(
                            court=court,
                            date=top["date"],
                            start=top["start"],
                            end=top["end"],
                        )
                        outcome = await e.booking.place_order(
                            target,
                            source=BookingSource.WATCH,
                            run_id=run_id,
                            deadline_mono=deadline,
                        )
                        await e.booking.publish_result(target, outcome)
                        if outcome.uncertain:
                            await self.publish(
                                "uncertain",
                                "下单结果不确定，请核对官方订单；监控已停止",
                            )
                            return
                        if outcome.success:
                            await self.publish("success", "预约成功，监控结束")
                            return
                        if outcome.dry_run:
                            await self.publish(
                                "notify",
                                f"{outcome.msg}（未真实提交）",
                            )
                        if plan.stop_after_success and (outcome.success or outcome.dry_run):
                            await self.publish("completed", "监控结束")
                            return
                        # max_attempts counts orders that reached the school; a slot taken
                        # before the recheck sends nothing and the watch keeps looking.
                        if (outcome.diagnostics or {}).get("sent") is True:
                            self.state["orders_sent"] = self.state.get("orders_sent", 0) + 1
                            if self.state["orders_sent"] >= plan.max_attempts:
                                await self.publish(
                                    "exhausted",
                                    f"已下单 {self.state['orders_sent']} 次都没成功，监控结束",
                                )
                                return

                if login_lost:
                    failures = 0
                    if not await self._pause_for_login(deadline):
                        break
                    continue

                failures = min(failures + 1, 5) if round_failed else 0
                delay = plan.poll_interval if not round_failed else min(
                    300, plan.poll_interval * (2 ** failures),
                )
                notified = (
                    bool(matches)
                    and plan.auto_reserve
                    and not round_failed
                )
                if round_failed:
                    await self.publish("backoff")
                elif not notified:
                    await self.publish("watching")
                remaining = max(0, deadline - self._mono())
                self.state["next_scan_at"] = (self._now() + timedelta(seconds=min(delay, remaining))).isoformat()
                await self._sleep(min(delay, remaining))

            self.state["next_scan_at"] = None
            await self.publish("completed", "目标时段已结束" if ends_at == target_end else "已到监控结束时间")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.publish("error", "监控异常，已停止；请检查配置和服务日志")
        finally:
            self.state["running"] = False
            await self.publish()
