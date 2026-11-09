import json
import shutil
import subprocess
from typing import Any

import pytest

from risk_agent.alpaca_cli import AlpacaCLI, AlpacaCLIError, BracketOrder, resolve_binary

NOT_FOUND = json.dumps({"code": 40410000, "error": "position does not exist", "status": 404})


class FakeRunner:
    """Stands in for subprocess.run — the process boundary."""

    def __init__(self, responses: list[tuple[int, str, str]]):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        returncode, stdout, stderr = self.responses.pop(0)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def make_cli(*responses: tuple[int, str, str]) -> tuple[AlpacaCLI, FakeRunner]:
    runner = FakeRunner(list(responses))
    return AlpacaCLI(binary="alpaca", feed="iex", runner=runner), runner


def ok(payload: object) -> tuple[int, str, str]:
    return (0, json.dumps(payload), "")


def test_account_when_success_parses_json_and_passes_args():
    cli, runner = make_cli(ok({"equity": "100000"}))
    assert cli.account() == {"equity": "100000"}
    assert runner.calls == [["alpaca", "account", "get"]]


def test_clock_when_success_returns_payload():
    cli, runner = make_cli(ok({"is_open": True, "timestamp": "2026-11-09T10:00:00-05:00"}))
    assert cli.clock()["is_open"] is True
    assert runner.calls[0] == ["alpaca", "clock"]


def test_position_when_cli_reports_404_on_stderr_returns_none():
    cli, runner = make_cli((1, "", NOT_FOUND))
    assert cli.position("AAPL") is None
    assert runner.calls[0] == ["alpaca", "position", "get", "--symbol-or-asset-id", "AAPL"]


def test_position_when_other_error_raises_with_code():
    body = json.dumps({"code": 40110000, "error": "unauthorized", "status": 401})
    cli, _ = make_cli((1, "", body))
    with pytest.raises(AlpacaCLIError) as excinfo:
        cli.position("AAPL")
    assert excinfo.value.code == 40110000
    assert excinfo.value.status == 401
    assert "unauthorized" in str(excinfo.value)


def test_run_when_error_body_not_json_raises_raw_text():
    cli, _ = make_cli((2, "", "segfault-ish garbage"))
    with pytest.raises(AlpacaCLIError, match="segfault-ish garbage"):
        cli.account()


def test_run_when_success_but_stdout_not_json_raises():
    cli, _ = make_cli((0, "not json", ""))
    with pytest.raises(AlpacaCLIError, match="non-JSON"):
        cli.account()


