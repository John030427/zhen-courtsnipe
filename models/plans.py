from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, model_validator

CHINA = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(CHINA)


def minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    if parsed.strftime("%H:%M") != value:
        raise ValueError("Time must use HH:MM")
    return parsed.hour * 60 + parsed.minute


class Target(BaseModel):
    start: str = "19:00"
    end: str = "22:00"
    min_duration: int = Field(default=60, ge=30, le=240, multiple_of=30)

    @model_validator(mode="after")
    def validate_window(self):
        start, end = minutes(self.start), minutes(self.end)
        if start % 30 or end % 30 or end - start < self.min_duration:
            raise ValueError("Window must fit duration and use half-hour boundaries")
        return self


class WatchPlan(BaseModel):
    sport_id: str = "badminton"
    dates: list[str] = Field(min_length=1, max_length=7)
    courts: list[int] = Field(min_length=1, max_length=10)
    targets: list[Target] = Field(min_length=1, max_length=8)
    poll_interval: int = Field(default=20, ge=15, le=3600)
    auto_reserve: bool = False
    stop_after_success: bool = False
    max_attempts: int = Field(default=1, ge=1, le=3)
    start_at: str | None = None
    duration_minutes: int = Field(default=30, ge=1, le=1440)
    continuous: bool = False

    @model_validator(mode="after")
    def validate_dates(self):
        from core.sports import normalize_sport_id

        self.sport_id = normalize_sport_id(self.sport_id)
        for value in self.dates:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
            if parsed < now().date() or parsed > now().date() + timedelta(days=7):
                raise ValueError("Dates must be within the next seven days")
        self.dates = list(dict.fromkeys(self.dates))
        self.courts = list(dict.fromkeys(self.courts))
        if self.start_at:
            parsed = datetime.fromisoformat(self.start_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CHINA)
            if parsed <= now():
                raise ValueError("Scheduled start must be in the future")
            self.start_at = parsed.isoformat()
        return self
