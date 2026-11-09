"""Single-shot entry point for a scheduler (Windows Task Scheduler or cron) — no loop, no
memory between runs. Each run re-derives state from Alpaca and decides from scratch.

Check order per run:  market open -> account tradable -> daily loss halt
Check order per symbol: stale entries -> working entry -> open position (+ stop live?)
                        -> already entered today -> signal -> price -> sizing -> submit
Every symbol ends in exactly one logged decision, whether or not it traded.
"""

import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from dotenv import load_dotenv

from risk_agent.alpaca_cli import AlpacaCLI, AlpacaCLIError, BracketOrder, resolve_binary
from risk_agent.config import (
    MIN_TRADABLE_PRICE,
    ORDER_ID_PREFIX,
    PROJECT_ROOT,
    LiveTradingRefused,
    RiskLimits,
    Settings,
    StrategyParams,
    assert_paper_only,
    load_settings,
)
from risk_agent.decision_log import DecisionLog
from risk_agent.risk_rules import (
    breaches_daily_loss_limit,
    daily_pnl_pct,
    entry_limit_price,
    size_position,
    stop_loss_price,
    take_profit_price,
)
from risk_agent.strategy import completed_closes, signal

logger = logging.getLogger("risk_agent")
FAILURE_ACTIONS = frozenset({"alert", "error"})


class Broker(Protocol):
    def clock(self) -> dict[str, Any]: ...
    def account(self) -> dict[str, Any]: ...
    def open_orders(self, symbol: str) -> list[dict[str, Any]]: ...
    def orders_since(self, symbol: str, day: str) -> list[dict[str, Any]]: ...
    def cancel_order(self, order_id: str) -> None: ...
    def position(self, symbol: str) -> dict[str, Any] | None: ...
    def daily_bars(self, symbol: str, start: str) -> list[dict[str, Any]]: ...
    def latest_trade_price(self, symbol: str) -> float: ...
    def submit_bracket(self, order: BracketOrder, dry_run: bool) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RunResult:
    status: str
    decisions: tuple[dict[str, Any], ...]

    @property
    def exit_code(self) -> int:
        failed = self.status != "ok" and self.status != "market_closed"
        return 1 if failed or any(d["action"] in FAILURE_ACTIONS for d in self.decisions) else 0


@dataclass(frozen=True)
class RunContext:
    broker: Broker
    settings: Settings
    limits: RiskLimits
    params: StrategyParams
    today: str
    equity: float


def client_order_id(symbol: str, today: str) -> str:
    """Deterministic per symbol per day, so Alpaca itself rejects a duplicate entry even if
    two scheduled runs overlap."""
    return f"{ORDER_ID_PREFIX}-{symbol.lower()}-{today}"


def account_problem(account: dict[str, Any]) -> str | None:
    if account.get("trading_blocked") or account.get("account_blocked"):
        return "account not tradable (trading or account blocked)"
    if account.get("status") != "ACTIVE":
        return f"account not tradable (status {account.get('status')})"
    return None


def cancel_stale_entries(ctx: RunContext, open_orders: list[dict[str, Any]]) -> list[str]:
    """A GTC entry left unfilled from an earlier session would fill later at stale sizing."""
    stale = [
        o["id"]
        for o in open_orders
        if o.get("group_role") == "entry" and str(o.get("submitted_at", ""))[:10] < ctx.today
    ]
    for order_id in stale:
        ctx.broker.cancel_order(order_id)
    return stale


def check_existing_exposure(
    ctx: RunContext, symbol: str, open_orders: list[dict[str, Any]]
) -> dict[str, Any] | None:
    working = [o for o in open_orders if o.get("group_role") == "entry"]
    if working:
        return {"action": "skip", "reason": f"entry order {working[0]['id']} still working"}
    if ctx.broker.position(symbol) is not None:
        stops = [o for o in open_orders if o.get("group_role") == "stop_loss"]
        if not stops:
            return {
                "action": "alert",
                "reason": "position open with no live stop-loss leg — protect it manually",
            }
        return {"action": "skip", "reason": f"position already open (stop-loss leg {stops[0]['id']} live)"}
    order_id = client_order_id(symbol, ctx.today)
    if any(o.get("client_order_id") == order_id for o in ctx.broker.orders_since(symbol, ctx.today)):
        return {"action": "skip", "reason": f"already entered {symbol} today (one entry per symbol per day)"}
    return None


def price_trade(ctx: RunContext, symbol: str, reference: float) -> tuple[BracketOrder, dict[str, Any]]:
    limit = entry_limit_price(reference, ctx.limits)
    stop = stop_loss_price(reference, ctx.limits)
    take_profit = take_profit_price(limit, stop, ctx.limits)
    sizing = size_position(ctx.equity, limit, stop, existing_value=0.0, limits=ctx.limits)
    risk_math = {
        "reference_price": reference,
        "limit_price": limit,
        "stop_price": stop,
        "take_profit_price": take_profit,
        "risk_per_share": round(limit - stop, 4),
        "dollar_risk_budget": round(ctx.equity * ctx.limits.max_risk_per_trade_pct, 2),
        "risk_qty": sizing.risk_qty,
        "cap_qty": sizing.cap_qty,
        "qty": sizing.qty,
        "binding_constraint": sizing.binding,
        "concentration_cap_pct": round(ctx.limits.max_position_concentration_pct * 100),
        "max_dollar_risk": round(sizing.qty * (limit - stop), 2),
    }
    order = BracketOrder(
        symbol=symbol,
        qty=sizing.qty,
        limit_price=limit,
        stop_price=stop,
        take_profit_price=take_profit,
        client_order_id=client_order_id(symbol, ctx.today),
    )
    return order, risk_math


