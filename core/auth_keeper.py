"""Bounded recovery using the existing, previously verified capture transport."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from core.audit import record
from models.auth import TokenState


class AuthKeeper:
    def __init__(self, engine):
        self.engine = engine
        self.task = None
        self._gate = asyncio.Lock()
        self._refresh_version = 0
        self._refresh_ok = False
        self.status = "idle"
        self.message = ""
        self.checked_at = None
        self._wake = asyncio.Event()
        self._prepared_release = None
        self._preparation_baseline = None
        engine.tokens.configure(on_auth_failure=self.request_recovery)

    def request_recovery(self):
        if self.enabled:
            self._wake.set()

    @property
    def enabled(self):
        return self.engine.config_data.get("connection", {}).get("auto_recover") is True

    def public_view(self):
        return {"enabled": self.enabled, "status": self.status,
                "message": self.message, "checked_at": self.checked_at,
                "retry_in_sec": 0}

    def _valid(self):
        tokens = self.engine.tokens
        return tokens.state == TokenState.VALID and not tokens.is_degraded_for_orders()

    def _busy(self):
        e = self.engine
        return bool(e._reserve_inflight or any(lock.locked() for lock in e.booking._locks.values()))

    def _set(self, status, message):
        self.status, self.message = status, message
        record("token.keeper", token=self.engine.token, userid=self.engine.user_id,
               phase=status, message=message)

    async def check(self, *, known_failure_only=False):
        refresh_version = self._refresh_version
        async with self._gate:
            e = self.engine
            if refresh_version != self._refresh_version:
                return self._refresh_ok and self._valid()
            if known_failure_only and e.tokens.state != TokenState.EXPIRED:
                return e.tokens.state == TokenState.VALID and not e.tokens.is_degraded_for_orders()
            if self._busy():
                return False
            self._set("checking", "正在检查登录状态")
            # A current committed auth rejection is already sufficient evidence.
            ok = False if e.tokens.state == TokenState.EXPIRED else await e.tokens.validate_current()
            e._sync_from_token_manager()
            self.checked_at = datetime.now(timezone.utc).isoformat()
            if ok and not e.tokens.is_degraded_for_orders():
                self._set("ready", "学校验证通过")
                return True
            if e.tokens.state != TokenState.EXPIRED:
                self._set("query_failed", "查询或网络异常，未触发登录刷新")
                return False
            return await self._refresh_locked()

    async def prepare_rush(self, release_at: str):
        """Prefer renewal; explicitly validate an unchanged credential before reuse."""
        if not self.enabled:
            return await self.check()
        refresh_version = self._refresh_version
        async with self._gate:
            tokens = self.engine.tokens
            revision = tokens.auth_revision
            if (self._prepared_release == (release_at, revision)
                    and self._valid()):
                return True
            # A manual recapture after failed preparation supersedes that failure.
            baseline = self._preparation_baseline
            snap = getattr(tokens, 'snapshot', None)
            captured_at = getattr(snap, 'captured_at', None)
            recent = (isinstance(captured_at, datetime) and captured_at.tzinfo is not None
                      and 0 <= (datetime.now(timezone.utc) - captured_at).total_seconds() <= 120)
            if (baseline and baseline[0] == release_at and revision > baseline[1]
                    and recent and self._valid()):
                self._prepared_release = (release_at, revision)
                self._set("ready", "已采用重新同步且验证通过的新 Token")
                return True
            if refresh_version != self._refresh_version:
                return False
            self._preparation_baseline = (release_at, revision)
            previous = tokens.token
            attempt_version = self._refresh_version
            ok = await self._refresh_locked()
            if (not ok or tokens.token == previous or tokens.auth_revision == revision
                    or not self._valid()):
                # Reload can retain a valid session. A fresh probe is not token renewal.
                if (self._refresh_version != attempt_version and not self._busy()
                        and tokens.token == previous and tokens.auth_revision == revision):
                    verified = await tokens.validate_current()
                    self.engine._sync_from_token_manager()
                    self.checked_at = datetime.now(timezone.utc).isoformat()
                    if verified and self._valid():
                        self._prepared_release = (release_at, tokens.auth_revision)
                        self._set("ready", "未换发 Token；当前 Token 已重新验证通过，继续等待放号（不代表续期）")
                        return True
                self._set("needs_action", "放号前未确认有效 Token，请在官方页同步后重新准备任务")
                return False
            self._prepared_release = (release_at, tokens.auth_revision)
            self._set("ready", "放号前新 Token 已同步并验证")
            return True

    async def _refresh_locked(self):
        e = self.engine
        if not self.enabled:
            self._set("needs_action", "登录失效，请同步 Token 或启用自动恢复")
            return False
        if self._busy():
            self._set("needs_action", "订单正在处理中，未刷新登录")
            return False
        capture = e.clash_capture_state()
        if not (capture.get("enabled") and capture.get("listening") and capture.get("captured")):
            self._set("needs_action", "捕获链路未验证，请先在官方页切场完成一次同步")
            return False
        self._set("refreshing", "正在刷新官方窗口，等待新 Token 并验证")
        self._refresh_ok = False
        try:
            result = await e.fetch_token(allow_page_capture=True)
            self._refresh_ok = bool(result.get("ok") and not e.tokens.is_degraded_for_orders())
        finally:
            # Waiters share this attempt; a later explicit request may retry immediately.
            self._refresh_version += 1
        self.checked_at = datetime.now(timezone.utc).isoformat()
        ok = self._refresh_ok
        self._set("ready" if ok else "needs_action",
                  "自动恢复成功" if ok else "自动恢复失败，请检查官方页登录；任务不会绕过鉴权下单")
        return ok

    async def _run(self):
        while True:
            notified = False
            try:
                async with asyncio.timeout(90):
                    await self._wake.wait()
                notified = True
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            e = self.engine
            if not self.enabled:
                continue
            if notified:
                if e.tokens.state != TokenState.EXPIRED:
                    continue
                if self._busy():
                    self._set('waiting_order', '登录已失效，等待当前订单处理结束再刷新')
                    await asyncio.sleep(1)
                    self._wake.set()
                    continue
            elif not (e.rush.running or e.watcher.any_running()):
                continue
            # Rush performs its own pre-release check; background work yields the critical window.
            if not notified and e.rush.running:
                state = e.rush.state_dict()
                states = list(state.get("by_sport", {}).values()) or [state]
                if any(s.get("running") and (s.get("phase") != "waiting" or
                       (s.get("release_in_sec") or 0) <= 180) for s in states):
                    continue
            try:
                await self.check(known_failure_only=notified)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._set("error", "本机自动恢复异常，等待下次检查")

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="auth-keeper")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
