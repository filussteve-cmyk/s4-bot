"""Tests for bot.py's REST-polling bracket (place_bracket_rest).

The fake suite stands in for the TopstepX REST managers: no network, no
real orders. Covers the Nov 10, 2026 failure mode — the SDK bracket waited
60s on a websocket fill event that never comes in REST-only mode, so the
protective stop was placed after price had already crossed it and got
rejected. The REST bracket must: detect the fill in ~1.5s polls, re-base
stop/target on the actual fill price, and emergency-close if a protective
is rejected.

Run:  .venv/bin/python -m pytest test_bracket.py -q
"""

import asyncio
from types import SimpleNamespace

import bot
from strategy import Signal

bot.FILL_POLL_SECONDS = 0.01  # keep tests fast

ACCOUNT = 18447721
CONTRACT = "CON.F.US.EP.Z26"


def _resp(order_id, success=True, msg=None):
    return SimpleNamespace(orderId=order_id, success=success,
                           errorMessage=msg)


def _order(status, filled=None):
    return SimpleNamespace(status=status, filledPrice=filled)


def _position(avg_price, size=1):
    return SimpleNamespace(contractId=CONTRACT, size=size,
                           averagePrice=avg_price)


class FakeOrders:
    """Scripted OrderManager stand-in.

    fill_after: number of get_order_by_id polls before the entry reports
    Filled (None = never fills). stop_error / target_error: exception to
    raise from the matching placement.
    """

    def __init__(self, fill_after=1, fill_price=7502.50,
                 stop_error=None, target_error=None):
        self.fill_after = fill_after
        self.fill_price = fill_price
        self.stop_error = stop_error
        self.target_error = target_error
        self.polls = 0
        self.placed = []      # (kind, kwargs) in placement order
        self.cancelled = []

    async def place_limit_order(self, **kw):
        is_entry = not self.placed
        self.placed.append(("entry_limit" if is_entry else "target_limit", kw))
        return _resp(111 if is_entry else 333)

    async def place_stop_order(self, **kw):
        self.placed.append(("stop", kw))
        if self.stop_error:
            raise self.stop_error
        return _resp(222)

    async def get_order_by_id(self, order_id):
        self.polls += 1
        if self.fill_after is not None and self.polls >= self.fill_after:
            return _order(2, filled=self.fill_price)
        return _order(1)

    async def cancel_order(self, order_id=None, account_id=None):
        self.cancelled.append(order_id)
        return True


class FakePositions:
    def __init__(self, orders: FakeOrders):
        self.orders = orders
        self.closed = 0

    async def get_all_positions(self, account_id=None):
        filled = (self.orders.fill_after is not None
                  and self.orders.polls >= self.orders.fill_after)
        return [_position(self.orders.fill_price)] if filled else []

    async def close_position_by_contract(self, contract_id=None,
                                         account_id=None):
        self.closed += 1
        return {"success": True}


def _suite(**kw):
    orders = FakeOrders(**kw)
    return SimpleNamespace(orders=orders, positions=FakePositions(orders))


def _sig(direction="long"):
    if direction == "long":
        return Signal(direction="long", L=7503.00, entry=7503.00,
                      stop=7501.00, target=7510.00, bar_idx=37)
    return Signal(direction="short", L=7503.00, entry=7503.00,
                  stop=7505.00, target=7496.00, bar_idx=37)


def _run(suite, sig, entry_side):
    return asyncio.run(bot.place_bracket_rest(suite, ACCOUNT, CONTRACT,
                                              sig, 1, entry_side))


# ---------------------------------------------------------------------------
# bracket_prices (pure)
# ---------------------------------------------------------------------------

def test_bracket_prices_long():
    stop, target = bot.bracket_prices("long", 7502.50, 2.0, 7.0)
    assert (stop, target) == (7500.50, 7509.50)


def test_bracket_prices_short():
    stop, target = bot.bracket_prices("short", 7502.50, 2.0, 7.0)
    assert (stop, target) == (7504.50, 7495.50)


# ---------------------------------------------------------------------------
# place_bracket_rest against the fake broker
# ---------------------------------------------------------------------------

def test_long_fill_protects_rebased_on_fill_price():
    suite = _suite(fill_after=2, fill_price=7502.50)
    res = _run(suite, _sig("long"), entry_side=0)
    assert res["outcome"] == "protected"
    assert res["fill_price"] == 7502.50
    kinds = [k for k, _ in suite.orders.placed]
    assert kinds == ["entry_limit", "stop", "target_limit"]
    # Stop/target re-based on the FILL (7502.50), not on L (7503.00).
    assert suite.orders.placed[1][1]["stop_price"] == 7500.50
    assert suite.orders.placed[2][1]["limit_price"] == 7509.50
    # Exit side is the opposite of the entry side (buy -> sell).
    assert suite.orders.placed[1][1]["side"] == 1
    assert suite.orders.placed[2][1]["side"] == 1
    assert res["stop_order_id"] == 222 and res["target_order_id"] == 333


def test_short_fill_protects_rebased_on_fill_price():
    suite = _suite(fill_after=1, fill_price=7503.25)
    res = _run(suite, _sig("short"), entry_side=1)
    assert res["outcome"] == "protected"
    assert suite.orders.placed[1][1]["stop_price"] == 7505.25
    assert suite.orders.placed[2][1]["limit_price"] == 7496.25
    assert suite.orders.placed[1][1]["side"] == 0


def test_no_fill_cancels_and_reports_no_trade():
    old_timeout = bot.FILL_TIMEOUT_SECONDS
    bot.FILL_TIMEOUT_SECONDS = 0.05
    try:
        suite = _suite(fill_after=None)
        res = _run(suite, _sig("long"), entry_side=0)
    finally:
        bot.FILL_TIMEOUT_SECONDS = old_timeout
    assert res["outcome"] == "no_trade"
    assert suite.orders.cancelled == [111]
    assert [k for k, _ in suite.orders.placed] == ["entry_limit"]
    assert suite.positions.closed == 0


def test_stop_rejected_emergency_closes():
    # The Nov 10 case: price already through the stop when the protective
    # is placed -> gateway rejects -> close at market, never sit exposed.
    suite = _suite(fill_after=1,
                   stop_error=RuntimeError("Order price is outside "
                                           "allowed range."))
    res = _run(suite, _sig("long"), entry_side=0)
    assert res["outcome"] == "emergency_closed"
    assert suite.positions.closed == 1
    assert [k for k, _ in suite.orders.placed] == ["entry_limit", "stop"]


def test_target_rejected_cancels_stop_and_closes():
    suite = _suite(fill_after=1,
                   target_error=RuntimeError("target rejected"))
    # place_limit_order serves both entry and target in the fake; emulate a
    # target-only failure by raising on the second limit placement.
    orig = suite.orders.place_limit_order
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("target rejected")
        return await orig(**kw)

    suite.orders.place_limit_order = flaky
    res = _run(suite, _sig("long"), entry_side=0)
    assert res["outcome"] == "emergency_closed"
    assert suite.orders.cancelled == [222]  # stop pulled first
    assert suite.positions.closed == 1
