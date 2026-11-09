"""Turns a logged decision into one human-readable sentence. Shared by the dashboard and
the explain MCP server so both say exactly the same thing."""

from collections.abc import Sequence
from typing import Any


def _buy_rationale(decision: dict[str, Any]) -> str:
    sig, rm = decision["signal"], decision["risk_math"]
    cap_pct = rm["concentration_cap_pct"]
    text = (
        f"SMA long signal (short {sig['sma_short']} > long {sig['sma_long']}); "
        f"bought {rm['qty']} sh with limit ${rm['limit_price']:.2f} "
        f"(stop ${rm['stop_price']:.2f}, take-profit ${rm['take_profit_price']:.2f}). "
        f"Worst-case loss ${rm['max_dollar_risk']:.2f} vs ${rm['dollar_risk_budget']:.2f} budget; "
        f"binding constraint: {rm['binding_constraint']} "
        f"(risk allows {rm['risk_qty']}, {cap_pct}% concentration cap allows {rm['cap_qty']})."
    )
    return f"[dry run] {text}" if decision.get("dry_run") else text


def rationale(decision: dict[str, Any]) -> str:
    if decision.get("action") == "buy":
        return _buy_rationale(decision)
    return str(decision.get("reason") or "(no reason recorded)")


def explain_latest(decisions: Sequence[dict[str, Any]], symbol: str) -> str:
    if not decisions:
        return f"No decisions logged for {symbol} yet."
    latest = decisions[-1]
    return f"{symbol} at {latest['timestamp']}: {str(latest['action']).upper()}. {rationale(latest)}"
