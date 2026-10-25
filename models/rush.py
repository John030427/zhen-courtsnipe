from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

CHINA = timezone(timedelta(hours=8))


class RushWindow(BaseModel):
    start: str
    end: str

    @model_validator(mode="after")
    def validate_times(self):
        for value in (self.start, self.end):
            datetime.strptime(value, "%H:%M")
        if self.start >= self.end:
            raise ValueError("window end must be after start")
        return self


class RushPlan(BaseModel):
    """Absolute release time in Asia/Shanghai; booking date explicit."""

    date: str
    sport_id: str = "badminton"
    release_at: datetime
    windows: list[RushWindow] = Field(min_length=1)
    min_duration: int = Field(default=60, ge=30, le=240, multiple_of=30)
    court_priority: list[int] = Field(default_factory=list)
    excluded_courts: list[int] = Field(default_factory=list)
    token_refresh_lead_sec: int = Field(default=120, ge=0, le=3600)
    vid_warmup_sec: int = Field(default=8, ge=0, le=120)
    scan_window_sec: int = Field(default=60, ge=3, le=120)
    scan_interval_far_sec: int = Field(default=60, ge=15, le=600)
    scan_interval_near_sec: float = Field(default=3.0, ge=1.0, le=10.0)
    near_release_sec: int = Field(default=30, ge=5, le=300)
    stop_after_success: bool = True
    repeat_daily: bool = False

    @field_validator("release_at")
    @classmethod
    def release_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("release_at must include timezone")
        return value.astimezone(CHINA)

    @model_validator(mode="after")
    def validate_date(self):
        datetime.strptime(self.date, "%Y-%m-%d")
        return self


    @classmethod
    def from_config(
        cls,
        data: dict[str, Any],
        *,
        target_date: str,
        now: datetime,
        sport_id: str | None = None,
    ) -> RushPlan:
        from core.sports import normalize_sport_id

        rush = data.get("rush") or {}
        auto = data.get("auto") or {}
        sid = normalize_sport_id(sport_id or rush.get("sport_id") or auto.get("sport_id"))

        release_raw = rush.get("release_at") or auto.get("refresh_at") or "20:00:00"
        release_at = cls._parse_release_at(release_raw, now)

        slot_start = str(rush.get("slot_start") or auto.get("slot_start") or "19:30")
        slot_end = str(rush.get("slot_end") or auto.get("slot_end") or "21:30")
        windows = [RushWindow(start=slot_start, end=slot_end)]
        for fb in rush.get("fallback_windows") or []:
            if isinstance(fb, dict) and fb.get("start") and fb.get("end"):
                windows.append(RushWindow(start=str(fb["start"]), end=str(fb["end"])))

        courts = rush.get("court_priority") or rush.get("courts") or []
        if isinstance(courts, str):
            courts = [int(x) for x in courts.split(",") if x.strip()]
        excluded = rush.get("excluded_courts") or auto.get("excluded_courts") or []

        return cls(
            date=target_date,
            sport_id=sid,
            release_at=release_at,
            windows=windows,
            min_duration=int(rush.get("min_duration", 60)),
            court_priority=[int(c) for c in courts],
            excluded_courts=[int(c) for c in excluded],
            token_refresh_lead_sec=int(
                rush.get("token_refresh_lead_sec")
                or (data.get("tuning") or {}).get("token_refresh_lead_sec", 120)
            ),
            vid_warmup_sec=int(rush.get("vid_warmup_sec", 8)),
            scan_window_sec=int(rush.get("scan_window_sec", 60)),
            scan_interval_far_sec=int(rush.get("scan_interval_far_sec", 60)),
            scan_interval_near_sec=float(rush.get("scan_interval_near_sec", 3)),
            near_release_sec=int(rush.get("near_release_sec", 30)),
            stop_after_success=bool(rush.get("stop_after_success", True)),
            repeat_daily=bool(rush.get("repeat_daily", False)),
        )

    @staticmethod
    def _parse_release_at(raw: str, now: datetime) -> datetime:
        """Parse release clock.

        Absolute ISO keeps its own date. Time-only values (HH:MM[:SS]) attach to
        *today* in Asia/Shanghai (the real wall-clock release), not to the court
        booking day. That enables "tonight 20:00 grab day-after-tomorrow courts".
        """
        now_cn = now.astimezone(CHINA) if now.tzinfo else now.replace(tzinfo=CHINA)
        text = str(raw).strip()
        # Absolute datetime: ISO with T, or date + time, optional offset / Z
        if "T" in text or (len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-"):
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            if "T" not in text and " " in text:
                text = text.replace(" ", "T", 1)
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CHINA)
            return dt.astimezone(CHINA)

        parts = [int(x) for x in text.split(":")]
        while len(parts) < 3:
            parts.append(0)
        day = now_cn.date()
        dt = datetime(
            day.year, day.month, day.day,
            parts[0], parts[1], parts[2],
            tzinfo=CHINA,
        )
        if dt <= now_cn:
            raise ValueError(
                "放号触发已过期：时间-only 按今天计算；"
                "今晚已过请改触发，或填完整日期时间（如 2026-10-12T20:00:06+08:00）",
            )
        return dt

    def validate_not_past(self, now: datetime) -> None:
        now_cn = now.astimezone(CHINA) if now.tzinfo else now.replace(tzinfo=CHINA)
        if self.release_at <= now_cn:
            raise ValueError("release_at 已过期，请重新选择放场时刻")
