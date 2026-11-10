"""How old a solved captcha (VID) may be when an order uses it, learned from real orders.

The school's captcha (tianai-captcha, by its request shapes) keeps a passed check usable for two
minutes by default; this workbench used to throw a VID away after 8 s and solve a new one at
release, the busiest moment. A VID is now kept longer and its real limit is learned:

- an order that succeeds with an older VID proves that age;
- an order the school refuses naming the captcha caps the limit below that age for good;
- a refusal that names nothing proves nothing, and neither does an error reply after which the
  slot turned out not booked (a bad captcha may come back as a plain "系统异常"), so only a few
  of those are tried with VIDs older than what is proven; after that, only proven ages are used.

The freshest VID is always used first, so the first order of a Rush is not part of this.
"""
import json
import logging
import re
from pathlib import Path

from app_paths import DATA_DIR

log = logging.getLogger("court")

PATH = DATA_DIR / "vid-lifetime.json"
UNPROVEN_TRIES = 3       # unclear failures of older-than-proven VIDs before trying stops
REFUSED_MARGIN = 0.8     # stay well below an age the school refused
_NAMES_CAPTCHA = re.compile(r"验证码|验证失败|校验失败|校验码|captcha|verif", re.I)


class VidLifetime:
    def __init__(self, path: Path = PATH):
        self._path = path
        self.proven = 0.0            # oldest VID age (s) an order succeeded with
        self.refused: float | None = None  # youngest VID age the school refused, naming the captcha
        self.tries = 0               # unclear refusals above `proven` since the last proof
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.proven = max(0.0, float(data.get("proven") or 0))
            self.refused = float(data["refused"]) if data.get("refused") is not None else None
            self.tries = max(0, int(data.get("tries") or 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def limit(self, fresh: float, hold: float) -> float:
        """Oldest VID an order may use. `fresh` is the age used before any evidence (always
        allowed); `hold` is how long a VID is kept at most (0 keeps the old behaviour)."""
        cap = hold
        if self.refused is not None:
            cap = min(cap, self.refused * REFUSED_MARGIN)
        if self.tries >= UNPROVEN_TRIES:
            cap = min(cap, self.proven)
        return max(fresh, cap)

    def note(self, age: float, *, success: bool, uncertain: bool, message: str, fresh: float) -> str:
        """Record what the school did with an order whose VID was `age` seconds old. Returns
        "proven", "refused", "unclear" or "" (nothing learned)."""
        if uncertain or age <= fresh:
            return ""
        if success:
            self.proven, self.tries, learned = max(self.proven, age), 0, "proven"
        elif _NAMES_CAPTCHA.search(message or ""):
            self.refused = age if self.refused is None else min(self.refused, age)
            learned = "refused"
        elif age > self.proven:
            self.tries, learned = self.tries + 1, "unclear"
        else:
            return ""
        self._save()
        return learned

    def view(self) -> dict:
        return {"proven_sec": round(self.proven, 1),
                "refused_sec": None if self.refused is None else round(self.refused, 1),
                "tries": self.tries}

    def _save(self) -> None:
        try:
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps({"proven": round(self.proven, 1), "refused": self.refused,
                                        "tries": self.tries}), encoding="utf-8")
            temp.replace(self._path)
        except OSError as exc:
            log.warning("vid lifetime not saved: %s", type(exc).__name__)
