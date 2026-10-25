"""Release rush mode — bounded VID warmup, scan window, BookingService orders."""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable

from core.booking_service import BookingSource, BookingTarget
from core.audit import record
from core.auth_context import validation_sport
from core.request_priority import request_priority
from core.rush_diagnostics import describe_window
from core.token_ttl_obs import try_record_first_auth_fail
from core.safety import dry_run_orders_enabled
from core.watch_engine import candidates
from models.auth import TokenState
from models.plans import Target
from models.rush import CHINA, RushPlan
from utils import get_order_once, reset_server_clock, server_clock_offset, slot_covers

log = logging.getLogger("court")

# Scan query timeout bounds (seconds); 4 s is get_order_once's default.
SCAN_TIMEOUT_MAX_SEC = 4.0
SCAN_TIMEOUT_MIN_SEC = 2.0

ClockNow = Callable[[], datetime]

# Stable stop reasons so the UI can map an ending to one action without parsing msg.
STOP_REASONS = (
    "auth_expired",        # school rejected the current login
    "auth_check_timeout",  # pre-release revalidation did not answer in time
    "order_conflict",      # local order journal blocks every target window
    "vid_pool_failed",     # bounded VID warmup could not start
    "result_uncertain",    # an order was sent but the result is unknown
    "order_blocked",       # a safety gate refused to send the order
    "scan_window_ended",   # scan window closed without a booking
    "stopped",             # user stop or task cancellation
    "internal_error",      # unexpected exception inside the Rush task
)
Sleep = Callable[[float], Any]


def _default_sleep(sec: float):
    return asyncio.sleep(sec)


def _pre_release_auth_failed(tokens) -> bool:
    """Hard-stop Rush pre-release refresh only on school auth failure."""
    view = tokens.public_view() if hasattr(tokens, "public_view") else {}
    validation = view.get("validation") or {}
    if validation.get("auth_fail"):
        return True
    state = getattr(tokens, "state", None)
    return state == TokenState.EXPIRED


