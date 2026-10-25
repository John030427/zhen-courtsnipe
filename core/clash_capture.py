"""Clash → local mitmproxy capture status (observability only; no proxy mutation)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MITM_SOURCES = frozenset({"mitm", "mitm-profile"})


def is_mitm_capture_source(source: str | None) -> bool:
    return bool(source and source in MITM_SOURCES)


def clash_capture_label(
    *,
    enabled: bool,
    listening: bool,
    captured: bool,
    rejected: bool = False,
) -> tuple[str, str]:
    """Return (status_key, Chinese label) for the workbench panel."""
    if not enabled:
        return "disabled", "未启用"
    if rejected:
        return "rejected", "抓到但未采用"
    if captured:
        return "captured", "已抓到"
    if listening:
        return "listening", "监听中"
    return "not_ready", "未就绪"


def build_clash_capture_state(
    *,
    enabled: bool,
    mitm: dict[str, Any],
    last_mitm_capture_at: datetime | None,
    host: str,
    port: int,
    reservation_host: str,
    last_reject: dict[str, str] | None = None,
) -> dict[str, Any]:
    listening = bool(mitm.get("port_listening") or mitm.get("listening"))
    captured = last_mitm_capture_at is not None
    # A turned-down capture matters until a newer one is accepted.
    rejected = bool(last_reject) and (
        not captured or str(last_reject.get("at") or "") > last_mitm_capture_at.isoformat())
    status, label = clash_capture_label(
        enabled=enabled, listening=listening, captured=captured, rejected=rejected
    )
    return {
        "enabled": enabled,
        "status": status,
        "label": label,
        "listening": listening,
        "captured": captured,
        "last_capture_at": (
            last_mitm_capture_at.isoformat() if last_mitm_capture_at else None
        ),
        "mitm_host": host,
        "mitm_port": port,
        "reservation_host": reservation_host,
        "last_reject_at": (last_reject or {}).get("at") if rejected else None,
        "last_reject_reason": (last_reject or {}).get("reason") if rejected else None,
        "port_conflict": bool(mitm.get("port_conflict")),
        "proc_alive": bool(mitm.get("proc_alive")),
        "setup": {
            "clash_mode": "Rule（规则）",
            "domain_rule": f"DOMAIN,{reservation_host},SUSTech-MITM",
            "mitm_upstream": f"http://{host}:{port}",
            "prepare_cmd": "python prepare-capture.py --output-dir .local-capture",
            "ca_hint": "信任 mitmproxy 根证书（见 README_zh.md § mitm CA）；勿开启全局系统代理",
        },
    }
