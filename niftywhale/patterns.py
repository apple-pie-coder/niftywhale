"""
Candlestick patterns: price-action confirmation next to the CHoCH.

Bullish ones for long zones; for short zones the same detectors run on the
mirrored chart, which turns each into its bearish twin (an engulfing into a
bearish engulfing, a hammer into a shooting star, a morning star into an
evening star, an inside-bar breakout into a breakdown).

Pure functions over an OHLC frame, like smc.py. Each detector looks at the
candle at position i (and the ones just before it) and answers yes or no;
`detect()` runs all of them over the most recent candles.

The reversal patterns (engulfing, hammer, morning star) only count after a
short decline -- a hammer in the middle of a rally is not a reversal of
anything -- which is what keeps this from flagging half of all candles.
"""
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

PATTERNS = {
    'engulfing':    'Bullish engulfing',
    'hammer':       'Hammer / pin bar',
    'inside_break': 'Inside-bar breakout',
    'morning_star': 'Morning star',
}
SHORT = {'engulfing': 'BE', 'hammer': 'H', 'inside_break': 'IB', 'morning_star': 'MS'}
BEARISH = {'engulfing': ('engulfing_bear', 'Bearish engulfing', 'BrE'),
           'hammer': ('shooting_star', 'Shooting star', 'SS'),
           'inside_break': ('inside_breakdown', 'Inside-bar breakdown', 'IBd'),
           'morning_star': ('evening_star', 'Evening star', 'ES')}
PATTERNS.update({k: label for k, label, _ in BEARISH.values()})
SHORT.update({k: short for k, _, short in BEARISH.values()})

DECLINE_BARS = 3          # "after a decline": the close before the pattern is below the close this many bars earlier
AVG_BODY_BARS = 20


def _arrays(frame: pd.DataFrame):
    df = frame[['Open', 'High', 'Low', 'Close']].dropna()
    return df, *(df[k].to_numpy(dtype=float) for k in ('Open', 'High', 'Low', 'Close'))


def _declining(c: np.ndarray, i: int) -> bool:
    """The close before candle i is lower than DECLINE_BARS closes earlier."""
    j = i - 1
    return j - DECLINE_BARS >= 0 and c[j] < c[j - DECLINE_BARS]


def engulfing(o, h, l, c, i, avg_body) -> bool:
    """A down candle, then an up candle whose body covers it completely."""
    if i < 1:
        return False
    prev_down, now_up = c[i - 1] < o[i - 1], c[i] > o[i]
    return (prev_down and now_up and o[i] <= c[i - 1] and c[i] >= o[i - 1]
            and (c[i] - o[i]) > (o[i - 1] - c[i - 1]) and _declining(c, i))


def hammer(o, h, l, c, i, avg_body) -> bool:
    """
    A long lower wick (at least twice the body and over half the range) with
    little above the body: sellers pushed down, buyers took it all back.
    """
    rng = h[i] - l[i]
    if rng <= 0:
        return False
    body = abs(c[i] - o[i])
    lower = min(o[i], c[i]) - l[i]
    upper = h[i] - max(o[i], c[i])
    return (lower >= 2 * body and lower >= 0.55 * rng and upper <= max(body, 0.25 * rng)
            and _declining(c, i))


def inside_break(o, h, l, c, i, avg_body) -> bool:
    """A candle inside the one before it, then an up close above its high."""
    if i < 2:
        return False
    inside = h[i - 1] < h[i - 2] and l[i - 1] > l[i - 2]
    return inside and c[i] > o[i] and c[i] > h[i - 1]


def morning_star(o, h, l, c, i, avg_body) -> bool:
    """A strong down candle, a small-bodied pause, then an up candle that
    closes back above the middle of the first one."""
    if i < 2:
        return False
    b1 = o[i - 2] - c[i - 2]
    b2 = abs(c[i - 1] - o[i - 1])
    return (b1 > 0 and b1 >= avg_body and b2 <= 0.35 * b1
            and c[i] > o[i] and c[i] > (o[i - 2] + c[i - 2]) / 2
            and _declining(c, i - 1))


DETECTORS = {'engulfing': engulfing, 'hammer': hammer,
             'inside_break': inside_break, 'morning_star': morning_star}


def detect(frame: pd.DataFrame, last_n: Optional[int] = None,
           zone: Optional[Dict[str, float]] = None, side: str = 'long') -> List[Dict[str, Any]]:
    """
    Every pattern for `side` completed by one of the last `last_n` candles
    (all candles if None), oldest first: bullish ones for a long, bearish for
    a short. With a zone ({'low', 'high'}), each hit says whether the
    pattern's candles traded inside it.
    """
    if side == 'short':
        df = frame[['Open', 'High', 'Low', 'Close']].dropna()
        k = float(2 * df['High'].max()) if len(df) else 0.0
        flipped = df.copy()
        flipped['Open'], flipped['Close'] = k - df['Open'], k - df['Close']
        flipped['High'], flipped['Low'] = k - df['Low'], k - df['High']
        z = {'low': k - zone['high'], 'high': k - zone['low']} if zone else None
        hits = detect(flipped, last_n, z)
        for hit in hits:
            hit['key'], hit['label'], hit['short'] = BEARISH[hit['key']]
            hit['close'] = k - hit['close']
            hit['low'], hit['high'] = k - hit['high'], k - hit['low']
            hit['side'] = 'short'
        return hits
    df, o, h, l, c = _arrays(frame)
    n = len(c)
    if n < DECLINE_BARS + 2:
        return []
    bodies = np.abs(c - o)
    start = max(DECLINE_BARS + 1, n - last_n) if last_n else DECLINE_BARS + 1
    hits = []
    for i in range(start, n):
        avg_body = float(np.mean(bodies[max(0, i - AVG_BODY_BARS):i])) if i else 0.0
        for key, fn in DETECTORS.items():
            if not fn(o, h, l, c, i, avg_body):
                continue
            span = 3 if key == 'morning_star' else 2 if key in ('engulfing', 'inside_break') else 1
            lo = float(l[i - span + 1:i + 1].min())
            hi = float(h[i - span + 1:i + 1].max())
            hit = {'key': key, 'label': PATTERNS[key], 'short': SHORT[key], 'index': i,
                   'time': pd.Timestamp(df.index[i]).isoformat(),
                   'close': float(c[i]), 'low': lo, 'high': hi}
            if zone:
                hit['at_zone'] = bool(lo <= zone['high'] and hi >= zone['low'])
            hits.append(hit)
    return hits


def completed(frame: pd.DataFrame, now, minutes: int = 15) -> pd.DataFrame:
    """Drop the candle still forming: a pattern is only real once it has closed."""
    if now is None or frame.empty:
        return frame
    now_ts = pd.Timestamp(now)
    idx = frame.index
    if idx.tz is not None and now_ts.tz is None:
        now_ts = now_ts.tz_localize(idx.tz)
    return frame[idx + pd.Timedelta(minutes=minutes) <= now_ts]
