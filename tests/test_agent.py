from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from risk_agent.agent import run, run_safely
from risk_agent.alpaca_cli import AlpacaCLIError, BracketOrder
from risk_agent.config import RiskLimits, Settings, StrategyParams
from risk_agent.decision_log import DecisionLog

TODAY = "2026-11-09"
NOW = datetime(2026, 11, 9, 15, 30, tzinfo=timezone.utc)


def rising_bars(count: int = 20) -> list[dict[str, Any]]:
    start = date(2026, 10, 1)
    return [
        {"t": f"{(start + timedelta(days=i)).isoformat()}T05:00:00Z", "c": 100.0 + i} for i in range(count)
    ]


def falling_bars(count: int = 20) -> list[dict[str, Any]]:
    return [{**bar, "c": 200.0 - i} for i, bar in enumerate(rising_bars(count))]


@dataclass
class FakeBroker:
    """In-memory stand-in for the Alpaca CLI — the network boundary."""

    is_open: bool = True
    account_payload: dict[str, Any] = field(
        default_factory=lambda: {
            "equity": "100000",
            "last_equity": "100000",
            "status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
        }
    )
    bars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    positions_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    today_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fail_on: dict[str, str] = field(default_factory=dict)
    submitted: list[tuple[BracketOrder, bool]] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def _maybe_fail(self, method: str, symbol: str) -> None:
        self.calls.append(f"{method}:{symbol}")
        if self.fail_on.get(symbol) == method:
            raise AlpacaCLIError(f"{method} failed for {symbol}", code=50010000, status=500)

    def clock(self) -> dict[str, Any]:
        return {"is_open": self.is_open, "timestamp": f"{TODAY}T10:30:00.0-05:00"}

    def account(self) -> dict[str, Any]:
        return self.account_payload

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        self._maybe_fail("open_orders", symbol)
        return self.open_by_symbol.get(symbol, [])

    def orders_since(self, symbol: str, day: str) -> list[dict[str, Any]]:
        self._maybe_fail("orders_since", symbol)
        return self.today_by_symbol.get(symbol, [])

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)

    def position(self, symbol: str) -> dict[str, Any] | None:
        self._maybe_fail("position", symbol)
        return self.positions_by_symbol.get(symbol)

    def daily_bars(self, symbol: str, start: str) -> list[dict[str, Any]]:
        self._maybe_fail("daily_bars", symbol)
        return self.bars.get(symbol, [])

    def latest_trade_price(self, symbol: str) -> float:
        self._maybe_fail("latest_trade_price", symbol)
        return self.prices[symbol]

    def submit_bracket(self, order: BracketOrder, dry_run: bool) -> dict[str, Any]:
        self._maybe_fail("submit_bracket", order.symbol)
        self.submitted.append((order, dry_run))
        return {"id": f"order-{order.symbol}", "status": "accepted"}


def settings_for(tmp_path: Path, *symbols: str, dry_run: bool = True) -> Settings:
    return Settings(
        dry_run=dry_run, watchlist=symbols, data_feed="iex", decision_log_path=tmp_path / "decisions.jsonl"
    )


def execute(
    broker: FakeBroker, tmp_path: Path, *symbols: str, limits: RiskLimits | None = None, dry_run: bool = True
):
    settings = settings_for(tmp_path, *symbols, dry_run=dry_run)
    log = DecisionLog(settings.decision_log_path)
    result = run(broker, settings, limits or RiskLimits(), StrategyParams(), log, now=NOW)
    return result, log.read(limit=100)


def test_run_when_market_closed_logs_nothing_and_places_nothing(tmp_path):
    broker = FakeBroker(is_open=False, bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05})
    result, records = execute(broker, tmp_path, "AAPL")
    assert result.status == "market_closed"
    assert records == []
    assert broker.submitted == []


def test_run_when_long_signal_submits_limit_bracket_sized_by_worst_case_entry(tmp_path):
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05})
    result, records = execute(broker, tmp_path, "AAPL")
    order, dry_run = broker.submitted[0]
    assert dry_run is True
    assert order == BracketOrder(
        symbol="AAPL",
        qty=50,
        limit_price=312.67,
        stop_price=302.69,
        take_profit_price=332.63,
        client_order_id="risk-agent-aapl-2026-11-09",
    )
    decision = records[0]
    assert decision["action"] == "buy"
    assert decision["risk_math"]["max_dollar_risk"] == 499.0  # 50 sh * (312.67 - 302.69)
    assert decision["risk_math"]["dollar_risk_budget"] == 500.0
    assert decision["risk_math"]["binding_constraint"] == "risk"
    assert decision["signal"]["direction"] == "long"
    assert result.status == "ok"
    assert result.exit_code == 0


