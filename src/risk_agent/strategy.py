"""The trade signal: a deliberately plain SMA crossover. The point of this project is the
risk layer around it — swap this module out and nothing else changes."""

from collections.abc import Sequence
from typing import Any

SIGNAL_DECIMALS = 2


def sma(closes: Sequence[float], window: int) -> float | None:
    if window <= 0 or len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def completed_closes(bars: Sequence[dict[str, Any]], today: str) -> list[float]:
    """Drop today's daily bar: during market hours it is still forming, and letting a
    half-finished candle vote in the SMA makes the signal flip intraday."""
    return [float(bar["c"]) for bar in bars if not str(bar["t"]).startswith(today)]


def signal(closes: Sequence[float], short_window: int, long_window: int) -> dict[str, Any]:
    """Return the direction plus the numbers behind it, so the decision log can say why."""
    short = sma(closes, short_window)
    long = sma(closes, long_window)
    if short is None or long is None:
        return {
            "direction": "flat",
            "reason": f"not enough completed bars for SMA window ({len(closes)} < {long_window})",
            "sma_short": short,
            "sma_long": long,
        }
    return {
        "direction": "long" if short > long else "flat",
        "sma_short": round(short, SIGNAL_DECIMALS),
        "sma_long": round(long, SIGNAL_DECIMALS),
    }
