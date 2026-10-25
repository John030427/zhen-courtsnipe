"""Read-only, desensitized logging for school venue / time-slot API traffic."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.audit import record
from utils import norm_hm

ORDER_TIME_CONFIG_PATH = "/api/blade-app/qywx/getOrderTimeConfigList"
SAVE_ORDER_PATH = "/api/blade-app/qywx/saveOrder"
QYWX_API_PREFIX = "/api/blade-app/qywx/"

# Detailed slot-matrix probe (response JSON shape).
VENUE_PROBE_PATHS = frozenset(
    {
        ORDER_TIME_CONFIG_PATH,
    }
)

_PROFILE_API = "/api/blade-app/qywx/getUserInfoByUserid"


def _query_map(flow) -> dict[str, str]:
    try:
        q = flow.request.query
        if hasattr(q, "get"):
            return {k: (q.get(k) or "") for k in ("groundId", "gymId", "startDate", "endDate", "userid")}
    except Exception:
        pass
    try:
        parsed = urlparse(flow.request.url)
        raw = parse_qs(parsed.query, keep_blank_values=True)
        return {k: (raw.get(k, [""])[0] or "") for k in ("groundId", "gymId", "startDate", "endDate", "userid")}
    except Exception:
        return {}


def _minutes_between(start: str, end: str) -> int | None:
    try:
        sh, sm = (int(x) for x in start.split(":", 1))
        eh, em = (int(x) for x in end.split(":", 1))
        delta = (eh * 60 + em) - (sh * 60 + sm)
        return delta if delta > 0 else None
    except (TypeError, ValueError):
        return None


def _slot_step_minutes(blocks: list[dict[str, Any]]) -> int | None:
    steps: list[int] = []
    for blk in blocks:
        t = norm_hm(str(blk.get("time") or ""))
        e = norm_hm(str(blk.get("endTime") or ""))
        if not t or not e:
            continue
        m = _minutes_between(t, e)
        if m:
            steps.append(m)
    if not steps:
        return None
    return min(steps)


def _safe_blocks(blocks: list[dict[str, Any]], limit: int = 8) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for blk in blocks[:limit]:
        if not isinstance(blk, dict):
            continue
        out.append(
            {
                "time": norm_hm(str(blk.get("time") or "")),
                "endTime": norm_hm(str(blk.get("endTime") or "")),
                "status": str(blk.get("status") or ""),
            }
        )
    return out


def summarize_order_time_config(
    *,
    ground_id: str = "",
    gym_id: str = "",
    start_date: str = "",
    end_date: str = "",
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a compact, PII-free summary from getOrderTimeConfigList JSON."""
    out: dict[str, Any] = {
        "groundId": ground_id or "",
        "gymId": gym_id or "",
        "startDate": start_date or "",
        "endDate": end_date or "",
        "ok": False,
        "config_dates": [],
        "slot_step_min": None,
        "blocks_sample": [],
        "block_count": 0,
        "gymName": "",
        "groundName": "",
        "school_msg": "",
    }
    if not isinstance(payload, dict):
        out["error_kind"] = "parse"
        return out

    if not payload.get("success"):
        out["error_kind"] = "school"
        out["school_msg"] = str(payload.get("msg") or payload.get("message") or "")[:200]
        return out

    data = payload.get("data")
    if not isinstance(data, dict):
        out["error_kind"] = "shape"
        return out

    out["gymId"] = str(data.get("gymId") or gym_id or "")
    out["gymName"] = str(data.get("gymName") or "")
    out["groundId"] = str(data.get("groundId") or ground_id or "")
    out["groundName"] = str(data.get("groundName") or "")

    configs = data.get("configList")
    if not isinstance(configs, list):
        out["error_kind"] = "shape"
        return out

    dates: list[str] = []
    all_blocks: list[dict[str, Any]] = []
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        d = str(cfg.get("date") or "")
        if d:
            dates.append(d)
        tlist = cfg.get("timeBlockList")
        if not isinstance(tlist, list):
            continue
        for blk in tlist:
            if isinstance(blk, dict):
                all_blocks.append(blk)

    out["config_dates"] = dates
    out["block_count"] = len(all_blocks)
    out["slot_step_min"] = _slot_step_minutes(all_blocks)
    out["blocks_sample"] = _safe_blocks(all_blocks)
    out["ok"] = True
    return out