def test_run_when_tight_stop_concentration_cap_shrinks_qty_and_still_trades(tmp_path):
    loose = RiskLimits(max_risk_per_trade_pct=0.01, stop_loss_pct=0.02)
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 100.0})
    _, records = execute(broker, tmp_path, "AAPL", limits=loose)
    order, _ = broker.submitted[0]
    assert order.qty == 199  # floor($20,000 / $100.20 worst-case entry)
    assert records[0]["risk_math"]["binding_constraint"] == "concentration"
    assert records[0]["risk_math"]["risk_qty"] > order.qty


def test_run_when_flat_signal_skips_with_sma_numbers(tmp_path):
    broker = FakeBroker(bars={"MSFT": falling_bars()}, prices={"MSFT": 400.0})
    _, records = execute(broker, tmp_path, "MSFT")
    assert broker.submitted == []
    assert records[0]["action"] == "skip"
    assert records[0]["reason"] == "no long signal (SMA short <= SMA long)"
    assert records[0]["signal"]["sma_short"] < records[0]["signal"]["sma_long"]
    assert "latest_trade_price:MSFT" not in broker.calls


def test_run_when_todays_partial_bar_present_ignores_it(tmp_path):
    # Completed bars fall; only today's partial bar spikes. Signal must stay flat.
    spike = {"t": f"{TODAY}T05:00:00Z", "c": 10_000.0}
    broker = FakeBroker(bars={"AAPL": [*falling_bars(), spike]}, prices={"AAPL": 100.0})
    _, records = execute(broker, tmp_path, "AAPL")
    assert records[0]["action"] == "skip"
    assert broker.submitted == []


def test_run_when_entry_order_pending_today_skips_without_double_order(tmp_path):
    pending = [
        {
            "id": "e1",
            "group_role": "entry",
            "side": "buy",
            "status": "new",
            "submitted_at": f"{TODAY}T14:00:00Z",
        }
    ]
    broker = FakeBroker(
        bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05}, open_by_symbol={"AAPL": pending}
    )
    _, records = execute(broker, tmp_path, "AAPL")
    assert broker.submitted == []
    assert records[0]["reason"] == "entry order e1 still working"


def test_run_when_entry_order_stale_from_prior_day_cancels_it_and_reevaluates(tmp_path):
    stale = [
        {
            "id": "old1",
            "group_role": "entry",
            "side": "buy",
            "status": "new",
            "submitted_at": "2026-11-06T15:00:00Z",
        }
    ]
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05}, open_by_symbol={"AAPL": stale})
    _, records = execute(broker, tmp_path, "AAPL")
    assert broker.canceled == ["old1"]
    assert records[0]["canceled_stale_entries"] == ["old1"]
    assert records[0]["action"] == "buy"


def test_run_when_position_open_with_live_stop_skips(tmp_path):
    legs = [
        {
            "id": "s1",
            "group_role": "stop_loss",
            "side": "sell",
            "status": "held",
            "submitted_at": "2026-11-06T15:00:00Z",
        }
    ]
    broker = FakeBroker(
        bars={"AAPL": rising_bars()},
        prices={"AAPL": 312.05},
        positions_by_symbol={"AAPL": {"symbol": "AAPL", "qty": "50"}},
        open_by_symbol={"AAPL": legs},
    )
    result, records = execute(broker, tmp_path, "AAPL")
    assert records[0]["action"] == "skip"
    assert records[0]["reason"] == "position already open (stop-loss leg s1 live)"
    assert result.exit_code == 0


def test_run_when_position_open_without_stop_raises_alert_and_nonzero_exit(tmp_path):
    broker = FakeBroker(
        bars={"AAPL": rising_bars()},
        prices={"AAPL": 312.05},
        positions_by_symbol={"AAPL": {"symbol": "AAPL", "qty": "50"}},
    )
    result, records = execute(broker, tmp_path, "AAPL")
    assert records[0]["action"] == "alert"
    assert "no live stop-loss" in records[0]["reason"]
    assert result.exit_code == 1
    assert broker.submitted == []


def test_run_when_already_entered_today_skips(tmp_path):
    done = [{"id": "x", "client_order_id": "risk-agent-aapl-2026-11-09", "status": "filled"}]
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05}, today_by_symbol={"AAPL": done})
    _, records = execute(broker, tmp_path, "AAPL")
    assert broker.submitted == []
    assert records[0]["reason"] == "already entered AAPL today (one entry per symbol per day)"


