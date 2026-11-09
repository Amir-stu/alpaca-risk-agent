# alpaca-risk-agent

A scheduled paper-trading agent for Alpaca. It takes a deliberately simple signal (SMA 5/15 crossover)
and wraps it in a risk layer that sizes each trade from the stop distance, caps how much of the account any
one symbol can use, halts after a bad day, and writes down why it did or did not trade for every symbol on
every run. A read-only MCP server exposes that log, so an assistant can answer "why did you buy 50 AAPL?"
from the agent's own record instead of guessing.

It started from Stephen Kimoi's tutorial *A Trading Agent That Sizes Its Own Risk and Explains Itself*
(Aug 2026, for the lablab.ai Alpaca hackathon). Running the tutorial's design against the real CLI turned up
six problems that would show up in unattended operation. They are fixed here and each one has a test.

## What changed from the tutorial, and why

| # | Tutorial behaviour | Failure it causes | Fix here | Test |
|---|---|---|---|---|
| 1 | Checks for an open *position* only | A run before the fill (or outside market hours) sees no position and buys again: double orders | Checks the market clock, open orders, and today's orders. The client order ID is deterministic per symbol per day, so Alpaca also rejects duplicates if two runs overlap | `test_run_when_entry_order_pending_today_skips_without_double_order`, `..._already_entered_today_skips`, `..._market_closed_...` |
| 2 | Concentration cap breach skips the trade | A valid signal is thrown away instead of sized down | Size is `min(risk-based qty, concentration cap qty)`. The log records which constraint was binding | `test_run_when_tight_stop_concentration_cap_shrinks_qty_and_still_trades` |
| 3 | Market entry, stop computed from the last trade | The fill can land above the reference price while the stop stays fixed, so the real loss exceeds the stated budget | Marketable **limit** entry at reference +0.2%. Shares are sized against that worst-case price, so the loss at the stop is capped by construction | `test_size_position_when_dollar_risk_never_exceeds_budget`, `test_run_when_long_signal_submits_limit_bracket_sized_by_worst_case_entry` |
| 4 | Bracket with `time_in_force=day` | If the stop and take-profit legs inherit `day`, the position is held overnight with no stop | `gtc` brackets. Every run checks that each open position has a live stop-loss leg and raises an `ALERT` (non-zero exit) if not. Unfilled GTC entries from earlier days are canceled | `..._position_open_without_stop_raises_alert_and_nonzero_exit`, `..._entry_order_stale_from_prior_day_cancels_it_and_reevaluates` |
| 5 | SMA computed over bars that include today's | Today's daily bar is still forming during market hours, so the signal can flip intraday on a half-built candle | Today's bar is dropped; only completed sessions vote. Bars are split-adjusted | `test_run_when_todays_partial_bar_present_ignores_it` |
| 6 | Assistant gets Alpaca's full MCP toolset | A chat session can place orders that collide with the scheduled agent | Alpaca's MCP server is limited to `account,assets,stock-data,news`. Positions and rationale come from this repo's own MCP server, which has no write tools | `test_build_server_when_built_exposes_only_read_only_tools` |

