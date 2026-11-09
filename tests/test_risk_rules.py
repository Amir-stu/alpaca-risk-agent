import pytest

from risk_agent.config import RiskLimits
from risk_agent.risk_rules import (
    breaches_daily_loss_limit,
    concentration_qty_cap,
    entry_limit_price,
    risk_based_qty,
    size_position,
    stop_loss_price,
    take_profit_price,
)

DEFAULT = RiskLimits()


@pytest.mark.parametrize(
    ("reference", "expected"),
    [(100.0, 97.0), (312.05, 302.69), (50.0, 48.5), (1.0, 0.97), (999.99, 969.99)],
)
def test_stop_loss_price_when_default_limits_sits_three_percent_below(reference, expected):
    assert stop_loss_price(reference, DEFAULT) == expected


@pytest.mark.parametrize(
    ("reference", "expected"),
    [(100.0, 100.2), (312.05, 312.67), (50.0, 50.1), (250.0, 250.5)],
)
def test_entry_limit_price_when_default_limits_adds_slippage_buffer(reference, expected):
    assert entry_limit_price(reference, DEFAULT) == expected


@pytest.mark.parametrize(
    ("entry", "stop", "expected"),
    [(100.0, 97.0, 106.0), (312.05, 302.69, 330.77), (50.0, 48.5, 53.0)],
)
def test_take_profit_price_when_two_to_one_is_twice_stop_distance(entry, stop, expected):
    assert take_profit_price(entry, stop, DEFAULT) == expected


@pytest.mark.parametrize(
    ("equity", "entry", "stop", "expected"),
    [
        (100_000.0, 100.0, 97.0, 166),  # $500 budget / $3 = 166.67 -> 166
        (100_000.0, 312.05, 302.69, 53),  # tutorial's AAPL example
        (100_000.0, 50.0, 49.0, 500),  # tight stop -> large qty
        (10_000.0, 100.0, 97.0, 16),
        (100_000.0, 100.0, 100.0, 0),  # zero distance -> no trade
        (100_000.0, 100.0, 101.0, 0),  # inverted stop -> no trade
        (0.0, 100.0, 97.0, 0),  # no equity -> no trade
    ],
)
def test_risk_based_qty_when_known_inputs_matches_hand_calculation(equity, entry, stop, expected):
    assert risk_based_qty(equity, entry, stop, DEFAULT) == expected


@pytest.mark.parametrize(
    ("equity", "existing_value", "entry", "expected"),
    [
        (100_000.0, 0.0, 100.0, 200),  # $20k cap / $100
        (100_000.0, 15_000.0, 100.0, 50),  # $5k room left
        (100_000.0, 20_000.0, 100.0, 0),  # already at cap
        (100_000.0, 25_000.0, 100.0, 0),  # over cap never goes negative
        (100_000.0, 0.0, 312.05, 64),
    ],
)
def test_concentration_qty_cap_when_known_inputs_floors_to_remaining_room(
    equity, existing_value, entry, expected
):
    assert concentration_qty_cap(equity, existing_value, entry, DEFAULT) == expected


def test_size_position_when_risk_qty_below_cap_risk_binds():
    sizing = size_position(100_000.0, 100.0, 97.0, existing_value=0.0, limits=DEFAULT)
    assert (sizing.qty, sizing.risk_qty, sizing.cap_qty, sizing.binding) == (166, 166, 200, "risk")


def test_size_position_when_tight_stop_concentration_cap_shrinks_instead_of_skipping():
    # 1% risk / 2% stop: the tutorial's original parameters sized ~50% of equity.
    loose = RiskLimits(max_risk_per_trade_pct=0.01, stop_loss_pct=0.02)
    sizing = size_position(100_000.0, 100.0, 98.0, existing_value=0.0, limits=loose)
    assert sizing.risk_qty == 500
    assert sizing.cap_qty == 200
    assert sizing.qty == 200
    assert sizing.binding == "concentration"


def test_size_position_when_dollar_risk_never_exceeds_budget():
    for entry in (12.5, 47.3, 101.0, 312.05, 845.2):
        stop = stop_loss_price(entry, DEFAULT)
        sizing = size_position(100_000.0, entry, stop, existing_value=0.0, limits=DEFAULT)
        assert sizing.qty * (entry - stop) <= 100_000.0 * DEFAULT.max_risk_per_trade_pct + 1e-9


def test_size_position_when_zero_entry_returns_zero():
    sizing = size_position(100_000.0, 0.0, 0.0, existing_value=0.0, limits=DEFAULT)
    assert sizing.qty == 0


@pytest.mark.parametrize(
    ("equity", "last_equity", "expected"),
    [
        (97_000.0, 100_000.0, True),  # exactly -3%
        (96_000.0, 100_000.0, True),
        (97_001.0, 100_000.0, False),
        (105_000.0, 100_000.0, False),
        (100_000.0, 0.0, False),  # brand-new account, no prior close
    ],
)
def test_breaches_daily_loss_limit_when_known_inputs_matches_threshold(equity, last_equity, expected):
    assert breaches_daily_loss_limit(equity, last_equity, DEFAULT) is expected
