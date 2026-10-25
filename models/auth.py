from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TokenState(str, Enum):
    UNKNOWN = "unknown"
    REFRESHING = "refreshing"
    VALID = "valid"
    EXPIRED = "expired"
    FAILED = "failed"


def token_preview(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:min(8, max(0, len(value) // 3))]}..."


def is_placeholder(value: str | None) -> bool:
    """config.example.toml values such as YOUR_USER_ID mean "not set yet", not an account."""
    return str(value or "").strip().upper().startswith("YOUR_")


@dataclass(frozen=True)
class ProfileSnapshot:
    userid: str
    customer_id: str = ""
    name: str = ""
    tel: str = ""

    def without_placeholders(self) -> "ProfileSnapshot":
        return ProfileSnapshot(*("" if is_placeholder(v) else v
                                 for v in (self.userid, self.customer_id, self.name, self.tel)))

    def complete(self) -> bool:
        return bool(
            self.userid
            and not self.userid.startswith("YOUR_")
            and self.customer_id
            and not self.customer_id.startswith("YOUR_")
            and self.name
            and self.tel
        )


@dataclass(frozen=True)
class AuthSnapshot:
    token: str
    profile: ProfileSnapshot
    captured_at: datetime
    auth_revision: int
    source: str = "config"
    capture_seq: int = 0

    @property
    def userid(self) -> str:
        return self.profile.userid

    def with_token(self, token: str, *, captured_at: datetime, auth_revision: int,
                   source: str, capture_seq: int) -> AuthSnapshot:
        return AuthSnapshot(
            token=token,
            profile=self.profile,
            captured_at=captured_at,
            auth_revision=auth_revision,
            source=source,
            capture_seq=capture_seq,
        )

    def with_profile(self, profile: ProfileSnapshot, *, auth_revision: int) -> AuthSnapshot:
        return AuthSnapshot(
            token=self.token,
            profile=profile,
            captured_at=self.captured_at,
            auth_revision=auth_revision,
            source=self.source,
            capture_seq=self.capture_seq,
        )


def public_auth_view(
    state: TokenState,
    snapshot: AuthSnapshot | None,
    *,
    last_success_at: datetime | None,
    last_refresh_at: datetime | None,
    message: str = "",
) -> dict[str, Any]:
    tok = snapshot.token if snapshot else ""
    profile = snapshot.profile if snapshot else ProfileSnapshot(userid="")
    return {
        "state": state.value,
        "token_set": bool(tok),
        "token_preview": token_preview(tok),
        "userid": profile.userid if profile.userid and not profile.userid.startswith("YOUR_") else "",
        "profile_ready": profile.complete(),
        "auth_revision": snapshot.auth_revision if snapshot else 0,
        "captured_at": snapshot.captured_at.isoformat() if snapshot else None,
        "last_success_at": last_success_at.isoformat() if last_success_at else None,
        "last_refresh_at": last_refresh_at.isoformat() if last_refresh_at else None,
        "validated": state == TokenState.VALID,
        "message": message,
    }
