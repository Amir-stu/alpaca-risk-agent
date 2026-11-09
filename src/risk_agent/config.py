"""Risk limits, strategy parameters and runtime settings — every tunable number lives here."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WATCHLIST_PATH = PROJECT_ROOT / "watchlist.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DECISION_LOG_FILENAME = "decisions.jsonl"

ALLOWED_FEEDS = frozenset({"iex", "sip", "delayed_sip"})
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z.]{0,9}$")
ORDER_ID_PREFIX = "risk-agent"
MIN_TRADABLE_PRICE = 1.0


class LiveTradingRefused(RuntimeError):
    """Raised when the environment would route the CLI to a live brokerage account."""


@dataclass(frozen=True)
class RiskLimits:
    max_risk_per_trade_pct: float = 0.005  # lose at most 0.5% of equity if the stop fills
    max_daily_loss_pct: float = 0.03  # halt new entries after -3% on the day
    max_position_concentration_pct: float = 0.20  # no symbol above 20% of equity
    stop_loss_pct: float = 0.03  # stop sits 3% below the reference price
    reward_risk_ratio: float = 2.0  # take-profit is 2x the worst-case risk per share
    entry_slippage_pct: float = 0.002  # limit entry 0.2% above reference bounds the fill price


@dataclass(frozen=True)
class StrategyParams:
    short_window: int = 5
    long_window: int = 15
    lookback_days: int = 45  # calendar days; ~30 trading days, enough for long_window + holidays


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    watchlist: tuple[str, ...]
    data_feed: str
    decision_log_path: Path


def assert_paper_only(env: Mapping[str, str]) -> None:
    """The CLI routes to live trading when ALPACA_LIVE_TRADE=true. This agent never does."""
    if env.get("ALPACA_LIVE_TRADE", "").strip().lower() == "true":
        raise LiveTradingRefused("ALPACA_LIVE_TRADE=true — this agent only runs against paper accounts")


def _load_watchlist(path: Path) -> tuple[str, ...]:
    symbols = json.loads(path.read_text())
    if not isinstance(symbols, list) or not symbols:
        raise ValueError(f"watchlist {path} must be a non-empty JSON list of ticker symbols")
    invalid = [s for s in symbols if not isinstance(s, str) or not SYMBOL_PATTERN.match(s)]
    if invalid:
        raise ValueError(f"watchlist {path} has invalid symbols: {invalid!r}")
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"watchlist {path} has duplicate symbols")
    return tuple(symbols)


def load_settings(
    env: Mapping[str, str],
    watchlist_path: Path = DEFAULT_WATCHLIST_PATH,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Settings:
    feed = env.get("ALPACA_DATA_FEED", "iex").strip().lower()
    if feed not in ALLOWED_FEEDS:
        raise ValueError(f"ALPACA_DATA_FEED must be one of {sorted(ALLOWED_FEEDS)}, got {feed!r}")
    return Settings(
        # Anything except an explicit "false" stays in dry-run: failing safe beats failing loud here.
        dry_run=env.get("DRY_RUN", "true").strip().lower() != "false",
        watchlist=_load_watchlist(watchlist_path),
        data_feed=feed,
        decision_log_path=data_dir / DECISION_LOG_FILENAME,
    )
