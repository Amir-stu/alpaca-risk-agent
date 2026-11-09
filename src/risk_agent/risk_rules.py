"""Pure risk math. No I/O — the one module the agent is not allowed to route around.

Position size falls out of the stop distance: decide the dollars you'll lose if the stop
fills, decide where the stop sits, and the share count follows. A separate concentration
cap then bounds exposure, because a tight stop can make risk-based sizing put half the
account into one name while each parameter still looks conservative.
"""

import math
from dataclasses import dataclass
from typing import Literal

from risk_agent.config import RiskLimits

PRICE_DECIMALS = 2


@dataclass(frozen=True)
class Sizing:
    qty: int
    risk_qty: int
    cap_qty: int
    binding: Literal["risk", "concentration"]


def stop_loss_price(reference_price: float, limits: RiskLimits) -> float:
    return round(reference_price * (1 - limits.stop_loss_pct), PRICE_DECIMALS)


def entry_limit_price(reference_price: float, limits: RiskLimits) -> float:
    return round(reference_price * (1 + limits.entry_slippage_pct), PRICE_DECIMALS)


def take_profit_price(entry_price: float, stop_price: float, limits: RiskLimits) -> float:
    return round(entry_price + (entry_price - stop_price) * limits.reward_risk_ratio, PRICE_DECIMALS)


def risk_based_qty(equity: float, entry_price: float, stop_price: float, limits: RiskLimits) -> int:
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0 or equity <= 0:
        return 0
    return int((equity * limits.max_risk_per_trade_pct) // risk_per_share)


def concentration_qty_cap(
    equity: float, existing_value: float, entry_price: float, limits: RiskLimits
) -> int:
    if entry_price <= 0:
        return 0
    room = equity * limits.max_position_concentration_pct - existing_value
    return max(math.floor(room / entry_price), 0)


def size_position(
    equity: float, entry_price: float, stop_price: float, existing_value: float, limits: RiskLimits
) -> Sizing:
    """Shrink to the concentration cap instead of skipping: the trade is still valid, just smaller."""
    risk_qty = risk_based_qty(equity, entry_price, stop_price, limits)
    cap_qty = concentration_qty_cap(equity, existing_value, entry_price, limits)
    binding: Literal["risk", "concentration"] = "risk" if risk_qty <= cap_qty else "concentration"
    return Sizing(qty=min(risk_qty, cap_qty), risk_qty=risk_qty, cap_qty=cap_qty, binding=binding)


def daily_pnl_pct(equity: float, last_equity: float) -> float:
    return 0.0 if last_equity <= 0 else (equity - last_equity) / last_equity


def breaches_daily_loss_limit(equity: float, last_equity: float, limits: RiskLimits) -> bool:
    return last_equity > 0 and daily_pnl_pct(equity, last_equity) <= -limits.max_daily_loss_pct