def test_run_when_process_times_out_raises():
    def slow(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    cli = AlpacaCLI(binary="alpaca", feed="iex", runner=slow, timeout_seconds=5)
    with pytest.raises(AlpacaCLIError, match="timed out after 5s"):
        cli.account()


def test_positions_when_success_returns_list():
    cli, runner = make_cli(ok([{"symbol": "AAPL"}]))
    assert cli.positions() == [{"symbol": "AAPL"}]
    assert runner.calls[0] == ["alpaca", "position", "list"]


def test_open_orders_when_called_lists_nested_orders_for_symbol():
    cli, runner = make_cli(ok([]))
    assert cli.open_orders("MSFT") == []
    assert runner.calls[0] == [
        "alpaca",
        "order",
        "list",
        "--status",
        "all",
        "--nested",
        "--symbols",
        "MSFT",
        "--limit",
        "500",
    ]


def test_open_orders_when_filled_bracket_has_held_stop_returns_working_legs():
    # Shape observed on paper 2026-11-09: the filled entry's held stop leg is missing
    # from `--status open`, but present under `legs` in a nested listing.
    filled_bracket = {
        "id": "entry-1",
        "group_role": "entry",
        "status": "filled",
        "legs": [
            {"id": "tp-1", "group_role": "take_profit", "status": "new"},
            {"id": "sl-1", "group_role": "stop_loss", "status": "held"},
        ],
    }
    old_bracket = {
        "id": "entry-0",
        "group_role": "entry",
        "status": "filled",
        "legs": [
            {"id": "tp-0", "group_role": "take_profit", "status": "filled"},
            {"id": "sl-0", "group_role": "stop_loss", "status": "canceled"},
        ],
    }
    working_entry = {"id": "entry-2", "group_role": "entry", "status": "new", "legs": None}
    cli, _ = make_cli(ok([working_entry, filled_bracket, old_bracket]))
    assert [o["id"] for o in cli.open_orders("MSFT")] == ["entry-2", "tp-1", "sl-1"]


def test_orders_since_when_called_queries_all_statuses_after_date():
    cli, runner = make_cli(ok([]))
    cli.orders_since("SPY", "2026-11-09")
    assert runner.calls[0] == [
        "alpaca",
        "order",
        "list",
        "--status",
        "all",
        "--symbols",
        "SPY",
        "--after",
        "2026-11-09T00:00:00Z",
        "--limit",
        "500",
    ]


def test_cancel_order_when_called_passes_id_and_tolerates_empty_stdout():
    cli, runner = make_cli((0, "", ""))
    cli.cancel_order("abc-123")
    assert runner.calls[0] == ["alpaca", "order", "cancel", "--order-id", "abc-123"]


def test_get_order_queries_exact_order_id():
    cli, runner = make_cli(ok({"id": "abc-123", "status": "filled"}))
    assert cli.get_order("abc-123")["status"] == "filled"
    assert runner.calls[0] == ["alpaca", "order", "get", "--order-id", "abc-123"]


def test_close_position_targets_only_the_requested_symbol():
    cli, runner = make_cli(ok({"id": "close-1"}))
    cli.close_position("AAPL")
    assert runner.calls[0] == ["alpaca", "position", "close", "--symbol-or-asset-id", "AAPL"]


def test_daily_bars_when_called_uses_feed_and_returns_bars():
    cli, runner = make_cli(ok({"bars": [{"t": "2026-11-06T05:00:00Z", "c": 1.0}]}))
    assert cli.daily_bars("AAPL", start="2026-09-25") == [{"t": "2026-11-06T05:00:00Z", "c": 1.0}]
    assert runner.calls[0] == [
        "alpaca",
        "data",
        "bars",
        "--symbol",
        "AAPL",
        "--timeframe",
        "1Day",
        "--start",
        "2026-09-25",
        "--feed",
        "iex",
        "--adjustment",
        "split",
    ]


def test_daily_bars_when_response_has_null_bars_returns_empty():
    cli, _ = make_cli(ok({"bars": None}))
    assert cli.daily_bars("AAPL", start="2026-09-25") == []


def test_latest_trade_price_when_success_returns_float():
    cli, runner = make_cli(ok({"symbol": "AAPL", "trade": {"p": 312.05}}))
    assert cli.latest_trade_price("AAPL") == 312.05
    assert runner.calls[0] == ["alpaca", "data", "latest-trade", "--symbol", "AAPL", "--feed", "iex"]


ORDER = BracketOrder(
    symbol="AAPL",
    qty=53,
    limit_price=312.67,
    stop_price=302.69,
    take_profit_price=332.63,
    client_order_id="risk-agent-aapl-2026-11-09",
)


def test_bracket_order_to_args_when_built_is_gtc_limit_bracket():
    assert ORDER.to_args() == [
        "order",
        "submit",
        "--symbol",
        "AAPL",
        "--side",
        "buy",
        "--qty",
        "53",
        "--type",
        "limit",
        "--limit-price",
        "312.67",
        "--time-in-force",
        "gtc",
        "--order-class",
        "bracket",
        "--stop-loss",
        "302.69",
        "--take-profit",
        "332.63",
        "--client-order-id",
        "risk-agent-aapl-2026-11-09",
    ]


def test_submit_bracket_when_dry_run_appends_flag():
    cli, runner = make_cli(ok({"order_class": "bracket"}))
    cli.submit_bracket(ORDER, dry_run=True)
    assert runner.calls[0][-1] == "--dry-run"


def test_submit_bracket_when_live_paper_omits_dry_run_flag():
    cli, runner = make_cli(ok({"id": "o1"}))
    cli.submit_bracket(ORDER, dry_run=False)
    assert "--dry-run" not in runner.calls[0]


def test_resolve_binary_when_env_override_set_uses_it():
    assert resolve_binary({"ALPACA_CLI_PATH": "C:/tools/alpaca.exe"}) == "C:/tools/alpaca.exe"


def test_resolve_binary_when_not_on_path_raises(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="alpaca CLI not found"):
        resolve_binary({})


@pytest.mark.binary
@pytest.mark.skipif(shutil.which("alpaca") is None, reason="alpaca CLI not installed")
def test_submit_bracket_when_real_binary_dry_run_builds_expected_request(monkeypatch):
    """Runs the real CLI offline: --dry-run prints the request body without sending it.
    Pins our flag spelling to what the binary actually accepts."""
    monkeypatch.setenv("ALPACA_API_KEY", "PKOBVIOUSLYFAKE")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "obviously-fake-secret")
    cli = AlpacaCLI(binary=resolve_binary({}), feed="iex")
    body = cli.submit_bracket(ORDER, dry_run=True)
    assert body == {
        "client_order_id": "risk-agent-aapl-2026-11-09",
        "limit_price": "312.67",
        "order_class": "bracket",
        "qty": "53",
        "side": "buy",
        "stop_loss": {"stop_price": "302.69"},
        "symbol": "AAPL",
        "take_profit": {"limit_price": "332.63"},
        "time_in_force": "gtc",
        "type": "limit",
    }