def log_order_time_config_from_mitm(flow, *, path: str = ORDER_TIME_CONFIG_PATH) -> None:
    """Log desensitized venue probe from a mitmproxy flow (response phase)."""
    q = _query_map(flow)
    token = ""
    try:
        token = str(flow.request.query.get("token") or "")
    except Exception:
        pass

    payload: dict[str, Any] | None = None
    if flow.response and flow.response.status_code == 200:
        try:
            payload = flow.response.json()
        except Exception:
            try:
                import json as _json

                payload = _json.loads(flow.response.get_text())
            except Exception:
                payload = None

    summary = summarize_order_time_config(
        ground_id=q.get("groundId", ""),
        gym_id=q.get("gymId", ""),
        start_date=q.get("startDate", ""),
        end_date=q.get("endDate", ""),
        payload=payload if isinstance(payload, dict) else None,
    )
    _emit_probe(
        token=token,
        userid=q.get("userid", ""),
        path=path,
        via="mitm",
        http_status=flow.response.status_code if flow.response else None,
        **summary,
    )


def log_order_time_config_from_server(
    *,
    token: str,
    userid: str,
    ground_id: str,
    start_date: str,
    end_date: str,
    payload: dict[str, Any] | None,
    http_status: int | None = None,
) -> None:
    summary = summarize_order_time_config(
        ground_id=ground_id,
        start_date=start_date,
        end_date=end_date,
        payload=payload,
    )
    _emit_probe(
        token=token,
        userid=userid,
        path=ORDER_TIME_CONFIG_PATH,
        via="server",
        http_status=http_status,
        **summary,
    )


def is_qywx_api_path(path: str) -> bool:
    base = path.split("?", 1)[0]
    return base.startswith(QYWX_API_PREFIX) and base != _PROFILE_API


