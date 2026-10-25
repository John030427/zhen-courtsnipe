"""SSE event envelopes and log/token sanitization."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# UUID-like tokens, long digit strings (userid/customer), mainland mobile.
_TOKEN_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
_LONG_DIGITS_RE = re.compile(r"\b\d{11,}\b")
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")

_SENSITIVE_KEYS = frozenset({
    "token", "userid", "user_id", "customer_id", "customer_tel", "tel",
    "customer_name", "name", "customerEmail", "password", "secret",
})


def sanitize_text(text: str) -> str:
    if not text:
        return text
    out = _TOKEN_RE.sub(lambda m: m.group(0)[:8] + "…", text)
    out = _PHONE_RE.sub("1**********", out)
    out = _LONG_DIGITS_RE.sub(lambda m: m.group(0)[:4] + "…", out)
    return out


def sanitize_payload(data: Any) -> Any:
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            lk = str(key).lower()
            if lk in _SENSITIVE_KEYS or lk.endswith("_token"):
                cleaned[key] = "[redacted]"
                continue
            cleaned[key] = sanitize_payload(value)
        return cleaned
    if isinstance(data, list):
        return [sanitize_payload(x) for x in data]
    if isinstance(data, str):
        return sanitize_text(data)
    return data


def build_envelope(
    *,
    seq: int,
    topic: str,
    payload: dict[str, Any],
    run_id: str | None = None,
    scan_id: str | None = None,
    date: str | None = None,
    court: int | None = None,
    auth_revision: int | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "run_id": run_id,
        "scan_id": scan_id,
        "date": date,
        "court": court,
        "auth_revision": auth_revision,
        "payload": sanitize_payload(payload),
    }


def payload_from_sse_root(root: dict[str, Any]) -> dict[str, Any]:
    """Extract business payload from SSE JSON (envelope or legacy)."""
    data = root.get("data") if isinstance(root, dict) else {}
    if not isinstance(data, dict):
        return {}
    if "payload" in data:
        return data.get("payload") or {}
    return data
