"""Target-only slot diagnostics; no customer fields or raw response content."""
from core.watch_engine import candidates
from models.plans import Target, minutes
from utils import slot_covers


def describe_window(start, end, avail, blocks, min_duration):
    try:
        target = Target(start=start, end=end, min_duration=min_duration)
    except ValueError:
        return {"start": start, "end": end, "reason": "invalid_window", "slots": [],
                "longest_free_min": 0}
    slots = []
    longest = consecutive = 0
    missing = False
    type_mismatch = False
    for minute in range(minutes(start), minutes(end), 30):
        key = f"{minute // 60:02d}:{minute % 60:02d}"
        raw = avail.get(key)
        missing = missing or key not in avail
        value = str(raw) if type(raw) in (str, int) and str(raw) in ("0", "1", "2", "3", "4") else "other"
        slots.append({"time": key, "status": value if key in avail else "missing",
                      "status_type": type(raw).__name__})
        type_mismatch = type_mismatch or (raw == 1 and type(raw) is not str)
        consecutive = consecutive + 30 if raw == "1" else 0
        longest = max(longest, consecutive)
    if not avail:
        reason = "empty_slots"
    elif missing:
        reason = "missing_slots"
    elif type_mismatch:
        reason = "status_type"
    elif not slot_covers(avail, start, end):
        reason = "unavailable_slots"
    elif not candidates(blocks, target):
        reason = "blocks_mismatch"
    else:
        reason = "match"
    return {"start": start, "end": end, "reason": reason, "slots": slots,
            "longest_free_min": longest}
