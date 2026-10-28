"""engine.py — 运行时大脑

    维护所有跨调用状态：config / token / VID pool / refresh / auto / mitm 生命周期。
    依赖 utils 提供的稳定原子能力。
"""
from __future__ import annotations

import asyncio
import logging
import random
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from core.watch_registry import SportWatchRegistry

from core.booking_service import BookingService, BookingSource, BookingTarget
from core.rush_engine import RushEngine
from core.order_guard import VidLease
from core.config_store import ConfigStore
from core.safety import dry_run_orders_enabled
from core.token_manager import TokenManager
from core.token_ttl_probe import TokenTtlProbe
from core.validation_display import format_validation_display, validation_fields_from_auth
from models.auth import ProfileSnapshot, token_preview
from network.capture_protocol import (
    ENV_CAPTURE_SECRET,
    ENV_CAPTURE_URL,
    CaptureEvent,
    default_capture_url,
    new_capture_secret,
)
from utils import (
    Court,
    CONFIG_PATH,
    MITM_HOST, MITM_PORT, MITM_UPSTREAM,
    read_config_dict, write_config_string, write_config_int,
    get_vid_once, get_order_once,
    refresh_window,
    slot_covers, classify_order_reply,
    port_listening, spawn_mitmdump,
)


log = logging.getLogger("court")


# ══════════════════════════════════════════════════════
# 可调参数
# ══════════════════════════════════════════════════════
@dataclass
class Tuning:
    refresh_pre_click_delay:  float = 0.05
    refresh_post_click_delay: float = 0.05
    probe_backoff:            float = 1.0
    probe_max_tries:          int   = 6
    auth_refresh_timeout:     float = 4.0
    token_refresh_lead_sec:   int   = 120
    vid_timeout:              float = 3.5
    vid_max_age:              float = 8.0
    vid_pool_size:            int   = 3
    vid_pool_period:          float = 8.0
    mitm_startup_timeout:     float = 12.0
    mitm_shutdown_grace:      float = 3.0
    ttl_probe_enabled:        bool  = True
    ttl_probe_interval_sec:   int   = 90
    ttl_probe_rush_buffer_sec: int  = 600


# ══════════════════════════════════════════════════════
# 自动模式状态
# ══════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════
# 事件总线
# ══════════════════════════════════════════════════════
class EventBus:
    def __init__(self, history: int = 500):
        self._subs: set[asyncio.Queue] = set()
        self._hist: deque = deque(maxlen=history)
        self._seq = 0
        self._auth_revision_fn = None

    @property
    def seq(self) -> int:
        return self._seq

    async def publish(
        self,
        topic: str,
        data: dict | None = None,
        *,
        run_id: str | None = None,
        scan_id: str | None = None,
        date: str | None = None,
        court: int | None = None,
        auth_revision: int | None = None,
    ):
        from core.events import build_envelope

        self._seq += 1
        if auth_revision is None and self._auth_revision_fn is not None:
            try:
                auth_revision = int(self._auth_revision_fn())
            except Exception:
                auth_revision = None
        envelope = build_envelope(
            seq=self._seq,
            topic=topic,
            payload=data or {},
            run_id=run_id,
            scan_id=scan_id,
            date=date,
            court=court,
            auth_revision=auth_revision,
        )
        evt = {"_seq": self._seq, "_topic": topic, "data": envelope}
        self._hist.append(evt)
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass

    def subscribe(self, since: int = 0) -> tuple[asyncio.Queue, list]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q, [e for e in self._hist if e["_seq"] > since]

    def unsubscribe(self, q):
        self._subs.discard(q)


# ══════════════════════════════════════════════════════
# 日志缓冲
# ══════════════════════════════════════════════════════
_log_buf: deque = deque(maxlen=800)
_log_seq = 0


class _BufHandler(logging.Handler):
    def emit(self, record):
        from core.events import sanitize_text

        global _log_seq
        _log_seq += 1
        _log_buf.append({
            "seq": _log_seq,
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "level": record.levelname,
            "msg": sanitize_text(record.getMessage()),
        })


def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    log.addHandler(_BufHandler())
    log.addHandler(logging.StreamHandler())


def get_logs(since: int = 0):
    return [e for e in _log_buf if e["seq"] > since]


