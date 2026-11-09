from dataclasses import dataclass, field
from typing import Any

import pytest
from dotenv import dotenv_values
from test_agent import FakeBroker, rising_bars, settings_for

import risk_agent.paper_verification as verification_module
from risk_agent.alpaca_cli import AlpacaCLIError
from risk_agent.paper_verification import update_readme, verify
from risk_agent.protection import covering_stops


def stop_order(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "stop-1", "group_id": "group-1", "group_role": "stop_loss", "symbol": "AAPL",
        "qty": "50", "filled_qty": "0", "side": "sell", "type": "stop", "status": "new",
        "stop_price": "302.69", "time_in_force": "gtc", **overrides,
    }


@pytest.mark.parametrize("overrides", [
    {"qty": "49"}, {"status": "canceled"}, {"status": "pending_cancel"}, {"side": "buy"},
    {"type": "limit"}, {"stop_price": "0"}, {"qty": "NaN"}, {"filled_qty": "1"},
    {"time_in_force": "day"}, {"symbol": "MSFT"},
])
def test_stop_proof_rejects_invalid_or_insufficient_protection(overrides):
    assert covering_stops({"symbol": "AAPL", "qty": "50"}, [stop_order(**overrides)]) == []


def test_stop_proof_accepts_full_remaining_quantity():
    assert covering_stops({"symbol": "AAPL", "qty": "50"}, [stop_order()])[0]["id"] == "stop-1"


@dataclass
class VerificationBroker(FakeBroker):
    fill: bool = True
    expose_stop: bool = True
    order_payload: dict[str, Any] = field(default_factory=dict)
    closed: list[str] = field(default_factory=list)

    def account(self):
        return {**super().account(), "account_number": "PA_TEST"}

    def positions(self):
        return list(self.positions_by_symbol.values())

    def submit_bracket(self, order, dry_run):
        result = super().submit_bracket(order, dry_run)
        if not dry_run:
            self.order_payload = {
                **result, "status": "filled" if self.fill else "new", "group_id": "group-1",
                "client_order_id": order.client_order_id, "qty": str(order.qty),
                "filled_qty": str(order.qty if self.fill else 0), "filled_avg_price": "312.40",
            }
            self.today_by_symbol[order.symbol] = [self.order_payload]
            if self.fill:
                self.positions_by_symbol[order.symbol] = {"symbol": order.symbol, "qty": str(order.qty)}
                self.open_by_symbol[order.symbol] = (
                    [stop_order(qty=str(order.qty))] if self.expose_stop else []
                )
        return self.order_payload if not dry_run else result

    def get_order(self, order_id):
        return self.order_payload

    def close_position(self, symbol):
        self.closed.append(symbol)
        self.positions_by_symbol.pop(symbol, None)


def broker_for(**kwargs):
    return VerificationBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05}, **kwargs)


def test_verification_requires_fill_visible_stop_and_second_run(tmp_path):
    broker = broker_for()
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    assert result["status"] == "verified"
    assert result["confirmation"]["reason"] == "position already open (stop-loss leg stop-1 live)"
    assert result["confirmation"]["dry_run"] is False
    assert sum(not dry for _, dry in broker.submitted) == 1
    assert result["stop_orders"][0]["id"] == "stop-1"
    assert broker.closed == []


def test_market_closed_cannot_pass_or_submit(tmp_path):
    broker = broker_for(is_open=False)
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    assert result["status"] == "market_closed"
    assert broker.submitted == []


def test_no_signal_cannot_be_marked_verified(tmp_path):
    broker = VerificationBroker()
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    assert result["status"] == "no_signal"
    assert not any(not dry for _, dry in broker.submitted)


def test_unfilled_entry_is_canceled_and_does_not_pass(tmp_path):
    broker = broker_for(fill=False)
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", attempts=2,
                    pause=lambda: None)
    assert result["status"] == "failed"
    assert broker.canceled == ["order-AAPL"]
    assert broker.closed == []


