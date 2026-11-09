import pytest

from risk_agent.strategy import completed_closes, signal, sma


@pytest.mark.parametrize(
    ("closes", "window", "expected"),
    [
        ([1.0, 2.0, 3.0, 4.0, 5.0], 5, 3.0),
        ([1.0, 2.0, 3.0, 4.0, 5.0], 2, 4.5),
        ([10.0], 1, 10.0),
        ([1.0, 2.0], 3, None),
        ([], 1, None),
    ],
)
def test_sma_when_known_inputs_matches_mean_of_tail(closes, window, expected):
    assert sma(closes, window) == expected


def test_signal_when_rising_series_is_long_with_numbers():
    closes = [float(i) for i in range(1, 21)]
    result = signal(closes, short_window=5, long_window=15)
    assert result == {"direction": "long", "sma_short": 18.0, "sma_long": 13.0}


def test_signal_when_falling_series_is_flat():
    closes = [float(i) for i in range(20, 0, -1)]
    assert signal(closes, 5, 15)["direction"] == "flat"


def test_signal_when_equal_smas_is_flat():
    assert signal([5.0] * 20, 5, 15)["direction"] == "flat"


def test_signal_when_too_few_bars_is_flat_with_reason():
    result = signal([1.0] * 10, 5, 15)
    assert result["direction"] == "flat"
    assert result["reason"] == "not enough completed bars for SMA window (10 < 15)"


def test_completed_closes_when_todays_partial_bar_present_drops_it():
    bars = [
        {"t": "2026-11-05T05:00:00Z", "c": 100.0},
        {"t": "2026-11-06T05:00:00Z", "c": 101.0},
        {"t": "2026-11-09T05:00:00Z", "c": 150.0},
    ]
    assert completed_closes(bars, "2026-11-09") == [100.0, 101.0]


def test_completed_closes_when_no_bar_for_today_keeps_all():
    bars = [{"t": "2026-11-05T05:00:00Z", "c": 100.0}, {"t": "2026-11-06T05:00:00Z", "c": 101.0}]
    assert completed_closes(bars, "2026-11-09") == [100.0, 101.0]
