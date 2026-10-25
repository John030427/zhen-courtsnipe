"""Calendar labels for Asia/Shanghai (booking UI)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_RELATIVE = {0: "今天", 1: "明天", 2: "后天"}


def shanghai_midnight(now: datetime | None = None) -> datetime:
    ref = now.astimezone(CHINA) if now and now.tzinfo else datetime.now(CHINA)
    return ref.replace(hour=0, minute=0, second=0, microsecond=0)


def date_matrix_labels(iso_date: str, *, now: datetime | None = None) -> dict[str, str]:
    """Relative day (今天/明天/后天) + weekday + YYYY-MM-DD for matrix headers."""
    target = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=CHINA)
    today = shanghai_midnight(now)
    delta = (target.date() - today.date()).days
    weekday = _WEEKDAYS[target.weekday()]
    relative = _RELATIVE.get(delta, "")

    if relative:
        title = f"{relative}（{iso_date}）"
        caption = f"{relative} · {iso_date}"
    else:
        title = f"{weekday}（{iso_date}）"
        caption = f"{weekday} · {iso_date}"

    return {
        "date": iso_date,
        "relative": relative,
        "weekday": weekday,
        "title": title,
        "caption": caption,
    }