def test_missing_stop_fails_and_closes_only_the_test_position(tmp_path):
    broker = broker_for(expose_stop=False)
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", attempts=2,
                    pause=lambda: None)
    assert result["status"] == "failed"
    assert broker.closed == ["AAPL"]
    assert broker.positions() == []


def test_existing_exposure_is_not_touched(tmp_path):
    broker = broker_for(positions_by_symbol={"MSFT": {"symbol": "MSFT", "qty": "2"}})
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    assert result["status"] == "preexisting_exposure"
    assert broker.submitted == []
    assert broker.closed == []


def test_live_account_cannot_reach_submission(tmp_path):
    broker = broker_for()
    broker.account = lambda: {"account_number": "LIVE_TEST", "status": "ACTIVE"}
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    assert result["status"] == "refused"
    assert broker.submitted == []


def test_completed_verification_does_not_submit_again(tmp_path):
    broker = broker_for()
    path = tmp_path / "proof.json"
    settings = settings_for(tmp_path, "AAPL")
    verify(broker, settings, path, pause=lambda: None)
    result = verify(broker, settings, path, pause=lambda: None)
    assert result["status"] == "verified"
    assert sum(not dry for _, dry in broker.submitted) == 1


def test_ambiguous_submission_is_reconciled_without_retry(tmp_path):
    class AmbiguousBroker(VerificationBroker):
        def submit_bracket(self, order, dry_run):
            result = super().submit_bracket(order, dry_run)
            if not dry_run:
                raise AlpacaCLIError("response lost after acceptance")
            return result

    broker = AmbiguousBroker(bars={"AAPL": rising_bars()}, prices={"AAPL": 312.05})
    result = verify(broker, settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    assert result["status"] == "failed"
    assert result["reconciled_order_id"] == "order-AAPL"
    assert result["cleanup"]["status"] == "protected"
    assert sum(not dry for _, dry in broker.submitted) == 1


def test_readme_only_changes_after_observed_success(tmp_path):
    readme = tmp_path / "README.md"
    pending = "Before\n<!-- paper-verification:start -->\nPending\n<!-- paper-verification:end -->\nAfter"
    readme.write_text(pending, encoding="utf-8")
    update_readme(tmp_path, {"status": "failed"})
    assert readme.read_text(encoding="utf-8") == pending
    report = verify(broker_for(), settings_for(tmp_path, "AAPL"), tmp_path / "proof.json", pause=lambda: None)
    update_readme(tmp_path, report)
    content = readme.read_text(encoding="utf-8")
    assert "position already open (stop-loss leg stop-1 live)" in content
    assert "50 shares filled" in content
    assert content.startswith("Before") and content.endswith("After")


@pytest.mark.parametrize("is_open", [True, False])
def test_command_enables_env_and_updates_readme_only_after_full_pass(tmp_path, monkeypatch, is_open):
    broker = broker_for(is_open=is_open)
    monkeypatch.setattr(verification_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verification_module, "resolve_binary", lambda env: "fake-alpaca")
    monkeypatch.setattr(verification_module, "AlpacaCLI", lambda binary, feed: broker)
    monkeypatch.setattr(verification_module, "load_settings", lambda env: settings_for(tmp_path, "AAPL"))
    monkeypatch.setenv("ALPACA_API_KEY", "PK_TEST")
    monkeypatch.setenv("ALPACA_LIVE_TRADE", "false")
    monkeypatch.setattr("sys.argv", ["paper_verification", "--execute-paper"])
    (tmp_path / ".env").write_text("DRY_RUN=true\n", encoding="utf-8")
    pending = "<!-- paper-verification:start -->\nPending\n<!-- paper-verification:end -->"
    (tmp_path / "README.md").write_text(pending, encoding="utf-8")
    code = verification_module.main()
    assert code == (0 if is_open else 1)
    assert dotenv_values(tmp_path / ".env")["DRY_RUN"] == ("false" if is_open else "true")
    assert not (tmp_path / "data" / "paper-verification.lock").exists()
    content = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert ("Paper execution verified" in content) is is_open