def evaluate_symbol(ctx: RunContext, symbol: str, decision: dict[str, Any]) -> dict[str, Any]:
    """Mutates nothing: each step returns a new decision dict with what it learned."""
    open_orders = ctx.broker.open_orders(symbol)
    canceled = cancel_stale_entries(ctx, open_orders)
    if canceled:
        decision = {**decision, "canceled_stale_entries": canceled}
        open_orders = [o for o in open_orders if o.get("id") not in canceled]
    exposure = check_existing_exposure(ctx, symbol, open_orders)
    if exposure is not None:
        return {**decision, **exposure}

    start = (date.fromisoformat(ctx.today) - timedelta(days=ctx.params.lookback_days)).isoformat()
    closes = completed_closes(ctx.broker.daily_bars(symbol, start), ctx.today)
    sig = signal(closes, ctx.params.short_window, ctx.params.long_window)
    decision = {**decision, "signal": sig}
    if sig["direction"] != "long":
        return {
            **decision,
            "action": "skip",
            "reason": sig.get("reason", "no long signal (SMA short <= SMA long)"),
        }

    reference = ctx.broker.latest_trade_price(symbol)
    if reference < MIN_TRADABLE_PRICE:
        reason = (
            f"price ${reference:.2f} below ${MIN_TRADABLE_PRICE:.2f} minimum "
            "(sub-penny tick rules not handled)"
        )
        return {**decision, "action": "skip", "reason": reason}
    order, risk_math = price_trade(ctx, symbol, reference)
    decision = {**decision, "risk_math": risk_math}
    if order.qty < 1:
        return {**decision, "action": "skip", "reason": "position size rounds to zero shares"}
    try:
        placed = ctx.broker.submit_bracket(order, dry_run=ctx.settings.dry_run)
    except AlpacaCLIError as error:
        return {**decision, "action": "error", "reason": f"order rejected: {error}", "error_code": error.code}
    return {**decision, "action": "buy", "client_order_id": order.client_order_id, "order": placed}


def halt_all(settings: Settings, base: dict[str, Any], action: str, reason: str) -> list[dict[str, Any]]:
    return [{"symbol": s, **base, "action": action, "reason": reason} for s in settings.watchlist]


def run(
    broker: Broker,
    settings: Settings,
    limits: RiskLimits,
    params: StrategyParams,
    log: DecisionLog,
    now: datetime,
) -> RunResult:
    clock = broker.clock()
    if not clock.get("is_open"):
        logger.info("market closed (next open %s) — nothing to decide", clock.get("next_open"))
        return RunResult(status="market_closed", decisions=())
    today = str(clock["timestamp"])[:10]  # exchange-local (ET) date
    account = broker.account()
    equity, last_equity = float(account["equity"]), float(account["last_equity"])
    base = {"equity": equity, "dry_run": settings.dry_run}

    problem = account_problem(account)
    if problem is not None:
        decisions = halt_all(settings, base, "skip", problem)
        status = "account_blocked"
    elif breaches_daily_loss_limit(equity, last_equity, limits):
        pnl = daily_pnl_pct(equity, last_equity)
        reason = f"daily loss limit breached ({pnl:.2%} vs -{limits.max_daily_loss_pct:.0%} cap)"
        decisions, status = halt_all(settings, base, "skip", reason), "ok"
    else:
        ctx = RunContext(broker, settings, limits, params, today, equity)
        decisions, status = [_safe_evaluate(ctx, s, base) for s in settings.watchlist], "ok"

    stored = tuple(log.append(d, now=now) for d in decisions)
    for d in stored:
        logger.info("%s: %s — %s", d["symbol"], d["action"].upper(), d.get("reason", "order submitted"))
    return RunResult(status=status, decisions=stored)


def _safe_evaluate(ctx: RunContext, symbol: str, base: dict[str, Any]) -> dict[str, Any]:
    """One symbol's broker failure must not stop the others from being evaluated."""
    decision = {"symbol": symbol, **base}
    try:
        return evaluate_symbol(ctx, symbol, decision)
    except AlpacaCLIError as error:
        return {**decision, "action": "error", "reason": str(error), "error_code": error.code}


BROKER_FAILURE_EXIT_CODE = 2


def run_safely(
    broker: Broker,
    settings: Settings,
    limits: RiskLimits,
    params: StrategyParams,
    log: DecisionLog,
    now: datetime,
) -> int:
    """Account-level broker failures (auth, network) end the run with a distinct exit code
    the scheduler surfaces, instead of a traceback nobody reads."""
    try:
        return run(broker, settings, limits, params, log, now).exit_code
    except AlpacaCLIError as error:
        logger.error("broker unreachable, no decisions made: %s", error)
        return BROKER_FAILURE_EXIT_CODE


CONFIG_FAILURE_EXIT_CODE = 3


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windows consoles default to cp1252
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return _configure_and_run()
    except (LiveTradingRefused, ValueError, FileNotFoundError) as error:
        logger.error("refusing to run: %s", error)
        return CONFIG_FAILURE_EXIT_CODE


def _configure_and_run() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    assert_paper_only(os.environ)
    settings = load_settings(os.environ)
    broker = AlpacaCLI(binary=resolve_binary(os.environ), feed=settings.data_feed)
    log = DecisionLog(settings.decision_log_path)
    return run_safely(broker, settings, RiskLimits(), StrategyParams(), log, now=datetime.now(timezone.utc))


if __name__ == "__main__":
    sys.exit(main())
