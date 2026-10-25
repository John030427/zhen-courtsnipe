"""Background readonly TTL probe after a successful token sync (observation only)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from core.audit import record
from core.token_ttl_obs import (
    is_auth_failure_signal,
    public_ttl_fields,
    try_record_first_auth_fail,
)
from models.auth import AuthSnapshot, token_preview

if TYPE_CHECKING:
    from engine import Engine

log = logging.getLogger("court")

CheckFn = Callable[[AuthSnapshot], Awaitable[dict]]
SnapshotFn = Callable[[], AuthSnapshot | None]
ReleaseAtFn = Callable[[], datetime | None]


class TokenTtlProbe:
    """Periodically re-runs readonly validation until auth failure, then records ttl_sec."""

    def __init__(
        self,
        engine: Engine,
        *,
        observation_check: CheckFn,
        current_snapshot: SnapshotFn,
    ):
        self._engine = engine
        self._observation_check = observation_check
        self._current_snapshot = current_snapshot
        self._task: asyncio.Task | None = None
        self._segment_revision: int | None = None
        self._active = False
        self._stop_reason = ""

    def configure(self, engine: Engine | None = None) -> None:
        if engine is not None:
            self._engine = engine

    def _enabled(self) -> bool:
        return bool(getattr(self._engine.tuning, "ttl_probe_enabled", True))

    def _interval_sec(self) -> float:
        raw = int(getattr(self._engine.tuning, "ttl_probe_interval_sec", 90))
        return float(max(60, min(180, raw)))

    def _rush_buffer_sec(self) -> float:
        raw = int(getattr(self._engine.tuning, "ttl_probe_rush_buffer_sec", 600))
        return float(max(300, min(3600, raw)))

    def _next_release_at(self) -> datetime | None:
        rush = self._engine.rush
        if hasattr(rush, "slots") and isinstance(rush.slots, dict):
            releases = [datetime.fromisoformat(slot.plan["release_at"])
                        for slot in rush.slots.values() if slot.running and slot.plan.get("release_at")]
            if releases:
                return min(releases)
        if rush.running and rush.plan.get("release_at"):
            try:
                return datetime.fromisoformat(str(rush.plan["release_at"]))
            except (TypeError, ValueError):
                pass
        from models.rush import CHINA, RushPlan

        try:
            plan = RushPlan.from_config(
                self._engine.config_data,
                target_date=self._engine.target_date,
                now=datetime.now(CHINA),
            )
            now_cn = datetime.now(CHINA)
            if plan.release_at > now_cn:
                return plan.release_at
        except (ValueError, TypeError):
            pass
        return None

    def _inside_rush_buffer(self) -> bool:
        release = self._next_release_at()
        if release is None:
            return False
        from models.rush import CHINA

        now = datetime.now(CHINA)
        remain = (release - now).total_seconds()
        return remain <= self._rush_buffer_sec()

    def public_fields(self, last_sync_at: datetime | None, token: str) -> dict[str, Any]:
        base = public_ttl_fields(last_sync_at, token)
        if not self._enabled():
            status = "未知"
        elif self._active:
            status = "探针中"
        elif base.get("estimated_ttl_sec") is not None:
            status = "已估出"
        else:
            status = "未知"
        base["ttl_probe_status"] = status
        base["ttl_probe_enabled"] = self._enabled()
        if self._stop_reason and not self._active:
            base["ttl_probe_stop_reason"] = self._stop_reason
        return base

    async def on_sync_segment(self, auth_revision: int) -> None:
        await self.stop(reason="resync")
        self._segment_revision = auth_revision
        self._stop_reason = ""

    async def on_validate_success(self, snap: AuthSnapshot) -> None:
        if not self._enabled() or not snap.token:
            return
        if self._segment_revision is None:
            return
        if snap.auth_revision != self._segment_revision:
            return
        await self._start_loop(snap.auth_revision)

    async def stop(self, *, reason: str = "") -> None:
        self._active = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if reason:
            self._stop_reason = reason

    async def close(self) -> None:
        await self.stop(reason="shutdown")

    async def _start_loop(self, auth_revision: int) -> None:
        await self.stop()
        self._segment_revision = auth_revision
        self._active = True
        self._stop_reason = ""
        snap = self._current_snapshot()
        record(
            "token.ttl_probe",
            token=snap.token if snap else "",
            userid=snap.userid if snap else "",
            action="start",
            auth_revision=auth_revision,
            interval_sec=self._interval_sec(),
        )
        self._task = asyncio.create_task(self._run(auth_revision), name="token-ttl-probe")

    async def _run(self, auth_revision: int) -> None:
        interval = self._interval_sec()
        backoff = interval
        try:
            while True:
                if not self._enabled():
                    await self._finish_segment("disabled", auth_revision)
                    return
                if self._inside_rush_buffer():
                    await self._finish_segment("rush_buffer", auth_revision)
                    return
                snap = self._current_snapshot()
                if not snap or snap.auth_revision != auth_revision:
                    await self._finish_segment("stale_identity", auth_revision)
                    return
                await asyncio.sleep(backoff)
                if self._inside_rush_buffer():
                    await self._finish_segment("rush_buffer", auth_revision)
                    return
                snap = self._current_snapshot()
                if not snap or snap.auth_revision != auth_revision:
                    await self._finish_segment("stale_identity", auth_revision)
                    return
                result = await self._observation_check(snap)
                if is_auth_failure_signal(
                    auth_fail=bool(result.get("auth_fail")),
                    error_kind=str(result.get("error_kind") or ""),
                    school_msg=str(result.get("school_msg") or ""),
                ):
                    try_record_first_auth_fail(
                        snap.token,
                        auth_fail=True,
                        error_kind=result.get("error_kind", "auth"),
                        school_msg=result.get("school_msg", ""),
                        code=result.get("code", ""),
                        userid=snap.userid,
                        via="ttl_probe",
                        auth_revision=auth_revision,
                    )
                    await self._finish_segment("auth_fail", auth_revision)
                    await self._publish_auth_view()
                    return
                if result.get("ok"):
                    backoff = interval
                    continue
                kind = str(result.get("error_kind") or "query")
                if kind in ("network", "query", "internal"):
                    backoff = min(180.0, max(interval, backoff * 1.5))
                    log.info(
                        "ttl probe backoff %ss after %s (revision=%s)",
                        int(backoff),
                        kind,
                        auth_revision,
                    )
                    continue
                backoff = interval
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ttl probe crashed revision=%s", auth_revision)
            await self._finish_segment("error", auth_revision)
        finally:
            self._active = False

    async def _finish_segment(self, reason: str, auth_revision: int) -> None:
        self._active = False
        self._stop_reason = reason
        snap = self._current_snapshot()
        record(
            "token.ttl_probe",
            token=snap.token if snap else "",
            userid=snap.userid if snap else "",
            action="stop",
            reason=reason,
            auth_revision=auth_revision,
        )
        await self._publish_auth_view()

    async def _publish_auth_view(self) -> None:
        try:
            await self._engine.bus.publish("token.updated", self._engine.tokens.public_view())
        except Exception:
            log.exception("ttl probe publish failed")
