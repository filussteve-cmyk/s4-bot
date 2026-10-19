# S4 Double Top/Bottom Bot

Intraday ES futures strategy bot for TopstepX, built on the
[`project-x-py`](https://pypi.org/project/project-x-py/) SDK (TopstepX /
ProjectX Gateway API client).

**Strategy:** double top / double bottom reversals on 5-minute ES bars, RTH
only (09:30–16:00 ET). A swing extreme (±5 bars) pulls back ≥10 points, then
retests ≥30 minutes later with the touch bar's extreme inside [L−1.0, L+0.5]
(tops) / [L−0.5, L+1.0] (bottoms). Entry is a limit at exactly L with a
native 4-point stop / 5-point target bracket. One attempt per level per day,
max 1 position, everything flattened at 16:09 ET — never held overnight.

## Safety: paper mode is the default

`config.yaml` ships with `mode: "paper"`. In paper mode the bot connects,
reads bars, detects setups and **logs every signal — it never calls any
order-placement API**. There is no code path that places orders in paper mode;
the order call sits behind an explicit `if not live: ... continue` gate.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Credentials — pick one (env vars take precedence, never commit real keys):

```bash
export TOPSTEPX_USERNAME="your_topstepx_username"
export TOPSTEPX_API_KEY="your_api_key"   # TopstepX → Settings → API
export TOPSTEPX_ACCOUNT_ID="123456"      # numeric account id (practice/combine)
```

Or copy `config.yaml` values into a local, git-ignored file — but note the
bot only reads `config.yaml` + env vars, so env vars are the recommended
route.

## Run (paper)

```bash
python bot.py
```

Watch the log for `SIGNAL ...` lines and `PAPER: signal logged, no order
placed.` Nothing will be sent to the broker.

## Enabling live trading — explicit step required

Live trading happens **only** when you deliberately do all three:

1. Set `mode: "live"` in `config.yaml` (it defaults to `"paper"`).
2. Set a real `account_id` (practice/combine account — the TopstepX API
   accepts automation on simulated accounts, not funded ones).
3. Provide real credentials via the env vars above.

There is no flag, env var, or argument that enables live trading by
accident. On startup the bot logs `S4 bot starting in LIVE mode` so it is
unmistakable. Risk guards always apply: max 1 position, no entries outside
RTH, daily realized-loss halt at −$500 (`max_daily_loss`), flatten-all at
16:09 ET, and startup reconciliation (existing positions/working orders are
queried first — the bot never double-orders after a restart).

## Layout

| File | Purpose |
|---|---|
| `strategy.py` | Pure strategy logic (no I/O): swing detection, level lifecycle, signals. Unit-tested. |
| `bot.py` | Async main loop (60s cadence, RTH): bars → strategy → risk → paper log / live bracket order. |
| `risk.py` | Guards: RTH window, 16:09 flatten, −$500 daily halt, max-1-position, flatten-all + P&L helpers. |
| `config.yaml` | Placeholders only. `mode: paper` default. |
| `state.json` | Runtime state (git-ignored): today's levels, signals, P&L. |
| `test_strategy.py` | 18 tests: rule unit tests + synthetic-month sanity (~1 signal/day). |

## Tests

```bash
python -m pytest test_strategy.py -v
```

## Notes / SDK deviations

- Built against `project-x-py` v4.x (TopstepX-only). Auth is
  `TradingSuite.create(instrument="ES", username=..., api_key=...)` (async).
- Entries use the SDK's **native Gateway bracket** in one call:
  `suite.orders.place_bracket_order(contract_id, side, size, entry_price,
  stop_loss_price, take_profit_price, entry_type="limit", account_id=...)`.
- Bars come from `suite.client.get_bars(symbol="ES", interval=5, unit=2)`
  as a polars DataFrame (`timestamp`/`open`/`high`/`low`/`close`/`volume`,
  UTC); the bot converts to ET and filters RTH.
- Daily realized P&L for the loss halt is computed from
  `client.search_trades(...)` (`profitAndLoss − fees − commissions`,
  assuming gross P&L with separate fee fields per the Gateway convention).
- The ES front-month contract is resolved at startup via
  `list_available_contracts()` using the `activeContract` flag.
