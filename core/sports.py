"""Public sport capability flags and per-sport court defaults."""

from __future__ import annotations

from typing import Any

from utils import Court

# Verified via mitm venue.config_probe (欣园网球场).
DEFAULT_TENNIS_GYM_ID = "1297442093383360513"
DEFAULT_TENNIS_COURTS: dict[int, str] = {
    1: "1298266458362535938",
    2: "1298266633873186817",
    3: "1298266739389292545",
    4: "1298266889507627010",
    5: "1298267016767004674",
    6: "1298267145322422273",
    7: "1298267247285952514",
}

DEFAULT_TENNIS_GYM_NAME = "欣园网球场"

SPORT_BADMINTON = "badminton"
SPORT_TENNIS = "tennis"


def normalize_sport_id(sport_id: str | None) -> str:
    sid = str(sport_id or SPORT_BADMINTON).strip().lower()
    if sid == SPORT_TENNIS:
        return SPORT_TENNIS
    return SPORT_BADMINTON


def courts_from_map(courts_map: dict[int, str]) -> list[Court]:
    return [Court(no, cid, f"{no}号场") for no, cid in sorted(courts_map.items())]


def parse_courts_table(raw: dict[str, Any] | None) -> dict[int, str]:
    if not raw:
        return {}
    out: dict[int, str] = {}
    for key, value in raw.items():
        try:
            no = int(key)
        except (TypeError, ValueError):
            continue
        cid = str(value or "").strip()
        if cid:
            out[no] = cid
    return out


def tennis_section_from_config(data: dict[str, Any]) -> dict[str, Any]:
    sports = data.get("sports") or {}
    if not isinstance(sports, dict):
        return {}
    tennis = sports.get("tennis") or sports.get(SPORT_TENNIS)
    return tennis if isinstance(tennis, dict) else {}


def load_tennis_courts(data: dict[str, Any]) -> list[Court]:
    section = tennis_section_from_config(data)
    parsed = parse_courts_table(section.get("courts"))
    courts_map = parsed or dict(DEFAULT_TENNIS_COURTS)
    return courts_from_map(courts_map)


def tennis_gym_name_from_config(data: dict[str, Any]) -> str:
    section = tennis_section_from_config(data)
    name = str(section.get("gym_name") or section.get("name") or "").strip()
    return name or DEFAULT_TENNIS_GYM_NAME


def tennis_gym_id_from_config(data: dict[str, Any]) -> str:
    section = tennis_section_from_config(data)
    for key in ("gym_id", "gymId", "id"):
        val = str(section.get(key) or "").strip()
        if val:
            return val
    return DEFAULT_TENNIS_GYM_ID


def public_sports_capabilities() -> dict[str, dict[str, Any]]:
    """Capability map exposed via GET /api/config — not tennis query APIs."""
    return {
        SPORT_BADMINTON: {
            "id": SPORT_BADMINTON,
            "label": "羽毛球",
            "emoji": "🏸",
            "matrix": True,
            "watch": True,
            "rush": True,
            "reserve": True,
        },
        SPORT_TENNIS: {
            "id": SPORT_TENNIS,
            "label": "网球",
            "emoji": "🎾",
            "matrix": True,
            "watch": True,
            "rush": True,
            "reserve": True,
        },
    }
