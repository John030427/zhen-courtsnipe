"""Release rush mode — bounded VID warmup, scan window, BookingService orders."""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import Counter, deque
from datetime import datetime, timedelta
from typing import Any, Callable

from core.booking_service import BookingSource, BookingTarget, ScanSnapshot
from core.audit import record
from core.auth_context import validation_sport
from core.request_priority import request_priority, RUSH_SCAN_PRIORITY, RUSH_YIELD_PRIORITY
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
    "target_not_open",     # every target slot seen was 0 (not released), never taken by others
    "target_full",         # no target slot was ever free (booked or not released)
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


# School slot status in query results: 1 free, 2 booked, 0 not bookable (not released).
TAKEN_RECHECK_EVERY = 3  # a fully booked court is looked at again every 3rd round, not every round
# Right at release the school may still be flipping slots (or its clock may lag ours); a court
# that looks fully booked this early is not trusted as booked.
TAKEN_TRUST_AFTER_SEC = 5
# An order with no usable reply is checked again this long after it was sent: a slot that is
# still free by then was not booked by it.
SETTLE_AFTER_SEC = 8
TIMELINE_MAX = 80
# Opt-in order at release without a scan (plan.order_first): the Rush looks at its first courts
# this long before the captcha warm-up (so neither delays the other, and the request slot at
# release stays free), and waits at most this long past the local release time to be sure the
# school's own clock has passed it too.
PEEK_LEAD_SEC = 3
STRAIGHT_MAX_DELAY_SEC = 1.0


