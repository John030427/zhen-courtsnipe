from __future__ import annotations

from utils import wait_http_slot


class RateLimiter:
    """Process-wide HTTP pacing facade (single slot, shared backoff in utils)."""

    async def wait(self) -> None:
        await wait_http_slot()