def test_run_when_daily_loss_breached_halts_every_symbol(tmp_path):
    broker = FakeBroker(bars={"AAPL": rising_bars(), "SPY": rising_bars()}, prices={"AAPL": 1.0, "SPY": 1.0})
    broker.account_payload = {**broker.account_payload, "equity": "96000"}
    _, records = execute(broker, tmp_path, "AAPL", "SPY")
    assert [r["reason"] for r in records] == ["daily loss limit breached (-4.00% vs -3% cap)"] * 2
    assert broker.calls == []


@pytest.mark.parametrize(
    "overrides",
    [{"trading_blocked": True}, {"account_blocked": True}, {"status": "ACCOUNT_UPDATED"}],
)
def test_run_when_account_not_tradable_skips_all_with_nonzero_exit(tmp_path, overrides):
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05})
    broker.account_payload = {**broker.account_payload, **overrides}
    result, records = execute(broker, tmp_path, "AAPL")
    assert records[0]["reason"].startswith("account not tradable")
    assert result.exit_code == 1


def test_run_when_sub_dollar_price_skips(tmp_path):
    broker = FakeBroker(bars={"PENNY": rising_bars()}, prices={"PENNY": 0.75})
    _, records = execute(broker, tmp_path, "PENNY")
    assert records[0]["reason"] == "price $0.75 below $1.00 minimum (sub-penny tick rules not handled)"


def test_run_when_size_rounds_to_zero_skips(tmp_path):
    broker = FakeBroker(bars={"BRK.A": rising_bars()}, prices={"BRK.A": 700_000.0})
    _, records = execute(broker, tmp_path, "BRK.A")
    assert records[0]["reason"] == "position size rounds to zero shares"


def test_run_when_one_symbol_errors_others_still_evaluated_and_exit_nonzero(tmp_path):
    broker = FakeBroker(
        bars={"AAPL": rising_bars(), "SPY": falling_bars()},
        prices={"AAPL": 312.05, "SPY": 600.0},
        fail_on={"AAPL": "submit_bracket"},
    )
    result, records = execute(broker, tmp_path, "AAPL", "SPY")
    assert [r["action"] for r in records] == ["error", "skip"]
    assert "submit_bracket failed" in records[0]["reason"]
    assert records[0]["risk_math"]["qty"] == 50
    assert result.exit_code == 1


def test_run_when_not_dry_run_passes_flag_through(tmp_path):
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05})
    _, records = execute(broker, tmp_path, "AAPL", dry_run=False)
    assert broker.submitted[0][1] is False
    assert records[0]["dry_run"] is False
    assert records[0]["order"] == {"id": "order-AAPL", "status": "accepted"}


def test_run_when_two_symbols_logs_one_decision_each_in_order(tmp_path):
    broker = FakeBroker(
        bars={"AAPL": rising_bars(), "MSFT": falling_bars()}, prices={"AAPL": 312.05, "MSFT": 400.0}
    )
    _, records = execute(broker, tmp_path, "AAPL", "MSFT")
    assert [(r["symbol"], r["action"]) for r in records] == [("AAPL", "buy"), ("MSFT", "skip")]


def test_strategy_params_when_custom_windows_used(tmp_path):
    broker = FakeBroker(bars={"AAPL": rising_bars(6)}, prices={"AAPL": 312.05})
    settings = settings_for(tmp_path, "AAPL")
    log = DecisionLog(settings.decision_log_path)
    params = replace(StrategyParams(), short_window=2, long_window=5)
    run(broker, settings, RiskLimits(), params, log, now=NOW)
    assert log.read()[0]["action"] == "buy"


class UnreachableBroker(FakeBroker):
    def clock(self) -> dict[str, Any]:
        raise AlpacaCLIError("authentication required", code=0, status=0)


def test_run_safely_when_broker_unreachable_returns_exit_code_two_and_logs_nothing(tmp_path):
    settings = settings_for(tmp_path, "AAPL")
    log = DecisionLog(settings.decision_log_path)
    code = run_safely(UnreachableBroker(), settings, RiskLimits(), StrategyParams(), log, now=NOW)
    assert code == 2
    assert log.read() == []


def test_run_safely_when_run_succeeds_returns_run_exit_code(tmp_path):
    settings = settings_for(tmp_path, "AAPL")
    broker = FakeBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05})
    code = run_safely(
        broker, settings, RiskLimits(), StrategyParams(), DecisionLog(tmp_path / "d.jsonl"), now=NOW
    )
    assert code == 0
