"""Tests for strategy.py (pure functions) and risk.py guards.

Run:  python -m pytest test_strategy.py -v   (or: python test_strategy.py)

Section 1: hand-crafted bar sequences verifying each exact rule.
Section 2: synthetic ~1-month 5-min ES dataset asserting roughly
           1 signal per day (the validated strategy's observed rate).
"""

import random
from datetime import datetime

import pytest

from strategy import (
    Bar, Level, Signal,
    is_swing_high, is_swing_low,
    detect_new_swings, update_levels, run_detection, would_fill,
    SWING_LOOKBACK,
)
import risk
from risk import ET


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mk(idx, o, h, l, c):
    return Bar(idx=idx, open=o, high=h, low=l, close=c)


def flat(start, n, price=6000.0):
    """n flat bars starting at idx `start`."""
    return [mk(start + i, price, price + 0.5, price - 0.5, price)
            for i in range(n)]


def run_all(bars):
    """Feed bars sequentially exactly like the live loop."""
    levels = []
    signals = []
    for i in range(1, len(bars) + 1):
        _, sigs = run_detection(bars[:i], levels)
        signals.extend(sigs)
    return levels, signals


def double_top_bars(touch_high=6014.9, touch_idx=11, pullback_low=6004.0,
                    breakout_high=None):
    """Hand-crafted double top. Swing high L=6015 at idx 5."""
    bars = flat(0, 5)
    bars.append(mk(5, 6012, 6015, 6011, 6014))          # swing high, L=6015
    bars.append(mk(6, 6014, 6014.2, 6010, 6011))
    bars.append(mk(7, 6011, 6011.5, pullback_low + 1.0, 6008))
    bars.append(mk(8, 6008, 6008.5, pullback_low + 2.0, 6006))
    bars.append(mk(9, 6006, 6006.5, pullback_low, 6005))  # pullback extreme
    bars.append(mk(10, 6005, 6007, pullback_low + 0.5, 6006))
    if breakout_high is not None:
        bars.append(mk(11, 6006, breakout_high, 6009, 6012))
    else:
        bars.append(mk(11, 6006, touch_high, 6009, 6012))  # second touch
    # extra bars so a third touch can be appended by the caller
    bars.append(mk(12, 6012, 6012.5, 6008, 6010))
    bars.append(mk(13, 6010, 6011, 6007, 6008))
    return bars


def double_bottom_bars(touch_low=5985.1, pullback_high=5996.0):
    """Hand-crafted double bottom. Swing low L=5985 at idx 5."""
    bars = flat(0, 5)
    bars.append(mk(5, 5988, 5989, 5985, 5986))          # swing low, L=5985
    bars.append(mk(6, 5986, 5990, 5985.5, 5989))
    bars.append(mk(7, 5989, 5992, 5988, 5991))
    bars.append(mk(8, 5991, 5994, 5990, 5993))
    bars.append(mk(9, 5993, pullback_high, 5992, 5995))  # pullback
    bars.append(mk(10, 5995, 5996.5, 5993, 5994))
    bars.append(mk(11, 5994, 5995, touch_low, 5990))     # second touch
    bars.append(mk(12, 5990, 5992, 5988, 5989))
    return bars


# ---------------------------------------------------------------------------
# 1. Swing detection
# ---------------------------------------------------------------------------

def test_swing_high_detection():
    highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10]
    assert is_swing_high(highs, 5)
    assert not is_swing_high(highs, 4)
    assert not is_swing_high(highs, 6)
    # edges can never confirm (incomplete window)
    assert not is_swing_high(highs, 0)
    assert not is_swing_high(highs, 10)


def test_swing_low_detection():
    lows = [10, 9, 8, 7, 6, 2, 6, 7, 8, 9, 10]
    assert is_swing_low(lows, 5)
    assert not is_swing_low(lows, 4)
    assert not is_swing_low(lows, 6)


def test_swing_tie_goes_to_first_bar():
    # Two bars share the extreme; only the first counts (deterministic).
    highs = [10, 10, 10, 10, 10, 20, 20, 10, 10, 10, 10]
    assert is_swing_high(highs, 5)
    assert not is_swing_high(highs, 6)


# ---------------------------------------------------------------------------
# 2. Double top / bottom lifecycle
# ---------------------------------------------------------------------------

def test_double_top_full_lifecycle():
    bars = double_top_bars()
    levels, signals = run_all(bars)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction == "short"
    assert sig.L == 6015
    assert sig.entry == 6015
    assert sig.stop == 6019      # 4-pt stop
    assert sig.target == 6010    # 5-pt target
    assert sig.bar_idx == 11
    # level consumed: one attempt per level per day
    top = [lv for lv in levels if lv.direction == "top" and lv.L == 6015][0]
    assert top.state == "attempted"
    assert top.touches == 2
    assert top.pullback_done