def court_view(checks: list[dict]) -> str:
    """How one successful query left this court for the plan's target windows."""
    if any(w.get("reason") == "match" for w in checks):
        return "free"
    statuses = {slot.get("status") for w in checks for slot in w.get("slots") or []}
    if statuses == {"2"}:
        return "taken"
    if statuses == {"0"}:
        return "closed"
    return "mixed"


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
        self.finished_on: str = ""  # Beijing date of finished_at, so a card can drop last night's ending
        self.plan: dict[str, Any] = {}
        self.run_id: str | None = None
        self.preflight: dict = {}
        self._reset_diagnostics()

    def _reset_diagnostics(self):
        self.diagnostics = {"queries": 0, "query_errors": 0, "matches": 0,
                            "rounds": 0, "last_queries": [], "recheck_queries": 0,
                            "recheck_errors": 0, "vid_errors": 0, "order_sends": 0,
                            "not_sent": 0, "skipped_taken": 0, "verify_queries": 0}
        self._scan_request_ms = deque(maxlen=8)
        self._court_views: dict[int, tuple[str, int]] = {}  # court -> (view, round seen)
        self._seen_status: set[str] = set()
        self._yielding = False
        self._last_ok_at = ""
        self._ordering: BookingTarget | None = None  # the order place_order is working on
        # Orders sent with no usable reply that a query could not settle yet: target, row, time.
        self._unresolved: list[dict] = []
        self.timeline: list[dict] = []  # what happened, in seconds from release, for the replay
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

    def _note_court(self, court_no: int, checks: list[dict], round_no: int, total: int,
                    settled: bool = True) -> None:
        """Remember what a successful query showed. While every court of this plan shows its
        targets as not released, the next scans step behind the other sport's (see _run)."""
        view = court_view(checks)
        if view == "taken" and not settled:
            view = "mixed"  # too close to release to believe; keep checking it every round
        self._court_views[court_no] = (view, round_no)
        self._last_ok_at = self._now().strftime("%H:%M:%S")
        self._seen_status.update(slot.get("status") for w in checks for slot in w.get("slots") or [])
        all_closed = (len(self._court_views) >= total
                      and all(v == "closed" for v, _ in self._court_views.values()))
        self._yielding = all_closed

    def _step(self, kind: str, text: str, court: int | None = None) -> None:
        """One line of the replay a person reads afterwards: seconds from release, court, what."""
        release = self.plan.get("release_at")
        try:
            t = round((self._now() - datetime.fromisoformat(release)).total_seconds(), 1) if release else None
        except (TypeError, ValueError):
            t = None
        if len(self.timeline) < TIMELINE_MAX:
            self.timeline.append({"t": t, "kind": kind, "court": court, "text": text})

    @staticmethod
    def _school_answered(outcome) -> bool:
        """The school's application replied to the order (even with an error), as opposed to a
        timeout or a gateway page, after which the order may still be on its way."""
        status = (outcome.raw or {}).get("http_status")
        return isinstance(status, int) and status not in (502, 503, 504)

    async def _settle(self, bt: BookingTarget, entry: dict, *, answered: bool, final: bool = False):
        """Ask the school who holds the slot of an order that got no usable reply, and write the
        answer into its row. Returns (state, fresh): "mine" booked after all; "lost" someone else
        holds it, or it is still free although the school had answered (or enough time passed);
        "open" cannot tell yet. `fresh` is (query result, snapshot) when the court is still free."""
        verify = getattr(self.engine.booking, "verify_order", None)
        if not callable(verify):
            return "open", None
        try:
            verdict, result, snapshot = await verify(bt, run_id=self.run_id, settle_free=answered or final)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("order check failed: %s", type(exc).__name__)
            return "open", None
        self.diagnostics["verify_queries"] += 1
        state = ("mine" if verdict == "mine" else
                 "lost" if verdict == "other" or (verdict == "free" and (answered or final)) else "open")
        entry["verified"] = verdict
        if state == "mine":
            entry.update(ok=True, uncertain=False)
        elif state == "lost":
            entry["uncertain"] = False
        self._step("verify", {"mine": "核对：已约上", "other": "核对：被别人约走了",
                              "free": "核对：还空着，这一单没约上" if state == "lost" else "核对：还空着，暂时不能确定",
                              }.get(verdict, "核对：查不出结果"), bt.court.no)
        record('rush.verify', token=self.engine.token, userid=self.engine.user_id, run_id=self.run_id,
               sport_id=self.plan.get('sport_id'), date=bt.date, court=bt.court.no, verdict=verdict,
               reason=state)
        return state, (result, snapshot) if verdict == "free" else None

    async def _settle_unresolved(self) -> None:
        """Check every order still of unknown result once more, a few seconds after it was sent."""
        for item in list(self._unresolved):
            if item.get("checked"):
                continue
            wait = SETTLE_AFTER_SEC - (time.monotonic() - item["at"])
            if wait > 0:
                await self._sleep(wait)
            state, _ = await self._settle(item["bt"], item["entry"], answered=False, final=True)
            item["checked"] = True
            if state != "open":
                self._unresolved.remove(item)

    def _conclude_unresolved(self, booked_before: bool) -> None:
        """Final words once the late checks are in."""
        booked = [a for a in self.applied if a.get("ok")]
        confirmed = [a for a in booked if a.get("verified") == "mine"]
        per_window = Counter((a.get("start"), a.get("end")) for a in booked)
        doubled = "、".join(str(a["court"]) for a in booked if per_window[(a.get("start"), a.get("end"))] > 1)
        unknown = "、".join(str(u["entry"]["court"]) for u in self._unresolved)
        if confirmed and doubled:
            self._set_phase("success", f"同一时段约上了不止一个场（{doubled} 号），请到企业微信「我的预约」取消多余的")
        elif confirmed and booked_before:
            also = "、".join(str(a["court"]) for a in confirmed)
            self._set_phase("success", f"{self.msg}；{also} 号场经查询确认也已约上")
        elif confirmed:
            self._set_phase("success", f"预约成功：{confirmed[0]['court']} 号场（学校当时没正常回复，查询确认已约上）")
        elif unknown and booked_before:
            self._set_phase("success", f"{self.msg}；另有 {unknown} 号场的订单结果无法确认，请到企业微信核对")
        elif unknown:
            self._set_phase("degraded", f"{unknown} 号场的订单已发出，但结果无法确认，请到企业微信「我的预约」核对",
                            reason="result_uncertain")
        elif self.phase == "degraded" and self.reason == "result_uncertain":
            self._set_phase("exhausted", "发出的订单都没有约上（已向学校查询确认）", reason="scan_window_ended")

    def _no_booking_reason(self) -> tuple[str, str]:
        """Why the scan window ended empty, in words a person can act on."""
        queries, errors = self.diagnostics["queries"], self.diagnostics["query_errors"]
        if queries and errors * 2 > queries:
            # Mostly failed queries say nothing about the courts; do not call the target full.
            return "scan_window_ended", f"{errors}/{queries} 次查询失败，没能确认有没有空场。"
        seen = self._seen_status
        if seen == {"0"}:
            return ("target_not_open",
                    "目标时段在查到的场地上一直是「不可约」：学校没放出这段（还没放号，或这段本来不开放），"
                    f"不是被别人抢完。{queries - errors} 次查询都是这样，最后一次在 {self._last_ok_at}。")
        if seen and seen <= {"0", "2"}:
            return "target_full", "目标时段在查到的场地上都已约满或不可约，没有出现过空位。"
        return "scan_window_ended", ""

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
        self.finished_at = self.finished_on = ""
        self.run_id = e.booking.new_run_id("rush")
        self.plan = {
            "date": plan.date,
            "sport_id": plan.sport_id,
            "release_at": plan.release_at.isoformat(),
            "windows": [w.model_dump() for w in plan.windows],
            "court_priority": plan.court_priority,
            # Every court that will be scanned: the chosen ones first, then the rest of the sport.
            "court_count": len(self._court_order(plan)),
            "excluded_courts": plan.excluded_courts,
            "repeat_daily": plan.repeat_daily,
            "prefer_first_scan": plan.prefer_first_scan,
            "order_first": plan.order_first,
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

    def _mark_finished(self) -> None:
        now = self._now()
        self.finished_at, self.finished_on = now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d")

    def _order_maybe_sent(self) -> bool:
        """For a stop that lands while place_order runs: once the order is written down as
        pending (the step right before saveOrder) it may have reached the school."""
        target, conflict = self._ordering, getattr(self.engine.booking, "order_conflict", None)
        if target is None or not callable(conflict):
            return False
        try:
            return conflict(target.date, target.start, target.end, ground_id=target.court.id) is not None
        except Exception:
            return True  # cannot tell: say so rather than report a plain stop

    async def stop(self):
        was_running = bool(self.task and not self.task.done())
        if was_running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self._shutdown_pool()
        self.running = False
        # Only a run this call stopped reads as stopped: a finished ending, or an order that
        # may have gone out, keeps its own words (stopping both sports also reaches idle ones).
        if was_running and self.reason != "result_uncertain":
            self._set_phase("stopped", "已手动停止", reason="stopped")
            self._mark_finished()

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
                self._mark_finished()

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
            "finished_on": self.finished_on,
            "timeline": list(self.timeline),
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

    async def _peek(self, plan: RushPlan, courts: list):
        """For the opt-in order at release: look at the first two courts a few seconds before
        the slots open and take the first whose target is there and not already held (a class,
        a team). Returns (court, window, snapshot), or None to scan first as usual."""
        e = self.engine
        blocked = {(w['start'], w['end']) for w in self.preflight.get('windows', []) if w['blocked']}
        for court in courts[:2]:
            if (plan.release_at - self._now()).total_seconds() < 2:
                break  # too close: the request slot at release stays free for the scan
            identity = (e.token, e.user_id, e.customer_id, e.customer_name, e.customer_tel)
            revision = e.tokens.auth_revision
            res = await get_order_once(court, plan.date, e.token, e.user_id, timeout=SCAN_TIMEOUT_MIN_SEC)
            snapshot = ScanSnapshot.capture(res, identity, revision)
            for window in plan.windows:
                target = BookingTarget(court=court, date=plan.date, start=window.start, end=window.end)
                if (window.start, window.end) not in blocked and snapshot.rejection_reason(
                        target, identity, revision, before_release=True) is None:
                    self._step("peek", "放号前看了一眼：时段在，没被占", court.no)
                    return court, window, snapshot
            self._step("peek", "放号前看了一眼：" + ("已被占，或看不到时段" if res.get("ok") else "查询失败"), court.no)
        return None

    @staticmethod
    def _straight_delay() -> float:
        """Seconds past the local release time before the straight order goes out, so that the
        school's clock has passed the release too: the measured clock difference and how unsure
        it is, at most a second."""
        measured = server_clock_offset()
        if measured is None:
            return 0.15
        offset, uncertainty = measured
        return min(STRAIGHT_MAX_DELAY_SEC, max(0.05, uncertainty - offset + 0.03))

    async def _order_court(self, plan: RushPlan, court, window, scan_snapshot, scan_deadline, *,
                           straight: bool = False) -> bool:
        """Order one court for one window and deal with the answer. True: this run is over (its
        ending is set here or in _run's finally block); False: keep scanning. `straight` is the
        order sent at release from a look taken just before it, without a scan in between."""
        e = self.engine
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
        again_here = 0
        while True:
            self._ordering = bt
            outcome = await e.booking.place_order(
                bt,
                source=BookingSource.AUTO,
                run_id=self.run_id,
                deadline_mono=time.monotonic() + max(0, (scan_deadline - self._now()).total_seconds()),
                take_vid=take_vid_cb,
                scan_snapshot=scan_snapshot,
                after_uncertain=tuple(u["bt"] for u in self._unresolved),
                **({"before_release": True} if straight and not again_here else {}),
            )
            self._ordering = None
            detail = outcome.diagnostics
            for key in ("recheck_queries", "recheck_errors", "vid_errors"):
                self.diagnostics[key] += detail.get(key, 0)
            self.diagnostics["order_sends"] += detail.get("order_sends", 0)
            self.diagnostics["not_sent"] += int(detail.get("sent") is False)
            await e.booking.publish_result(bt, outcome)
            entry = {
                "court": court.no,
                "name": court.name,
                "start": window.start,
                "end": window.end,
                "ok": outcome.success,
                "msg": outcome.msg,
                "dry_run": outcome.dry_run,
                "uncertain": outcome.uncertain,
                "reply_hint": (outcome.raw or {}).get("reply_hint") or "",
                "diagnostics": detail,
            }
            self.applied.append(entry)
            self._step("order", ("没先查询，放号就下单：" if straight and not again_here else "") + (
                       "下单成功" if outcome.success else
                       "模拟下单（未提交）" if outcome.dry_run else
                       f"订单已发出，学校没正常回复（{entry['reply_hint'] or outcome.msg or '无回复'}）"
                       if outcome.uncertain else
                       f"没发出订单：{outcome.msg}" if detail.get("sent") is False else
                       f"学校拒绝：{outcome.msg}"), court.no)
            settled = None
            if outcome.uncertain:
                # Not a blind retry: one query shows who holds the slot now.
                settled, fresh = await self._settle(bt, entry, answered=self._school_answered(outcome))
                if settled == "lost" and fresh and not again_here and self._now() < scan_deadline:
                    # The school answered with an error and the court is still free:
                    # one more order here, straight from that query, before moving on.
                    again_here, scan_snapshot = 1, fresh[1]
                    continue
            break

        if outcome.uncertain:
            if settled == "mine":
                self._set_phase("success", f"预约成功：学校回复异常，查询确认 {court.name} 已约上")
                return plan.stop_after_success
            if settled == "open":
                self._unresolved.append({"bt": bt, "entry": entry, "at": time.monotonic()})
                if len(self._unresolved) > plan.uncertain_extra_orders:
                    # Enough orders are out with no answer: wait for them instead of
                    # sending more, then go on only if none of them landed.
                    self._set_phase("scanning", "等待确认已发出的订单…")
                    await self._settle_unresolved()
                    if any(a.get("verified") == "mine" for a in self.applied) or self._unresolved:
                        return True  # the ending is written in _run's finally block
            return False

        if outcome.success or outcome.dry_run:
            self._set_phase("success", "预约成功" if outcome.success else "DRY RUN 模拟完成（未提交）")
            return plan.stop_after_success

        if outcome.blocked:
            self._set_phase("degraded", outcome.msg or "订单门闸阻止下单", reason="order_blocked")
            return True
        return False

    async def _run(self, plan: RushPlan):
        priority = request_priority.set(RUSH_SCAN_PRIORITY)
        e = self.engine
        cancelled = False
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

            courts = self._court_order(plan)
            total = len(courts)
            straight = None
            if plan.order_first:
                peek_at = warmup_at - timedelta(seconds=PEEK_LEAD_SEC)
                if self._now() < peek_at:
                    await self._wait_until(peek_at, label="等待放号前订单检查")
                straight = await self._peek(plan, courts)

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
            if straight:
                await self._wait_until(plan.release_at + timedelta(seconds=self._straight_delay()))
                if await self._order_court(plan, *straight, scan_deadline, straight=True):
                    return

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
                    view, seen_round = self._court_views.get(court.no, ("", 0))
                    if view == "taken" and (round_no - seen_round) % TAKEN_RECHECK_EVERY:
                        # Booked by someone else: the budget goes to courts that can still open.
                        self.diagnostics["skipped_taken"] += 1
                        continue
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
                    query_identity = (e.token, e.user_id, e.customer_id, e.customer_name, e.customer_tel)
                    query_revision = e.tokens.auth_revision
                    query_started = time.monotonic()
                    scan_timeout = self._scan_timeout()
                    # The optional head start covers only the first query. While this plan's
                    # targets are not released its scans step behind the other sport's, but only
                    # every other one, so a late opening is still seen in time.
                    yield_now = self._yielding and self.diagnostics["queries"] % 2 == 1
                    scan_priority = request_priority.set(
                        1 if plan.prefer_first_scan and round_no == 1 and idx == 1
                        else RUSH_YIELD_PRIORITY if yield_now else RUSH_SCAN_PRIORITY)
                    try:
                        res = await get_order_once(court, plan.date, query_token, query_user, timeout=scan_timeout)
                        scan_snapshot = ScanSnapshot.capture(res, query_identity, query_revision)
                    finally:
                        request_priority.reset(scan_priority)
                    checks = self._record_scan(plan, court, res, round_no=round_no,
                        elapsed_ms=round((time.monotonic() - query_started) * 1000),
                        token=query_token, userid=query_user, scan_timeout_ms=round(scan_timeout * 1000))
                    answered_ms = (res.get("timing") or {}).get("request_ms")
                    if res.get("ok") and isinstance(answered_ms, (int, float)):
                        self._scan_request_ms.append(answered_ms)
                    if res.get("ok"):
                        settled = (self._now() - plan.release_at).total_seconds() >= TAKEN_TRUST_AFTER_SEC
                        self._note_court(court.no, checks, round_no, total, settled)
                    seen = court_view(checks) if res.get("ok") else "failed"
                    if round_no == 1 or seen in ("free", "failed"):
                        self._step("scan", {"free": "查询：整段空", "taken": "查询：已被约", "closed": "查询：不可约（没放号）",
                                            "mixed": "查询：只剩一部分", "failed": "查询失败（学校没回应）"}[seen], court.no)
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

                    if await self._order_court(plan, court, window, scan_snapshot, scan_deadline):
                        return

                if not success and self._now() < scan_deadline:
                    await self._sleep(interval)

            if not success and not degraded:
                d = self.diagnostics
                booked = sum(bool(a['ok']) for a in self.applied)
                simulated = sum(bool(a['dry_run']) for a in self.applied)
                result = f"已预约 {booked} 次" if booked else (
                    f"DRY RUN 模拟完成 {simulated} 次（未提交）" if simulated else "未预约")
                ending, why = (None, "") if booked or simulated else self._no_booking_reason()
                self._set_phase("success" if booked or simulated else "exhausted",
                                reason=ending,
                                message=why + f"扫描结束：{d['rounds']} 轮 / {d['queries']} 次查询，"
                                f"{d['matches']} 次目标匹配，{d['query_errors']} 次扫描失败，"
                                f"{d['recheck_errors']} 次复查失败，{d['vid_errors']} 次 VID 失败；{result}")

        except asyncio.CancelledError:
            cancelled = True
            if self._order_maybe_sent():
                court = self._ordering.court
                self.applied.append({"court": court.no, "name": court.name, "ok": False, "uncertain": True,
                                     "dry_run": False, "msg": "停止时正在下单，订单可能已经发出",
                                     "reply_hint": "", "diagnostics": {}})
                self._set_phase("degraded", "停止时正在下单：订单可能已经发出，请到企业微信「我的预约」核对",
                                reason="result_uncertain")
            else:
                self._set_phase("stopped", "Rush 已取消", reason="stopped")
            raise
        except Exception as ex:
            log.exception("Rush 异常: %s", ex)
            self._set_phase("degraded", f"Rush 异常: {type(ex).__name__}: {ex}", reason="internal_error")
        finally:
            if not cancelled and (self._unresolved or any(a.get("verified") == "mine" for a in self.applied)):
                booked_before = any(a.get("ok") and a.get("verified") != "mine" for a in self.applied)
                try:
                    await self._settle_unresolved()
                except asyncio.CancelledError:
                    pass  # stopped during the late check: keep what is known
                except Exception:
                    log.exception("late order check failed")
                self._conclude_unresolved(booked_before)
            self._step("end", self.msg)
            self.diagnostics.update(self._clock_diagnostics())
            record('rush.summary', token=e.token, userid=e.user_id, run_id=self.run_id,
                   sport_id=plan.sport_id, date=plan.date, phase=self.phase,
                   **{k: v for k, v in self.diagnostics.items() if k != 'last_queries'})
            request_priority.reset(priority)
            await self._shutdown_pool()
            self.running = False
            self._ordering = None
            if self.phase not in ("success", "stopped", "degraded", "exhausted"):
                self._set_phase("exhausted", self.msg or "Rush 结束", reason="scan_window_ended")
            self._mark_finished()
            await self._publish("rush.stopped", {
                "run_id": self.run_id,
                "phase": self.phase,
                "reason": self.reason,
                "outcome": self.outcome(),
            })