# ══════════════════════════════════════════════════════
# Engine
# ══════════════════════════════════════════════════════
class Engine:
    def __init__(self):
        # ── 配置快照 ──
        self.config_data: dict[str, Any] = {}
        self.config_mtime: float = 0.0
        self.tuning = Tuning()

        # ── 派生字段 ──
        self.user_id = ""
        self.customer_id = ""
        self.customer_name = ""
        self.customer_tel = ""
        self.gym_id = ""
        self.gym_name = ""
        self.courts: list[Court] = []
        self.court_by_no: dict[int, Court] = {}
        self.courts_by_sport: dict[str, list[Court]] = {}
        self.tennis_gym_name = ""
        self.tennis_gym_id = ""

        # ── session ──
        self.token = ""
        self.offset_days = 1
        self._validation_sport_id: str | None = None

        # ── runtime ──
        self.bus = EventBus()
        from core.rush_registry import SportRushRegistry
        self.rush = SportRushRegistry(self)

        self._vid_queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        self._vid_tasks: list[asyncio.Task] = []
        self._vid_stats: deque = deque(maxlen=30)

        self._refresh_probe_task: asyncio.Task | None = None
        self._refresh_tasks: list[asyncio.Task] = []

        self._reserve_inflight: set = set()
        self._solve_sem = asyncio.Semaphore(3)

        self._mitm_proc: subprocess.Popen | None = None
        self._mitm_lock: asyncio.Lock | None = None

        self.config_store = ConfigStore()
        self._capture_secret = new_capture_secret()
        self.tokens = TokenManager()
        self.ttl_probe = TokenTtlProbe(
            self,
            observation_check=self.tokens.observation_check,
            current_snapshot=lambda: self.tokens.snapshot,
        )
        self.tokens.set_ttl_probe(self.ttl_probe)
        self.bus._auth_revision_fn = lambda: self.tokens.auth_revision
        self._wire_token_manager()

        self.booking = BookingService(self)
        self.watcher = SportWatchRegistry(self)
        from core.auth_keeper import AuthKeeper
        self.auth_keeper = AuthKeeper(self)
        self._manual_run_id: str | None = None

        self._watch_task: asyncio.Task | None = None
        self._boot_task: asyncio.Task | None = None

        self._matrix_caches: dict[str, dict[str, Any]] = {}

        # 首帧加载
        self.reload_config(force=True)

    def _matrix_bucket(self, sport_id: str) -> dict[str, Any]:
        from core.sports import normalize_sport_id

        sid = normalize_sport_id(sport_id)
        if sid not in self._matrix_caches:
            self._matrix_caches[sid] = {
                "current": None,
                "last_good": None,
                "by_date": {},
            }
        return self._matrix_caches[sid]

    @property
    def _matrix_state(self) -> dict | None:
        return self._matrix_bucket("badminton")["current"]

    @_matrix_state.setter
    def _matrix_state(self, value: dict | None) -> None:
        self._matrix_bucket("badminton")["current"] = value

    @property
    def _matrix_last_good(self) -> dict | None:
        return self._matrix_bucket("badminton")["last_good"]

    @_matrix_last_good.setter
    def _matrix_last_good(self, value: dict | None) -> None:
        self._matrix_bucket("badminton")["last_good"] = value

    @property
    def _matrix_by_date(self) -> dict[str, dict]:
        return self._matrix_bucket("badminton")["by_date"]

    def courts_for_sport(self, sport_id: str | None) -> list[Court]:
        from core.sports import SPORT_BADMINTON, SPORT_TENNIS, normalize_sport_id

        sid = normalize_sport_id(sport_id)
        if sid == SPORT_TENNIS:
            return list(self.courts_by_sport.get(SPORT_TENNIS) or [])
        return list(self.courts)

    def court_for_sport(self, sport_id: str | None, court_no: int) -> Court | None:
        from core.sports import SPORT_BADMINTON, normalize_sport_id

        sid = normalize_sport_id(sport_id)
        if sid == SPORT_BADMINTON:
            return self.court_by_no.get(court_no)
        by_no = {c.no: c for c in self.courts_for_sport(sid)}
        return by_no.get(court_no)

    def sport_for_ground_id(self, ground_id: str) -> str:
        from core.sports import SPORT_BADMINTON, SPORT_TENNIS

        gid = str(ground_id or "").strip()
        for court in self.courts_by_sport.get(SPORT_TENNIS) or []:
            if court.id == gid:
                return SPORT_TENNIS
        return SPORT_BADMINTON

    def venue_for_sport(self, sport_id: str | None) -> tuple[str, str]:
        from core.sports import SPORT_TENNIS, normalize_sport_id

        sid = normalize_sport_id(sport_id)
        if sid == SPORT_TENNIS:
            return (self.tennis_gym_id or "", self.tennis_gym_name or "")
        return (self.gym_id or "", self.gym_name or "")

    def _wire_token_manager(self) -> None:
        self.tokens.configure(
            persist=self._persist_auth_snapshot,
            validate=self._validate_auth_snapshot,
            on_sync_segment=self.ttl_probe.on_sync_segment,
            on_validate_success=self.ttl_probe.on_validate_success,
            can_switch_account=self.can_switch_account,
        )

    def can_switch_account(self) -> bool:
        """All sports and manual orders share one identity, locked while any is active."""
        return not (
            self.rush.running or self.watcher.any_running() or self._reserve_inflight
            or any(lock.locked() for lock in self.booking._locks.values())
        )

    def _token_runtime_settings(self) -> dict:
        raw = self.config_data.get("token", {}) or {}
        timeout = raw.get("refresh_timeout_sec", self.tuning.auth_refresh_timeout)
        attempts = raw.get("max_refresh_attempts", 2)
        net = self.config_data.get("network", {}) or {}
        hosts = net.get("allowed_hosts") or ["reservation.sustech.edu.cn"]
        if isinstance(hosts, str):
            hosts = [hosts]
        return {
            "refresh_timeout_sec": float(timeout),
            "max_refresh_attempts": int(attempts),
            "allowed_hosts": list(hosts),
        }

    def _persist_auth_snapshot(self, snap) -> None:
        from core.clash_capture import is_mitm_capture_source

        # Keep order callers in step with TokenManager before validation yields.
        self._sync_from_token_manager()
        connection: dict[str, str] = {"last_sync_at": snap.captured_at.isoformat()}
        if is_mitm_capture_source(snap.source):
            connection["last_mitm_capture_at"] = snap.captured_at.isoformat()
        sections = {
            "order": {"token": snap.token},
            "connection": connection,
            "user": {
                "id": snap.profile.userid,
                "customer_id": snap.profile.customer_id,
                "name": snap.profile.name,
                "tel": snap.profile.tel,
            },
        }
        self.config_store.update_sections(sections)
        for section, fields in sections.items():
            self.config_data.setdefault(section, {}).update(fields)

    def _validation_matrix_bucket(self) -> dict[str, Any]:
        from core.sports import normalize_sport_id

        from core.auth_context import validation_sport
        sid = normalize_sport_id(validation_sport.get() or getattr(self, "_validation_sport_id", None))
        return self._matrix_bucket(sid)

    def _validation_probe_date(self) -> str:
        """Date for readonly validate: last green matrix day, else nearer readonly window."""
        bucket = self._validation_matrix_bucket()
        last_good = bucket.get("last_good") or {}
        if last_good.get("date"):
            return str(last_good["date"])

        matrix = bucket.get("current") or {}
        matrix_date = matrix.get("date")
        if matrix_date:
            md = str(matrix_date)
            # Matrix may be pinned to the unreleased booking day; prefer a nearer probe.
            if md != self.target_date or self.offset_days < 2:
                return md

        from core.dates import shanghai_midnight

        today = shanghai_midnight().date()
        try:
            booking = datetime.strptime(self.target_date, "%Y-%m-%d").date()
        except ValueError:
            return self.target_date
        if (booking - today).days >= 2:
            return (today + timedelta(days=1)).isoformat()
        return self.target_date

    def _validation_probe_offset_days(self, iso_date: str) -> int | None:
        from core.dates import shanghai_midnight

        try:
            target = datetime.strptime(iso_date, "%Y-%m-%d").date()
        except ValueError:
            return None
        delta = (target - shanghai_midnight().date()).days
        return delta if 0 <= delta <= 2 else None

    def _validation_probe_court(self, date: str) -> Court:
        """Prefer a matrix-scanned court (non-1 when possible), not always courts[0]."""
        from core.sports import normalize_sport_id

        from core.auth_context import validation_sport
        sid = normalize_sport_id(validation_sport.get() or getattr(self, "_validation_sport_id", None))
        bucket = self._validation_matrix_bucket()
        matrix = None
        for candidate in (bucket.get("current"), bucket.get("last_good")):
            if candidate and candidate.get("date") == date:
                matrix = candidate
                break
        if matrix:
            ok_nos = [int(c["court"]) for c in (matrix.get("courts") or []) if c.get("ok")]
            for no in sorted(ok_nos, key=lambda n: (n == 1, n)):
                court = self.court_for_sport(sid, no)
                if court:
                    return court
        courts = self.courts_for_sport(sid)
        for court in courts:
            if court.no != 1:
                return court
        if courts:
            return courts[0]
        for court in self.courts:
            if court.no != 1:
                return court
        return self.courts[0]

    async def _validate_auth_snapshot(self, snap) -> dict:
        if not snap.token or not snap.profile.userid:
            return {"ok": False, "error_kind": "config", "error": "缺少 Token 或 userid"}
        if not self.courts:
            return {"ok": False, "error_kind": "config", "error": "未配置验证场地"}
        date = self._validation_probe_date()
        court = self._validation_probe_court(date)
        result = await get_order_once(
            court, date, snap.token, snap.profile.userid,
        )
        if isinstance(result, dict):
            result.setdefault("target_date", date)
            offset = self._validation_probe_offset_days(date)
            if offset is not None:
                result.setdefault("offset_days", offset)
        return result

    def _sync_from_token_manager(self) -> None:
        snap = self.tokens.snapshot
        self.token = self.tokens.token
        if not snap:
            return
        p = snap.profile
        self.user_id = p.userid
        self.customer_id = p.customer_id
        self.customer_name = p.name
        self.customer_tel = p.tel

    def capture_ipc_env(self) -> dict[str, str]:
        return {
            ENV_CAPTURE_SECRET: self._capture_secret,
            ENV_CAPTURE_URL: default_capture_url(),
        }

    async def ingest_capture_event(self, event: CaptureEvent) -> dict:
        result = await self.tokens.ingest_capture(event)
        if result.get("ok"):
            self._sync_from_token_manager()
            mode = result.get("mode")
            if mode in ("idle_commit", "refresh_pending"):
                topic = "token.updated" if result.get("validated") else "token.failed"
                await self.bus.publish(topic, self.tokens.public_view())
                if mode == "idle_commit":
                    await self.bus.publish("config.updated", self.public_config())
            if mode in ("idle_commit", "refresh_pending", "reachable"):
                await self.bus.publish(
                    "capture.clash",
                    self.clash_capture_state(),
                )
        return result

    # ══════════════════════════════════════════════════
    # 配置
    # ══════════════════════════════════════════════════
    @property
    def target_date(self) -> str:
        d = datetime.now() + timedelta(days=self.offset_days)
        return d.strftime("%Y-%m-%d")

    def reload_config(self, force: bool = False) -> bool:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self.config_mtime:
            return False

        try:
            data = read_config_dict()
        except Exception as e:
            log.warning("config.toml 读取失败: %s", e)
            return False

        self.config_data = data
        self.config_mtime = mtime

        from core.safety import apply_safety_from_config

        apply_safety_from_config(data)

        u = data.get("user", {}) or {}
        g = data.get("gym", {}) or {}
        o = data.get("order", {}) or {}

        self.user_id       = str(u.get("id", "") or "")
        self.customer_id   = str(u.get("customer_id", "") or "")
        self.customer_name = str(u.get("name", "") or "")
        self.customer_tel  = str(u.get("tel", "") or "")
        self.gym_id        = str(g.get("id", "") or "")
        self.gym_name      = str(g.get("name", "") or "")

        courts_map = {int(k): str(v) for k, v in (data.get("courts", {}) or {}).items()}
        self.courts = [Court(no, cid, f"{no}号场")
                       for no, cid in sorted(courts_map.items())]
        self.court_by_no = {c.no: c for c in self.courts}

        from core.sports import (
            SPORT_BADMINTON,
            SPORT_TENNIS,
            load_tennis_courts,
            tennis_gym_id_from_config,
            tennis_gym_name_from_config,
        )

        tennis_courts = load_tennis_courts(data)
        self.tennis_gym_name = tennis_gym_name_from_config(data)
        self.tennis_gym_id = tennis_gym_id_from_config(data)
        self.courts_by_sport = {
            SPORT_BADMINTON: list(self.courts),
            SPORT_TENNIS: tennis_courts,
        }

        self.token = str(o.get("token", "") or "")
        try:
            self.offset_days = int(o.get("offset_days", 1))
        except Exception:
            self.offset_days = 1
        self.offset_days = max(0, min(2, self.offset_days))

        self._load_tuning()

        profile = ProfileSnapshot(
            userid=self.user_id,
            customer_id=self.customer_id,
            name=self.customer_name,
            tel=self.customer_tel,
        )
        settings = self._token_runtime_settings()
        self.tokens.configure(
            refresh_timeout_sec=settings["refresh_timeout_sec"],
            max_refresh_attempts=settings["max_refresh_attempts"],
        )
        self.tokens.allowed_hosts = settings["allowed_hosts"]
        connection = self.config_data.get("connection") or {}
        self.tokens.hydrate_from_config(
            self.token,
            profile,
            captured_at=connection.get("last_sync_at"),
        )
        self.tokens.hydrate_mitm_capture_at(connection.get("last_mitm_capture_at"))
        self._sync_from_token_manager()
        return True

    def _load_tuning(self):
        self.tuning = Tuning()
        raw = self.config_data.get("tuning", {}) or {}
        for k, v in raw.items():
            if not hasattr(self.tuning, k):
                log.warning("config.toml [tuning] 未知字段: %s", k)
                continue
            cur = getattr(self.tuning, k)
            try:
                setattr(self.tuning, k, type(cur)(v))
            except (TypeError, ValueError) as e:
                log.warning("config.toml [tuning].%s 值无效: %r (%s)", k, v, e)

    def public_config(self) -> dict:
        from core.sports import SPORT_TENNIS, public_sports_capabilities

        auth = self.tokens.public_view()
        a = self.config_data.get("auto", {}) or {}
        tennis_courts = self.courts_by_sport.get(SPORT_TENNIS) or []
        return {
            "user": self.config_data.get("user", {}),
            "gym":  self.config_data.get("gym", {}),
            "order": {
                "offset_days": self.offset_days,
                "token_set": auth["token_set"],
                "token_preview": auth["token_preview"],
                "target_date": self.target_date,
            },
            "auth": auth,
            "courts": {str(c.no): c.id for c in self.courts},
            "sport_venues": {
                SPORT_TENNIS: {
                    "gym_id": self.tennis_gym_id,
                    "gym_name": self.tennis_gym_name,
                    "courts": {str(c.no): c.id for c in tennis_courts},
                },
            },
            "auto": {
                "slot_start": a.get("slot_start", "19:30"),
                "slot_end":   a.get("slot_end", "21:30"),
                "refresh_at": a.get("refresh_at", "20:00:00"),
            },
            "safety": {
                "dry_run": dry_run_orders_enabled(),
                "max_order_attempts": int((self.config_data.get("safety") or {}).get("max_order_attempts", 1)),
                "max_orders_per_run": int((self.config_data.get("safety") or {}).get("max_orders_per_run", 1)),
            },
            "sports": public_sports_capabilities(),
        }

    def update_offset_days(self, offset: int) -> None:
        offset = max(0, min(2, int(offset)))
        write_config_int("offset_days", offset)

    def update_token(self, token: str) -> None:
        token = token.strip()
        if token:
            self.config_store.write_string_field("order", "token", token)

    def update_auto_config(
        self,
        slot_start: str,
        slot_end: str,
        refresh_at: str,
        *,
        court_priority: list[int] | None = None,
        sport_id: str | None = None,
    ) -> None:
        write_config_string("slot_start", slot_start.strip())
        write_config_string("slot_end",   slot_end.strip())
        write_config_string("refresh_at", refresh_at.strip())
        rush_patch: dict = {}
        if court_priority is not None:
            courts = [int(c) for c in court_priority if int(c) > 0]
            rush_patch["court_priority"] = courts
        if sport_id is not None:
            from core.sports import normalize_sport_id

            rush_patch["sport_id"] = normalize_sport_id(sport_id)
        if rush_patch:
            self.config_store.update_sections({"rush": rush_patch})


    async def apply_config_change(self):
        self.reload_config(force=True)
        await self.bus.publish("config.updated", self.public_config())

    # ══════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════
    def mitm_capture_enabled(self) -> bool:
        raw = (self.config_data.get("token") or {}).get("capture_mode", "mitm")
        return str(raw).strip().lower() != "page"

    def clash_capture_state(self) -> dict[str, Any]:
        from core.clash_capture import build_clash_capture_state
        from utils import MITM_HOST, MITM_PORT, RESERVATION_HOST

        ms = self.mitm_state()
        return build_clash_capture_state(
            enabled=self.mitm_capture_enabled(),
            mitm=ms,
            last_mitm_capture_at=self.tokens.last_mitm_capture_at,
            last_reject=self.tokens.last_capture_reject,
            host=MITM_HOST,
            port=MITM_PORT,
            reservation_host=RESERVATION_HOST,
        )

    async def start(self):
        log.info("服务已启动")
        from utils import set_rate_limit_publisher

        async def _rate_limited(backoff: float, status: int, message: str):
            await self.bus.publish(
                "rate_limited",
                {"backoff_sec": round(backoff, 1), "http_status": status, "hint": message},
            )
            await self.bus.publish(
                "system.warning",
                {"kind": "rate_limited", "backoff_sec": round(backoff, 1)},
            )

        set_rate_limit_publisher(_rate_limited)
        self._watch_task = asyncio.create_task(self._watch_config())
        self.auth_keeper.start()
        if self.mitm_capture_enabled():
            self._boot_task = asyncio.create_task(self._boot_mitm())

    async def stop(self):
        await self.auth_keeper.close()
        await self.watcher.stop_all()
        for t in (self._watch_task, self._boot_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._reserve_inflight:
            log.info("shutdown: %d reserve task(s) still marked in-flight",
                     len(self._reserve_inflight))
        await self.stop_vid_pool()
        await self.stop_refresh()
        await self.stop_auto()
        await self.ttl_probe.close()
        await self.tokens.close()
        self._kill_mitm()
        from utils import close_http_client

        await close_http_client()

    def _dashboard_watch(self) -> dict[str, Any]:
        by_sport = self.watcher.all_states()
        running_slots = [sid for sid, st in by_sport.items() if st.get("running")]
        primary = by_sport.get("badminton") or {}
        return {
            "by_sport": by_sport,
            "running": bool(running_slots),
            "running_sports": running_slots,
            "phase": primary.get("phase", "idle"),
            "scans": primary.get("scans", 0),
            "run_id": primary.get("run_id"),
        }

    def dashboard_state(self) -> dict[str, Any]:
        auth = self.tokens.public_view()
        matrix = self._matrix_state or {}
        from core.dates import date_matrix_labels

        matrix_date = matrix.get("date")
        matrix_labels = date_matrix_labels(matrix_date) if matrix_date else None
        from core.version import service_identity

        identity = service_identity()
        return {
            "sse_seq": self.bus.seq,
            "service": {key: identity[key] for key in ("version", "commit", "instance_id", "started_at")},
            "safety": {
                "dry_run": dry_run_orders_enabled(),
            },
            "token": {
                "state": auth.get("state"),
                "validated": auth.get("validated"),
                "token_set": auth.get("token_set"),
                "token_preview": auth.get("token_preview"),
            },
            "mitm": self.mitm_state(),
            "clash_capture": self.clash_capture_state(),
            "watch": self._dashboard_watch(),
            "rush": {
                "running": self.rush.running,
                "phase": self.rush.phase,
                "run_id": self.rush.run_id,
                "reason": self.rush.reason,
                "outcome": self.rush.outcome(),
                # Badminton and tennis each have their own Rush; both can run at once.
                "by_sport": {sid: {"running": slot.running, "phase": slot.phase, "outcome": slot.outcome()}
                             for sid, slot in self.rush.slots.items()},
            },
            "matrix": {
                "scan_id": matrix.get("scan_id"),
                "complete": matrix.get("complete"),
                "date": matrix_date,
                "date_labels": matrix_labels,
                "caption": (matrix_labels or {}).get("caption") if matrix_labels else None,
            },
            "order_date": self.target_date,
        }

    async def _watch_config(self, poll: float = 1.0):
        log.info("配置监听已启动: %s", CONFIG_PATH)
        while True:
            try:
                if self.reload_config():
                    log.info("检测到 config.toml 变化: date=%s token=%s courts=%d",
                             self.target_date,
                             token_preview(self.token) if self.token else "空",
                             len(self.courts))
                    await self.bus.publish("config.updated", self.public_config())
            except Exception as e:
                log.warning("配置监听异常: %s", e)
            await asyncio.sleep(poll)

    # ══════════════════════════════════════════════════
    # mitmdump 生命周期（严格单实例：本进程启动，本进程关闭）
    # ══════════════════════════════════════════════════
    def _mitm_lock_get(self) -> asyncio.Lock:
        if self._mitm_lock is None:
            self._mitm_lock = asyncio.Lock()
        return self._mitm_lock

    async def _ensure_mitm(self) -> bool:
        """确保 mitmdump 就绪。

        规则：
            1) 自启进程存活且端口在监听 → 直接 True。
            2) 端口被其他进程占用 → 返回失败。
            3) 未启动 → 启动并等待端口监听。
        """
        async with self._mitm_lock_get():
            # 清理已退出的句柄
            if self._mitm_proc is not None and self._mitm_proc.poll() is not None:
                log.warning("[mitm] 之前启动的 mitmdump 已退出 (code=%s)",
                            self._mitm_proc.returncode)
                self._mitm_proc = None

            # 自启进程仍在监听 → 就绪
            if self._mitm_proc is not None and port_listening(MITM_HOST, MITM_PORT, 0.2):
                log.info("[mitm] ✅ mitmproxy 已就绪：%s:%d", MITM_HOST, MITM_PORT)
                return True

            # Only manage the proxy process owned by this engine.
            if self._mitm_proc is None and port_listening(MITM_HOST, MITM_PORT, 0.2):
                log.error("mitm 端口被其他进程占用，请配置空闲端口")
                return False

            # 未启动或句柄已死 → 拉起
            if self._mitm_proc is None or self._mitm_proc.poll() is not None:
                log.info("[mitm] 正在拉起 mitmdump（%s:%d，无窗口）…",
                         MITM_HOST, MITM_PORT)
                proc = await asyncio.to_thread(spawn_mitmdump, self.capture_ipc_env())
                if proc is None:
                    return False
                self._mitm_proc = proc

            # 等待端口监听
            deadline = time.monotonic() + self.tuning.mitm_startup_timeout
            while time.monotonic() < deadline:
                if self._mitm_proc.poll() is not None:
                    log.error("[mitm] mitmdump 提前退出 (code=%s)",
                              self._mitm_proc.returncode)
                    self._mitm_proc = None
                    return False
                if port_listening(MITM_HOST, MITM_PORT):
                    log.info("[mitm] ✅ mitmproxy 已就绪：%s:%d", MITM_HOST, MITM_PORT)
                    return True
                await asyncio.sleep(0.2)

            log.error("[mitm] %.0fs 内未监听 %d，放弃",
                      self.tuning.mitm_startup_timeout, MITM_PORT)
            return False

    def _kill_mitm(self):
        proc = self._mitm_proc
        self._mitm_proc = None
        if proc is None or proc.poll() is not None:
            return

        log.info("[mitm] 关闭 mitmdump")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=self.tuning.mitm_shutdown_grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as e:
            log.warning("[mitm] 关闭进程异常: %s", e)

    async def _boot_mitm(self):
        try:
            ok = await self._ensure_mitm()
            if not ok:
                log.warning("⚠ mitmdump 未能就绪；点击「自动获取 Token」时会重试")
        except Exception as e:
            log.warning("mitmdump 预启动异常: %s", e)

    def mitm_state(self) -> dict:
        proc = self._mitm_proc
        listening = port_listening(MITM_HOST, MITM_PORT, 0.2)
        owned = proc is not None and proc.poll() is None
        port_conflict = proc is None and listening
        return {
            "host": MITM_HOST,
            "port": MITM_PORT,
            "port_listening": listening,
            "listening": listening,
            "proc_alive": owned,
            "port_conflict": port_conflict,
            "upstream": MITM_UPSTREAM or None,
        }

    async def restart_mitm(self) -> bool:
        self._kill_mitm()
        await asyncio.sleep(0.3)
        return await self._ensure_mitm()

    # ══════════════════════════════════════════════════
    # Token 抓取
    # ══════════════════════════════════════════════════
    async def fetch_token(self, *, allow_page_capture: bool = False) -> dict:
        if self.config_data.get('connection', {}).get('mode') == 'page' and not allow_page_capture:
            return {'ok': False, 'msg': '请在企业微信预约页面切换场馆以同步新登录信息；整页刷新后需重新接入'}

        async def trigger() -> bool:
            ok = await self._ensure_mitm()
            if not ok:
                log.error("❌ mitmdump 未就绪")
                return False
            log.info("🪟 触发预约窗口刷新（Ctrl+R）…")
            return await asyncio.to_thread(
                refresh_window,
                pre_click_delay=self.tuning.refresh_pre_click_delay,
                post_click_delay=self.tuning.refresh_post_click_delay,
            )

        result = await self.tokens.refresh(trigger)
        preview = result.get("token_preview") or token_preview(self.token)
        result.setdefault("token_preview", preview)
        if result.get("ok"):
            self.reload_config(force=True)
            self._sync_from_token_manager()
            await self.bus.publish("token.updated", self.tokens.public_view())
            await self.bus.publish("config.updated", self.public_config())
        else:
            await self.bus.publish("token.failed", self.tokens.public_view())
        return result

    # ══════════════════════════════════════════════════
    # VID 池（只服务自动模式）
    # ══════════════════════════════════════════════════
    @property
    def vid_running(self) -> bool:
        return any(not t.done() for t in self._vid_tasks)

    def vid_count(self) -> int:
        self._prune_vid_queue()
        return self._vid_queue.qsize()

    def _prune_vid_queue(self, max_age=None):
        limit = self.tuning.vid_max_age if max_age is None else max_age
        now = time.monotonic()
        valid = []
        discarded = 0
        while not self._vid_queue.empty():
            item = self._vid_queue.get_nowait()
            if now - item[0] <= limit:
                valid.append(item)
            else:
                discarded += 1
        for item in valid:
            self._vid_queue.put_nowait(item)
        if discarded:
            from core.audit import record
            record("vid.pool_pruned", discarded=discarded, valid_count=len(valid))

    def vid_headroom(self) -> float:
        """Seconds the freshest pooled VID stays valid; 0 when the pool has none."""
        self._prune_vid_queue()
        items = []
        while not self._vid_queue.empty():
            items.append(self._vid_queue.get_nowait())
        for item in items:
            self._vid_queue.put_nowait(item)
        if not items:
            return 0.0
        newest = max(ts for ts, _ in items)
        return max(0.0, float(self.tuning.vid_max_age) - (time.monotonic() - newest))

    def return_vid(self, lease: VidLease) -> None:
        """Put back a VID an order solved but did not use, keeping its original age."""
        if lease.age() > float(self.tuning.vid_max_age):
            return
        self._prune_vid_queue()
        try:
            self._vid_queue.put_nowait((lease.issued_mono, lease.value))
        except asyncio.QueueFull:
            pass

    def vid_state(self) -> dict:
        return {
            "running": self.vid_running,
            "count": self.vid_count(),
            "stats": list(self._vid_stats)[-3:],
        }

    async def start_vid_pool(self, *, rush_owner=None) -> dict:
        if self.watcher.any_running() and not rush_owner:
            return {"ok": False, "msg": "监控期间不运行 VID 池"}
        if self.vid_running:
            return {"ok": True, "started": False, "count": self.vid_count()}

        size = max(1, int(self.tuning.vid_pool_size))
        period = max(1.0, float(self.tuning.vid_pool_period))

        while not self._vid_queue.empty():
            try:
                self._vid_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        stagger = period / size
        self._vid_tasks = [
            asyncio.create_task(self._vid_worker(i + 1, i * stagger, period))
            for i in range(size)
        ]
        log.info("VID 池启动: %d workers / 周期 %.1fs / 有效期 %.1fs / 队列上限 %d",
                 size, period, self.tuning.vid_max_age, self._vid_queue.maxsize)
        return {"ok": True, "started": True, "count": self.vid_count()}

    async def stop_vid_pool(self):
        for t in self._vid_tasks:
            t.cancel()
        if self._vid_tasks:
            await asyncio.gather(*self._vid_tasks, return_exceptions=True)
        self._vid_tasks = []
        log.info("VID 池已停止")

    async def take_vid(self, timeout: float = 5.0,
                       max_age: float | None = None) -> VidLease | None:
        limit = self.tuning.vid_max_age if max_age is None else max_age
        deadline = time.monotonic() + timeout
        while True:
            self._prune_vid_queue(limit)
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            try:
                ts, vid = await asyncio.wait_for(self._vid_queue.get(),
                                                 timeout=remain)
            except asyncio.TimeoutError:
                return None
            age = time.monotonic() - ts
            if age <= limit:
                # Prefer the freshest lease without rejuvenating its original timestamp.
                items = [(ts, vid)]
                while not self._vid_queue.empty():
                    items.append(self._vid_queue.get_nowait())
                items.sort(key=lambda item: item[0])
                ts, vid = items.pop()
                for item in items:
                    self._vid_queue.put_nowait(item)
                self._prune_vid_queue(limit)
                return VidLease(vid, ts)
            log.warning("VID 过期丢弃 (存活 %.1fs > %.1fs)", age, limit)

    async def _vid_worker(self, tid: int, delay: float, period: float):
        from core.request_priority import request_priority
        request_priority.set(4)
        if delay > 0:
            await asyncio.sleep(delay)
        while True:
            try:
                vid, timing = await get_vid_once(timeout=self.tuning.vid_timeout)
                self._vid_stats.append({
                    "ok": True,
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    **timing,
                })
                item = (time.monotonic(), vid)
                self._prune_vid_queue()
                try:
                    self._vid_queue.put_nowait(item)
                except asyncio.QueueFull:
                    try:
                        self._vid_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._vid_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._vid_stats.append({
                    "ok": False,
                    "error": str(e),
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })
                log.warning("VID-ERR [T%d] %s", tid, e)
            await asyncio.sleep(period)

    # ══════════════════════════════════════════════════
    # Refresh（刷新场地）
    # ══════════════════════════════════════════════════
    @property
    def refresh_running(self) -> bool:
        if self._refresh_probe_task and not self._refresh_probe_task.done():
            return True
        return any(not t.done() for t in self._refresh_tasks)

    async def scan_matrix(
        self,
        date: str | None = None,
        *,
        sport_id: str = "badminton",
        concurrency: int = 3,
    ) -> dict:
        from core.availability import scan_all_courts
        from core.sports import normalize_sport_id

        sid = normalize_sport_id(sport_id)
        bucket = self._matrix_bucket(sid)
        courts = self.courts_for_sport(sid)
        if not courts:
            return {"ok": False, "msg": f"未配置 {sid} 场地", "sport_id": sid}

        if not self.token or not self.user_id:
            return {"ok": False, "msg": "未配置 token 或 userid", "sport_id": sid}
        if self.tokens.is_degraded_for_orders():
            auth = self.tokens.public_view()
            fields = validation_fields_from_auth(auth)
            return {
                "ok": False,
                "msg": format_validation_display(auth) or "Token 尚未验证，请先只读复验",
                "validated": False,
                "sport_id": sid,
                **fields,
            }
        target = date or self.target_date
        snap = await scan_all_courts(
            self,
            target,
            courts=courts,
            sport_id=sid,
            concurrency=concurrency,
        )
        public = snap.to_public()
        current = bucket["current"]
        if current and current.get("date") == target:
            public["previous_scan_id"] = current.get("scan_id")
        court_rows = public.get("courts") or []
        has_cells = bool(public.get("times")) or bool(public.get("matrix"))
        auth_failed = any(c.get("auth_fail") for c in court_rows)
        if auth_failed and self.tokens.auth_revision == snap.auth_revision:
            self.tokens.mark_expired('矩阵查询鉴权失败')
        # 仅有格子的结果写入缓存，避免刷新页把空 matrix 当命中
        bucket["current"] = public
        if has_cells:
            bucket["by_date"][target] = public
            if snap.complete:
                bucket["last_good"] = public
        elif target in bucket["by_date"]:
            # 本次失败：清掉该日空/坏缓存，避免 loadMatrixState 再渲染「只有时间」
            bucket["by_date"].pop(target, None)
        await self.bus.publish(
            "matrix.scan",
            public,
            scan_id=str(public.get("scan_id", "")),
            date=str(public.get("date", "")),
        )
        def _first_court_error() -> dict:
            for c in court_rows:
                if c.get("error") or not c.get("ok", True):
                    return c
            return {}

        if auth_failed and not has_cells:
            err = _first_court_error()
            return {
                "ok": False,
                "msg": "鉴权失败，请重跑连接脚本同步",
                "auth_fail": True,
                "error_kind": "auth",
                "school_msg": err.get("school_msg") or "",
                "code": err.get("code") or "",
                **public,
            }
        if not has_cells:
            err = _first_court_error()
            first_err = err.get("error") or "扫描无可用场次格子"
            return {
                "ok": False,
                "msg": first_err,
                "auth_fail": bool(err.get("auth_fail")),
                "error_kind": err.get("error_kind") or "query",
                "school_msg": err.get("school_msg") or "",
                "code": err.get("code") or "",
                **public,
            }
        return {"ok": True, **public}


    def matrix_public_state(self, sport_id: str = "badminton") -> dict:
        from core.sports import normalize_sport_id

        bucket = self._matrix_bucket(normalize_sport_id(sport_id))
        return {
            "sport_id": normalize_sport_id(sport_id),
            "current": bucket["current"],
            "last_good": bucket["last_good"],
            "by_date": bucket["by_date"],
        }

    async def start_refresh(self) -> dict:
        if self.watcher.any_running():
            return {"ok": False, "msg": "监控正在扫描，请查看监控结果"}
        if not self.token:
            return {"ok": False, "msg": "未配置 token"}
        await self.stop_refresh()
        pool = list(self.courts)
        random.shuffle(pool)
        if not pool:
            return {"ok": False, "msg": "config.toml 未配置任何场地"}
        log.info("启动刷新: 探测 %s → 成功后并发剩余 %d 个场地 (退避 %.1fs / 最多 %d 次)",
                 pool[0].name, len(pool) - 1,
                 self.tuning.probe_backoff, self.tuning.probe_max_tries)
        self._refresh_probe_task = asyncio.create_task(self._refresh_run(pool))
        return {"ok": True, "total": len(pool)}

    async def stop_refresh(self):
        tasks = [t for t in ([self._refresh_probe_task] + self._refresh_tasks)
                 if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_probe_task = None
        self._refresh_tasks = []

    async def _refresh_run(self, pool: list[Court]):
        try:
            await self._refresh_probe_then_fanout(pool)
        finally:
            await self.bus.publish("refresh.done",
                                   {"ts": datetime.now().strftime("%H:%M:%S")})

    async def _refresh_probe_then_fanout(self, pool: list[Court]):
        probe, rest = pool[0], pool[1:]
        max_tries = max(1, int(self.tuning.probe_max_tries))
        backoff   = max(0.0, float(self.tuning.probe_backoff))

        ok = False
        for attempt in range(1, max_tries + 1):
            t0 = time.monotonic()
            res = await get_order_once(probe, self.target_date,
                                       self.token, self.user_id)
            dt = (time.monotonic() - t0) * 1000

            if res.get("ok"):
                log.info("探测 %s 成功 (%.0fms, 第 %d 次)，并发剩余 %d 个场地",
                         probe.name, dt, attempt, len(rest))
                await self._publish_refresh(probe, res)
                ok = True
                break

            if res.get("auth_fail"):
                log.warning("探测 %s 鉴权失败 (%.0fms, %d/%d): %s，自动获取 Token…",
                            probe.name, dt, attempt, max_tries,
                            res.get("error", ""))
                try:
                    await self.fetch_token()
                except Exception as e:
                    log.warning("自动获取 Token 异常: %s", e)
            else:
                log.info("探测 %s 失败 (%.0fms, %d/%d): %s",
                         probe.name, dt, attempt, max_tries,
                         res.get("error", ""))

            if backoff > 0:
                await asyncio.sleep(backoff)

        if not ok:
            log.warning("探测 %s %d 次全部失败，放弃本次刷新", probe.name, max_tries)
            await self._publish_refresh(probe, {
                "ok": False,
                "error": f"探测 {max_tries} 次全部失败",
            })
            return

        self._refresh_tasks = [
            asyncio.create_task(self._refresh_one(c)) for c in rest
        ]
        await asyncio.gather(*self._refresh_tasks, return_exceptions=True)

    async def _refresh_one(self, court: Court):
        t0 = time.monotonic()
        res = await get_order_once(court, self.target_date,
                                   self.token, self.user_id)
        dt = (time.monotonic() - t0) * 1000

        if res.get("ok"):
            log.info("%s OK (%.0fms)", court.name, dt)
        else:
            tag = "鉴权失败" if res.get("auth_fail") else "失败"
            log.info("%s %s (%.0fms): %s",
                     court.name, tag, dt, res.get("error", ""))

        await self._publish_refresh(court, res)

    async def _publish_refresh(self, court: Court, res: dict):
        evt = {
            "court": court.no,
            "name":  court.name,
            "ok":    bool(res.get("ok")),
            "ts":    datetime.now().strftime("%H:%M:%S"),
        }
        if res.get("ok"):
            evt["avail"]  = res["avail"]
            evt["blocks"] = res["blocks"]
        else:
            evt["error"] = res.get("error", "查询失败")
        await self.bus.publish("refresh.result", evt)

    # ══════════════════════════════════════════════════
    # 手动下单（不拉起 VID 池）
    # ══════════════════════════════════════════════════
    async def reserve(
        self,
        court_no: int,
        start: str,
        end: str,
        date: str | None = None,
        sport_id: str = "badminton",
    ) -> dict:
        book_date = (date or self.target_date or "").strip() or self.target_date
        if self.watcher.any_running() or self.rush.running:
            return {"accepted": False, "msg": "请先停止自动任务"}
        if self.tokens.is_degraded_for_orders():
            auth = self.tokens.public_view()
            return {
                "accepted": False,
                "kind": "err",
                "label": "❌ 认证未就绪",
                "msg": auth.get("message") or "Token 尚未通过只读验证，无法下单",
            }
        if not self.token:
            return {"accepted": False, "kind": "err",
                    "label": "❌ 未配置 Token",
                    "msg": "请先保存或获取 Token"}

        from core.sports import normalize_sport_id, public_sports_capabilities

        sid = normalize_sport_id(sport_id)
        caps = public_sports_capabilities().get(sid) or {}
        if not caps.get("reserve", True):
            return {
                "accepted": False,
                "kind": "err",
                "label": "❌ 不支持预约",
                "msg": caps.get("stub_message") or f"{sid} 未开放预约",
            }

        court = self.court_for_sport(sid, court_no)
        if not court:
            return {"accepted": False, "kind": "err",
                    "label": "❌ 非法场地",
                    "msg": f"非法场地号 {court_no}（{sid}）"}

        key = (book_date, court.no, start, end)
        if key in self._reserve_inflight:
            return {"accepted": False, "kind": "warn",
                    "label": "⏳ 该时段已在提交",
                    "msg": "同一时段已有请求在飞"}

        self._reserve_inflight.add(key)
        asyncio.create_task(self._reserve_worker(key, court, book_date, start, end))

        return {"accepted": True, "court": court.no, "name": court.name,
                "date": book_date, "start": start, "end": end,
                "dry_run": dry_run_orders_enabled()}

    async def _reserve_worker(self, key, court: Court, book_date: str, start: str, end: str):
        """手动下单 — 统一经 BookingService。"""
        target = BookingTarget(
            court=court, date=book_date, start=start, end=end,
        )
        try:
            async def take_vid() -> str | None:
                if self.vid_running:
                    v = await self.take_vid(timeout=1.5)
                    if v:
                        return v
                return None

            outcome = await self.booking.place_order(
                target,
                source=BookingSource.MANUAL,
                take_vid=take_vid,
            )
            await self.booking.publish_result(target, outcome)
        except Exception as e:
            log.exception("reserve worker 异常: %s", e)
            await self.bus.publish("order.result", {
                "court": court.no, "name": court.name,
                "kind": "error", "label": "❌ 下单异常",
                "msg": str(e),
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
        finally:
            self._reserve_inflight.discard(key)

    # ══════════════════════════════════════════════════
    # Rush（原全自动 /api/auto/*）
    # ══════════════════════════════════════════════════
    @property
    def auto(self):
        """Backward-compatible alias for RushEngine."""
        return self.rush

    def auto_state(self, sport_id=None) -> dict:
        return self.rush.state_dict(sport_id)

    async def start_auto(self, sport_id: str = "badminton", settings=None) -> dict:
        return await self.rush.start_from_config(sport_id=sport_id, settings=settings)

    async def stop_auto(self, sport_id=None):
        await self.rush.stop(sport_id)


# ══════════════════════════════════════════════════════
# 模块级单例
# ══════════════════════════════════════════════════════
engine = Engine()
setup_logging()
