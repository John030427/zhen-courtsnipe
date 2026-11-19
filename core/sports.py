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
SPORT_TABLE_TENNIS = "table_tennis"
SPORT_BASKETBALL = "indoor_basketball"

# Venues fetched read-only from getAppGroundPageByGymId on 2026-11-20 (private repo
# docs/venues/table-tennis-indoor-basketball.json). `courts`: number shown here → (groundId, the
# school's own court name, which saveOrder sends as groundName).
OTHER_VENUES: dict[str, dict[str, Any]] = {
    SPORT_TABLE_TENNIS: {
        "gym_id": "2017132944518397954",
        "gym_name": "综合训练馆乒乓球场",
        "courts": {
            1: ("2017133810461818881", "1号场"), 2: ("2017133939587661825", "2号场"),
            3: ("2017134192525164546", "3号场"), 4: ("2017134653915381762", "4号场"),
            5: ("2017134783364186114", "5号场"), 6: ("2017134958220525569", "6号场"),
            7: ("2017135085953859585", "7号场"), 8: ("2017135195890761729", "8号场"),
            9: ("2017135724498894850", "9号场"), 10: ("2017135882603184130", "10号场"),
            11: ("2017136017701715969", "11号场"), 12: ("2017136176208658433", "12号场"),
            13: ("2017136298707501057", "13号场"), 14: ("2017136441930399745", "14号场"),
            15: ("2017136625569611778", "15号场"),
        },
    },
    SPORT_BASKETBALL: {
        "gym_id": "2017137857432502273",
        "gym_name": "综合训练馆室内篮球场",
        # The school names these courts itself; the full 1号场 and its halves A/B likely share
        # one floor (whether booking one blocks the others is not verified).
        "courts": {
            1: ("2017138599727841282", "1号场"), 2: ("2017139568683368450", "1号场A场"),
            3: ("2017139707825209345", "1号场B场"), 4: ("2017162408761606145", "2号场（半场）"),
            5: ("2017162559693635586", "3号场（半场）"),
        },
    },
}

ALL_SPORTS = (SPORT_BADMINTON, SPORT_TENNIS, SPORT_TABLE_TENNIS, SPORT_BASKETBALL)
SPORT_LABELS = {SPORT_BADMINTON: "羽毛球", SPORT_TENNIS: "网球",
                SPORT_TABLE_TENNIS: "乒乓球", SPORT_BASKETBALL: "室内篮球"}


def normalize_sport_id(sport_id: str | None) -> str:
    sid = str(sport_id or SPORT_BADMINTON).strip().lower()
    return sid if sid in ALL_SPORTS else SPORT_BADMINTON


def is_sport(sport_id: str | None) -> bool:
    return str(sport_id or "").strip().lower() in ALL_SPORTS


def courts_from_map(courts_map: dict[int, str]) -> list[Court]:
    return [Court(no, cid, f"{no}号场") for no, cid in sorted(courts_map.items())]


def _sport_section(data: dict[str, Any], sid: str) -> dict[str, Any]:
    sports = data.get("sports") or {}
    section = sports.get(sid) if isinstance(sports, dict) else None
    return section if isinstance(section, dict) else {}


def load_other_venue(data: dict[str, Any], sid: str) -> tuple[str, str, list[Court]]:
    """(gym id, gym name, courts) of a sport beyond badminton and tennis. [sports.<sid>] in the
    config may override the gym and the ground id of a numbered court; names stay the school's."""
    base = OTHER_VENUES[sid]
    section = _sport_section(data, sid)
    gym_id = str(section.get("gym_id") or section.get("gymId") or base["gym_id"]).strip()
    gym_name = str(section.get("gym_name") or base["gym_name"]).strip()
    override = parse_courts_table(section.get("courts"))
    courts = [Court(no, override.get(no, gid), name) for no, (gid, name) in sorted(base["courts"].items())]
    return gym_id, gym_name, courts


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
        SPORT_TABLE_TENNIS: {
            "id": SPORT_TABLE_TENNIS,
            "label": "乒乓球",
            "emoji": "🏓",
            "matrix": True,
            "watch": True,
            "rush": True,
            "reserve": True,
            "new": True,
        },
        SPORT_BASKETBALL: {
            "id": SPORT_BASKETBALL,
            "label": "室内篮球",
            "emoji": "🏀",
            "matrix": True,
            "watch": True,
            "rush": True,
            "reserve": True,
            "new": True,
        },
    }
