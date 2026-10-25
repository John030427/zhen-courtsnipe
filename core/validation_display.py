"""Shared formatting for school validation / query errors (API + tests)."""
from __future__ import annotations

from typing import Any

KIND_LABELS: dict[str, str] = {
    "auth": "学校鉴权失败",
    "query": "学校查询失败",
    "network": "学校连接异常",
    "config": "验证配置不完整",
    "internal": "本机验证异常",
    "unverified": "尚未验证",
}


def validation_fields_from_auth(auth: dict[str, Any]) -> dict[str, Any]:
    v = auth.get("validation") or {}
    code = v.get("code")
    return {
        "error_kind": v.get("error_kind") or "query",
        "auth_fail": bool(v.get("auth_fail")),
        "school_msg": (v.get("school_msg") or "").strip(),
        "code": code,
        "code_display": code if code not in (None, "") else "无",
        "http_status": v.get("http_status"),
    }


def validation_fields_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Fields from a single get_order_once (or validate hook) result dict."""
    code = result.get("code")
    kind = result.get("error_kind") or ("auth" if result.get("auth_fail") else "query")
    school = (result.get("school_msg") or result.get("msg") or result.get("error") or "").strip()
    return {
        "error_kind": kind,
        "auth_fail": bool(result.get("auth_fail")),
        "school_msg": school,
        "code": code,
        "code_display": code if code not in (None, "") else "无",
        "http_status": result.get("http_status"),
    }


def format_validation_display(auth: dict[str, Any]) -> str:
    if auth.get("validated"):
        return auth.get("message") or "学校只读验证通过"
    v = auth.get("validation") or {}
    kind = v.get("error_kind") or ""
    if not kind or kind == "none":
        return auth.get("message") or "等待登录信息"
    fields = validation_fields_from_auth(auth)
    head = KIND_LABELS.get(kind, auth.get("message") or "学校查询失败")
    parts = [head]
    if fields["school_msg"]:
        parts.append(f"学校：{fields['school_msg']}")
    parts.append(f"code：{fields['code_display']}")
    return " · ".join(parts)


def format_result_display(result: dict[str, Any], *, default_head: str = "学校查询失败") -> str:
    if result.get("ok"):
        return result.get("message") or "查询成功"
    fields = validation_fields_from_result(result)
    head = KIND_LABELS.get(fields["error_kind"], default_head)
    parts = [head]
    if fields["school_msg"]:
        parts.append(f"学校：{fields['school_msg']}")
    parts.append(f"code：{fields['code_display']}")
    return " · ".join(parts)
