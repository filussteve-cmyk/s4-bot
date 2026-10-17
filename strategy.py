"""S4 Double Top/Bottom strategy — pure functions, no I/O.

Implements the exact validated rules; nothing here touches the network,
the filesystem, or the clock. All bars are identified by integer index,
so this module is fully unit-testable.

Rules (do not improvise):
  - 5-minute bars, ES futures.
  - Double top: a swing high = the highest high of +/-5 bars. Price pulls
    back >=10 points, then retests the high >=30 minutes (>=6 five-minute
    bars) after the first touch. The second touch counts only if its bar's
    high is within [L-1.0, L+0.5], where L = the first extreme's price.
    Mirror image for double bottoms (lowest low of +/-5 bars, pullback
    >=10 points up, retest low within [L-0.5, L+1.0]).
  - Level retired: any bar prints high > L+0.5 (tops) / low < L-0.5
    (bottoms) before the second touch.
  - Invalid: a third touch of the same level (no triple tops/bottoms).
  - Entry: limit at exactly L on the second touch. A fill requires the
    touch bar to trade within 1 tick (0.25 points) of L.
  - Stop: 4 points. Target: 5 points.
  - One attempt per level per day. Max 1 open position at a time
    (enforced by the caller via risk.can_open).
"""

from dataclasses import dataclass, field
from typing import List, Tuple

# --- Strategy constants (validated spec; do not tune casually) ---
TICK = 0.25
SWING_LOOKBACK = 5            # swing = extreme of +/-5 bars
MIN_PULLBACK_POINTS = 10.0    # pullback must be >=10 points
MIN_BARS_BETWEEN_TOUCHES = 6  # retest >=30 min (>=6 five-min bars) after first touch
TOUCH_WINDOW_BELOW = 1.0      # tops: second-touch high must be within [L-1.0, L+0.5]
TOUCH_WINDOW_ABOVE = 0.5
FILL_TOL_POINTS = TICK        # fill requires the touch bar within 1 tick of L
STOP_POINTS = 4.0
TARGET_POINTS = 5.0


@dataclass
class Bar:
    """One 5-minute bar. idx is the intraday bar index (0-based)."""
    idx: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class Level:
    """A swing extreme being watched for a double top/bottom retest."""
    direction: str        # "top" | "bottom"
    L: float              # extreme price (first touch)
    first_idx: int        # bar index of the extreme
    state: str = "active"  # active | retired | attempted | invalidated
    touches: int = 1
    pullback_done: bool = False


@dataclass
class Signal:
    """A second touch: place a limit order at exactly L with bracket."""
    direction: str   # "short" | "long"
    L: float
    entry: float     # == L
    stop: float      # L+4 (short) / L-4 (long)
    target: float    # L-5 (short) / L+5 (long)
    bar_idx: int


# ---------------------------------------------------------------------------
# Swing detection
# ---------------------------------------------------------------------------

def _is_unique_extreme(values: List[float], i: int, lookback: int,
                       want_max: bool) -> bool:
    lo = max(0, i - lookback)
    hi = min(len(values), i + lookback + 1)
    window = values[lo:hi]
    extreme = max(window) if want_max else min(window)
    if values[i] != extreme:
        return False
    # Deterministic tie-break: only the first bar carrying the extreme counts.
    first = window.index(extreme) + lo
    return first == i


def is_swing_high(highs: List[float], i: int,
                  lookback: int = SWING_LOOKBACK) -> bool:
    """True if bar i is the (first) highest high of the +/-lookback window."""
    if i - lookback < 0 or i + lookback >= len(highs):
        return False
    return _is_unique_extreme(highs, i, lookback, want_max=True)


def is_swing_low(lows: List[float], i: int,
                 lookback: int = SWING_LOOKBACK) -> bool:
    """True if bar i is the (first) lowest low of the +/-lookback window."""
    if i - lookback < 0 or i + lookback >= len(lows):
        return False
    return _is_unique_extreme(lows, i, lookback, want_max=False)


