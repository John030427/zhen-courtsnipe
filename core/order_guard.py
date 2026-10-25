"""Metadata carried to the final HTTP send boundary."""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class VidLease:
    value: str
    issued_mono: float

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.issued_mono)


class OrderNotSent(RuntimeError):
    """A local guard rejected an order before any HTTP transmission."""

    def __init__(self, reason: str, *, dry_run: bool = False):
        super().__init__(reason)
        self.dry_run = dry_run
