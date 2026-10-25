from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from models.auth import (
    AuthSnapshot,
    ProfileSnapshot,
    TokenState,
    is_placeholder,
    public_auth_view,
    token_preview,
)
from network.capture_protocol import CaptureEvent, next_addon_sequence, validate_capture_event
from core.audit import record, redact
from core.auth_context import validation_sport
from core.clash_capture import is_mitm_capture_source
from core.token_ttl_obs import public_ttl_fields, record_sync, try_record_first_auth_fail

log = logging.getLogger("court")

ValidateFn = Callable[[AuthSnapshot], Awaitable[bool | dict]]
PersistFn = Callable[[AuthSnapshot], None]
LifecycleFn = Callable[..., Awaitable[None] | None]


class TokenManager:
    """Single owner of in-memory auth state in the main process."""

    def __init__(
        self,
        *,
        allowed_hosts: list[str] | None = None,
        refresh_timeout_sec: float = 8.0,
        max_refresh_attempts: int = 2,
    ):
        self.allowed_hosts = allowed_hosts or ["reservation.sustech.edu.cn"]
        self.refresh_timeout_sec = refresh_timeout_sec
        self.max_refresh_attempts = max(1, int(max_refresh_attempts))

        self._state = TokenState.UNKNOWN
        self._snapshot: AuthSnapshot | None = None
        self._last_success_at: datetime | None = None
        self._last_refresh_at: datetime | None = None
        self._last_sync_at: datetime | None = None
        self._last_mitm_capture_at: datetime | None = None
        self._last_capture_reject: dict[str, str] | None = None
        self._message = ""

        self._auth_revision = 0
        self._auth_failure_epoch = 0
        self._last_accepted_capture_seq = 0
        self._refresh_generation = 0
        self._refresh_task: asyncio.Task | None = None
        self._refresh_previous_token = ""
        self._refresh_min_seq = 0
        self._lock = asyncio.Lock()
        self._refresh_gate = asyncio.Lock()
        self._capture_event = asyncio.Event()
        self._pending_capture: AuthSnapshot | None = None

        self._persist: PersistFn | None = None
        self._validate: ValidateFn | None = None
        self._validation_tasks: dict[tuple, asyncio.Task] = {}
        self._validation: dict = {}
        self._persist_error: str | None = None
        self._ttl_probe: Any = None
        self._on_sync_segment: LifecycleFn | None = None
        self._on_validate_success: LifecycleFn | None = None
        self._on_auth_failure: Callable[[], None] | None = None

    def configure(
        self,
        *,
        persist: PersistFn | None = None,
        validate: ValidateFn | None = None,
        refresh_timeout_sec: float | None = None,
        max_refresh_attempts: int | None = None,
        on_sync_segment: LifecycleFn | None = None,
        on_validate_success: LifecycleFn | None = None,
        on_auth_failure: Callable[[], None] | None = None,
    ) -> None:
        if persist is not None:
            self._persist = persist
        if validate is not None:
            self._validate = validate
        if refresh_timeout_sec is not None:
            self.refresh_timeout_sec = float(refresh_timeout_sec)
        if max_refresh_attempts is not None:
            self.max_refresh_attempts = max(1, int(max_refresh_attempts))
        if on_sync_segment is not None:
            self._on_sync_segment = on_sync_segment
        if on_validate_success is not None:
            self._on_validate_success = on_validate_success
        if on_auth_failure is not None:
            self._on_auth_failure = on_auth_failure

    def _notify_auth_failure(self):
        if self._on_auth_failure is not None and not self._refresh_active():
            try:
                self._on_auth_failure()
            except Exception:
                log.warning('Unable to notify auth recovery')

    def set_ttl_probe(self, probe: Any) -> None:
        self._ttl_probe = probe

    async def _emit_sync_segment(self, auth_revision: int) -> None:
        if self._on_sync_segment is not None:
            out = self._on_sync_segment(auth_revision)
            if asyncio.iscoroutine(out):
                await out

    async def _emit_validate_success(self, snap: AuthSnapshot) -> None:
        if self._on_validate_success is not None:
            out = self._on_validate_success(snap)
            if asyncio.iscoroutine(out):
                await out

    async def observation_check(self, snap: AuthSnapshot) -> dict:
        """Readonly validation for TTL probe — does not mutate token state."""
        return await self._check(snap)

    @property
    def _state(self):
        return self.__state

    @_state.setter
    def _state(self, value):
        previous = getattr(self, '_TokenManager__state', None)
        self.__state = value
        if previous != value:
            snap = getattr(self, '_snapshot', None)
            record('token.phase', token=snap.token if snap else '', userid=snap.userid if snap else '',
                   phase=value.value, previous_phase=previous.value if previous else None)

    @property
    def state(self) -> TokenState:
        return self._state

    @property
    def snapshot(self) -> AuthSnapshot | None:
        return self._snapshot

    @property
    def token(self) -> str:
        return self._snapshot.token if self._snapshot else ""

    @property
    def userid(self) -> str:
        return self._snapshot.userid if self._snapshot else ""

    @property
    def auth_revision(self) -> int:
        return self._auth_revision

    @property
    def last_mitm_capture_at(self) -> datetime | None:
        return self._last_mitm_capture_at

    @property
    def last_capture_reject(self) -> dict[str, str] | None:
        """The newest capture the workbench turned down: when, why, from which source."""
        return dict(self._last_capture_reject) if self._last_capture_reject else None

    def _note_mitm_capture(self, event: CaptureEvent) -> None:
        if not is_mitm_capture_source(event.source):
            return
        self._last_mitm_capture_at = datetime.fromtimestamp(
            event.captured_at, tz=timezone.utc
        )

    def is_degraded_for_orders(self) -> bool:
        return self._state != TokenState.VALID or self._refresh_active()

    def public_view(self) -> dict[str, Any]:
        view = public_auth_view(
            self._state,
            self._snapshot,
            last_success_at=self._last_success_at,
            last_refresh_at=self._last_refresh_at,
            message=self._message,
        )
        view.update(validation=dict(self._validation),
                    validating=any(not t.done() for t in self._validation_tasks.values()),
                    persist_error=self._persist_error,
                    last_validation_at=self._validation.get('checked_at'),
                    last_sync_at=self._last_sync_at.isoformat() if self._last_sync_at else None,
                    last_mitm_capture_at=(
                        self._last_mitm_capture_at.isoformat()
                        if self._last_mitm_capture_at
                        else None
                    ),
                    source=self._snapshot.source if self._snapshot else None)
        if self._ttl_probe is not None:
            view.update(self._ttl_probe.public_fields(self._last_sync_at, self.token))
        else:
            view.update(public_ttl_fields(self._last_sync_at, self.token))
        return view

    def hydrate_mitm_capture_at(self, captured_at: str | None) -> None:
        if not captured_at:
            return
        try:
            self._last_mitm_capture_at = datetime.fromisoformat(captured_at)
        except (TypeError, ValueError):
            pass

    def hydrate_from_config(
        self,
        token: str,
        profile: ProfileSnapshot,
        *,
        force: bool = False,
        captured_at: str | None = None,
    ) -> None:
        """Load disk config into memory without marking valid."""
        # The template's YOUR_USER_ID is not an account; holding it as one made every capture of
        # the real account look like an account switch and get rejected.
        token = "" if is_placeholder(token) else (token or "").strip()
        profile = profile.without_placeholders()
        if not token and not profile.userid:
            if self._snapshot and not force:
                record('token.config_empty_ignored', token=self.token, userid=self.userid)
                return
            self._snapshot = None
            self._state = TokenState.UNKNOWN
            self._message = "未配置 Token"
            return

        now = datetime.now(timezone.utc)
        if captured_at:
            try:
                now = datetime.fromisoformat(captured_at)
                self._last_sync_at = now
            except (TypeError, ValueError):
                pass
        if self._snapshot and not force:
            if (
                token
                and profile.userid
                and (profile.userid != self._snapshot.userid or token != self._snapshot.token)
            ):
                self._auth_revision += 1
                self._snapshot = AuthSnapshot(
                    token=token,
                    profile=profile,
                    captured_at=now,
                    auth_revision=self._auth_revision,
                    source="config",
                    capture_seq=self._last_accepted_capture_seq,
                )
                self._state = TokenState.UNKNOWN
                self._validation = {}
                self._message = "配置已更新，待验证"
            elif token and self._snapshot.token == token:
                if profile != self._snapshot.profile:
                    self._auth_revision += 1
                    self._snapshot = self._snapshot.with_profile(
                        profile, auth_revision=self._auth_revision
                    )
            return

        self._auth_revision += 1
        self._snapshot = AuthSnapshot(
            token=token,
            profile=profile,
            captured_at=now,
            auth_revision=self._auth_revision,
            source="config",
            capture_seq=0,
        )
        self._state = TokenState.UNKNOWN
        self._message = "已从配置加载，待验证" if token else "未配置 Token"

    def mark_expired(self, reason: str = "鉴权失败") -> None:
        self._auth_failure_epoch += 1
        self._state = TokenState.EXPIRED
        self._validation = {"ok": False, "auth_fail": True, "error_kind": "auth",
                            "message": reason, "school_msg": reason,
                            "checked_at": datetime.now(timezone.utc).isoformat()}
        self._message = reason
        record('token.expired', token=self.token, userid=self.userid, message=reason, auth_fail=True)
        try_record_first_auth_fail(
            self.token,
            auth_fail=True,
            error_kind='auth',
            school_msg=reason,
            userid=self.userid,
            via='expired',
        )
        self._notify_auth_failure()

    def _reject_account_switch(self, userid: str) -> bool:
        if not self._snapshot or not self._snapshot.userid or is_placeholder(self._snapshot.userid):
            return False
        if not userid:
            return False
        return userid != self._snapshot.userid

    def _refresh_active(self) -> bool:
        return (
            self._refresh_task is not None
            and not self._refresh_task.done()
        )

    def _profile_from_event(self, event: CaptureEvent) -> ProfileSnapshot:
        return ProfileSnapshot(
            userid=event.userid or (self._snapshot.userid if self._snapshot else ""),
            customer_id=event.customer_id or (self._snapshot.profile.customer_id if self._snapshot else ""),
            name=event.name or (self._snapshot.profile.name if self._snapshot else ""),
            tel=event.tel or (self._snapshot.profile.tel if self._snapshot else ""),
        )

    def _snapshot_from_event(self, event: CaptureEvent, profile: ProfileSnapshot) -> AuthSnapshot:
        return AuthSnapshot(
            token=event.token,
            profile=profile,
            captured_at=datetime.fromtimestamp(event.captured_at, tz=timezone.utc),
            auth_revision=self._auth_revision,
            source=event.source,
            capture_seq=event.capture_seq,
        )

    async def _persist_snapshot(self, snap: AuthSnapshot) -> str | None:
        if not self._persist:
            return None
        try:
            self._persist(snap)
        except Exception as e:
            self._persist_error = type(e).__name__
            record('token.persist', token=snap.token, userid=snap.userid, ok=False, persist_error=self._persist_error)
            return self._persist_error
        self._persist_error = None
        record('token.persist', token=snap.token, userid=snap.userid, ok=True, source=snap.source)
        return None

    async def _idle_commit_and_validate(self, snap: AuthSnapshot) -> dict[str, Any]:
        async with self._lock:
            self._auth_revision += 1
            pending = AuthSnapshot(
                token=snap.token,
                profile=snap.profile,
                captured_at=snap.captured_at,
                auth_revision=self._auth_revision,
                source=snap.source,
                capture_seq=snap.capture_seq,
            )
            self._snapshot = pending
            self._last_sync_at = pending.captured_at
            self._last_accepted_capture_seq = snap.capture_seq
            self._state = TokenState.UNKNOWN
            self._message = "捕获已写入内存，只读验证中"

        record_sync(
            pending.token,
            pending.captured_at,
            userid=pending.userid,
            source=pending.source,
            auth_revision=pending.auth_revision,
        )
        await self._emit_sync_segment(pending.auth_revision)
        persist_error = await self._persist_snapshot(pending)
        validated = await self.validate_current()
        return {
            "ok": True,
            "mode": "idle_commit",
            "validated": validated,
            "state": self._state.value,
            "token_preview": token_preview(pending.token),
            "persist_error": persist_error,
        }

    async def ingest_capture(self, event: CaptureEvent) -> dict[str, Any]:
        """Accept or turn down one capture, and say which in the log and audit (never the token)."""
        result = await self._ingest_capture(event)
        where = f"seq={event.capture_seq} source={event.source} path={event.path}"
        if result.get("ok"):
            self._last_capture_reject = None
            log.info("capture accepted mode=%s %s", result.get("mode"), where)
        else:
            reason = str(result.get("msg") or "rejected")
            self._last_capture_reject = {
                "at": datetime.now(timezone.utc).isoformat(), "reason": reason, "source": event.source}
            log.warning("capture rejected: %s (%s)", reason, where)
            record("token.capture_rejected", token=event.token, userid=event.userid,
                   reason=reason, source=event.source, path=event.path)
        return result

    async def _ingest_capture(self, event: CaptureEvent) -> dict[str, Any]:
        err = validate_capture_event(event, self.allowed_hosts)
        if err:
            return {"ok": False, "msg": err}
        if event.capture_seq <= self._last_accepted_capture_seq:
            return {"ok": False, "msg": "stale capture sequence"}

        async with self._lock:
            if self._reject_account_switch(event.userid):
                return {"ok": False, "msg": "账号不匹配，已拒绝捕获"}
            if event.profile_revision and self._snapshot:
                if event.profile_revision < self._auth_revision:
                    return {"ok": False, "msg": "profile revision rollback"}

            profile = self._profile_from_event(event)
            snap = self._snapshot_from_event(event, profile)
            current = self._snapshot.token if self._snapshot else ""

            if self._refresh_active():
                if event.capture_seq <= self._refresh_min_seq:
                    return {"ok": False, "msg": "stale capture sequence"}
                self._pending_capture = snap
                self._capture_event.set()
                self._note_mitm_capture(event)
                log.info(
                    "capture queued for refresh seq=%s preview=%s",
                    event.capture_seq,
                    token_preview(event.token),
                )
                return {"ok": True, "mode": "refresh_pending", "capture_seq": event.capture_seq}

            if current and snap.token == current:
                self._last_sync_at = snap.captured_at
                self._snapshot = AuthSnapshot(
                    token=snap.token,
                    profile=snap.profile,
                    captured_at=snap.captured_at,
                    auth_revision=self._snapshot.auth_revision if self._snapshot else self._auth_revision,
                    source=snap.source,
                    capture_seq=event.capture_seq,
                )
                self._last_accepted_capture_seq = event.capture_seq
                self._note_mitm_capture(event)
                log.info("capture reachability seq=%s preview=%s", event.capture_seq, token_preview(snap.token))
                return {"ok": True, "mode": "reachable", "capture_seq": event.capture_seq}

        if not snap.token:
            return {"ok": False, "msg": "empty token"}

        self._note_mitm_capture(event)
        return await self._idle_commit_and_validate(snap)

    async def apply_page_session(
        self,
        *,
        token: str,
        userid: str,
        customer_id: str = "",
        name: str = "",
        tel: str = "",
        allow_account_switch: bool = True,
    ) -> dict[str, Any]:
        token = token.strip()
        userid = userid.strip()
        if not token or not userid:
            return {"ok": False, "msg": "token/userid required"}
        if not allow_account_switch and self._reject_account_switch(userid):
            return {"ok": False, "msg": "运行期间不能切换账号"}

        event = CaptureEvent(
            token=token,
            userid=userid,
            path="/api/blade-app/qywx/getOrderTimeConfigList",
            host=self.allowed_hosts[0],
            source="page",
            capture_seq=next_addon_sequence(),
            captured_at=time.time(),
            customer_id=customer_id,
            name=name,
            tel=tel,
            profile_revision=self._auth_revision + 1,
        )
        async with self._lock:
            if not allow_account_switch and self._reject_account_switch(userid):
                return {"ok": False, "msg": "运行期间不能切换账号"}
            # Only inherit profile fields from the same account.
            prev = self._snapshot.profile if self._snapshot else None
            same_user = bool(prev and prev.userid == userid)
            profile = ProfileSnapshot(userid=userid,
                customer_id=customer_id.strip() or (prev.customer_id if same_user else ''),
                name=name.strip() or (prev.name if same_user else ''),
                tel=tel.strip() or (prev.tel if same_user else ''))
            same_identity = bool(self._snapshot and self.token == token and self.userid == userid)
            snap = AuthSnapshot(token=token, profile=profile,
                captured_at=datetime.now(timezone.utc),
                auth_revision=self._auth_revision if same_identity else self._auth_revision + 1,
                source='page', capture_seq=event.capture_seq)
            self._auth_revision = snap.auth_revision
            self._snapshot = snap
            self._last_sync_at = snap.captured_at
            self._last_accepted_capture_seq = event.capture_seq
            if not same_identity:
                self._state = TokenState.UNKNOWN
                self._validation = {}
                self._message = "页面同步成功，待验证"
        record_sync(token, snap.captured_at, userid=userid, source='page', auth_revision=snap.auth_revision)
        await self._emit_sync_segment(snap.auth_revision)
        if self._state == TokenState.VALID:
            await self._emit_validate_success(snap)
        persist_error = await self._persist_snapshot(snap)
        out = {"ok": True, "state": self._state.value}
        if persist_error:
            out["persist_error"] = persist_error
        return out

    async def validate_current(self) -> bool:
        if self._refresh_active():
            await asyncio.shield(self._refresh_task)
        if not self._snapshot or not self._validate:
            return False
        # Follow a newly synced identity, without treating the old result as its verdict.
        for _ in range(3):
            revision = self._auth_revision
            ok = await asyncio.shield(self.start_validation())
            if revision == self._auth_revision:
                return ok
            if self._state == TokenState.VALID:
                return True
        return False

    def start_validation(self) -> asyncio.Task:
        snap = self._snapshot
        revision = (snap.auth_revision if snap else -1, validation_sport.get())
        task = self._validation_tasks.get(revision)
        if task is None or task.done():
            task = asyncio.create_task(self._validate_and_commit(snap))
            self._validation_tasks[revision] = task
            task.add_done_callback(lambda done: self._validation_tasks.pop(revision, None)
                                   if self._validation_tasks.get(revision) is done else None)
        return task

    async def close(self):
        tasks = list(self._validation_tasks.values())
        if self._refresh_task and not self._refresh_task.done():
            tasks.append(self._refresh_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._state == TokenState.REFRESHING:
            self._state = TokenState.UNKNOWN
            self._message = '刷新已取消，Token 已保留'

    async def _check(self, snap) -> dict:
        try:
            raw = await asyncio.wait_for(self._validate(snap), timeout=12) if self._validate else False
            result = dict(raw) if isinstance(raw, dict) else {'ok': bool(raw), 'auth_fail': False}
        except (TimeoutError, OSError) as exc:
            result = {'ok': False, 'error_kind': 'network', 'error': type(exc).__name__}
        except Exception as exc:
            result = {'ok': False, 'error_kind': 'internal', 'error': type(exc).__name__}
        ok = bool(result.get('ok'))
        kind = 'none' if ok else ('auth' if result.get('auth_fail') else result.get('error_kind', 'query'))
        labels = {'auth': '学校鉴权失败', 'network': '学校连接异常', 'query': '学校查询失败',
                  'config': '验证配置不完整', 'internal': '本机验证异常'}
        detail = redact(result.get('error') or result.get('msg') or '', snap.token)
        out = {'ok': ok, 'auth_fail': kind == 'auth', 'error_kind': kind,
               'message': '学校只读验证通过' if ok else labels.get(kind, '学校查询失败') + ('：' + detail if detail else ''),
               'school_msg': redact(result.get('school_msg', result.get('msg', '')), snap.token),
               'http_status': result.get('http_status'), 'code': redact(result.get('code'), snap.token),
               'checked_at': datetime.now(timezone.utc).isoformat()}
        for key in ('groundId', 'startDate', 'endDate', 'target_date', 'offset_days'):
            if key in result:
                out[key] = result[key]
        return out

    async def _validate_and_commit(self, snap) -> bool:
        if not snap or not snap.token:
            return False
        failure_epoch = self._auth_failure_epoch
        record('token.validate_start', token=snap.token, userid=snap.userid, auth_revision=snap.auth_revision)
        result = await self._check(snap)
        async with self._lock:
            stale = not self._snapshot or self._snapshot.auth_revision != snap.auth_revision
            stale = stale or (not result['auth_fail'] and failure_epoch != self._auth_failure_epoch)
            record('token.validate_result', token=snap.token, userid=snap.userid,
                   auth_revision=snap.auth_revision, stale=stale, **result)
            if not stale:
                try_record_first_auth_fail(
                    snap.token,
                    auth_fail=result.get('auth_fail'),
                    error_kind=result.get('error_kind', ''),
                    school_msg=result.get('school_msg', ''),
                    code=result.get('code', ''),
                    userid=snap.userid,
                    via='validate',
                    auth_revision=snap.auth_revision,
                )
            if stale:
                return False
            self._validation = result
            if result['ok']:
                self._state = TokenState.VALID
                self._last_success_at = datetime.now(timezone.utc)
            elif result['auth_fail']:
                self._auth_failure_epoch += 1
                self._state = TokenState.EXPIRED
            elif self._state == TokenState.VALID and self._last_success_at:
                # Business query/network blips on a readonly probe must not drop a good session.
                pass
            elif self._state == TokenState.EXPIRED:
                # A later network/business error cannot undo a known auth rejection.
                pass
            else:
                self._state = TokenState.UNKNOWN
            self._message = result['message']
        if result['ok']:
            await self._emit_validate_success(snap)
        elif result['auth_fail']:
            self._notify_auth_failure()
        return result['ok']

    async def refresh(
        self,
        trigger: Callable[[], Awaitable[bool]],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        async with self._refresh_gate:
            if self._refresh_task and not self._refresh_task.done():
                task = self._refresh_task
            else:
                self._refresh_task = asyncio.create_task(
                    self._refresh_once(trigger, cancel_event=cancel_event)
                )
                task = self._refresh_task
        try:
            return await asyncio.shield(task)
        finally:
            async with self._refresh_gate:
                if self._refresh_task is task and task.done():
                    self._refresh_task = None

    async def _refresh_once(
        self,
        trigger: Callable[[], Awaitable[bool]],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        if not self._snapshot or not self._snapshot.token:
            return {"ok": False, "msg": "未配置 token", "token_preview": ""}

        previous = self._snapshot.token
        base_revision = self._auth_revision
        last_failure = None
        self._state = TokenState.REFRESHING
        self._last_refresh_at = datetime.now(timezone.utc)
        self._message = "正在刷新 Token"
        self._refresh_generation += 1
        self._refresh_previous_token = previous
        self._refresh_min_seq = self._last_accepted_capture_seq

        for attempt in range(self.max_refresh_attempts):
            if cancel_event and cancel_event.is_set():
                self._state = TokenState.UNKNOWN
                return {"ok": False, "msg": "刷新已取消", "token_preview": token_preview(previous)}

            self._capture_event.clear()
            self._pending_capture = None

            ok = await trigger()
            if not ok:
                self._state = TokenState.FAILED
                self._message = "无法触发页面刷新"
                return {"ok": False, "msg": self._message, "token_preview": token_preview(previous)}

            deadline = time.monotonic() + self.refresh_timeout_sec
            new_snap: AuthSnapshot | None = None
            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    self._state = TokenState.UNKNOWN
                    return {"ok": False, "msg": "刷新已取消", "token_preview": token_preview(previous)}

                pending = self._pending_capture
                if pending and pending.capture_seq > self._refresh_min_seq and pending.token != previous:
                    new_snap = pending
                    break
                try:
                    await asyncio.wait_for(self._capture_event.wait(), timeout=0.2)
                    self._capture_event.clear()
                except asyncio.TimeoutError:
                    pass
                pending = self._pending_capture
                if pending and pending.capture_seq > self._refresh_min_seq and pending.token != previous:
                    new_snap = pending
                    break

            if not new_snap:
                log.warning("refresh attempt %s: no new token before timeout", attempt + 1)
                continue

            if new_snap.token == previous:
                continue

            if self._reject_account_switch(new_snap.userid):
                self._state = TokenState.FAILED
                return {"ok": False, "msg": "捕获账号与当前不一致", "token_preview": token_preview(previous)}

            result = await self._check(new_snap)
            record('token.refresh_validate', token=new_snap.token, userid=new_snap.userid, **result)
            validated = result['ok']
            if not validated:
                last_failure = result
                log.warning("refresh: validation failed for %s", token_preview(new_snap.token))
                continue

            async with self._lock:
                if self._auth_revision != base_revision:
                    return {'ok': False, 'msg': '刷新结果已被较新的登录信息替代'}
                self._auth_revision = new_snap.auth_revision + 1
                self._snapshot = AuthSnapshot(
                    token=new_snap.token,
                    profile=new_snap.profile,
                    captured_at=new_snap.captured_at,
                    auth_revision=self._auth_revision,
                    source=new_snap.source,
                    capture_seq=new_snap.capture_seq,
                )
                self._last_sync_at = new_snap.captured_at
                self._last_accepted_capture_seq = new_snap.capture_seq
                self._state = TokenState.VALID
                self._validation = result
                self._last_success_at = datetime.now(timezone.utc)
                self._message = "Token 刷新并验证成功"
            record_sync(
                new_snap.token,
                new_snap.captured_at,
                userid=new_snap.userid,
                source=new_snap.source,
                auth_revision=self._auth_revision,
                via='refresh',
            )
            await self._emit_sync_segment(self._auth_revision)
            await self._emit_validate_success(self._snapshot)
            persist_error = await self._persist_snapshot(self._snapshot)
            out = {
                "ok": True,
                "msg": "已捕获新 Token",
                "token_preview": token_preview(self._snapshot.token),
                "auth_revision": self._auth_revision,
            }
            if persist_error:
                out["persist_error"] = persist_error
                out["msg"] = "验证成功但配置落盘失败"
            return out

        if self._auth_revision != base_revision:
            return {'ok': False, 'msg': '刷新结果已被较新的登录信息替代'}
        self._state = TokenState.FAILED
        self._message = last_failure['message'] if last_failure else "等待新 Token 超时"
        return {
            "ok": False,
            "msg": self._message,
            "token_preview": token_preview(previous),
        }

    def __repr__(self) -> str:
        return f"TokenManager(state={self._state.value}, preview={token_preview(self.token)})"