def _request_json(flow) -> dict[str, Any] | None:
    try:
        raw = flow.request.get_text()
        if not raw:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _date_from_datetime_field(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _hm_from_datetime_field(value: str) -> str:
    text = str(value or "").strip()
    if " " in text:
        text = text.split(" ", 1)[1]
    if ":" not in text:
        return ""
    parts = text.split(":")
    if len(parts) >= 2:
        text = f"{parts[0]}:{parts[1]}"
    return norm_hm(text)


def summarize_save_order_request(
    *,
    query: dict[str, str],
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    """Desensitized saveOrder request fields (no PII / secrets)."""
    b = body or {}
    start_raw = str(b.get("startTime") or b.get("tmpStartTime") or "")
    end_raw = str(b.get("endTime") or b.get("tmpEndTime") or "")
    start_hm = _hm_from_datetime_field(start_raw)
    end_hm = _hm_from_datetime_field(end_raw)
    blocks: list[dict[str, str]] = []
    if start_hm and end_hm:
        blocks.append({"time": start_hm, "endTime": end_hm, "status": "req"})
    return {
        "groundId": str(b.get("groundId") or query.get("groundId") or ""),
        "gymId": str(b.get("gymId") or query.get("gymId") or ""),
        "startDate": _date_from_datetime_field(start_raw) or query.get("startDate", ""),
        "endDate": query.get("endDate", "") or _date_from_datetime_field(end_raw),
        "startTime": start_hm,
        "endTime": end_hm,
        "blocks_sample": blocks,
        "block_count": len(blocks),
    }


def summarize_qywx_generic(
    *,
    path: str,
    query: dict[str, str],
    response_payload: dict[str, Any] | None,
    http_status: int | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": path,
        "groundId": query.get("groundId", ""),
        "gymId": query.get("gymId", ""),
        "startDate": query.get("startDate", ""),
        "endDate": query.get("endDate", ""),
        "http_status": http_status,
        "response_ok": None,
        "school_msg": "",
    }
    if isinstance(response_payload, dict):
        if "success" in response_payload:
            out["response_ok"] = bool(response_payload.get("success"))
        out["school_msg"] = str(
            response_payload.get("msg") or response_payload.get("message") or ""
        )[:200]
    return out


def log_qywx_traffic_from_mitm(flow, *, path: str) -> None:
    """Read-only desensitized log for qywx/* (excludes profile + order-time probe)."""
    q = _query_map(flow)
    token = ""
    try:
        token = str(flow.request.query.get("token") or "")
    except Exception:
        pass

    response_payload: dict[str, Any] | None = None
    http_status = flow.response.status_code if flow.response else None
    if flow.response and flow.response.status_code == 200:
        try:
            response_payload = flow.response.json()
        except Exception:
            try:
                response_payload = json.loads(flow.response.get_text())
            except Exception:
                response_payload = None
        if not isinstance(response_payload, dict):
            response_payload = None

    if path == SAVE_ORDER_PATH:
        fields = summarize_save_order_request(query=q, body=_request_json(flow))
        event = "qywx.save_order_probe"
    else:
        fields = summarize_qywx_generic(
            path=path,
            query=q,
            response_payload=response_payload,
            http_status=http_status,
        )
        event = "qywx.traffic"

    blocks_sample = fields.pop("blocks_sample", None)
    block_count = fields.pop("block_count", None)
    kwargs: dict[str, Any] = {
        "token": token,
        "userid": q.get("userid", ""),
        "path": path,
        "via": "mitm",
        "http_status": http_status,
        **{k: v for k, v in fields.items() if v is not None and k != "path"},
    }
    if blocks_sample is not None:
        kwargs["blocks_sample"] = json.dumps(blocks_sample, ensure_ascii=False)
    if block_count is not None:
        kwargs["block_count"] = block_count
    record(event, **kwargs)


def qywx_console_fields_from_mitm(flow, path: str) -> dict[str, Any]:
    q = _query_map(flow)
    if path == SAVE_ORDER_PATH:
        return summarize_save_order_request(query=q, body=_request_json(flow))
    payload: dict[str, Any] | None = None
    if flow.response and flow.response.status_code == 200:
        try:
            payload = flow.response.json()
        except Exception:
            try:
                payload = json.loads(flow.response.get_text())
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            payload = None
    return summarize_qywx_generic(
        path=path,
        query=q,
        response_payload=payload,
        http_status=flow.response.status_code if flow.response else None,
    )


def format_qywx_console_line(path: str, fields: dict[str, Any]) -> str:
    """Single-line mitm stdout hint (no secrets)."""
    gid = fields.get("groundId") or ""
    gym = fields.get("gymId") or ""
    sd = fields.get("startDate") or ""
    ed = fields.get("endDate") or ""
    st = fields.get("startTime") or ""
    et = fields.get("endTime") or ""
    slot = f"{st}-{et}" if st and et else ""
    tail = path.rsplit("/", 1)[-1]
    parts = [f"path={tail}", f"groundId={gid}", f"gymId={gym}"]
    if sd:
        parts.append(f"date={sd}")
    elif ed:
        parts.append(f"endDate={ed}")
    if slot:
        parts.append(f"slot={slot}")
    return " ".join(parts)


def _emit_probe(
    *,
    token: str,
    userid: str,
    path: str,
    via: str,
    http_status: int | None,
    config_dates: list[str],
    blocks_sample: list[dict[str, str]],
    **fields: Any,
) -> None:
    record(
        "venue.config_probe",
        token=token,
        userid=userid,
        path=path,
        via=via,
        http_status=http_status,
        config_dates=",".join(config_dates) if config_dates else "",
        blocks_sample=json.dumps(blocks_sample, ensure_ascii=False),
        **{k: v for k, v in fields.items() if v is not None and k not in ("config_dates", "blocks_sample")},
    )
