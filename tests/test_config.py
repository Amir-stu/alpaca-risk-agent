import json
from pathlib import Path

import pytest

from risk_agent.config import LiveTradingRefused, assert_paper_only, load_settings


def write_watchlist(tmp_path: Path, symbols: object) -> Path:
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(symbols))
    return path


def test_load_settings_when_env_empty_defaults_to_dry_run(tmp_path):
    watchlist = write_watchlist(tmp_path, ["AAPL", "MSFT"])
    settings = load_settings({}, watchlist_path=watchlist, data_dir=tmp_path)
    assert settings.dry_run is True
    assert settings.watchlist == ("AAPL", "MSFT")
    assert settings.data_feed == "iex"
    assert settings.decision_log_path == tmp_path / "decisions.jsonl"


@pytest.mark.parametrize(
    ("raw", "expected"), [("false", False), ("FALSE", False), ("true", True), ("no", True)]
)
def test_load_settings_when_dry_run_set_only_literal_false_disables(tmp_path, raw, expected):
    watchlist = write_watchlist(tmp_path, ["SPY"])
    settings = load_settings({"DRY_RUN": raw}, watchlist_path=watchlist, data_dir=tmp_path)
    assert settings.dry_run is expected


def test_load_settings_when_feed_overridden_uses_it(tmp_path):
    watchlist = write_watchlist(tmp_path, ["SPY"])
    settings = load_settings({"ALPACA_DATA_FEED": "sip"}, watchlist_path=watchlist, data_dir=tmp_path)
    assert settings.data_feed == "sip"


def test_load_settings_when_feed_unknown_raises(tmp_path):
    watchlist = write_watchlist(tmp_path, ["SPY"])
    with pytest.raises(ValueError, match="ALPACA_DATA_FEED"):
        load_settings({"ALPACA_DATA_FEED": "nasdaq"}, watchlist_path=watchlist, data_dir=tmp_path)


@pytest.mark.parametrize("bad", [["aapl"], ["AAPL; rm"], ["--qty"], [""], "AAPL", [1], []])
def test_load_settings_when_watchlist_malformed_raises(tmp_path, bad):
    watchlist = write_watchlist(tmp_path, bad)
    with pytest.raises(ValueError, match="watchlist"):
        load_settings({}, watchlist_path=watchlist, data_dir=tmp_path)


def test_load_settings_when_watchlist_has_duplicates_raises(tmp_path):
    watchlist = write_watchlist(tmp_path, ["AAPL", "AAPL"])
    with pytest.raises(ValueError, match="duplicate"):
        load_settings({}, watchlist_path=watchlist, data_dir=tmp_path)


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_assert_paper_only_when_live_flag_true_refuses(value):
    with pytest.raises(LiveTradingRefused):
        assert_paper_only({"ALPACA_LIVE_TRADE": value})


@pytest.mark.parametrize("env", [{}, {"ALPACA_LIVE_TRADE": "false"}, {"ALPACA_LIVE_TRADE": ""}])
def test_assert_paper_only_when_flag_absent_or_false_passes(env):
    assert_paper_only(env)
