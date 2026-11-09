"""Read-only Gradio dashboard over the same account and decision log the agent writes.
No trading logic here — rationale text comes from presenter.py, data from alpaca_cli.py.

Run: .venv/Scripts/python -m risk_agent.ui   (needs the [ui] extra: pip install -e ".[ui]")
"""

import os
from typing import Any

import gradio as gr
import pandas as pd
from dotenv import load_dotenv

from risk_agent.alpaca_cli import AlpacaCLI, resolve_binary
from risk_agent.config import PROJECT_ROOT, load_settings
from risk_agent.decision_log import DecisionLog
from risk_agent.presenter import rationale

DECISIONS_SHOWN = 50
ACCOUNT_FIELDS = (
    ("Equity", "equity"),
    ("Cash", "cash"),
    ("Buying power", "buying_power"),
    ("Last equity (prior close)", "last_equity"),
)
POSITION_COLUMNS = ["Symbol", "Qty", "Avg entry", "Current price", "Unrealized P/L"]
DECISION_COLUMNS = ["Time", "Symbol", "Action", "Rationale"]


def account_frame(account: dict[str, Any]) -> pd.DataFrame:
    rows = [(label, f"${float(account[key]):,.2f}") for label, key in ACCOUNT_FIELDS]
    return pd.DataFrame(rows, columns=["Metric", "Value"])


def positions_frame(positions: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [
        [
            p["symbol"],
            p["qty"],
            f"${float(p['avg_entry_price']):.2f}",
            f"${float(p['current_price']):.2f}",
            f"${float(p['unrealized_pl']):.2f}",
        ]
        for p in positions
    ]
    return pd.DataFrame(rows, columns=POSITION_COLUMNS)


def decisions_frame(decisions: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [[d["timestamp"], d["symbol"], d["action"].upper(), rationale(d)] for d in reversed(decisions)]
    return pd.DataFrame(rows, columns=DECISION_COLUMNS)


def build_app(broker: AlpacaCLI, log: DecisionLog) -> gr.Blocks:
    def refresh() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        return (
            account_frame(broker.account()),
            positions_frame(broker.positions()),
            decisions_frame(log.read(limit=DECISIONS_SHOWN)),
        )

    with gr.Blocks(title="Risk Agent Dashboard") as app:
        gr.Markdown("# Risk agent — account and decisions")
        gr.Markdown("Read-only. Nothing on this page places orders. Refresh pulls live paper-account state.")
        refresh_button = gr.Button("Refresh")
        gr.Markdown("## Account")
        account_table = gr.Dataframe(interactive=False)
        gr.Markdown("## Open positions")
        positions_table = gr.Dataframe(interactive=False)
        gr.Markdown("## Decisions — what the agent did and why")
        decisions_table = gr.Dataframe(interactive=False, wrap=True)
        outputs = [account_table, positions_table, decisions_table]
        app.load(refresh, outputs=outputs)
        refresh_button.click(refresh, outputs=outputs)
    return app


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(os.environ)
    broker = AlpacaCLI(binary=resolve_binary(os.environ), feed=settings.data_feed)
    build_app(broker, DecisionLog(settings.decision_log_path)).launch()


if __name__ == "__main__":
    main()
