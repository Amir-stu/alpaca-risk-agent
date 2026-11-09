from datetime import datetime, timezone

import pytest

from risk_agent.decision_log import DecisionLog

FIXED_NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def test_append_when_called_prefixes_timestamp_and_does_not_mutate_input(tmp_path):
    log = DecisionLog(tmp_path / "decisions.jsonl")
    record = {"symbol": "AAPL", "action": "skip"}
    stored = log.append(record, now=FIXED_NOW)
    assert record == {"symbol": "AAPL", "action": "skip"}
    assert stored == {"timestamp": "2026-11-09T15:00:00+00:00", "symbol": "AAPL", "action": "skip"}


def test_read_when_file_missing_returns_empty(tmp_path):
    assert DecisionLog(tmp_path / "missing.jsonl").read() == []


def test_read_when_symbol_filter_and_limit_returns_latest_matching(tmp_path):
    log = DecisionLog(tmp_path / "decisions.jsonl")
    for index in range(5):
        log.append({"symbol": "AAPL", "n": index}, now=FIXED_NOW)
        log.append({"symbol": "MSFT", "n": index}, now=FIXED_NOW)
    records = log.read(symbol="AAPL", limit=2)
    assert [r["n"] for r in records] == [3, 4]
    assert {r["symbol"] for r in records} == {"AAPL"}


def test_read_when_parent_dir_missing_append_creates_it(tmp_path):
    log = DecisionLog(tmp_path / "nested" / "decisions.jsonl")
    log.append({"symbol": "SPY"}, now=FIXED_NOW)
    assert len(log.read()) == 1


def test_read_when_line_corrupt_raises_with_line_number(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"symbol": "AAPL"}\n\n{broken\n')
    with pytest.raises(ValueError, match="line 3"):
        DecisionLog(path).read()


def test_read_when_limit_not_positive_raises(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        DecisionLog(tmp_path / "d.jsonl").read(limit=0)
