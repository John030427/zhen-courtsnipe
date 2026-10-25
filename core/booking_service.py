"""Unified booking — sole entry for order writes (manual / watch / auto / CLI)."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from core.order_journal import OrderJournal, JournalUnavailable
from core.order_guard import OrderNotSent, VidLease
from core.audit import record as audit
from core.safety import DRY_RUN_ORDER_RESPONSE, dry_run_orders_enabled
from models.plans import minutes
from utils import Court, classify_order_reply, get_order_once, get_vid_once, save_order_once, slot_covers

log = logging.getLogger("court")


class BookingSource(str, Enum):
    MANUAL = "manual"
    WATCH = "watch"
    AUTO = "auto"
    CLI = "cli"


@dataclass
class BookingTarget:
    court: Court
    date: str
    start: str
    end: str


@dataclass
class BookingOutcome:
    ok: bool
    success: bool = False
    uncertain: bool = False
    dry_run: bool = False
    blocked: bool = False
    msg: str = ""
    label: str = ""
    kind: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    journal_state: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


# A pooled VID needs this much validity left to outlast the recheck and the submit queue.
RECHECK_HEADROOM_SEC = 3.0


@dataclass
class _EarlyVid:
    """A captcha solved alongside the recheck. Unused ones go back to the pool."""
    task: asyncio.Task | None = None
    used: bool = False
    abandoned: bool = False


class _BookingTrace:
    """Measure sequential stages without serializing credentials or replies."""
    def __init__(self, context):
        self.context = context
        self.started = self.changed = time.monotonic()
        self.stage = "identity"
        self.reason = ""
        self.timing = {}
        self.recheck_queries = self.recheck_errors = self.vid_errors = 0
        self.order_sends = 0
        self.sent = False

    def enter(self, stage):
        now = time.monotonic()
        elapsed = round((now - self.changed) * 1000, 2)
        self.timing[self.stage + "_ms"] = round(
            self.timing.get(self.stage + "_ms", 0) + elapsed, 2)
        audit("booking.stage", **self.context, stage=self.stage, elapsed_ms=elapsed)
        self.stage, self.changed = stage, now

    def finish(self, outcome, error=None):
        last_stage = self.stage
        self.enter("finished")
        reason = ("cancelled" if isinstance(error, asyncio.CancelledError) else
            "exception" if error else self.reason or (
            "uncertain" if outcome.uncertain else
            "dry_run" if outcome.dry_run else
            "success" if outcome.success else
            "blocked" if outcome.blocked else last_stage + "_failed"))
        data = dict(stage=last_stage, reason=reason, timing=self.timing,
                    recheck_queries=self.recheck_queries, recheck_errors=self.recheck_errors,
                    vid_errors=self.vid_errors, sent=self.sent,
                    order_sends=self.order_sends,
                    elapsed_ms=round((time.monotonic() - self.started) * 1000, 2))
        if outcome is not None:
            outcome.diagnostics = data
        audit("booking.outcome", **self.context, **data,
              ok=bool(outcome and outcome.success),
              uncertain=bool(outcome and outcome.uncertain),
              dry_run=bool(outcome and outcome.dry_run),
              error_kind=type(error).__name__ if error else None)


@dataclass
class _RunBudget:
    run_id: str
    orders_completed: int = 0
    attempts_used: int = 0


class BookingService:
    def __init__(self, engine):
        self._engine = engine
        self._journal = OrderJournal()
        self._locks: dict[str, asyncio.Lock] = {}
        self._runs: dict[str, _RunBudget] = {}

    def journal_path(self):
        return self._journal._path

    def new_run_id(self, prefix: str = "run") -> str:
        rid = f"{prefix}-{uuid.uuid4().hex[:12]}"
        self._runs[rid] = _RunBudget(run_id=rid)
        return rid

    def reset_run(self, run_id: str | None) -> None:
        if run_id and run_id in self._runs:
            del self._runs[run_id]

    def _safety_cfg(self) -> dict[str, Any]:
        return self._engine.config_data.get("safety") or {}

    def _max_order_attempts(self) -> int:
        v = int(self._safety_cfg().get("max_order_attempts", 1))
        return max(1, min(2, v))

    def _max_orders_per_run(self) -> int:
        v = int(self._safety_cfg().get("max_orders_per_run", 1))
        return max(1, min(3, v))

    def _order_cooldown_sec(self) -> float:
        return float(self._safety_cfg().get("order_cooldown_sec", 3))

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _validate_identity(self) -> BookingOutcome | None:
        e = self._engine
        if e.tokens.is_degraded_for_orders():
            auth = e.tokens.public_view()
            return BookingOutcome(
                ok=False,
                blocked=True,
                msg=auth.get("message") or "Token 尚未通过只读验证",
                label="❌ 认证未就绪",
                kind="err",
            )
        if not e.token or not e.user_id or e.user_id.startswith("YOUR_"):
            return BookingOutcome(
                ok=False, blocked=True, msg="请先配置账号并获取 Token",
                label="❌ 未配置 Token", kind="err",
            )
        for label, val in (
            ("customer_id", e.customer_id),
            ("name", e.customer_name),
            ("tel", e.customer_tel),
        ):
            if not val or str(val).startswith("YOUR_"):
                return BookingOutcome(
                    ok=False, blocked=True,
                    msg="预约需要完整的用户资料",
                    label="❌ 资料不完整", kind="err",
                )
        return None

    def order_conflict(self, date: str, start: str, end: str, *, ground_id=None) -> BookingOutcome | None:
        try:
            blockers = self._journal.find_blockers(
                self._engine.user_id, date=date, start=start, end=end, ground_id=ground_id,
            )
        except (JournalUnavailable, ValueError, TypeError):
            return BookingOutcome(ok=False, blocked=True, msg="订单记录不可读，请先核对官方订单", kind="error")
        if not blockers:
            return None
        conflict = blockers[0]
        return BookingOutcome(ok=False, blocked=True,
            msg=(f"本地订单记录冲突：{conflict.get('date')} "
                 f"{conflict.get('start')}-{conflict.get('end')}（{conflict.get('state')}），请先核对官方订单"),
            label="⏳ 已有相关订单记录", kind="warn")

    def preflight_windows(self, date, windows):
        rows = []
        for window in windows:
            conflict = self.order_conflict(date, window.start, window.end)
            rows.append({"start": window.start, "end": window.end,
                         "blocked": conflict is not None, "message": conflict.msg if conflict else ""})
        ok = any(not row['blocked'] for row in rows)
        return {"ok": ok, "windows": rows,
                "message": "本地订单检查通过" if ok else next((r['message'] for r in rows), "未设置预约时段")}

    def _fee_check(self, blocks: list[dict], start: str, end: str) -> BookingOutcome | None:
        """Block when fee is explicitly non-zero; unknown fee needs human check."""
        t0, t1 = minutes(start), minutes(end)
        for blk in blocks:
            try:
                bt = minutes(blk["time"])
            except (KeyError, ValueError, TypeError):
                continue
            if bt < t0 or bt >= t1:
                continue
            fee = blk.get("fee")
            if fee is None:
                continue
            try:
                amount = float(fee)
            except (TypeError, ValueError):
                return BookingOutcome(
                    ok=False, blocked=True,
                    msg="费用信息未知，请先在官方页面确认是否免费",
                    label="⚠ 费用未确认", kind="warn",
                )
            if amount > 0:
                return BookingOutcome(
                    ok=False, blocked=True,
                    msg="检测到非零费用，请先在官方页面确认后再预约",
                    label="⚠ 可能收费", kind="warn",
                )
        return None

    async def place_order(
        self,
        target: BookingTarget,
        *,
        source: BookingSource,
        run_id: str | None = None,
        deadline_mono: float | None = None,
        take_vid: Callable[[], Awaitable[VidLease | None]] | None = None,
    ) -> BookingOutcome:
        sport_id = self._engine.sport_for_ground_id(target.court.id)
        trace = _BookingTrace(dict(run_id=run_id, source=source.value,
            sport_id=sport_id, gymId=self._engine.venue_for_sport(sport_id)[0],
            court=target.court.no, groundId=target.court.id, date=target.date,
            startTime=target.start, endTime=target.end))
        outcome = None
        error = None
        try:
            outcome = await self._place_order(target, source=source, run_id=run_id,
                deadline_mono=deadline_mono, take_vid=take_vid, trace=trace)
            return outcome
        except BaseException as exc:
            error = exc
            raise
        finally:
            trace.finish(outcome, error)

    async def _place_order(self, target, *, source, run_id, deadline_mono, take_vid, trace):
        pre = self._validate_identity()
        if pre:
            return pre

        e = self._engine
        account = e.user_id
        lk = account
        trace.enter("account_queue")
        async with self._lock(lk):
            trace.enter("guards")
            if e.user_id != account:
                return BookingOutcome(ok=False, blocked=True, msg="排队期间账号已变更", kind="error")
            pre = self._validate_identity()
            if pre:
                return pre
            conflict = self.order_conflict(target.date, target.start, target.end, ground_id=target.court.id)
            if conflict:
                return conflict

            budget = self._runs.get(run_id) if run_id else None
            if budget and budget.orders_completed >= self._max_orders_per_run():
                return BookingOutcome(
                    ok=False, blocked=True,
                    msg="本轮预约次数已达上限",
                    label="⏳ 预算已用尽", kind="warn",
                )

            max_attempts = self._max_order_attempts()
            last_outcome: BookingOutcome | None = None

            for attempt in range(max_attempts):
                trace.reason = ""
                if budget:
                    budget.attempts_used += 1
                if deadline_mono is not None and time.monotonic() >= deadline_mono:
                    return BookingOutcome(
                        ok=False, blocked=True, msg="已超过截止时间",
                        label="⏳ 已超时", kind="warn",
                    )

                outcome = await self._single_attempt(
                    target,
                    source=source,
                    auth_revision_at_start=e.tokens.auth_revision,
                    take_vid=take_vid,
                    deadline_mono=deadline_mono,
                    run_id=run_id,
                    trace=trace,
                )
                last_outcome = outcome

                if outcome.dry_run or outcome.uncertain or outcome.success or outcome.blocked:
                    if outcome.success and budget:
                        budget.orders_completed += 1
                    return outcome

                if attempt + 1 < max_attempts:
                    trace.enter("retry_cooldown")
                    await asyncio.sleep(self._order_cooldown_sec())
                    continue
                return outcome

            return last_outcome or BookingOutcome(ok=False, msg="预约未执行")

    async def _single_attempt(
        self,
        target: BookingTarget,
        *,
        source: BookingSource,
        auth_revision_at_start: int,
        take_vid: Callable[[], Awaitable[VidLease | None]] | None,
        deadline_mono: float | None,
        run_id: str | None = None,
        trace: _BookingTrace,
    ) -> BookingOutcome:
        e = self._engine
        identity = (e.token, e.user_id, e.customer_id, e.customer_name, e.customer_tel)

        def check_current():
            if e.tokens.auth_revision != auth_revision_at_start or identity != (
                e.token, e.user_id, e.customer_id, e.customer_name, e.customer_tel
            ) or e.tokens.is_degraded_for_orders():
                raise OrderNotSent("认证已变化或失效，请重新验证后再预约")
            if deadline_mono is not None and time.monotonic() >= deadline_mono:
                raise OrderNotSent("预约截止时间已过")

        early = self._start_early_vid(take_vid, check_current, deadline_mono, trace)
        try:
            return await self._attempt_once(
                target, source=source, auth_revision_at_start=auth_revision_at_start, take_vid=take_vid,
                deadline_mono=deadline_mono, run_id=run_id, trace=trace, identity=identity,
                check_current=check_current, early=early)
        finally:
            if early is not None and not early.used:
                early.abandoned = True
                early.task.add_done_callback(self._return_unused_vid)

    def _start_early_vid(self, take_vid, check_current, deadline_mono, trace) -> _EarlyVid | None:
        """Real orders need a captcha. When the pool has none that outlasts the recheck, solve one
        now so the recheck's wait and the captcha overlap; the shared 1 s pacing still spaces
        every request, so this adds no concurrency against the school."""
        headroom = getattr(self._engine, "vid_headroom", None)
        if take_vid is None or headroom is None or dry_run_orders_enabled():
            return None
        if headroom() >= RECHECK_HEADROOM_SEC:
            return None
        early = _EarlyVid()
        early.task = asyncio.create_task(self._generate_vid(check_current, deadline_mono, trace, early=early))
        return early

    def _return_unused_vid(self, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        lease = task.result()[0]
        give_back = getattr(self._engine, "return_vid", None)
        if lease is not None and give_back is not None:
            give_back(lease)

    async def _generate_vid(self, check_current, deadline_mono, trace, early: _EarlyVid | None = None):
        """Solve one captcha for this order; a second try only when the first fails and it can
        still finish before the deadline. Returns (lease, error, error_kind, blocked)."""
        e = self._engine
        error = error_kind = None
        queued = time.monotonic()
        async with e._solve_sem:
            if early is None:
                trace.enter("vid_generate")
            else:
                trace.timing["vid_semaphore_ms"] = round((time.monotonic() - queued) * 1000, 2)
            started = time.monotonic()
            try:
                for attempt in (1, 2):
                    try:
                        check_current()
                    except OrderNotSent as exc:
                        return None, str(exc), None, True
                    try:
                        vid, timing = await get_vid_once(timeout=e.tuning.vid_timeout)
                    except Exception as exc:
                        error_kind = type(exc).__name__
                        error = f"验证码生成失败（{error_kind}）"
                        audit("booking.vid", **trace.context, ok=False, error_kind=error_kind,
                              vid_attempt=attempt, vid_source="early" if early else "on_demand")
                        left = float("inf") if deadline_mono is None else deadline_mono - time.monotonic()
                        if (early is not None and early.abandoned) or left < float(e.tuning.vid_timeout) + 1.0:
                            break
                        continue
                    audit("booking.vid", **trace.context, ok=True, vid_attempt=attempt,
                          vid_source="early" if early else "on_demand", timing={
                              k: timing[k] for k in ("gen_ms", "rec_ms", "trk_ms", "chk_ms", "total_ms")
                              if isinstance(timing.get(k), (int, float))})
                    return VidLease(vid, time.monotonic()), None, None, False
                return None, error, error_kind, False
            finally:
                if early is not None:
                    trace.timing["vid_generate_ms"] = round((time.monotonic() - started) * 1000, 2)

    async def _attempt_once(self, target, *, source, auth_revision_at_start, take_vid, deadline_mono,
                            run_id, trace, identity, check_current, early) -> BookingOutcome:
        e = self._engine
        trace.enter("recheck")
        trace.recheck_queries += 1
        fresh = await get_order_once(
            target.court, target.date, e.token, e.user_id,
        )
        audit("booking.recheck", **trace.context, ok=bool(fresh.get("ok")),
              auth_fail=bool(fresh.get("auth_fail")), error_kind=fresh.get("error_kind"),
              http_status=fresh.get("http_status"), timing=fresh.get("timing"))
        if not fresh.get("ok"):
            trace.recheck_errors += 1
            trace.reason = "recheck_" + (
                "auth" if fresh.get("auth_fail") else
                "network" if fresh.get("error_kind") == "network" else "query_failed")
            if fresh.get("auth_fail") and e.tokens.auth_revision == auth_revision_at_start:
                e.tokens.mark_expired("学校拒绝下单前复查鉴权，请重新同步 Token")
            return BookingOutcome(
                ok=False,
                blocked=bool(fresh.get("auth_fail")),
                msg=str(fresh.get("error") or "fresh query 失败"),
                label="❌ 查询失败",
                kind="error",
            )
        trace.enter("availability_fee")
        if not slot_covers(fresh.get("avail") or {}, target.start, target.end):
            trace.reason = "no_longer_available"
            return BookingOutcome(
                ok=False,
                msg="时段已不再空闲",
                label="⚠ 已被占用",
                kind="warn",
            )
        fee_block = self._fee_check(fresh.get("blocks") or [], target.start, target.end)
        if fee_block:
            trace.reason = "fee_blocked"
            return fee_block

        if e.tokens.auth_revision != auth_revision_at_start:
            return BookingOutcome(
                ok=False, blocked=True,
                msg="认证状态已变化，请重新验证后再预约",
                label="❌ 认证已变更", kind="err",
            )
        if deadline_mono is not None and time.monotonic() >= deadline_mono:
            return BookingOutcome(
                ok=False, blocked=True, msg="已超过截止时间",
                label="⏳ 已超时", kind="warn",
            )

        if dry_run_orders_enabled():
            await e.bus.publish(
                "order.started",
                {
                    "source": source.value,
                    "start": target.start,
                    "end": target.end,
                    "dry_run": True,
                },
                run_id=run_id,
                date=target.date,
                court=target.court.no,
            )
            return BookingOutcome(
                ok=True,
                dry_run=True,
                msg=DRY_RUN_ORDER_RESPONSE["msg"],
                label="🧪 DRY RUN（未提交）",
                kind="dry_run",
                raw=dict(DRY_RUN_ORDER_RESPONSE),
            )

        sport_id = e.sport_for_ground_id(target.court.id)
        max_age = float(e.tuning.vid_max_age)
        lease: VidLease | None = None
        vid_err: str | None = None
        vid_err_kind: str | None = None
        if early is not None:
            trace.enter("vid_wait")
            lease, vid_err, vid_err_kind, blocked = await early.task
            early.used = True
            if blocked:
                return BookingOutcome(ok=False, blocked=True, msg=vid_err, kind="warn")
            if lease is not None and lease.age() > max_age:
                lease = None
        if lease is None and take_vid:
            trace.enter("vid_pool")
            candidate = await take_vid()
            if isinstance(candidate, VidLease) and candidate.age() <= max_age:
                lease = candidate
        if lease is None and early is None:
            trace.enter("vid_semaphore")
            lease, vid_err, vid_err_kind, blocked = await self._generate_vid(check_current, deadline_mono, trace)
            if blocked:
                return BookingOutcome(ok=False, blocked=True, msg=vid_err, kind="warn")
        vid = lease.value if lease is not None else None

        def _vid_preview(value: str | None) -> str:
            if not value:
                return ""
            return value[:8] + "…" if len(value) > 8 else value

        if not vid:
            trace.vid_errors += 1
            trace.reason = "vid_unavailable"
            log.warning(
                "VID 不可用，跳过 saveOrder sport=%s court=%s ground=%s vid_preview=%s error_kind=%s error=%s",
                sport_id,
                target.court.no,
                target.court.id,
                _vid_preview(vid),
                vid_err_kind or "missing",
                vid_err or "",
            )
            return BookingOutcome(
                ok=False,
                msg=vid_err or "验证码 VID 获取失败",
                label="⚠ VID 不可用",
                kind="warn",
            )

        if lease.age() > float(e.tuning.vid_max_age):
            trace.vid_errors += 1
            trace.reason = "vid_expired"
            log.warning(
                "VID 已过期，跳过 saveOrder sport=%s court=%s ground=%s vid_preview=%s",
                sport_id,
                target.court.no,
                target.court.id,
                _vid_preview(vid),
            )
            return BookingOutcome(
                ok=False,
                msg="验证码 VID 已过期",
                label="⚠ VID 过期", kind="warn",
            )

        trace.enter("final_guard_journal")

        def before_send():
            check_current()
            if dry_run_orders_enabled():
                raise OrderNotSent("dry-run：已阻止 saveOrder", dry_run=True)
            if lease.age() > float(e.tuning.vid_max_age):
                raise OrderNotSent("验证码 VID 已过期，未发送订单")

        try:
            before_send()
        except OrderNotSent as exc:
            return BookingOutcome(ok=False, blocked=True, dry_run=exc.dry_run, msg=str(exc), kind="warn")

        pending = {
            "account_id": e.user_id,
            "ground_id": target.court.id,
            "date": target.date,
            "court": target.court.no,
            "start": target.start,
            "end": target.end,
            "state": "pending",
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": source.value,
            "auth_revision": auth_revision_at_start,
        }
        if not self._journal.persist_record(pending):
            return BookingOutcome(
                ok=False, blocked=True,
                msg="pending 无法写入 journal，已阻止 saveOrder",
                label="❌ 无法落盘", kind="err",
            )

        await e.bus.publish(
            "order.started",
            {
                "source": source.value,
                "start": target.start,
                "end": target.end,
                "dry_run": False,
            },
            run_id=run_id,
            date=target.date,
            court=target.court.no,
        )

        gym_id, gym_name = e.venue_for_sport(sport_id)
        trace.enter("submit")
        previously_sent = trace.sent
        trace.sent = True if previously_sent else None
        raw = await save_order_once(
            vid=vid,
            court=target.court,
            date=target.date,
            start=target.start,
            end=target.end,
            token=identity[0],
            user_id=identity[1],
            customer_id=identity[2],
            customer_name=identity[3],
            customer_tel=identity[4],
            gym_id=gym_id,
            gym_name=gym_name,
            _booking_internal=True,
            _before_send=before_send,
            _send_deadline=min(deadline_mono or float("inf"), lease.issued_mono + float(e.tuning.vid_max_age)),
            _audit_context={"run_id": run_id, "sport_id": sport_id,
                            "source": source.value, "auth_revision": auth_revision_at_start},
            _vid_issued_mono=lease.issued_mono,
        )
        trace.sent = True if previously_sent else raw.get("sent")
        trace.order_sends += int(raw.get("sent") is True)
        if not raw.get("success"):
            trace.reason = ("uncertain" if raw.get("uncertain") else
                            "submit_auth" if raw.get("auth_fail") else
                            "send_blocked" if raw.get("blocked") else "school_rejected")
        trace.enter("journal_result")

        if raw.get("auth_fail") and e.tokens.auth_revision == auth_revision_at_start:
            e.tokens.mark_expired("学校拒绝订单鉴权，请重新同步 Token")

        uncertain = bool(raw.get("uncertain"))
        success = bool(raw.get("success")) and not uncertain
        if uncertain:
            state = "uncertain"
        elif success:
            state = "success"
        else:
            state = "failed"

        record = {**pending, "state": state, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._journal.persist_record(record)

        kind, label = classify_order_reply(raw)
        return BookingOutcome(
            ok=success,
            success=success,
            uncertain=uncertain,
            blocked=bool(raw.get("blocked") or raw.get("auth_fail")),
            dry_run=bool(raw.get("dry_run")),
            msg=str(raw.get("msg", "") or ""),
            label=label,
            kind=kind,
            raw=raw,
            journal_state=state,
        )

    async def publish_result(self, target: BookingTarget, outcome: BookingOutcome) -> None:
        e = self._engine
        if outcome.kind in ("", "dry_run"):
            kind = outcome.kind or "dry_run"
            label = outcome.label or "🧪 DRY RUN（未提交）"
        else:
            kind, label = outcome.kind, outcome.label
            if not label and outcome.raw:
                kind, label = classify_order_reply(outcome.raw)
        await e.bus.publish(
            "order.result",
            {
                "court": target.court.no,
                "name": target.court.name,
                "kind": kind,
                "label": label,
                "msg": outcome.msg,
                "dry_run": outcome.dry_run,
                "sport_id": e.sport_for_ground_id(target.court.id),
                "uncertain": outcome.uncertain,
                "code": outcome.raw.get("code") if outcome.raw else None,
                "ts": time.strftime("%H:%M:%S"),
            },
            date=target.date,
            court=target.court.no,
        )


def overlaps_existing_for_account(
    journal: OrderJournal,
    account_id: str,
    date: str,
    start: str,
    end: str,
) -> bool:
    return bool(journal.find_blockers(account_id, date=date, start=start, end=end))
