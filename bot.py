"""S4 Double Top/Bottom bot — intraday ES futures on TopstepX via project-x-py.

Paper mode (default): connects, reads bars, detects setups and LOGS every
signal. It NEVER calls any order-placement API in paper mode.

Live mode: ONLY when config.yaml explicitly sets ``mode: live``. Then a
second-touch signal places a limit order at exactly L; the moment it fills,
a 2-point stop / 7-point target bracket is placed around the actual fill
price by our own REST-polling bracket (see place_bracket_rest — the SDK's
place_bracket_order waits on a websocket fill event that never arrives in
REST-only mode, leaving trades unprotected for its full 60s timeout).

Startup: existing positions and working orders are queried FIRST. If a
position or working order already exists, the bot manages/monitors it and
never double-orders after a restart.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from strategy import Bar, Level, Signal, run_detection, would_fill
import risk
from risk import ET

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.yaml"
STATE_PATH = BASE / "state.json"

log = logging.getLogger("s4bot")


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    # Environment variables take precedence (never commit real keys).
    cfg["username"] = os.environ.get("TOPSTEPX_USERNAME", cfg.get("username"))
    cfg["api_key"] = os.environ.get("TOPSTEPX_API_KEY", cfg.get("api_key"))
    cfg["account_id"] = int(os.environ.get("TOPSTEPX_ACCOUNT_ID",
                                           cfg.get("account_id", 0)))
    return cfg


def blank_state() -> dict:
    return {
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "levels": [],            # serialized Level dicts for today
        "signals_today": 0,
        "daily_pnl": 0.0,
        "halted": False,
        "has_position": False,   # set on startup if a position already exists
        "entry_order_id": None,
        "protective_order_ids": [],  # stop/target ids of the open bracket
    }


def load_state() -> dict:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text())
            if st.get("date") == today:
                return st
            log.info("New trading day — resetting daily state.")
        except Exception as e:
            log.warning("Could not read state.json (%s); starting fresh.", e)
    st = blank_state()
    st["date"] = today
    return st


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def serialize_level(lv: Level) -> dict:
    return {"direction": lv.direction, "L": lv.L, "first_idx": lv.first_idx,
            "state": lv.state, "touches": lv.touches,
            "pullback_done": lv.pullback_done}


def deserialize_level(d: dict) -> Level:
    return Level(direction=d["direction"], L=d["L"], first_idx=d["first_idx"],
                 state=d.get("state", "active"), touches=d.get("touches", 1),
                 pullback_done=d.get("pullback_done", False))


# ---------------------------------------------------------------------------
# Broker helpers
# ---------------------------------------------------------------------------

async def resolve_es_contract(client) -> str:
    """Return the front-month ES contract id (activeContract flag)."""
    contracts = await client.list_available_contracts(live=False)
    for c in contracts:
        name = (c.name or "").upper()
        if name.startswith("ES") and c.activeContract:
            log.info("ES front-month contract: %s (%s)", c.id, c.name)
            return c.id
    raise RuntimeError("No activeContract ES front-month contract found.")


def bars_from_df(df, trade_date: str):
    """Convert the SDK's polars bar DataFrame to today's RTH Bar list.

    Returns (bars, last_bar_end_idx). Bars are 5-minute, ET, RTH only,
    indexed 0..N-1 for the strategy module.
    """
    bars = []
    for row in df.to_dicts():
        ts = row["timestamp"]
        # SDK timestamps are UTC; convert to ET.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=risk.ET)  # pragma: no cover - defensive
        else:
            ts = ts.astimezone(ET)
        if ts.strftime("%Y-%m-%d") != trade_date:
            continue
        t = ts.time()
        if not (risk.RTH_OPEN <= t < risk.RTH_CLOSE):
            continue
        bars.append((ts, Bar(idx=len(bars), open=row["open"], high=row["high"],
                             low=row["low"], close=row["close"])))
    return bars


# ---------------------------------------------------------------------------
# REST-polling bracket (live entries)
# ---------------------------------------------------------------------------
# Why not the SDK's place_bracket_order: it waits for a websocket fill event
# before placing the protective stop/target. This bot is REST-only (the
# realtime websockets cannot traverse the egress proxy), so that event never
# arrives and EVERY filled trade sat unprotected for the SDK's full 60s
# timeout. On Nov 10, 2026 price crossed the 2-pt stop inside that minute;
# the late stop was rejected as already through the market and the SDK
# emergency-closed at market (-$166.28 instead of the intended -$103.78).
# The functions below poll order/position state over REST every ~1.5s, place
# the protectives within seconds of the fill, re-base them on the ACTUAL
# fill price, and market-close at once if a protective order is rejected.

FILL_POLL_SECONDS = 1.5
FILL_TIMEOUT_SECONDS = 60  # same entry-fill window the SDK used
_ORDER_FILLED = 2
_ORDER_TERMINAL_NO_FILL = {3, 4, 5}  # Cancelled, Expired, Rejected


def bracket_prices(direction: str, fill_price: float,
                   stop_points: float, target_points: float):
    """(stop, target) for a filled entry, re-based on the fill price."""
    if direction == "long":
        return fill_price - stop_points, fill_price + target_points
    return fill_price + stop_points, fill_price - target_points


async def _poll_entry_fill(suite, account_id: int, contract_id: str,
                           entry_order_id: int, timeout: float):
    """Poll REST until the entry order fills or goes terminal.

    Returns (fill_price, position) on fill, (None, None) otherwise (terminal
    no-fill status, or timeout with the order still working — the caller
    cancels and re-checks). Poll errors are transient; keep polling.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            order = await suite.orders.get_order_by_id(entry_order_id)
        except Exception as e:
            log.debug("Fill poll: get_order_by_id failed: %s", e)
            order = None
        if order is not None:
            if int(order.status) == _ORDER_FILLED:
                pos = await risk.get_open_position(suite, account_id,
                                                   contract_id)
                fill = order.filledPrice
                if fill is None and pos is not None:
                    fill = pos.averagePrice
                return fill, pos
            if int(order.status) in _ORDER_TERMINAL_NO_FILL:
                return None, None
        if asyncio.get_running_loop().time() >= deadline:
            return None, None
        await asyncio.sleep(FILL_POLL_SECONDS)


