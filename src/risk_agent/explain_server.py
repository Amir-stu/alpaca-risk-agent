"""Read-only MCP server: the "why" interface.

Alpaca's own MCP server reports what the account holds; it cannot know why the agent
sized a trade the way it did. This server exposes the decision log (plus positions via
the same CLI) so any MCP client can answer that — and it has no tool that can trade, so
a chat session can never place orders that collide with the scheduled agent.
"""

import os
from dataclasses import dataclass
from typing import Any, Protocol

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from risk_agent.alpaca_cli import AlpacaCLI, resolve_binary
from risk_agent.config import PROJECT_ROOT, load_settings
from risk_agent.decision_log import DecisionLog
from risk_agent.presenter import explain_latest

MAX_DECISIONS = 200
POSITION_FIELDS = ("symbol", "qty", "avg_entry_price", "unrealized_pl")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


class PositionSource(Protocol):
    def positions(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ExplainTools:
    log: DecisionLog
    broker: PositionSource

    def recent_decisions(self, symbol: str | None, limit: int) -> list[dict[str, Any]]:
        bounded = min(max(limit, 1), MAX_DECISIONS)
        return self.log.read(symbol=symbol.upper() if symbol else None, limit=bounded)

    def explain(self, symbol: str) -> str:
        normalised = symbol.upper()
        return explain_latest(self.log.read(symbol=normalised, limit=1), normalised)

    def open_positions(self) -> list[dict[str, Any]]:
        return [{k: p.get(k) for k in POSITION_FIELDS} for p in self.broker.positions()]


def build_server(tools: ExplainTools) -> MCPServer:
    server = MCPServer("risk-agent-explain")

    @server.tool(annotations=READ_ONLY)
    def recent_decisions(symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Recent agent decisions (buys, skips, alerts, errors) with full risk math, newest last."""
        return tools.recent_decisions(symbol, limit)

    @server.tool(annotations=READ_ONLY)
    def explain_decision(symbol: str) -> str:
        """One-sentence explanation of the latest decision for a symbol, including how it was sized."""
        return tools.explain(symbol)

    @server.tool(annotations=READ_ONLY)
    def open_positions() -> list[dict[str, Any]]:
        """Current open positions on the paper account, read via the alpaca CLI."""
        return tools.open_positions()

    return server


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(os.environ)
    broker = AlpacaCLI(binary=resolve_binary(os.environ), feed=settings.data_feed)
    build_server(ExplainTools(log=DecisionLog(settings.decision_log_path), broker=broker)).run("stdio")


if __name__ == "__main__":
    main()
