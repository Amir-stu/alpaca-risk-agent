from risk_agent.presenter import explain_latest, rationale

BUY = {
    "timestamp": "2026-11-09T15:30:00+00:00",
    "symbol": "AAPL",
    "action": "buy",
    "dry_run": False,
    "signal": {"direction": "long", "sma_short": 118.0, "sma_long": 113.0},
    "risk_math": {
        "reference_price": 312.05,
        "limit_price": 312.67,
        "stop_price": 302.69,
        "take_profit_price": 332.63,
        "risk_per_share": 9.98,
        "dollar_risk_budget": 500.0,
        "risk_qty": 50,
        "cap_qty": 63,
        "qty": 50,
        "binding_constraint": "risk",
        "max_dollar_risk": 499.0,
        "concentration_cap_pct": 20,
    },
}


def test_rationale_when_buy_explains_signal_sizing_and_binding_constraint():
    assert rationale(BUY) == (
        "SMA long signal (short 118.0 > long 113.0); bought 50 sh with limit $312.67 "
        "(stop $302.69, take-profit $332.63). Worst-case loss $499.00 vs $500.00 budget; "
        "binding constraint: risk (risk allows 50, 20% concentration cap allows 63)."
    )


def test_rationale_when_dry_run_buy_says_so():
    assert rationale({**BUY, "dry_run": True}).startswith("[dry run] ")


def test_rationale_when_skip_returns_reason():
    assert rationale({"action": "skip", "reason": "position already open"}) == "position already open"


def test_rationale_when_reason_missing_returns_placeholder():
    assert rationale({"action": "skip"}) == "(no reason recorded)"


def test_explain_latest_when_no_records_says_none_found():
    assert explain_latest([], "AAPL") == "No decisions logged for AAPL yet."


def test_explain_latest_when_records_uses_most_recent():
    older = {**BUY, "timestamp": "2026-11-06T15:30:00+00:00", "action": "skip", "reason": "no long signal"}
    text = explain_latest([older, BUY], "AAPL")
    assert text.startswith("AAPL at 2026-11-09T15:30:00+00:00: BUY. ")
    assert "Worst-case loss $499.00" in text
