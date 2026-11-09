import asyncio
from datetime import datetime, timezone
from typing import Any

from risk_agent.decision_log import DecisionLog
from risk_agent.explain_server import ExplainTools, build_server

NOW = datetime(2026, 11, 9, 15, 30, tzinfo=timezone.utc)


class FakePositions:
    def positions(self) -> list[dict[str, Any]]:
        return [{"symbol": "AAPL", "qty": "50", "avg_entry_price": "312.40", "unrealized_pl": "12.5"}]


def make_tools(tmp_path) -> ExplainTools:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    log.append({"symbol": "AAPL", "action": "skip", "reason": "no long signal"}, now=NOW)
    log.append({"symbol": "MSFT", "action": "skip", "reason": "position already open"}, now=NOW)
    return ExplainTools(log=log, broker=FakePositions())


def test_recent_decisions_when_symbol_given_filters(tmp_path):
    records = make_tools(tmp_path).recent_decisions(symbol="MSFT", limit=10)
    assert [r["symbol"] for r in records] == ["MSFT"]


def test_recent_decisions_when_symbol_lowercase_normalises(tmp_path):
    assert len(make_tools(tmp_path).recent_decisions(symbol="aapl", limit=10)) == 1


def test_recent_decisions_when_limit_out_of_range_clamps(tmp_path):
    assert len(make_tools(tmp_path).recent_decisions(symbol=None, limit=10_000)) == 2


def test_explain_when_symbol_logged_returns_rationale(tmp_path):
    assert make_tools(tmp_path).explain("AAPL") == "AAPL at 2026-11-09T15:30:00+00:00: SKIP. no long signal"


def test_open_positions_when_called_returns_trimmed_fields(tmp_path):
    assert make_tools(tmp_path).open_positions() == [
        {"symbol": "AAPL", "qty": "50", "avg_entry_price": "312.40", "unrealized_pl": "12.5"}
    ]


def test_build_server_when_built_exposes_only_read_only_tools(tmp_path):
    server = build_server(make_tools(tmp_path))
    names = sorted(tool.name for tool in asyncio.run(server.list_tools()))
    assert names == ["explain_decision", "open_positions", "recent_decisions"]
