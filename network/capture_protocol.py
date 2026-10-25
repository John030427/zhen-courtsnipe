from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from utils import RESERVATION_HOST

log = logging.getLogger("court")

CAPTURE_AUTH_HEADER = "X-Sustech-Capture-Key"
ENV_CAPTURE_URL = "SUSTECH_CAPTURE_URL"
ENV_CAPTURE_SECRET = "SUSTECH_CAPTURE_SECRET"
DEFAULT_CAPTURE_PATH = "/api/internal/capture"

# saveOrder is listed only so MITM can read token/userid from query params on order
# traffic; we never submit orders from the capture path.
ALLOWED_PATH_PREFIXES = (
    "/api/blade-app/qywx/getOrderTimeConfigList",
    "/api/blade-app/qywx/saveOrder",
    "/api/blade-app/qywx/getUserInfoByUserid",
)


def new_capture_secret() -> str:
    return secrets.token_urlsafe(32)


def default_capture_url(app_port: int | None = None) -> str:
    port = app_port or int(os.environ.get("SUSTECH_APP_PORT", "8766"))
    return f"http://127.0.0.1:{port}{DEFAULT_CAPTURE_PATH}"


def is_allowed_capture_path(path: str) -> bool:
    base = path.split("?", 1)[0]
    return any(base == p or base.startswith(p + "/") for p in ALLOWED_PATH_PREFIXES)


@dataclass(frozen=True)
class CaptureEvent:
    token: str
    userid: str
    path: str
    host: str
    source: str
    capture_seq: int
    captured_at: float
    customer_id: str = ""
    name: str = ""
    tel: str = ""
    profile_revision: int = 0

    def profile_fields(self) -> dict[str, str]:
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "tel": self.tel,
        }


class CaptureQueue:
    """Bounded queue for addon -> main process handoff (thread-safe)."""

    def __init__(self, maxsize: int = 64):
        self._q: queue.Queue[CaptureEvent] = queue.Queue(maxsize=maxsize)

    def offer(self, event: CaptureEvent) -> bool:
        try:
            self._q.put_nowait(event)
            return True
        except queue.Full:
            log.warning("capture queue full, dropping event seq=%s", event.capture_seq)
            return False

    def drain(self) -> list[CaptureEvent]:
        items: list[CaptureEvent] = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items


_addon_seq_lock = threading.Lock()
_addon_seq = 0


def next_addon_sequence() -> int:
    """Increasing across processes, not only within one: the workbench rejects a number it has
    already passed, so a mitmdump restarted under a running workbench must not count from 1."""
    global _addon_seq
    with _addon_seq_lock:
        _addon_seq = max(_addon_seq + 1, time.time_ns() // 1000)  # microseconds, < 2**53
        return _addon_seq


def build_capture_payload(
    *,
    token: str,
    userid: str,
    path: str,
    host: str = RESERVATION_HOST,
    source: str = "mitm",
    customer_id: str = "",
    name: str = "",
    tel: str = "",
    profile_revision: int = 0,
) -> dict[str, Any]:
    return {
        "token": token,
        "userid": userid,
        "path": path,
        "host": host,
        "source": source,
        "capture_seq": next_addon_sequence(),
        "captured_at": time.time(),
        "customer_id": customer_id,
        "name": name,
        "tel": tel,
        "profile_revision": profile_revision,
    }


def parse_capture_payload(data: dict[str, Any]) -> CaptureEvent | None:
    try:
        token = str(data.get("token") or "").strip()
        userid = str(data.get("userid") or "").strip()
        path = str(data.get("path") or "")
        host = str(data.get("host") or "")
        if not token or not path or not host:
            return None
        seq = int(data.get("capture_seq") or 0)
        if seq <= 0:
            return None
        return CaptureEvent(
            token=token,
            userid=userid,
            path=path,
            host=host,
            source=str(data.get("source") or "mitm"),
            capture_seq=seq,
            captured_at=float(data.get("captured_at") or time.time()),
            customer_id=str(data.get("customer_id") or ""),
            name=str(data.get("name") or ""),
            tel=str(data.get("tel") or ""),
            profile_revision=int(data.get("profile_revision") or 0),
        )
    except (TypeError, ValueError):
        return None


def verify_capture_secret(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


def validate_capture_event(event: CaptureEvent, allowed_hosts: list[str]) -> str | None:
    if event.host not in allowed_hosts:
        return "host not allowed"
    if not is_allowed_capture_path(event.path):
        return "path not allowed"
    return None


def _sanitize_ipc_detail(message: str, secret: str) -> str:
    out = message
    if secret:
        out = out.replace(secret, "***")
    if len(out) > 240:
        out = out[:237] + "..."
    return out


def post_capture_sync(
    payload: dict[str, Any],
    *,
    url: str | None = None,
    secret: str | None = None,
    timeout: float = 2.0,
    retries: int = 2,
) -> tuple[bool, str]:
    """POST capture event to workbench loopback IPC. Returns (ok, detail on failure)."""
    target = url or os.environ.get(ENV_CAPTURE_URL) or default_capture_url()
    key = secret or os.environ.get(ENV_CAPTURE_SECRET) or ""
    if not key:
        return False, "missing capture secret"
    headers = {CAPTURE_AUTH_HEADER: key, "Content-Type": "application/json"}
    last_detail = ""
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                r = client.post(target, headers=headers, json=payload)
                if r.status_code == 200:
                    # 200 only means the workbench answered; it can still turn the capture
                    # down (another account, an old sequence number). Not worth a retry.
                    try:
                        reply = r.json()
                    except ValueError:
                        reply = None
                    if isinstance(reply, dict) and reply.get("ok") is False:
                        reason = _sanitize_ipc_detail(str(reply.get("msg") or "rejected"), key)
                        return False, f"工作台未采用：{reason}"
                    return True, ""
                body = (r.text or "").strip()
                if body:
                    body = _sanitize_ipc_detail(body, key)
                    last_detail = f"HTTP {r.status_code}: {body[:120]}"
                else:
                    last_detail = f"HTTP {r.status_code}"
        except httpx.TimeoutException:
            last_detail = "timeout"
        except httpx.ConnectError as e:
            last_detail = f"connect error: {type(e).__name__}"
        except Exception as e:
            last_detail = f"{type(e).__name__}: {_sanitize_ipc_detail(str(e), key)}"
        time.sleep(0.15 * (attempt + 1))
    detail = _sanitize_ipc_detail(last_detail or "unknown", key)
    log.warning("capture IPC failed: %s", detail)
    return False, detail
