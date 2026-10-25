from __future__ import annotations

from utils import Court, get_order_once


async def query_court(court: Court, date: str, token: str, user_id: str) -> dict:
    return await get_order_once(court, date, token, user_id)
