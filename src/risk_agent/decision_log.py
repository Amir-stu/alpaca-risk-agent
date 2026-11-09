"""Append-only JSONL log of every decision — buys, skips, alerts and errors alike.

Alpaca's API knows what the account holds; only this file knows why. It is what the
dashboard and the explain MCP server read to answer "why did you size AAPL that way?".
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class DecisionLog:
    def __init__(self, path: Path):
        self.path = path

    def append(self, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        stored = {"timestamp": now.isoformat(), **record}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stored) + "\n")
        return stored

    def read(self, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if not self.path.exists():
            return []
        records = [
            self._parse(line, number)
            for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.strip()
        ]
        matching = [r for r in records if symbol is None or r.get("symbol") == symbol]
        return matching[-limit:]

    def _parse(self, line: str, number: int) -> dict[str, Any]:
        try:
            record: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{self.path} line {number} is not valid JSON: {error.msg}") from error
        return record
