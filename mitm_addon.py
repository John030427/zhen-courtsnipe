"""mitm_addon.py — mitmproxy 入口。

request: 精确 host/path 采集 token/userid，通过本机 IPC 通知主进程。
response: getUserInfoByUserid 资料同样走 IPC，不写 config.toml。
response: 只读记录 /api/blade-app/qywx/*（脱敏），便于对比网球确认页与羽毛球下单。

手工测试窗口刷新：
    python mitm_addon.py refresh
"""
from __future__ import annotations

import json
import sys
import time

from core.venue_config_probe import (
    VENUE_PROBE_PATHS,
    format_qywx_console_line,
    is_qywx_api_path,
    log_order_time_config_from_mitm,
    log_qywx_traffic_from_mitm,
    qywx_console_fields_from_mitm,
)
from utils import RESERVATION_HOST, capture_token_from_flow

PROFILE_API = "/api/blade-app/qywx/getUserInfoByUserid"


def request(flow) -> None:  # type: ignore[no-untyped-def]
    capture_token_from_flow(flow)


def response(flow) -> None:  # type: ignore[no-untyped-def]
    try:
        if flow.request.pretty_host != RESERVATION_HOST:
            return
        path = flow.request.path.split("?", 1)[0]
        if path in VENUE_PROBE_PATHS:
            try:
                log_order_time_config_from_mitm(flow, path=path)
                ts = time.strftime("%H:%M:%S")
                fields = qywx_console_fields_from_mitm(flow, path)
                print(f"[{ts}] [VENUE] {format_qywx_console_line(path, fields)}")
            except Exception as e:
                print(f"[VENUE] 时段配置采集失败: {e}", file=sys.stderr)
            return
        if path == PROFILE_API:
            _handle_profile(flow, path)
            return
        if is_qywx_api_path(path):
            try:
                log_qywx_traffic_from_mitm(flow, path=path)
                ts = time.strftime("%H:%M:%S")
                fields = qywx_console_fields_from_mitm(flow, path)
                print(f"[{ts}] [QYWX] {format_qywx_console_line(path, fields)}")
            except Exception as e:
                print(f"[QYWX] 采集失败 {path}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[mitm] response 处理失败: {e}", file=sys.stderr)


def _handle_profile(flow, path: str) -> None:
    if not flow.response or flow.response.status_code != 200:
        return
    try:
        payload = flow.response.json()
    except Exception:
        payload = json.loads(flow.response.get_text())
    if not isinstance(payload, dict) or not payload.get("success"):
        return
    data = payload.get("data")
    if not isinstance(data, dict):
        return

    token = flow.request.query.get("token") or data.get("token") or ""
    userid = data.get("code") or flow.request.query.get("userid") or ""
    if not token:
        return

    from network.capture_protocol import build_capture_payload, post_capture_sync

    event = build_capture_payload(
        token=str(token),
        userid=str(userid),
        path=path,
        host=RESERVATION_HOST,
        source="mitm-profile",
        customer_id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        tel=str(data.get("tel") or ""),
        profile_revision=int(time.time()),
    )
    ok, ipc_detail = post_capture_sync(event)
    preview = f"{str(token)[:8]}..."
    if ok:
        print(
            f"[{time.strftime('%H:%M:%S')}] [PROFILE] IPC ok id={userid} token={preview}"
        )
    else:
        reason = f" ({ipc_detail})" if ipc_detail else ""
        what = "IPC rejected" if ipc_detail.startswith("工作台未采用") else "IPC failed"
        print(f"[PROFILE] {what} id={userid}{reason}", file=sys.stderr)


if __name__ == "__main__":
    from utils import refresh_window

    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_window()
    else:
        print("用法：")
        print("  python mitm_addon.py refresh")
