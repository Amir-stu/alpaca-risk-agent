"""Thin subprocess wrapper around the `alpaca` CLI binary.

Every call goes through the same binary you can run by hand, so anything a scheduled run
did can be reproduced by typing the command. Arguments are always passed as a list
(no shell), and symbols are validated upstream in config.load_settings.

Verified against alpaca CLI v0.0.15:
- on failure the JSON error body is written to stderr, not stdout;
- `position get` takes --symbol-or-asset-id, and a flat symbol is HTTP 404 / code 40410000;
- `order submit --dry-run` prints the request body offline without sending it.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

POSITION_NOT_FOUND_CODE = 40410000
HTTP_NOT_FOUND = 404
DEFAULT_TIMEOUT_SECONDS = 60
ORDER_LIST_LIMIT = "500"
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
BAR_ADJUSTMENT = "split"  # split-adjusted closes so a stock split doesn't fake a crossover

Runner = Callable[..., subprocess.CompletedProcess[str]]


class AlpacaCLIError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class BracketOrder:
    """Limit entry + stop-loss + take-profit submitted as one unit.

    Limit, not market: the stop is a fixed price, so a market fill above the reference
    price would silently exceed the risk budget. A limit caps the worst-case entry.
    GTC, not day: protective legs must outlive the session the entry filled in.
    """

    symbol: str
    qty: int
    limit_price: float
    stop_price: float
    take_profit_price: float
    client_order_id: str
    side: str = "buy"
    time_in_force: str = "gtc"

    def to_args(self) -> list[str]:
        return [
            "order",
            "submit",
            "--symbol",
            self.symbol,
            "--side",
            self.side,
            "--qty",
            str(self.qty),
            "--type",
            "limit",
            "--limit-price",
            f"{self.limit_price:.2f}",
            "--time-in-force",
            self.time_in_force,
            "--order-class",
            "bracket",
            "--stop-loss",
            f"{self.stop_price:.2f}",
            "--take-profit",
            f"{self.take_profit_price:.2f}",
            "--client-order-id",
            self.client_order_id,
        ]


def resolve_binary(env: Mapping[str, str]) -> str:
    override = env.get("ALPACA_CLI_PATH")
    if override:
        return override
    found = shutil.which("alpaca")
    if found is None:
        raise FileNotFoundError("alpaca CLI not found on PATH; install it or set ALPACA_CLI_PATH")
    return found


def _error_from(result: subprocess.CompletedProcess[str]) -> AlpacaCLIError:
    raw = (result.stderr or "").strip() or (result.stdout or "").strip()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return AlpacaCLIError(raw or f"alpaca exited with code {result.returncode}")
    return AlpacaCLIError(str(body.get("error", raw)), code=body.get("code"), status=body.get("status"))


class AlpacaCLI:
    def __init__(
        self,
        binary: str,
        feed: str,
        runner: Runner = subprocess.run,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.binary = binary
        self.feed = feed
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def _run(self, *args: str) -> Any:
        command = [self.binary, *args]
        try:
            result = self.runner(
                command, capture_output=True, text=True, timeout=self.timeout_seconds, env=os.environ.copy()
            )
        except subprocess.TimeoutExpired as error:
            raise AlpacaCLIError(
                f"`alpaca {' '.join(args[:2])}` timed out after {self.timeout_seconds}s"
            ) from error
        if result.returncode != 0:
            raise _error_from(result)
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AlpacaCLIError(f"`alpaca {' '.join(args[:2])}` returned non-JSON output") from error

    def account(self) -> dict[str, Any]:
        payload: dict[str, Any] = self._run("account", "get")
        return payload

    def clock(self) -> dict[str, Any]:
        payload: dict[str, Any] = self._run("clock")
        return payload

    def positions(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = self._run("position", "list") or []
        return payload

    def position(self, symbol: str) -> dict[str, Any] | None:
        """A flat symbol is the normal state, not a failure — translate the 404 to None."""
        try:
            payload: dict[str, Any] = self._run("position", "get", "--symbol-or-asset-id", symbol)
        except AlpacaCLIError as error:
            if error.code == POSITION_NOT_FOUND_CODE or error.status == HTTP_NOT_FOUND:
                return None
            raise
        return payload

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Working orders, bracket legs included, filtered on status here rather than by the API.

        Observed on paper: once a bracket entry fills, its `held` stop-loss leg is absent from
        `--status open`, so a protected position looked unprotected. The nested listing carries
        every leg with its own status.
        """
        payload: list[dict[str, Any]] = (
            self._run(
                "order",
                "list",
                "--status",
                "all",
                "--nested",
                "--symbols",
                symbol,
                "--limit",
                ORDER_LIST_LIMIT,
            )
            or []
        )
        flattened = [o for parent in payload for o in (parent, *(parent.get("legs") or []))]
        return [o for o in flattened if o.get("status") not in TERMINAL_ORDER_STATUSES]

    def orders_since(self, symbol: str, day: str) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = (
            self._run(
                "order",
                "list",
                "--status",
                "all",
                "--symbols",
                symbol,
                "--after",
                f"{day}T00:00:00Z",
                "--limit",
                ORDER_LIST_LIMIT,
            )
            or []
        )
        return payload

    def cancel_order(self, order_id: str) -> None:
        self._run("order", "cancel", "--order-id", order_id)

    def get_order(self, order_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = self._run("order", "get", "--order-id", order_id)
        return payload

    def close_position(self, symbol: str) -> None:
        self._run("position", "close", "--symbol-or-asset-id", symbol)

    def daily_bars(self, symbol: str, start: str) -> list[dict[str, Any]]:
        payload = self._run(
            "data",
            "bars",
            "--symbol",
            symbol,
            "--timeframe",
            "1Day",
            "--start",
            start,
            "--feed",
            self.feed,
            "--adjustment",
            BAR_ADJUSTMENT,
        )
        bars: list[dict[str, Any]] = payload.get("bars") or []
        return bars

    def latest_trade_price(self, symbol: str) -> float:
        payload = self._run("data", "latest-trade", "--symbol", symbol, "--feed", self.feed)
        return float(payload["trade"]["p"])

    def submit_bracket(self, order: BracketOrder, dry_run: bool) -> dict[str, Any]:
        args = [*order.to_args(), "--dry-run"] if dry_run else order.to_args()
        payload: dict[str, Any] = self._run(*args)
        return payload
