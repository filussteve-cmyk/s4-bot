"""Risk guards for the S4 bot — pure checks plus async broker helpers.

Pure functions (in_rth, should_flatten, daily_loss_halted, can_open) take
explicit arguments and are unit-testable. The async helpers query the
TopstepX account through the project-x-py REST client (no realtime needed).
"""

from datetime import datetime, time
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)     # regular trading hours start (ET)
RTH_CLOSE = time(16, 0)    # regular trading hours end (ET)
FLATTEN_TIME = time(15, 55)  # flatten everything at 15:55 ET — no overnight, ever


# ---------------------------------------------------------------------------
# Pure guards
# ---------------------------------------------------------------------------

def now_et() -> datetime:
    return datetime.now(ET)


def in_rth(now: Optional[datetime] = None) -> bool:
    """True during the 09:30–16:00 ET RTH window."""
    now = now or now_et()
    cur = now.astimezone(ET).time() if now.tzinfo else now.time()
    return RTH_OPEN <= cur < RTH_CLOSE


def should_flatten(now: Optional[datetime] = None) -> bool:
    """True at/after 15:55 ET — flatten everything, no overnight positions."""
    now = now or now_et()
    cur = now.astimezone(ET).time() if now.tzinfo else now.time()
    return cur >= FLATTEN_TIME


def daily_loss_halted(daily_pnl: float, max_daily_loss: float = 500.0) -> bool:
    """Halt new entries once realized daily P&L hits -max_daily_loss."""
    return daily_pnl <= -abs(max_daily_loss)


def can_open(has_position: bool,
             has_working_orders: bool,
             now: Optional[datetime] = None,
             daily_pnl: float = 0.0,
             max_daily_loss: float = 500.0) -> Tuple[bool, str]:
    """Gate for a new entry. Returns (ok, reason)."""
    now = now or now_et()
    if not in_rth(now):
        return False, "outside RTH"
    if should_flatten(now):
        return False, "past flatten time (15:55 ET)"
    if has_position:
        return False, "position already open (max 1)"
    if has_working_orders:
        return False, "working orders present"
    if daily_loss_halted(daily_pnl, max_daily_loss):
        return False, f"daily loss halt hit ({daily_pnl:.2f} <= -{max_daily_loss:.2f})"
    return True, "ok"


# ---------------------------------------------------------------------------
# Async broker helpers (require a connected TradingSuite)
# ---------------------------------------------------------------------------

async def get_open_position(suite, account_id: int, contract_id: str):
    """Return the open position for our contract, or None."""
    positions = await suite.positions.get_all_positions(account_id=account_id)
    for p in positions:
        if p.contractId == contract_id and p.size != 0:
            return p
    return None


async def get_working_orders(suite, account_id: int, contract_id: str):
    """Return working (non-terminal) orders for our contract."""
    working_statuses = {1, 6, 7, 8}  # OPEN, PENDING, PENDING_CANCELLATION, SUSPENDED
    orders = await suite.orders.search_open_orders(
        contract_id=contract_id, account_id=account_id)
    return [o for o in orders
            if o.contractId == contract_id and int(o.status) in working_statuses]


async def flatten_all(suite, account_id: int, contract_id: str) -> None:
    """Cancel all working orders and close all positions. Idempotent."""
    await suite.orders.cancel_all_orders(
        contract_id=contract_id, account_id=account_id)
    await suite.positions.close_all_positions(
        contract_id=contract_id, account_id=account_id)


async def get_daily_realized_pnl(suite, account_id: int) -> float:
    """Net realized P&L for today (ET) from the trade history.

    Assumes the API reports gross profitAndLoss with fees/commissions in
    separate fields (TopstepX/ProjectX convention). If the venue ever nets
    fees into profitAndLoss, this would double-subtract — the state file
    keeps our own closed-trade record as a cross-check.
    """
    start = now_et().replace(hour=0, minute=0, second=0, microsecond=0)
    trades = await suite.client.search_trades(
        start_date=start, account_id=account_id, limit=500)
    pnl = 0.0
    for t in trades:
        if t.voided:
            continue
        pnl += (t.profitAndLoss or 0.0) - (t.fees or 0.0) - (t.commissions or 0.0)
    return pnl
