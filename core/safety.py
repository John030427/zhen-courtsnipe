"""Order safety gate — all saveOrder paths must pass through utils.save_order_once."""
from __future__ import annotations

import os
from typing import Any

# Process-wide cache; refreshed from config on engine reload.
_order_dry_run: bool = True


def dry_run_orders_enabled() -> bool:
    """When True, saveOrder HTTP must not be sent."""
    return _order_dry_run


def apply_safety_from_config(data: dict[str, Any] | None) -> bool:
    """Load [safety] dry_run (default True). Returns the effective flag."""
    global _order_dry_run
    if data is None:
        _order_dry_run = True
        return _order_dry_run
    safety = data.get("safety") or {}
    if not isinstance(safety, dict):
        safety = {}
    # Phase 4 default: block writes unless config explicitly disables (Phase 5).
    if "dry_run" in safety:
        _order_dry_run = bool(safety["dry_run"])
    else:
        _order_dry_run = True
    # Emergency kill-switch only (still requires config dry_run false in Phase 5).
    env = os.environ.get("SUSTECH_FORCE_DRY_RUN", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        _order_dry_run = True
    return _order_dry_run


def set_order_dry_run_for_tests(value: bool) -> None:
    global _order_dry_run
    _order_dry_run = bool(value)


DRY_RUN_ORDER_RESPONSE: dict[str, Any] = {
    "success": False,
    "msg": "dry-run：已阻止 saveOrder 请求（Phase 4 安全模式）",
    "dry_run": True,
}
