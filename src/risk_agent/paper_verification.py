"""One paper entry, a confirmed fill, visible protective stops, and a real agent rerun.

Invoke explicitly with --execute-paper. A closed market or a missing signal is pending,
never a pass. Broker evidence stays in ignored data/; README changes only after a pass.
"""

import argparse
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv, set_key

from risk_agent.agent import Broker, client_order_id, run
from risk_agent.alpaca_cli import AlpacaCLI, resolve_binary
from risk_agent.config import (
    PROJECT_ROOT,
    RiskLimits,
    Settings,
    StrategyParams,
    assert_paper_only,
    load_settings,
)
from risk_agent.decision_log import DecisionLog
from risk_agent.protection import covering_stops

logger = logging.getLogger(__name__)
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})


class VerificationBroker(Broker, Protocol):
    def positions(self) -> list[dict[str, Any]]: ...
    def get_order(self, order_id: str) -> dict[str, Any]: ...
    def close_position(self, symbol: str) -> None: ...


def _save(path: Path, report: dict[str, Any], status: str, **fields: Any) -> dict[str, Any]:
    report.update(status=status, **fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(path)
    return report


def _cleanup(
    broker: VerificationBroker, symbol: str, order_id: str, attempts: int, pause: Callable[[], None]
) -> dict[str, Any]:
    """Remove only exposure created by this validation in an initially flat paper account."""
    parent = broker.get_order(order_id)
    if parent.get("status") not in TERMINAL_ORDER_STATUSES:
        broker.cancel_order(order_id)
        for _ in range(attempts):
            if broker.get_order(order_id).get("status") in TERMINAL_ORDER_STATUSES:
                break
            pause()
        else:
            return {"status": "ALERT", "reason": "entry cancellation not confirmed"}
    position = broker.position(symbol)
    if position is None:
        return {"status": "flat"}
    orders = broker.open_orders(symbol)
    if covering_stops(position, orders):
        return {"status": "protected", "position": position}
    # A sibling take-profit can reserve the shares. Cancel this bracket's exits before closing.
    for order in orders:
        if parent.get("group_id") and order.get("group_id") == parent["group_id"]:
            broker.cancel_order(order["id"])
    for _ in range(attempts):
        related = [o for o in broker.open_orders(symbol)
                   if parent.get("group_id") and o.get("group_id") == parent["group_id"]]
        if not related:
            break
        pause()
    else:
        return {"status": "ALERT", "reason": "exit cancellation not confirmed"}
    broker.close_position(symbol)
    for _ in range(attempts):
        if broker.position(symbol) is None:
            return {"status": "closed_unprotected_test_position"}
        pause()
    return {"status": "ALERT", "reason": "test-position close not confirmed"}


def verify(
    broker: VerificationBroker,
    settings: Settings,
    report_path: Path,
    *,
    attempts: int = 60,
    pause: Callable[[], None] = lambda: time.sleep(2),
) -> dict[str, Any]:
    """Submit at most one signal-approved paper entry. Save every transition before proceeding."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if report_path.exists():
        previous: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        if previous.get("status") not in {"market_closed", "no_signal", "refused", "preexisting_exposure"}:
            return previous  # Do not resubmit after a crash, ambiguous submission, or completed test.
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
    clock = broker.clock()
    if not clock.get("is_open"):
        return _save(report_path, report, "market_closed", clock=clock)
    if not str(broker.account().get("account_number", "")).startswith("PA"):
        return _save(report_path, report, "refused", reason="account is not identified as paper")
    if broker.positions() or any(broker.open_orders(s) for s in settings.watchlist):
        return _save(report_path, report, "preexisting_exposure",
                     reason="verification requires a flat account")
    limits, params = RiskLimits(), StrategyParams()
    log = DecisionLog(settings.decision_log_path)
    dry = run(broker, replace(settings, dry_run=True), limits, params, log, datetime.now(timezone.utc))
    _save(report_path, report, "dry_run", dry_run={"status": dry.status, "decisions": dry.decisions})
    if dry.exit_code or dry.status != "ok":
        return _save(report_path, report, "failed", reason="dry run did not pass")
    candidates = [d["symbol"] for d in dry.decisions if d["action"] == "buy"]
    if not candidates:
        return _save(report_path, report, "no_signal", reason="no risk-approved long signal; no trade forced")
    symbol = candidates[0]
    today = str(clock["timestamp"])[:10]
    expected_client_id = client_order_id(symbol, today)
    real_settings = replace(settings, dry_run=False, watchlist=(symbol,))
    _save(report_path, report, "submitting", symbol=symbol, dry_run_enabled=False,
          client_order_id=expected_client_id)
    order_id = None
    try:
        entry = run(broker, real_settings, limits, params, log, datetime.now(timezone.utc))
        decision = entry.decisions[0] if entry.decisions else {}
        _save(report_path, report, "submitted", entry=decision)
        if entry.exit_code or decision.get("action") != "buy":
            raise RuntimeError("paper run did not accept an entry; do not retry blindly")
        order_id = str(decision["order"]["id"])
        _save(report_path, report, "waiting_for_fill", order_id=order_id)
        for _ in range(attempts):
            order = broker.get_order(order_id)
            if order.get("status") == "filled":
                break
            if order.get("status") in TERMINAL_ORDER_STATUSES:
                raise RuntimeError(f"entry terminated with status {order['status']}")
            pause()
        else:
            raise RuntimeError("entry did not fully fill before timeout")
        _save(report_path, report, "filled", filled_order=order)
        if (order.get("client_order_id") != expected_client_id
                or Decimal(str(order["qty"])) != Decimal(str(order["filled_qty"]))):
            raise RuntimeError("filled order identity or full-fill quantity did not match")
        for _ in range(attempts):
            position = broker.position(symbol)
            stops = covering_stops(position, broker.open_orders(symbol)) if position else []
            stops = [s for s in stops if order.get("group_id") and s.get("group_id") == order["group_id"]]
            if stops:
                break
            pause()
        else:
            raise RuntimeError("filled position has no visible covering GTC stop in its bracket")
        _save(report_path, report, "protected", position=position, stop_orders=stops)
        confirmation = run(broker, real_settings, limits, params, log, datetime.now(timezone.utc))
        next_decision = confirmation.decisions[0] if confirmation.decisions else {}
        expected = f"position already open (stop-loss leg {stops[0]['id']} live)"
        if confirmation.exit_code or next_decision.get("reason") != expected:
            raise RuntimeError("next agent run did not confirm the same live stop")
        final_position = broker.position(symbol)
        final_stops = covering_stops(final_position, broker.open_orders(symbol)) if final_position else []
        stop_ids = {s["id"] for s in stops}
        if not any(s["id"] in stop_ids for s in final_stops):
            raise RuntimeError("final position/stop snapshot did not confirm protection")
        for held in broker.positions():
            if not covering_stops(held, broker.open_orders(held["symbol"])):
                raise RuntimeError(f"final account audit found an unprotected position: {held['symbol']}")
        return _save(report_path, report, "verified", confirmation=next_decision,
                     verified_at=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        logger.exception("Paper verification failed")
        _save(report_path, report, "failed", reason=str(error))
        if not order_id:
            try:
                matches = [o for o in broker.orders_since(symbol, today)
                           if o.get("client_order_id") == expected_client_id]
                order_id = str(matches[0]["id"]) if matches else None
                if order_id:
                    report["reconciled_order_id"] = order_id
                else:
                    report["cleanup"] = {"status": "ALERT", "reason": "submission outcome not confirmed"}
            except Exception as reconcile_error:
                report["cleanup"] = {"status": "ALERT", "reason": str(reconcile_error)}
        if order_id:
            try:
                report["cleanup"] = _cleanup(broker, symbol, order_id, attempts, pause)
            except Exception as cleanup_error:
                logger.exception("ALERT: paper test cleanup failed")
                report["cleanup"] = {"status": "ALERT", "reason": str(cleanup_error)}
        return _save(report_path, report, "failed")


def update_readme(root: Path, report: dict[str, Any]) -> None:
    """Replace only the verification status block, and only with actual successful evidence."""
    if report.get("status") != "verified":
        return
    begin, end = "<!-- paper-verification:start -->", "<!-- paper-verification:end -->"
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8")
    before, remainder = text.split(begin, 1)
    _, after = remainder.split(end, 1)
    fill = report["filled_order"]
    body = (f"\nPaper execution verified at {report['verified_at']}: {report['symbol']} "
            f"{fill['filled_qty']} shares filled at ${fill['filled_avg_price']}. "
            f"The subsequent agent run with `DRY_RUN=false` reported:\n\n"
            f"`{report['confirmation']['reason']}`\n\n"
            "The open-order response contained a covering GTC sell stop from the same bracket. "
            "Raw broker evidence is saved locally in `data/paper-verification.json` (Git-ignored).\n")
    readme.write_text(before + begin + body + end + after, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-paper", action="store_true", required=True)
    parser.parse_args()
    log_path = PROJECT_ROOT / "logs" / "paper-verification.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=log_path, encoding="utf-8")
    load_dotenv(PROJECT_ROOT / ".env")
    assert_paper_only(os.environ)
    if not os.environ.get("ALPACA_API_KEY", "").startswith("PK"):
        raise RuntimeError("paper API key required")
    settings = load_settings(os.environ)
    broker = AlpacaCLI(resolve_binary(os.environ), settings.data_feed)
    report_path = PROJECT_ROOT / "data" / "paper-verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock = report_path.with_suffix(".lock")
    owned_lock = False
    try:
        with lock.open("x", encoding="utf-8"):
            owned_lock = True
            report = verify(broker, settings, report_path)
            if report["status"] == "verified":
                update_readme(PROJECT_ROOT, report)
                set_key(str(PROJECT_ROOT / ".env"), "DRY_RUN", "false", quote_mode="never")
            print(json.dumps({"status": report["status"], "report": str(report_path)}))
            return 0 if report["status"] == "verified" else 1
    finally:
        # Never remove a lock owned by a different process if exclusive creation failed.
        if owned_lock:
            lock.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