def detect_new_swings(bars: List[Bar],
                      levels: List[Level]) -> List[Level]:
    """Detect swings newly confirmed by the latest bar.

    When bar ``n`` arrives, bar ``n - SWING_LOOKBACK`` has just completed its
    +/-5 window, so only that bar can newly confirm. Returns new Level
    objects (deduped against ``levels`` by bar index + direction).
    """
    new_levels: List[Level] = []
    if len(bars) <= 2 * SWING_LOOKBACK:
        return new_levels
    i = len(bars) - 1 - SWING_LOOKBACK
    bar = bars[i]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    known = {(lv.first_idx, lv.direction) for lv in levels}
    if is_swing_high(highs, i) and (bar.idx, "top") not in known:
        new_levels.append(Level(direction="top", L=bar.high, first_idx=bar.idx))
    if is_swing_low(lows, i) and (bar.idx, "bottom") not in known:
        new_levels.append(Level(direction="bottom", L=bar.low, first_idx=bar.idx))
    return new_levels


# ---------------------------------------------------------------------------
# Level lifecycle + signal generation
# ---------------------------------------------------------------------------

def would_fill(bar: Bar, level: Level) -> bool:
    """Would a limit order at exactly L fill on this bar?

    The touch bar must trade within 1 tick (0.25 points) of L.
    """
    if level.direction == "top":      # sell limit at L
        return bar.high >= level.L - FILL_TOL_POINTS
    return bar.low <= level.L + FILL_TOL_POINTS  # buy limit at L


def update_levels(levels: List[Level], bars: List[Bar]) -> List[Signal]:
    """Advance every active level by one bar (bars[-1]).

    Order of checks per level: retirement -> second/third touch -> pullback.
    The pullback must complete on a strictly earlier bar than the retest
    ("pulls back, THEN retests"). Returns signals for second touches.
    """
    signals: List[Signal] = []
    if not bars:
        return signals
    bar = bars[-1]

    for lv in levels:
        if lv.state in ("retired", "invalidated"):
            continue
        if lv.direction == "top":
            # 1. Retired: any bar prints high > L+0.5 before the second touch.
            if lv.state == "active" and bar.high > lv.L + TOUCH_WINDOW_ABOVE:
                lv.state = "retired"
                continue
            # 2. Second/third touch: pullback done, >=6 bars after the first
            #    touch, and this bar's high within [L-1.0, L+0.5].
            if (lv.pullback_done
                    and bar.idx - lv.first_idx >= MIN_BARS_BETWEEN_TOUCHES
                    and lv.L - TOUCH_WINDOW_BELOW <= bar.high
                    <= lv.L + TOUCH_WINDOW_ABOVE):
                lv.touches += 1
                if lv.state == "active" and lv.touches == 2:
                    lv.state = "attempted"  # one attempt per level per day
                    signals.append(Signal(
                        direction="short",
                        L=lv.L,
                        entry=lv.L,
                        stop=lv.L + STOP_POINTS,
                        target=lv.L - TARGET_POINTS,
                        bar_idx=bar.idx,
                    ))
                else:
                    # Third touch of the same level: no triple tops.
                    lv.state = "invalidated"
                continue
            # 3. Pullback tracking: price fell >=10 points from L.
            if lv.state == "active" and bar.low <= lv.L - MIN_PULLBACK_POINTS:
                lv.pullback_done = True
        else:  # bottom — mirror image
            if lv.state == "active" and bar.low < lv.L - TOUCH_WINDOW_ABOVE:
                lv.state = "retired"
                continue
            if (lv.pullback_done
                    and bar.idx - lv.first_idx >= MIN_BARS_BETWEEN_TOUCHES
                    and lv.L - TOUCH_WINDOW_ABOVE <= bar.low
                    <= lv.L + TOUCH_WINDOW_BELOW):
                lv.touches += 1
                if lv.state == "active" and lv.touches == 2:
                    lv.state = "attempted"
                    signals.append(Signal(
                        direction="long",
                        L=lv.L,
                        entry=lv.L,
                        stop=lv.L - STOP_POINTS,
                        target=lv.L + TARGET_POINTS,
                        bar_idx=bar.idx,
                    ))
                else:
                    lv.state = "invalidated"
                continue
            if lv.state == "active" and bar.high >= lv.L + MIN_PULLBACK_POINTS:
                lv.pullback_done = True

    return signals


def run_detection(bars: List[Bar], levels: List[Level]) -> Tuple[List[Level], List[Signal]]:
    """One detection step for a newly arrived bar.

    Appends newly confirmed levels to ``levels`` in place and returns
    (new_levels, signals). The caller must NOT extend ``levels`` again.
    """
    new_levels = detect_new_swings(bars, levels)
    levels.extend(new_levels)
    signals = update_levels(levels, bars)
    return new_levels, signals