async def place_bracket_rest(suite, account_id: int, contract_id: str,
                             sig, size: int, entry_side: int) -> dict:
    """Place an S4 live entry + protectives using REST polling only.

    Returns a dict with outcome one of:
      "protected"        — filled; stop and target working at the broker.
      "no_trade"         — entry never filled; cancelled; account flat.
      "emergency_closed" — filled, but a protective was rejected (e.g.
                           price already through the stop); closed at
                           market immediately rather than sitting exposed.
      "unprotected"      — filled, protective rejected AND the emergency
                           close failed; needs manual attention NOW.
    plus entry_order_id / stop_order_id / target_order_id / fill_price.
    """
    result = {"outcome": "no_trade", "entry_order_id": None,
              "stop_order_id": None, "target_order_id": None,
              "fill_price": None}
    stop_points = abs(sig.entry - sig.stop)
    target_points = abs(sig.target - sig.entry)
    exit_side = 1 - entry_side

    entry_resp = await suite.orders.place_limit_order(
        contract_id=contract_id, side=entry_side, size=size,
        limit_price=sig.entry, account_id=account_id)
    if not entry_resp or not entry_resp.success:
        raise RuntimeError("entry placement rejected: %s"
                           % getattr(entry_resp, "errorMessage", entry_resp))
    entry_id = entry_resp.orderId
    result["entry_order_id"] = entry_id
    log.info("Bracket entry order %s placed (limit %.2f). "
             "Polling REST for fill...", entry_id, sig.entry)

    fill, _pos = await _poll_entry_fill(suite, account_id, contract_id,
                                        entry_id,
                                        timeout=FILL_TIMEOUT_SECONDS)
    if fill is None:
        # No fill inside the window: cancel the working entry, then check
        # once more — a fill can land between the last poll and the cancel.
        try:
            await suite.orders.cancel_order(order_id=entry_id,
                                            account_id=account_id)
        except Exception as e:
            log.warning("Cancel of unfilled entry %s failed: %s", entry_id, e)
        await asyncio.sleep(1.0)
        pos = await risk.get_open_position(suite, account_id, contract_id)
        if pos is None:
            log.info("Entry %s did not fill within %ss; cancelled. "
                     "No trade.", entry_id, FILL_TIMEOUT_SECONDS)
            return result
        fill = pos.averagePrice
        log.warning("Entry %s filled just as it was being cancelled; "
                    "proceeding to protect @ %.2f.", entry_id, fill)
    result["fill_price"] = fill

    stop_price, target_price = bracket_prices(sig.direction, fill,
                                              stop_points, target_points)

    async def _emergency_close(why: str) -> None:
        log.error("Emergency close: %s", why)
        try:
            await suite.positions.close_position_by_contract(
                contract_id=contract_id, account_id=account_id)
            result["outcome"] = "emergency_closed"
        except Exception as e:
            log.critical("Emergency close FAILED: %s — position may be "
                         "open and unprotected!", e)
            result["outcome"] = "unprotected"

    # Stop first — it is the risk limiter. If price is already through it,
    # the gateway rejects it (Nov 10) and we close at market instead.
    try:
        stop_resp = await suite.orders.place_stop_order(
            contract_id=contract_id, side=exit_side, size=size,
            stop_price=stop_price, account_id=account_id)
        if not stop_resp or not stop_resp.success:
            raise RuntimeError(getattr(stop_resp, "errorMessage",
                                       "stop rejected"))
        result["stop_order_id"] = stop_resp.orderId
    except Exception as e:
        await _emergency_close(
            f"protective stop @ {stop_price:.2f} rejected ({e})")
        return result

    try:
        tgt_resp = await suite.orders.place_limit_order(
            contract_id=contract_id, side=exit_side, size=size,
            limit_price=target_price, account_id=account_id)
        if not tgt_resp or not tgt_resp.success:
            raise RuntimeError(getattr(tgt_resp, "errorMessage",
                                       "target rejected"))
        result["target_order_id"] = tgt_resp.orderId
    except Exception as e:
        # Never sit on a half-bracket: pull the stop, then close.
        try:
            await suite.orders.cancel_order(
                order_id=result["stop_order_id"], account_id=account_id)
        except Exception:
            pass
        await _emergency_close(
            f"protective target @ {target_price:.2f} rejected ({e})")
        return result

    result["outcome"] = "protected"
    log.warning("LIVE bracket protected: entry %s filled @ %.2f; "
                "stop %s @ %.2f; target %s @ %.2f.",
                entry_id, fill, result["stop_order_id"], stop_price,
                result["target_order_id"], target_price)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    cfg = load_config()
    mode = str(cfg.get("mode", "paper")).lower()
    live = (mode == "live")
    account_id = int(cfg.get("account_id", 0))
    size = int(cfg.get("size", 1))
    max_daily_loss = float(cfg.get("max_daily_loss", 500.0))
    poll_seconds = int(cfg.get("poll_seconds", 60))
    symbol = str(cfg.get("symbol", "ES"))

    if not cfg.get("username") or not cfg.get("api_key") \
            or "YOUR_" in str(cfg.get("username", "")) \
            or "YOUR_" in str(cfg.get("api_key", "")):
        log.error("TopstepX credentials are placeholders. Set TOPSTEPX_USERNAME / "
                  "TOPSTEPX_API_KEY env vars or fill config.yaml (use a local, "
                  "git-ignored copy for real keys).")
        sys.exit(2)
    if live and not account_id:
        log.error("Live mode requires a real account_id in config.")
        sys.exit(2)

    log.info("S4 bot starting in %s mode (account %s).",
             "LIVE" if live else "PAPER", account_id if account_id else "n/a")
    if not live:
        log.info("Paper mode: signals will be logged; ZERO orders will be placed.")

    # NOTE: we deliberately use the ProjectX REST client directly instead of
    # TradingSuite: TradingSuite forces realtime WebSocket connections, which
    # cannot traverse this machine's egress proxy. The bot's 60s polling loop
    # only needs REST (bars, contracts, positions, orders, trades).
    from project_x_py import ProjectX, OrderSide  # imported late: needs install
    from project_x_py.order_manager import OrderManager
    from project_x_py.position_manager import PositionManager
    from project_x_py.event_bus import EventBus
    from types import SimpleNamespace

    client = ProjectX(username=cfg["username"], api_key=cfg["api_key"])
    await client.authenticate()
    log.info("Connected to TopstepX (REST).")
    _bus = EventBus()
    # Adapter so the rest of the code keeps its suite.client/orders/positions
    # shape without depending on TradingSuite's realtime stack.
    suite = SimpleNamespace(
        client=client,
        orders=OrderManager(client, _bus),
        positions=PositionManager(client, _bus),
    )
    try:
        contract_id = await resolve_es_contract(suite.client)

        # ---- Startup reconciliation FIRST: never double-order after restart.
        position = await risk.get_open_position(suite, account_id, contract_id)
        working = await risk.get_working_orders(suite, account_id, contract_id)
        if position:
            log.warning("Startup: existing position found (%s x%s @ %s). "
                        "Managing it; no new entries until flat.",
                        position.contractId, position.size, position.averagePrice)
        if working:
            log.warning("Startup: %d working order(s) found. No new entries "
                        "until they clear.", len(working))
            for o in working:
                log.warning("  working order id=%s side=%s type=%s limit=%s stop=%s",
                            o.id, o.side, o.type, o.limitPrice, o.stopPrice)

        state = load_state()
        # Trust the broker, not the state file: sync the position flag, and
        # cancel protective orders orphaned by a position that closed while
        # the bot was down (the SDK's client-side OCO never fires over REST).
        state["has_position"] = position is not None
        if live and position is None and state.get("protective_order_ids"):
            working_ids = {o.id for o in working}
            for oid in list(state["protective_order_ids"]):
                if oid in working_ids:
                    try:
                        await suite.orders.cancel_order(
                            order_id=oid, account_id=account_id)
                        log.warning("Startup: cancelled orphaned protective "
                                    "order %s.", oid)
                    except Exception as e:
                        log.warning("Startup: could not cancel order %s: %s",
                                    oid, e)
            state["protective_order_ids"] = []
            state["entry_order_id"] = None
            save_state(state)
        levels = [deserialize_level(d) for d in state.get("levels", [])]
        last_bar_count = 0
        flattened_today = False

        while True:
            now = risk.now_et()

            # Day rollover.
            today = now.strftime("%Y-%m-%d")
            if state.get("date") != today:
                state = blank_state()
                state["date"] = today
                levels = []
                last_bar_count = 0
                flattened_today = False
                log.info("New trading day %s.", today)

            # Outside RTH: wait (exit if the session is over for the day).
            if not risk.in_rth(now):
                if now.astimezone(ET).time() >= risk.RTH_CLOSE:
                    log.info("RTH closed. Exiting.")
                    break
                await asyncio.sleep(poll_seconds)
                continue

            # 16:09 ET flatten — once per day, then stand down.
            if risk.should_flatten(now) and not flattened_today:
                log.warning("16:09 ET flatten: cancelling orders and closing positions.")
                if live:
                    await risk.flatten_all(suite, account_id, contract_id)
                else:
                    log.info("PAPER: would flatten all (cancel orders + close positions).")
                flattened_today = True
                state["has_position"] = False
                state["protective_order_ids"] = []
                state["entry_order_id"] = None
                save_state(state)
                continue

            # Refresh daily realized P&L for the loss halt.
            try:
                state["daily_pnl"] = await risk.get_daily_realized_pnl(
                    suite, account_id) if account_id else 0.0
            except Exception as e:
                log.warning("Could not refresh daily P&L: %s", e)
            if risk.daily_loss_halted(state["daily_pnl"], max_daily_loss):
                if not state.get("halted"):
                    log.error("Daily loss halt hit (%.2f). No new entries today.",
                              state["daily_pnl"])
                    state["halted"] = True
                    save_state(state)

            # Reconcile position state with the broker every poll. When a
            # stop or target fills, the sibling protective order is orphaned
            # (the SDK's client-side OCO never fires over REST polling) and
            # has_position goes stale unless cleaned up here.
            if live and state.get("has_position"):
                try:
                    pos_now = await risk.get_open_position(
                        suite, account_id, contract_id)
                    if pos_now is None:
                        wo_now = await risk.get_working_orders(
                            suite, account_id, contract_id)
                        working_ids = {o.id for o in wo_now}
                        entry_id = state.get("entry_order_id")
                        if entry_id is not None and entry_id in working_ids:
                            pass  # entry limit still working — not filled yet
                        else:
                            for oid in list(state.get("protective_order_ids", [])):
                                if oid in working_ids:
                                    await suite.orders.cancel_order(
                                        order_id=oid, account_id=account_id)
                                    log.warning("Reconcile: cancelled orphaned "
                                                "protective order %s.", oid)
                            state["protective_order_ids"] = []
                            state["entry_order_id"] = None
                            state["has_position"] = False
                            log.info("Reconcile: position closed at broker; "
                                     "state synced to flat.")
                            save_state(state)
                except Exception as e:
                    log.warning("Position reconcile failed: %s", e)

            # Pull latest 5-min bars.
            try:
                df = await suite.client.get_bars(
                    symbol=symbol, days=2, interval=5, unit=2)
            except Exception as e:
                log.warning("get_bars failed: %s", e)
                await asyncio.sleep(poll_seconds)
                continue
            day_bars = bars_from_df(df, today)
            if len(day_bars) <= last_bar_count:
                await asyncio.sleep(poll_seconds)
                continue

            # Process only newly arrived bars.
            new_bars = [b for _, b in day_bars[last_bar_count:]]
            pure_bars = [b for _, b in day_bars]
            last_bar_count = len(day_bars)

            for b in new_bars:
                _, signals = run_detection(pure_bars[:b.idx + 1], levels)
                for sig in signals:
                    state["signals_today"] = state.get("signals_today", 0) + 1
                    fill_note = ("would fill" if would_fill(b, _level_for(sig, levels))
                                 else "fill NOT certain (touch bar >1 tick from L)")
                    log.info("SIGNAL %s L=%.2f entry=%.2f stop=%.2f target=%.2f "
                             "bar=%d (%s)",
                             sig.direction.upper(), sig.L, sig.entry,
                             sig.stop, sig.target, sig.bar_idx, fill_note)

                    # Re-check live broker state right before any order.
                    pos = await risk.get_open_position(suite, account_id, contract_id)
                    wo = await risk.get_working_orders(suite, account_id, contract_id)
                    ok, reason = risk.can_open(
                        has_position=pos is not None,
                        has_working_orders=len(wo) > 0,
                        now=risk.now_et(),
                        daily_pnl=state["daily_pnl"],
                        max_daily_loss=max_daily_loss)
                    if not ok:
                        log.info("Entry blocked by risk: %s", reason)
                        continue

                    if not live:
                        log.info("PAPER: signal logged, no order placed.")
                        continue

                    # LIVE: REST-polling bracket — limit at L; on fill the
                    # 2-pt stop / 7-pt target go in within seconds, re-based
                    # on the actual fill price (see place_bracket_rest).
                    side = (OrderSide.SELL.value if sig.direction == "short"
                            else OrderSide.BUY.value)
                    try:
                        res = await place_bracket_rest(
                            suite, account_id, contract_id, sig, size, side)
                    except Exception as e:
                        # Placement errored outside the handled paths. Do
                        # NOT claim "entry unfilled" (the Nov 10 log did,
                        # wrongly, after a filled emergency close) — the
                        # truth is whatever the broker shows; the pre-order
                        # risk check and the reconcile loop read the broker
                        # directly, so state stays consistent. Keep the
                        # loop alive so the next signal can fire.
                        log.error("LIVE bracket placement error: %s. "
                                  "Continuing; broker state governs.", e)
                        continue
                    if res["outcome"] == "protected":
                        state["has_position"] = True
                        state["entry_order_id"] = res["entry_order_id"]
                        state["protective_order_ids"] = [
                            oid for oid in (res["stop_order_id"],
                                            res["target_order_id"]) if oid]
                        save_state(state)
                    elif res["outcome"] == "unprotected":
                        # Position may be open with NO protectives. Flag it
                        # as ours so reconcile/startup treat it correctly;
                        # the daily-loss halt and 16:09 flatten still apply.
                        state["has_position"] = True
                        state["entry_order_id"] = res["entry_order_id"]
                        state["protective_order_ids"] = []
                        save_state(state)
                        log.critical("Position UNPROTECTED after bracket "
                                     "failure — manual attention needed.")
                    # "no_trade" / "emergency_closed": flat; nothing to record.

            state["levels"] = [serialize_level(lv) for lv in levels]
            save_state(state)
            await asyncio.sleep(poll_seconds)
    finally:
        # REST client holds no realtime connections; nothing to tear down.
        log.info("S4 bot stopped.")


def _level_for(sig: Signal, levels) -> Level:
    for lv in levels:
        if lv.L == sig.L and lv.first_idx <= sig.bar_idx:
            return lv
    return Level(direction="top" if sig.direction == "short" else "bottom",
                 L=sig.L, first_idx=sig.bar_idx)


if __name__ == "__main__":
    asyncio.run(main())
