"""Pair token sync timestamps with the first auth failure per fingerprint (observation only)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from models.auth import token_preview

_lock = Lock()
_segments: dict[str, _Segment] = {}
_last_estimate: dict[str, object] = {}


@dataclass
class _Segment:
    sync_at: datetime
    auth_fail_recorded: bool = False


def reset_for_tests() -> None:
    with _lock:
        _segments.clear()
        _last_estimate.clear()


def _fingerprint(token: str) -> str:
    return token_preview(token) or ""


def is_auth_failure_signal(*, auth_fail: bool, error_kind: str = "", school_msg: str = "") -> bool:
    if auth_fail or error_kind == "auth":
        return True
    return "鉴权失败" in str(school_msg or "")


def record_sync(token: str, sync_at: datetime, **audit_fields) -> None:
    """Start a new lifespan segment for this token fingerprint."""
    from core.audit import record

    fp = _fingerprint(token)
    if not fp:
        return
    if sync_at.tzinfo is None:
        sync_at = sync_at.replace(tzinfo=timezone.utc)
    with _lock:
        _segments[fp] = _Segment(sync_at=sync_at)
        if _last_estimate.get("token_preview") == fp:
            _last_estimate.clear()
    record(
        "token.sync",
        token=token,
        sync_at=sync_at.isoformat(),
        **audit_fields,
    )


def try_record_first_auth_fail(
    token: str,
    *,
    auth_fail: bool = False,
    error_kind: str = "",
    school_msg: str = "",
    code: str = "",
    userid: str = "",
    via: str = "",
    **extra,
) -> int | None:
    """On first auth failure after sync for this fingerprint, log ttl_sec (observation only)."""
    if not is_auth_failure_signal(auth_fail=auth_fail, error_kind=error_kind, school_msg=school_msg):
        return None
    from core.audit import record

    fp = _fingerprint(token)
    if not fp:
        return None
    auth_fail_at = datetime.now(timezone.utc)
    with _lock:
        seg = _segments.get(fp)
        if not seg or seg.auth_fail_recorded:
            return None
        seg.auth_fail_recorded = True
        sync_at = seg.sync_at
        ttl_sec = max(0, int((auth_fail_at - sync_at).total_seconds()))
        _last_estimate.clear()
        _last_estimate.update(
            {
                "token_preview": fp,
                "sync_at": sync_at.isoformat(),
                "auth_fail_at": auth_fail_at.isoformat(),
                "ttl_sec": ttl_sec,
            }
        )
    record(
        "token.auth_fail",
        token=token,
        userid=userid,
        auth_fail=True,
        error_kind=error_kind or "auth",
        school_msg=school_msg,
        code=code,
        auth_fail_at=auth_fail_at.isoformat(),
        sync_at=sync_at.isoformat(),
        via=via,
        **{k: v for k, v in extra.items() if v is not None},
    )
    record(
        "token.ttl_estimate",
        token=token,
        userid=userid,
        sync_at=sync_at.isoformat(),
        auth_fail_at=auth_fail_at.isoformat(),
        ttl_sec=ttl_sec,
        error_kind=error_kind or "auth",
        school_msg=school_msg,
        code=code,
        via=via,
    )
    return ttl_sec


def public_ttl_fields(last_sync_at: datetime | None, token: str) -> dict[str, object]:
    """Expose sync age and last ttl estimate for the current fingerprint (UI / API)."""
    fp = _fingerprint(token)
    sync_age_sec = None
    if last_sync_at is not None:
        at = last_sync_at if last_sync_at.tzinfo else last_sync_at.replace(tzinfo=timezone.utc)
        sync_age_sec = max(0, int((datetime.now(timezone.utc) - at).total_seconds()))
    estimate = _last_estimate if _last_estimate.get("token_preview") == fp else {}
    ttl_sec = estimate.get("ttl_sec")
    sync_at_iso = estimate.get("sync_at") or (
        last_sync_at.isoformat() if last_sync_at is not None else None
    )
    return {
        "sync_age_sec": sync_age_sec,
        "sync_at": sync_at_iso,
        "auth_fail_at": estimate.get("auth_fail_at"),
        "estimated_ttl_sec": ttl_sec,
        "ttl_sec": ttl_sec,
        "ttl_observation": ttl_sec if ttl_sec is not None else "未知",
    }