def test_double_bottom_mirror():
    bars = double_bottom_bars()
    levels, signals = run_all(bars)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction == "long"
    assert sig.L == 5985
    assert sig.entry == 5985
    assert sig.stop == 5981
    assert sig.target == 5990
    assert sig.bar_idx == 11


def test_would_fill_within_one_tick():
    bars = double_top_bars(touch_high=6014.9)  # 0.1 below L -> fills
    levels, _ = run_all(bars)
    top = [lv for lv in levels if lv.L == 6015][0]
    assert would_fill(bars[11], top)

    bars = double_top_bars(touch_high=6014.5)  # 0.5 below L -> no fill
    levels, signals = run_all(bars)
    assert len(signals) == 1  # still a valid second touch -> order placed
    top = [lv for lv in levels if lv.L == 6015][0]
    assert not would_fill(bars[11], top)


# ---------------------------------------------------------------------------
# 3. Retirement / invalidation rules
# ---------------------------------------------------------------------------

def test_level_retired_on_breakout():
    bars = double_top_bars(breakout_high=6015.8)  # > L+0.5 before 2nd touch
    levels, signals = run_all(bars)
    assert signals == []
    top = [lv for lv in levels if lv.L == 6015][0]
    assert top.state == "retired"


def test_level_retired_on_breakout_bottom():
    bars = double_bottom_bars()
    bars[11] = mk(11, 5994, 5995, 5984.2, 5990)  # low < L-0.5
    levels, signals = run_all(bars)
    assert signals == []
    bot = [lv for lv in levels if lv.L == 5985][0]
    assert bot.state == "retired"


def test_third_touch_invalidates_no_second_signal():
    bars = double_top_bars()
    # third touch of the same level at idx 15 (in window, pullback done)
    bars.extend([mk(14, 6008, 6010, 6005, 6007),
                 mk(15, 6007, 6014.8, 6005, 6010)])
    levels, signals = run_all(bars)
    assert len(signals) == 1  # exactly one attempt per level per day
    top = [lv for lv in levels if lv.L == 6015][0]
    assert top.state == "invalidated"
    assert top.touches == 3


def test_shallow_pullback_no_signal():
    bars = double_top_bars(pullback_low=6007.0)  # only 8 pts < 10 required
    levels, signals = run_all(bars)
    assert signals == []
    top = [lv for lv in levels if lv.L == 6015][0]
    assert top.state == "active"
    assert not top.pullback_done


def test_retest_too_early_no_signal_then_fires():
    # touch at idx 10 is only 5 bars after the extreme (< 6 required)
    bars = double_top_bars()
    bars[10] = mk(10, 6005, 6014.9, 6004.5, 6006)  # in window but too early
    levels, signals = run_all(bars)
    assert len(signals) == 1
    assert signals[0].bar_idx == 11  # fires on the valid retest, not idx 10


def test_touch_below_window_ignored_then_valid_touch():
    bars = double_top_bars(touch_high=6013.5)  # below [L-1.0, L+0.5]
    levels, signals = run_all(bars)
    assert signals == []
    top = [lv for lv in levels if lv.L == 6015][0]
    assert top.state == "active"  # not retired, not attempted
    # a later valid touch still works
    bars.append(mk(12, 6012, 6014.9, 6008, 6010))
    levels, signals = run_all(bars)
    assert len(signals) == 1
    assert signals[0].bar_idx == 12


def test_detect_new_swings_confirms_five_bars_late():
    bars = double_top_bars()
    levels = []
    # before bar 10 arrives, the swing at idx 5 cannot be confirmed
    new, _ = run_detection(bars[:10], levels)
    assert new == []
    new, _ = run_detection(bars[:11], levels)
    assert len(new) == 1 and new[0].L == 6015


# ---------------------------------------------------------------------------
# 4. Risk guards
# ---------------------------------------------------------------------------

def _et(h, m):
    return datetime(2026, 10, 19, h, m, tzinfo=ET)  # a Monday


def test_in_rth():
    assert risk.in_rth(_et(9, 30))
    assert risk.in_rth(_et(12, 0))
    assert risk.in_rth(_et(15, 54))
    assert not risk.in_rth(_et(9, 29))
    assert not risk.in_rth(_et(16, 0))
    assert not risk.in_rth(_et(20, 0))


def test_should_flatten():
    assert not risk.should_flatten(_et(16, 8))
    assert risk.should_flatten(_et(16, 9))
    assert risk.should_flatten(_et(16, 30))


def test_daily_loss_halt():
    assert risk.daily_loss_halted(-500.0, 500.0)
    assert risk.daily_loss_halted(-600.0, 500.0)
    assert not risk.daily_loss_halted(-499.99, 500.0)
    assert not risk.daily_loss_halted(100.0, 500.0)


