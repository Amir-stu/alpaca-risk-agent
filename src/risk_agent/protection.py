"""Broker-order evidence required before calling a long position protected."""

from decimal import Decimal, InvalidOperation
from typing import Any

ACTIVE_STOP_STATUSES = frozenset({"new", "accepted", "held"})


def covering_stops(position: dict[str, Any], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require a working GTC sell stop covering the complete remaining long position."""
    result = []
    for order in orders:
        try:
            qty = Decimal(str(position["qty"]))
            remaining = Decimal(str(order["qty"])) - Decimal(str(order.get("filled_qty") or "0"))
            price = Decimal(str(order["stop_price"]))
            numeric_ok = all(v.is_finite() for v in (qty, remaining, price))
            if not numeric_ok or not (qty > 0 and remaining >= qty and price > 0):
                continue
        except (KeyError, InvalidOperation, ValueError, TypeError):
            continue
        if (
            order.get("symbol") == position.get("symbol")
            and order.get("group_role") == "stop_loss"
            and order.get("side") == "sell"
            and order.get("type") in {"stop", "stop_limit"}
            and order.get("time_in_force") == "gtc"
            and order.get("status") in ACTIVE_STOP_STATUSES
        ):
            result.append(order)
    return result