class RushEngine:
    """Duck-types legacy AutoState fields for /api/auto/* compatibility."""

    def __init__(self, engine, *, clock_now: ClockNow | None = None, sleep: Sleep | None = None):
        self.engine = engine
        self._now = clock_now or (lambda: datetime.now(CHINA))
        self._sleep = sleep or _default_sleep
        self.task: asyncio.Task | None = None
        self.running: bool = False
        self.phase: str = "idle"
        self.msg: str = ""
        self.reason: str | None = None
        self.applied: list[dict] = []
        self.started_at: str = ""
        self.finished_at: str = ""
        self.plan: dict[str, Any] = {}
        self.run_id: str | None = None
        self.preflight: dict = {}
        self._reset_diagnostics()

    def _reset_diagnostics(self):
        self.diagnostics = {"queries": 0, "query_errors": 0, "matches": 0,
                            "rounds": 0, "last_queries": [], "recheck_queries": 0,
                            "recheck_errors": 0, "vid_errors": 0, "order_sends": 0,
                            "not_sent": 0}
        self._scan_request_ms = deque(maxlen=8)
        reset_server_clock()

    def _clock_diagnostics(self) -> dict:
        """School clock minus this computer's, measured from this run's replies (not corrected)."""
        measured = server_clock_offset()
        if measured is None:
            return {}
        offset, uncertainty = measured
        return {"clock_offset_ms": round(offset * 1000), "clock_uncertainty_ms": round(uncertainty * 1000)}

    def _scan_timeout(self) -> float:
        """Per-court query timeout for this run: about three times the typical answer time seen so
        far, kept between 2 and 4 s, so one stuck court does not hold up the rest of the round
        (10-20: courts 2 and 3 each sat out the full 4 s). It rises again when the school slows."""
        if len(self._scan_request_ms) < 3:
            return SCAN_TIMEOUT_MAX_SEC
        typical = statistics.median(self._scan_request_ms) / 1000
        return min(SCAN_TIMEOUT_MAX_SEC, max(SCAN_TIMEOUT_MIN_SEC, 3 * typical))

    def _record_scan(self, plan, court, result, *, round_no, elapsed_ms, token, userid, scan_timeout_ms=None):
        windows = [describe_window(w.start, w.end, result.get('avail') or {},
                                   result.get('blocks') or [], plan.min_duration)
                   for w in plan.windows] if result.get('ok') else []
        matched = any(w['reason'] == 'match' for w in windows)
        row = {"court": court.no, "round": round_no, "ok": bool(result.get('ok')),
               "elapsed_ms": elapsed_ms, "target_windows": windows,
               "error_kind": result.get('error_kind') or ("none" if result.get('ok') else "query"),
               "scan_timeout_ms": scan_timeout_ms}
        if isinstance(result.get('timing'), dict):
            row['timing'] = {key: result['timing'].get(key) for key in
                             ('queue_ms', 'request_ms', 'processing_ms', 'total_ms')}
        d = self.diagnostics
        d['queries'] += 1
        d['query_errors'] += not bool(result.get('ok'))
        d['matches'] += matched
        d['rounds'] = round_no
        d['last_queries'] = [r for r in d['last_queries'] if r['court'] != court.no][-9:] + [row]
        record('rush.query_result', token=token, userid=userid, run_id=self.run_id,
               sport_id=plan.sport_id, date=plan.date, groundId=court.id,
               school_msg=result.get('school_msg'), http_status=result.get('http_status'),
               code=str(result.get('code', '')), auth_fail=bool(result.get('auth_fail')), **row)
        return windows

    def _set_phase(self, phase: str, message: str | None = None, reason: str | None = None):
        if reason is not None and reason not in STOP_REASONS:
            raise ValueError(f"unknown Rush stop reason: {reason}")
        if phase != self.phase or reason != self.reason or (message is not None and message != self.msg):
            record('rush.phase', token=self.engine.token, userid=self.engine.user_id,
                   previous_phase=self.phase, phase=phase, message=message, reason=reason,
                   run_id=self.run_id, date=self.plan.get('date'), sport_id=self.plan.get('sport_id'))
        self.phase = phase
        self.reason = reason
        if message is not None:
            self.msg = message

    def _sync_hint(self) -> str:
        # Auto capture picks up a plain page refresh; a pasted page script is lost on refresh, so it needs a venue switch.
        capture = getattr(self.engine, "mitm_capture_enabled", None)
        return "去企业微信刷新预约页同步登录" if callable(capture) and capture() else "去官方页切换场地同步 Token"

    def outcome(self) -> str | None:
        """booked / uncertain / simulated / none once a run ends; None while idle or running."""
        if self.running or self.phase == "idle" or not self.run_id:
            return None
        if any(a.get("ok") for a in self.applied):
            return "booked"
        if any(a.get("uncertain") for a in self.applied):
            return "uncertain"
        if any(a.get("dry_run") for a in self.applied):
            return "simulated"
        return "none"

    async def _publish(
        self,
        topic: str,
        data: dict,
        *,
        date: str | None = None,
        court: int | None = None,
    ):
        await self.engine.bus.publish(
            topic,
            {**data, "sport_id": self.plan.get("sport_id", "badminton")},
            run_id=self.run_id,
            date=date,
            court=court,
        )

    async def start_from_config(self, sport_id: str = "badminton") -> dict:
        if self.running:
            return {"ok": False, "msg": "Rush 已在运行"}
        e = self.engine
        from core.sports import normalize_sport_id, public_sports_capabilities

        sid = normalize_sport_id(sport_id)
        caps = public_sports_capabilities().get(sid) or {}
        if not caps.get("rush"):
            return {
                "ok": False,
                "msg": caps.get("stub_message") or f"{sid} 不支持 Rush",
            }

        if e.watcher.is_running(sid):
            label = caps.get("label") or sid
            return {"ok": False, "msg": f"请先停止{label}监控"}
        if not e.token:
            return {"ok": False, "msg": "未配置 token"}
        try:
            plan = RushPlan.from_config(
                e.config_data,
                target_date=e.target_date,
                now=self._now(),
                sport_id=sid,
            )
        except ValueError as ex:
            return {"ok": False, "msg": str(ex)}
        except Exception as ex:
            # Surface pydantic/config errors clearly (e.g. ValidationError).
            return {"ok": False, "msg": f"{type(ex).__name__}: {ex}"}
        return await self.start(plan)

    def _order_preflight(self, plan, stage):
        check = self.engine.booking.preflight_windows(plan.date, plan.windows)
        self.preflight = {**check, 'stage': stage, 'checked_at': self._now().isoformat()}
        record('rush.preflight', token=self.engine.token, userid=self.engine.user_id,
               run_id=self.run_id if stage != 'start' else None, sport_id=plan.sport_id,
               date=plan.date, action=stage, ok=check['ok'], message=check['message'])
        return check

    async def start(self, plan: RushPlan) -> dict:
        e = self.engine
        if self.running:
            return {"ok": False, "msg": "Rush 已在运行"}
        try:
            plan.validate_not_past(self._now())
        except ValueError as ex:
            return {"ok": False, "msg": str(ex)}
        check = self._order_preflight(plan, 'start')
        if not check['ok']:
            return {"ok": False, "msg": check['message'], "preflight": self.preflight}
        self.running = True
        self.applied = []
        self._reset_diagnostics()
        self.started_at = self._now().strftime("%H:%M:%S")
        self.finished_at = ""
        self.run_id = e.booking.new_run_id("rush")
        self.plan = {
            "date": plan.date,
            "sport_id": plan.sport_id,
            "release_at": plan.release_at.isoformat(),
            "windows": [w.model_dump() for w in plan.windows],
            "court_priority": plan.court_priority,
            "excluded_courts": plan.excluded_courts,
            "repeat_daily": plan.repeat_daily,
            "scan_window_sec": plan.scan_window_sec,
            "revalidate_at": (plan.release_at - timedelta(seconds=plan.token_refresh_lead_sec)).isoformat(),
        }
        refresh_at = plan.release_at - timedelta(seconds=plan.token_refresh_lead_sec)
        mode = str((e.config_data.get("connection") or {}).get("mode") or "")
        wait_label = "Token 复验" if mode == "page" else "Token 刷新"
        # Human-readable next milestone (avoid blank "已计划").
        now = self._now()
        if now < refresh_at:
            self._set_phase(
                "waiting",
                f"等待{wait_label}（{refresh_at:%Y-%m-%d %H:%M:%S}），放号 {plan.release_at:%H:%M:%S}",
            )
        elif now < plan.release_at:
            self._set_phase(
                "waiting",
                f"等待放号（{plan.release_at:%Y-%m-%d %H:%M:%S}）",
            )
        else:
            self._set_phase("waiting", f"放号已到（{plan.release_at:%H:%M:%S}），开始扫场…")
        # Schedule runner BEFORE SSE publish so a slow subscriber cannot block the rush.
        self.task = asyncio.create_task(self._run(plan), name=f"rush-{self.run_id}")
        self.task.add_done_callback(self._on_task_done)
        await asyncio.sleep(0)  # let _run set the first staged waiting message
        try:
            await self._publish("rush.started", {"run_id": self.run_id, "plan": self.plan})
        except Exception:
            log.exception("rush.started publish failed")
        return {"ok": True}

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self._shutdown_pool()
        self.running = False
        self._set_phase("stopped", "已手动停止", reason="stopped")
        self.finished_at = self._now().strftime("%H:%M:%S")

    def _on_task_done(self, task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Rush task done callback failed")
            return
        if exc is not None:
            log.error("Rush task crashed: %s", exc, exc_info=exc)
            if self.running:
                self.running = False
                self._set_phase("degraded", f"Rush 任务崩溃: {type(exc).__name__}: {exc}",
                                reason="internal_error")
                self.finished_at = self._now().strftime("%H:%M:%S")

    async def _shutdown_pool(self):
        registry = getattr(self.engine, 'rush', None)
        if hasattr(registry, 'release_pool'):
            await registry.release_pool(self.run_id)
        elif self.engine.vid_running:
            await self.engine.stop_vid_pool()

    def state_dict(self) -> dict:
        mode = str((self.engine.config_data.get('connection') or {}).get('mode') or '')
        def remaining(key):
            value = self.plan.get(key)
            return max(0, int((datetime.fromisoformat(value) - self._now()).total_seconds())) if value else None
        return {
            "running": self.running,
            "phase": self.phase,
            "msg": self.msg,
            "reason": self.reason,
            "outcome": self.outcome(),
            "applied": list(self.applied),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "plan": self.plan,
            "run_id": self.run_id,
            "diagnostics": {**self.diagnostics, **self._clock_diagnostics()},
            "preflight": self.preflight,
            "connection_mode": mode,
            "revalidate_in_sec": remaining('revalidate_at'),
            "release_in_sec": remaining('release_at'),
            "next_action": self._sync_hint() if mode == 'page' else '',
        }

    async def _wait_until(self, target: datetime, label: str | None = None):
        while True:
            remain = (target - self._now()).total_seconds()
            if remain <= 0:
                return
            if label:
                self._set_phase(
                    "waiting",
                    f"{label}（{target:%Y-%m-%d %H:%M:%S}）",
                )
            await self._sleep(min(remain, 0.5))

    def _court_order(self, plan: RushPlan) -> list:
        e = self.engine
        pool = [c for c in e.courts_for_sport(plan.sport_id) if c.no not in plan.excluded_courts]
        if plan.court_priority:
            rank = {n: i for i, n in enumerate(plan.court_priority)}
            pool.sort(key=lambda c: (rank.get(c.no, 999), c.no))
        return pool

    def _pick_window(self, plan: RushPlan, avail: dict):
        for w in plan.windows:
            if slot_covers(avail, w.start, w.end):
                return w
        return None

    async def _run(self, plan: RushPlan):
        priority = request_priority.set(1 if plan.sport_id == 'badminton' else 2)
        e = self.engine
        log.info("Rush _run enter run_id=%s release=%s", self.run_id, plan.release_at.isoformat())
        scan_deadline = plan.release_at + timedelta(seconds=plan.scan_window_sec)
        success = False
        degraded = False

        try:
            refresh_at = plan.release_at - timedelta(seconds=plan.token_refresh_lead_sec)
            warmup_at = plan.release_at - timedelta(seconds=plan.vid_warmup_sec)

            if self._now() < refresh_at:
                mode = str((e.config_data.get("connection") or {}).get("mode") or "")
                wait_label = "Token 复验窗口" if mode == "page" else "Token 刷新窗口"
                await self._wait_until(refresh_at, label=f"等待{wait_label}")

            self._set_phase("token_refresh", "刷新并验证 Token…")
            mode = str((e.config_data.get("connection") or {}).get("mode") or "")
            context = validation_sport.set(plan.sport_id)
            try:
                if mode == "page" or hasattr(e, 'auth_keeper'):
                    self._set_phase("token_refresh", "复验当前 Token，必要时恢复登录…")
                    try:
                        check = e.auth_keeper.check if hasattr(e, 'auth_keeper') else e.tokens.validate_current
                        ok = await asyncio.wait_for(check(), timeout=45)
                    except asyncio.TimeoutError:
                        self._set_phase(
                            "degraded",
                            f"复验网络超时；订单已停止。{self._sync_hint()}，验证后重新启动 Rush",
                            reason="auth_check_timeout",
                        )
                        degraded = True
                        return
                    e._sync_from_token_manager()
                    if _pre_release_auth_failed(e.tokens):
                        self._set_phase(
                            "degraded",
                            getattr(e.tokens, 'public_view', lambda: {})().get('message', '学校验证未通过')
                            + f'；订单已停止。{self._sync_hint()}，验证后重新启动 Rush',
                            reason="auth_expired",
                        )
                        degraded = True
                        return
                    if not ok:
                        log.warning(
                            "pre-release token revalidation: non-auth issue (%s), continuing rush",
                            getattr(e.tokens, 'public_view', lambda: {})().get('message', ''),
                        )
                else:
                    tr = await e.fetch_token()
                    if not tr.get("ok"):
                        if _pre_release_auth_failed(e.tokens):
                            self._set_phase("degraded", f"Token 刷新失败: {tr.get('msg', '')}", reason="auth_expired")
                            degraded = True
                            return
                        log.warning("token refresh failed without auth loss (%s), continuing rush", tr.get('msg', ''))
                    elif _pre_release_auth_failed(e.tokens):
                        self._set_phase("degraded", "认证未通过只读验证", reason="auth_expired")
                        degraded = True
                        return
            finally:
                validation_sport.reset(context)

            dry = dry_run_orders_enabled()
            if self._now() < warmup_at:
                await self._wait_until(warmup_at, label="等待放号前订单检查")
            check = self._order_preflight(plan, 'pre_release')
            if not check['ok']:
                self._set_phase("degraded", check['message'], reason="order_conflict")
                degraded = True
                return
            if dry:
                self._set_phase("waiting", "DRY RUN：跳过 VID 池预热（零验证码）")
            else:
                self._set_phase("warmup", "启动受限 VID 池…")
                registry = getattr(e, 'rush', None)
                if hasattr(registry, 'acquire_pool'):
                    pool = await registry.acquire_pool(self.run_id)
                    if not pool.get('ok'):
                        self._set_phase("degraded", pool.get('msg') or 'VID 池启动失败', reason="vid_pool_failed")
                        return
                elif not e.vid_running:
                    await e.start_vid_pool()

            if self._now() < plan.release_at:
                await self._wait_until(plan.release_at, label="等待放号")

            courts = self._court_order(plan)
            total = len(courts)
            round_no = 0

            while self._now() < scan_deadline and not success:
                round_no += 1
                secs_to_release = (plan.release_at - self._now()).total_seconds()
                interval = (
                    plan.scan_interval_near_sec
                    if secs_to_release <= -plan.near_release_sec
                    or self._now() >= plan.release_at
                    else plan.scan_interval_far_sec
                )

                for idx, court in enumerate(courts, 1):
                    if self._now() >= scan_deadline or success:
                        break
                    self._set_phase(
                        "scanning",
                        f"扫描 {court.name}（{idx}/{total}）",
                    )
                    await self._publish(
                        "rush.scan",
                        {"round": round_no, "court": court.no},
                        date=plan.date,
                        court=court.no,
                    )

                    query_token, query_user = e.token, e.user_id
                    query_started = time.monotonic()
                    scan_timeout = self._scan_timeout()
                    res = await get_order_once(court, plan.date, query_token, query_user, timeout=scan_timeout)
                    checks = self._record_scan(plan, court, res, round_no=round_no,
                        elapsed_ms=round((time.monotonic() - query_started) * 1000),
                        token=query_token, userid=query_user, scan_timeout_ms=round(scan_timeout * 1000))
                    answered_ms = (res.get("timing") or {}).get("request_ms")
                    if res.get("ok") and isinstance(answered_ms, (int, float)):
                        self._scan_request_ms.append(answered_ms)
                    if not res.get("ok"):
                        record('rush.query_failure', token=query_token, userid=query_user,
                               run_id=self.run_id, date=plan.date, ok=False,
                               school_msg=res.get('school_msg') or res.get('error') or res.get('msg'),
                               auth_fail=bool(res.get('auth_fail')), error_kind=res.get('error_kind'),
                               http_status=res.get('http_status'), code=str(res.get('code', '')))
                        try_record_first_auth_fail(
                            query_token,
                            auth_fail=bool(res.get('auth_fail')),
                            error_kind=str(res.get('error_kind') or ''),
                            school_msg=str(res.get('school_msg') or res.get('error') or res.get('msg') or ''),
                            code=str(res.get('code', '')),
                            userid=query_user,
                            via='rush',
                            run_id=self.run_id,
                            date=plan.date,
                        )
                        if res.get("auth_fail"):
                            if query_token != e.token or query_user != e.user_id:
                                continue
                            e.tokens.mark_expired('学校鉴权失败：' + str(res.get('error') or res.get('msg') or ''))
                            remaining = (scan_deadline - self._now()).total_seconds()
                            if hasattr(e, 'auth_keeper') and remaining > 1:
                                self._set_phase("token_refresh", "鉴权失效，尝试受限自动恢复")
                                try:
                                    context = validation_sport.set(plan.sport_id)
                                    if await asyncio.wait_for(e.auth_keeper.check(), timeout=min(45, remaining)):
                                        continue
                                except asyncio.TimeoutError:
                                    pass
                                finally:
                                    validation_sport.reset(context)
                            self._set_phase("degraded", f"学校鉴权失败，已暂停 Rush；{self._sync_hint()}",
                                            reason="auth_expired")
                            degraded = True
                            return
                        continue

                    blocked_windows = {(w['start'], w['end']) for w in self.preflight.get('windows', [])
                                       if w['blocked']}
                    window = next((w for w, check in zip(plan.windows, checks)
                                   if check['reason'] == 'match' and
                                   (w.start, w.end) not in blocked_windows), None)
                    if not window:
                        continue

                    target = Target(
                        start=window.start,
                        end=window.end,
                        min_duration=plan.min_duration,
                    )
                    if not candidates(res.get("blocks") or [], target):
                        continue

                    await self._publish(
                        "rush.match",
                        {
                            "start": window.start,
                            "end": window.end,
                        },
                        date=plan.date,
                        court=court.no,
                    )

                    self._set_phase("ordering", f"下单 {court.name} {window.start}-{window.end}")
                    take_vid_cb = None
                    if not dry_run_orders_enabled():
                        async def take_vid():
                            return await e.take_vid(timeout=0.05)

                        take_vid_cb = take_vid

                    bt = BookingTarget(
                        court=court,
                        date=plan.date,
                        start=window.start,
                        end=window.end,
                    )
                    outcome = await e.booking.place_order(
                        bt,
                        source=BookingSource.AUTO,
                        run_id=self.run_id,
                        deadline_mono=time.monotonic() + max(0, (scan_deadline - self._now()).total_seconds()),
                        take_vid=take_vid_cb,
                    )
                    detail = outcome.diagnostics
                    for key in ("recheck_queries", "recheck_errors", "vid_errors"):
                        self.diagnostics[key] += detail.get(key, 0)
                    self.diagnostics["order_sends"] += detail.get("order_sends", 0)
                    self.diagnostics["not_sent"] += int(detail.get("sent") is False)
                    await e.booking.publish_result(bt, outcome)
                    self.applied.append({
                        "court": court.no,
                        "name": court.name,
                        "ok": outcome.success,
                        "msg": outcome.msg,
                        "dry_run": outcome.dry_run,
                        "uncertain": outcome.uncertain,
                        "diagnostics": detail,
                    })

                    if outcome.uncertain:
                        self._set_phase("degraded", "下单结果不确定，已停止", reason="result_uncertain")
                        return

                    if outcome.success or outcome.dry_run:
                        success = True
                        self._set_phase(
                            "success",
                            "预约成功" if outcome.success else "DRY RUN 模拟完成（未提交）",
                        )
                        if plan.stop_after_success:
                            return
                        success = False

                    if outcome.blocked:
                        self._set_phase("degraded", outcome.msg or "订单门闸阻止下单", reason="order_blocked")
                        return

                if not success and self._now() < scan_deadline:
                    await self._sleep(interval)

            if not success and not degraded:
                d = self.diagnostics
                booked = sum(bool(a['ok']) for a in self.applied)
                simulated = sum(bool(a['dry_run']) for a in self.applied)
                result = f"已预约 {booked} 次" if booked else (
                    f"DRY RUN 模拟完成 {simulated} 次（未提交）" if simulated else "未预约")
                self._set_phase("success" if booked or simulated else "exhausted",
                                reason=None if booked or simulated else "scan_window_ended",
                                message=f"扫描结束：{d['rounds']} 轮 / {d['queries']} 次查询，"
                                f"{d['matches']} 次目标匹配，{d['query_errors']} 次扫描失败，"
                                f"{d['recheck_errors']} 次复查失败，{d['vid_errors']} 次 VID 失败；{result}")

        except asyncio.CancelledError:
            self._set_phase("stopped", "Rush 已取消", reason="stopped")
            raise
        except Exception as ex:
            log.exception("Rush 异常: %s", ex)
            self._set_phase("degraded", f"Rush 异常: {type(ex).__name__}: {ex}", reason="internal_error")
        finally:
            self.diagnostics.update(self._clock_diagnostics())
            record('rush.summary', token=e.token, userid=e.user_id, run_id=self.run_id,
                   sport_id=plan.sport_id, date=plan.date, phase=self.phase,
                   **{k: v for k, v in self.diagnostics.items() if k != 'last_queries'})
            request_priority.reset(priority)
            await self._shutdown_pool()
            self.running = False
            if self.phase not in ("success", "stopped", "degraded", "exhausted"):
                self._set_phase("exhausted", self.msg or "Rush 结束", reason="scan_window_ended")
            self.finished_at = self._now().strftime("%H:%M:%S")
            await self._publish("rush.stopped", {
                "run_id": self.run_id,
                "phase": self.phase,
                "reason": self.reason,
                "outcome": self.outcome(),
            })