def test_can_open_gates():
    ok, _ = risk.can_open(False, False, _et(10, 0), 0.0, 500.0)
    assert ok
    ok, reason = risk.can_open(True, False, _et(10, 0), 0.0, 500.0)
    assert not ok and "position" in reason
    ok, reason = risk.can_open(False, True, _et(10, 0), 0.0, 500.0)
    assert not ok and "working" in reason
    ok, reason = risk.can_open(False, False, _et(10, 0), -500.0, 500.0)
    assert not ok and "halt" in reason
    ok, reason = risk.can_open(False, False, _et(16, 10), 0.0, 500.0)
    assert not ok and "RTH" in reason  # RTH close (16:00) gates entries before flatten (16:09)
    ok, reason = risk.can_open(False, False, _et(8, 0), 0.0, 500.0)
    assert not ok and "RTH" in reason


# ---------------------------------------------------------------------------
# 5. Synthetic ~1-month sanity test: roughly 1 signal per day
# ---------------------------------------------------------------------------

def _embed_top(bars, base):
    L = base + 15
    pts = {
        10: (base + 2.8, base + 3.3, base + 2.3, base + 3.0),
        11: (base + 5.8, base + 6.3, base + 5.3, base + 6.0),
        12: (base + 8.8, base + 9.3, base + 8.3, base + 9.0),
        13: (base + 11.8, base + 12.3, base + 11.3, base + 12.0),
        14: (base + 13.8, base + 14.3, base + 13.3, base + 14.0),
        15: (base + 14.5, L, base + 14.0, base + 14.7),          # swing high
    }
    for i in range(16, 27):  # pullback to L-12
        c = base + 14 - (i - 15)
        pts[i] = (c - 0.2, c + 0.3, c - 0.3, c)
    for i in range(27, 37):  # rally back
        c = base + 3 + (i - 27)
        pts[i] = (c - 0.2, c + 0.3, c - 0.3, c)
    pts[37] = (base + 14.0, L - 0.1, base + 13.5, base + 14.2)  # 2nd touch
    for i in range(38, 78):  # quiet drift, no third touch
        c = base + 11 + (i % 3) * 0.5
        pts[i] = (c - 0.2, c + 0.3, c - 0.3, c)
    for idx, (o, h, l, c) in pts.items():
        bars[idx] = [o, h, l, c]


def _embed_bottom(bars, base):
    Lb = base - 15
    pts = {
        10: (base - 2.8, base - 2.3, base - 3.3, base - 3.0),
        11: (base - 5.8, base - 5.3, base - 6.3, base - 6.0),
        12: (base - 8.8, base - 8.3, base - 9.3, base - 9.0),
        13: (base - 11.8, base - 11.3, base - 12.3, base - 12.0),
        14: (base - 13.8, base - 13.3, base - 14.3, base - 14.0),
        15: (base - 14.5, base - 14.0, Lb, base - 14.7),          # swing low
    }
    for i in range(16, 27):  # pullback up to Lb+12
        c = base - 14 + (i - 15)
        pts[i] = (c - 0.2, c + 0.3, c - 0.3, c)
    for i in range(27, 37):  # fade back
        c = base - 3 - (i - 27)
        pts[i] = (c - 0.2, c + 0.3, c - 0.3, c)
    pts[37] = (base - 14.0, base - 13.5, Lb + 0.1, base - 14.2)  # 2nd touch
    for i in range(38, 78):
        c = base - 11 - (i % 3) * 0.5
        pts[i] = (c - 0.2, c + 0.3, c - 0.3, c)
    for idx, (o, h, l, c) in pts.items():
        bars[idx] = [o, h, l, c]


def _gen_day(day_idx, pattern):
    rng = random.Random(1000 + day_idx)
    base = 6000.0
    raw = []
    for _ in range(78):
        c = base + rng.gauss(0, 0.2)
        o = c + rng.gauss(0, 0.1)
        h = max(o, c) + abs(rng.gauss(0, 0.15))
        l = min(o, c) - abs(rng.gauss(0, 0.15))
        raw.append([o, h, l, c])
    if pattern == "top":
        _embed_top(raw, base)
    elif pattern == "bottom":
        _embed_bottom(raw, base)
    return [Bar(idx=i, open=o, high=h, low=l, close=c)
            for i, (o, h, l, c) in enumerate(raw)]


def test_signals_per_day_roughly_one():
    days = 22
    total_signals = 0
    for d in range(days):
        pattern = "top" if d % 2 == 0 else "bottom"
        bars = _gen_day(d, pattern)
        levels = []  # fresh levels each day (one attempt per level per day)
        _, signals = run_all(bars)
        total_signals += len(signals)
    rate = total_signals / days
    print(f"\nsynthetic month: {total_signals} signals / {days} days = {rate:.2f}/day")
    # The validated strategy produced ~434 signals over ~455 sessions (~0.95/day).
    assert 0.5 <= rate <= 1.5, f"signal rate {rate:.2f}/day outside expected band"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