Also: the agent refuses to start if `ALPACA_LIVE_TRADE=true` (the CLI's switch to a live account), defaults to
dry-run unless `DRY_RUN` is literally `false`, validates watchlist symbols before they reach a subprocess, and
uses Windows Task Scheduler instead of cron.

**Not verified yet.** Whether Alpaca's bracket legs inherit `day` (fix 4) is not stated in Alpaca's docs. The
fix doesn't depend on the answer, and the stop-leg check will show the real behaviour on the first live run.

## How a run works

```
scheduler (every 15 min) ──> python -m risk_agent.agent
  market clock open? ── no ──> exit 0, nothing logged
  account ACTIVE and not blocked? ── no ──> skip all, exit 1
  daily P/L <= -3%? ── yes ──> skip all (circuit breaker)
  per symbol:
    cancel stale GTC entries from earlier days
    entry still working?                 -> skip
    position open? stop leg live?        -> skip  | no stop -> ALERT, exit 1
    already entered today?               -> skip
    SMA(5) > SMA(15) on completed bars?  -> no: skip (with both SMA values)
    price >= $1?  qty >= 1?              -> no: skip
    submit limit bracket (entry + stop + take-profit), log the full risk math
```

Every symbol ends in exactly one record in `data/decisions.jsonl`. A failure on one symbol is logged as
`error` and the others are still evaluated. Exit codes: `0` ok, `1` alert/error/account blocked,
`2` broker unreachable, `3` refused to start (config or live-trade guard).

Default risk limits (`src/risk_agent/config.py`): 0.5% of equity at risk per trade, stop 3% below
reference, take-profit at 2R, at most 20% of equity per symbol, halt at -3% on the day.

## Setup (Windows)

```powershell
# 1. Alpaca CLI: download cli_<ver>_windows_amd64.zip from github.com/alpacahq/cli/releases,
#    verify it against checksums.txt, put alpaca.exe on PATH.  uv (for Alpaca's MCP server):
winget install astral-sh.uv

# 2. Project
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"          # add ,ui for the dashboard
copy .env.example .env                                    # paste PAPER keys into .env

# 3. Verify, then dry-run
.venv\Scripts\python -m pytest
alpaca doctor
.venv\Scripts\python -m risk_agent.agent                  # DRY_RUN=true by default
```

Set `DRY_RUN=false` in `.env` to place real paper orders. To schedule:
`powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1`.

## Asking it why

`.mcp.json` registers two servers for Claude Code (or any MCP client):

- `alpaca`: Alpaca's official server, read-only toolsets. It reads `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` from
  your **shell environment** (it doesn't read `.env`), so set them there before starting the client.
- `risk-agent-explain`: this repo's server, with tools `recent_decisions`, `explain_decision`, `open_positions`.

Restart the client after adding servers. Then ask "why did you size AAPL that way?" and the answer comes from
the logged risk math, for example: *"SMA long signal (short 118.0 > long 113.0); bought 50 sh with limit
$312.67 (stop $302.69, take-profit $332.63). Worst-case loss $499.00 vs $500.00 budget; binding constraint:
risk (risk allows 50, 20% concentration cap allows 63)."*

Dashboard (optional): `.venv\Scripts\python -m risk_agent.ui`. **Known issue:** gradio 6.31 requires
`pillow<13`, and pillow 12.3 has open CVEs fixed in 13.0. The dashboard is localhost-only and never
processes images, so that code isn't reachable here, but it is a real gap in `pip-audit`. The agent and the
MCP server don't depend on gradio.

## Paper execution verification

<!-- paper-verification:start -->
**Pending.** No paper fill or live protective-stop observation has been verified yet.
<!-- paper-verification:end -->

Run `.venv\Scripts\python -m risk_agent.paper_verification --execute-paper` during market hours.
It requires an initially flat paper account, runs a dry run, submits at most one signal-approved
bracket with `DRY_RUN=false`, waits for a full fill, checks a covering GTC stop in the broker's open-order
response, and runs the same agent again. It only passes when that next run reports
`position already open (stop-loss leg ... live)`. A missing signal is pending, not success.
On failure it cancels an unfilled test entry and attempts to close an unprotected test position;
any cleanup failure is recorded as `ALERT`. It never touches preexisting positions.
After a pass it updates this status block and sets `.env` to `DRY_RUN=false`.
The raw report and logs remain local in `data/paper-verification.json` and `logs/paper-verification.log`.

## Limitations

- The signal is a toy. A 5/15 SMA crossover has no demonstrated edge, and this repo makes no claim that it does.
  The point is the execution and risk layer, which doesn't depend on the signal (`strategy.py` is swappable).
- Long-only, US equities, prices of $1 and up. No options and no shorting.
- Sizing uses account equity, which on a margin account can be backed by borrowed buying power. The cap limits
  concentration per symbol, not gross leverage across symbols.
- `iex` is the default data feed because free accounts can't query recent SIP data. IEX volume is thin, so
  latest-trade prices can lag the consolidated tape.
- One entry per symbol per day, by design. A stop-out is not re-entered the same session.
- Dry-run never cancels stale entries; they remain visible as working orders and block new entries.
- Daily-loss entry halts still audit watchlist positions for missing stops and fail if the audit fails.
  Monitoring currently runs during the regular market session and covers watchlist symbols only.
- A working entry does not suppress a missing-stop alert for an existing position in that symbol.
- Paper fill/protective-stop verification status is recorded above. Offline tests include one that runs the
  real `alpaca` binary in `--dry-run` mode to confirm the flags it builds.

## Development

```powershell
.venv\Scripts\python -m pytest          # unit + real-binary dry-run test, coverage gate 80%
.venv\Scripts\ruff check src tests
.venv\Scripts\mypy                      # strict
.venv\Scripts\pip-audit
```
