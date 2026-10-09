"""
The SMC filtering protocol as plain functions over OHLCV frames.

Everything here is pure: a pandas frame in, a dict of numbers out. No network,
no database, no clock except where a caller passes one in. That is what makes
the protocol testable against hand-built charts (tests/test_smc.py) and lets
the dashboard recompute any stock's levels on demand from cached candles.

The protocol is discretionary on paper ("a massive rally", "a minor swing
high"). Each of those phrases becomes one named number in `Rules`, all
overridable from the environment, so the mechanical reading is explicit and
tunable rather than buried in the code.

Both directions. The protocol is written for buys; a sell is the same
protocol on the chart turned upside down (premium instead of discount, SSL
instead of BSL, a sweep of a swing high and a CHoCH below a minor swing low).
So shorts are not a second implementation: `evaluate(side='short')` and
`choch_trigger(side='short')` mirror the candles (price p -> K - p), run the
long logic, and map every price back. A rule means exactly the same thing
both ways round.
"""
from dataclasses import dataclass, fields
from datetime import time as dtime
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
# The smallest value each integer rule may take (a window of 0 bars, or an
# ATR over 0 bars, would divide by zero or slice nothing). Default: 1.
MIN_INT = {'ob_search': 0, 'min_zone_age': 0}


def env_number(raw: Optional[str], default: Any, min_int: int = 1) -> Any:
    """`raw` from the environment cast to the type of `default`, or None to
    keep the default: unset, unparsable, not finite, negative, or an integer
    below `min_int`."""
    if raw is None or raw.strip() == '':
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    if isinstance(default, int):
        v = int(v)
        return v if v >= min_int else None
    return v


def apply_overrides(values: Dict[str, Any], overrides: Any,
                    tunable: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """`values` with the tunable keys of `overrides` applied: each cast to its
    field's type and clamped to its range. Unknown keys and values that are
    not finite numbers are ignored, and so is anything but a dict, so a stale
    or hand-made saved setting can never break a scan."""
    out = dict(values)
    if not isinstance(overrides, dict):
        return out
    for key, raw in overrides.items():
        spec = tunable.get(key)
        if spec is None or isinstance(raw, bool):
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        v = min(max(v, spec['min']), spec['max'])
        out[key] = int(round(v)) if isinstance(out[key], int) else v
    return out


@dataclass(frozen=True)
class Rules:
    # Phase 1 -- liquidity
    min_avg_volume: float = 1_000_000     # Step 2: shares/day (10 lakh)
    avg_volume_days: int = 63             # ~3 months of sessions
    atr_len: int = 14
    min_atr_pct: float = 1.5              # Step 3: daily ATR as % of price

    # Phase 2/3 -- structure and footprints
    swing_len: int = 3                    # bars each side that make a daily pivot
    bos_lookback: int = 120               # sessions searched for the last break of structure
    displacement_atr: float = 1.5         # Step 6: "massive" = origin-to-break >= this many ATRs
    ob_search: int = 5                    # bars back from the rally origin to find the down candle
    max_discount: float = 0.5             # Step 8: price at or below this fraction of the leg

    # Phase 5 -- risk
    pre_min_rr: float = 1.5               # daily stage, stop under the whole leg origin
    min_rr: float = 3.0                   # Step 12, on the tight 15m stop
    stop_buffer_atr: float = 0.1          # daily stop sits this many ATRs under the origin low
    stop_buffer_pct: float = 0.1          # 15m stop sits this % under the sweep low

    # Phase 4 -- the 15m trigger
    intraday_swing_len: int = 2
    intraday_sessions: int = 2            # how many sessions of 15m bars to search

    # Context gates on the entry (context_gate): off at their defaults; the autopilot learns them
    max_market_run: float = 10.0          # Nifty's 20-day return the trade's way, % (10 = off)
    min_zone_age: int = 0                 # days from the zone being set to the CHoCH (0 = off)
    max_rr: float = 30.0                  # the 15m R:R at most (30 = off)

    @classmethod
    def from_env(cls) -> 'Rules':
        """Every field can be overridden as SMC_<FIELD_NAME_UPPER>. A value
        that would break the arithmetic (not a number, inf, a negative, a
        zero-length window) falls back to the default, not a crash."""
        values = {}
        for f in fields(cls):
            v = env_number(os.getenv('SMC_' + f.name.upper()), f.default, MIN_INT.get(f.name, 1))
            if v is not None:
                values[f.name] = v
        return cls(**values)

    def as_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def with_overrides(self, overrides: Dict[str, Any]) -> 'Rules':
        """
        A copy with the tunable fields replaced, each cast to its field's
        type and clamped to its TUNABLE range. Unknown keys and unparsable
        values are ignored, so a stale saved setting can never break a scan.
        """
        return Rules(**apply_overrides(self.as_dict(), overrides, TUNABLE))


# The thresholds the rule tuner exposes, with the ranges its sliders allow.
# Each is one of the protocol's discretionary phrases made into a number.
TUNABLE: Dict[str, Dict[str, Any]] = {
    'min_avg_volume':   {'label': 'Min avg volume (3 months)', 'step': 100_000, 'min': 0, 'max': 5_000_000,
                         'unit': 'shares', 'help': 'Step 2. The doc says 10 lakh.'},
    'min_atr_pct':      {'label': 'Min daily ATR', 'step': 0.1, 'min': 0, 'max': 5, 'unit': '%',
                         'help': 'Step 3. Can it move enough to reach a target in 5 days?'},
    'swing_len':        {'label': 'Swing point size', 'step': 1, 'min': 2, 'max': 8, 'unit': 'bars each side',
                         'help': 'Step 4/5. Bigger = only major swings count.'},
    'displacement_atr': {'label': '"Massive" rally', 'step': 0.1, 'min': 0.5, 'max': 5, 'unit': '× ATR',
                         'help': 'Step 6. How far the rally must run from its origin to the break.'},
    'bos_lookback':     {'label': 'Look back for the break', 'step': 10, 'min': 20, 'max': 240, 'unit': 'sessions',
                         'help': 'Step 6. How old the break of structure may be.'},
    'max_discount':     {'label': 'Discount threshold', 'step': 0.05, 'min': 0.2, 'max': 0.8, 'unit': 'of leg',
                         'help': 'Step 8. 0.5 = the equilibrium; lower = deeper pullbacks only.'},
    'pre_min_rr':       {'label': 'Provisional R:R', 'step': 0.1, 'min': 0.5, 'max': 5, 'unit': ': 1',
                         'help': 'Daily stage, stop under the whole leg. Filters hopeless setups early.'},
    'min_rr':           {'label': 'Final R:R (15m)', 'step': 0.5, 'min': 1, 'max': 6, 'unit': ': 1',
                         'help': 'Step 12. Applied to the tight stop under the 15m sweep.'},
    # The context gates: checked at the entry, so they never change the screen or the zones.
    'max_market_run':   {'label': 'Max Nifty run (20 days)', 'step': 1, 'min': -3, 'max': 10, 'unit': '%', 'off': 10,
                         'values': [10, 6, 4, 3, 2, 1, 0, -1, -2],     # the autopilot's ladder, off first
                         'help': "Entry gate. Skip a CHoCH when Nifty has run this far the trade's way over 20 days "
                                 '(a long after a rally, a short after a fall). 10 = off.'},
    'min_zone_age':     {'label': 'Min zone age', 'step': 1, 'min': 0, 'max': 8, 'unit': 'days', 'off': 0,
                         'values': [0, 2, 3, 4, 5, 6],
                         'help': 'Entry gate. Skip a CHoCH that comes sooner than this many days after the zone was set '
                                 '(price diving straight through it). 0 = off.'},
    'max_rr':           {'label': 'Max R:R (15m)', 'step': 1, 'min': 4, 'max': 30, 'unit': ': 1', 'off': 30,
                         'values': [30, 15, 12, 10, 8, 6],
                         'help': 'Entry gate. Skip a CHoCH whose target is more than this many risks away: a stop that '
                                 'tight is usually noise. 30 = off.'},
}
GATES = ('max_market_run', 'min_zone_age', 'max_rr')


# Ordered as the protocol runs them. A stock's `failed_at` is the first of
# these it did not pass, which is what the dashboard's funnel counts.
STEPS = (
    ('history',    'Enough history'),
    ('liquidity',  'Avg volume ≥ 10 lakh'),
    ('volatility', 'ATR% > 1.5'),
    ('structure',  'Trending structure (HH/HL or LH/LL)'),
    ('order_block', 'Order block after a displacement break'),
    ('intact',     'Order block still intact'),
    ('discount',   'In discount / premium (50%)'),
    ('rr',         'Provisional R:R'),
)
SIDES = ('long', 'short')


# ---------------------------------------------------------------------------
# Mirroring: a short is a long on the upside-down chart
# ---------------------------------------------------------------------------
def mirror_k(frame: pd.DataFrame) -> float:
    """The constant a chart is mirrored around: any value keeps the geometry;
    twice the highest high keeps every mirrored price positive."""
    return float(2 * frame['High'].max()) if len(frame) else 0.0


def mirror(frame: pd.DataFrame, k: float) -> pd.DataFrame:
    """Price p becomes k - p: highs become lows, rallies become declines."""
    out = frame.copy()
    out['Open'], out['Close'] = k - frame['Open'], k - frame['Close']
    out['High'], out['Low'] = k - frame['Low'], k - frame['High']
    return out


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def avg_volume(vol: np.ndarray) -> float:
    """Mean volume, ignoring gaps. With no volume at all it is 0, not NaN:
    NaN compares False against any threshold, so it would pass Step 2."""
    vol = np.asarray(vol, dtype=float)
    ok = np.isfinite(vol)
    return float(vol[ok].mean()) if ok.any() else 0.0


def clean(frame: pd.DataFrame) -> pd.DataFrame:
    """OHLCV only, no NaN rows, oldest first."""
    cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    out = frame[[c for c in cols if c in frame.columns]].dropna(subset=['Open', 'High', 'Low', 'Close'])
    if 'Volume' not in out.columns:
        out = out.assign(Volume=0.0)
    return out.sort_index()


def atr(frame: pd.DataFrame, n: int) -> pd.Series:
    """Wilder's ATR -- the smoothing every charting package uses by default."""
    high, low, close = frame['High'], frame['Low'], frame['Close']
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def swing_points(high: np.ndarray, low: np.ndarray, n: int) -> Tuple[List[int], List[int]]:
    """
    Confirmed pivots as bar positions.

    A swing high is a bar whose high is strictly above the n bars before it
    and at least as high as the n bars after it (strict on one side so a flat
    double top yields one pivot, not two). Lows mirror that. The last n bars
    can never be pivots: they have not had their n bars after yet.
    """
    highs, lows = [], []
    for i in range(n, len(high) - n):
        if high[i] > high[i - n:i].max() and high[i] >= high[i + 1:i + n + 1].max():
            highs.append(i)
        if low[i] < low[i - n:i].min() and low[i] <= low[i + 1:i + n + 1].min():
            lows.append(i)
    return highs, lows


def structure(high: np.ndarray, low: np.ndarray, close: np.ndarray,
              swing_highs: List[int], swing_lows: List[int]) -> Dict[str, Any]:
    """
    Step 4: bias from the last two swing highs and the last two swing lows.

    Bullish needs a higher high AND a higher low, and the latest close must
    still be above that higher low -- a close under it is the structure
    already failing, whatever the pivots said.
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {'bias': 'unclear', 'reason': 'fewer than two swing highs and lows'}
    h1, h2 = swing_highs[-2], swing_highs[-1]
    l1, l2 = swing_lows[-2], swing_lows[-1]
    hh, hl = high[h2] > high[h1], low[l2] > low[l1]
    lh, ll = high[h2] < high[h1], low[l2] < low[l1]
    last = close[-1]
    info = {'highs': [float(high[h1]), float(high[h2])], 'lows': [float(low[l1]), float(low[l2])]}
    if hh and hl:
        if last < low[l2]:
            return {**info, 'bias': 'broken', 'reason': 'closed below the last higher low'}
        return {**info, 'bias': 'bullish', 'reason': 'higher high and higher low'}
    if lh and ll:
        return {**info, 'bias': 'bearish', 'reason': 'lower high and lower low'}
    return {**info, 'bias': 'range', 'reason': 'mixed swings'}


def liquidity_pools(high: np.ndarray, low: np.ndarray, price: float,
                    swing_highs: List[int], swing_lows: List[int]) -> Dict[str, List[float]]:
    """
    Step 5: untaken swing highs above price (BSL) and swing lows below (SSL).

    A level counts only while no later bar has traded through it -- once the
    stops there have been run, it is no longer a pool.
    """
    bsl = [float(high[i]) for i in swing_highs
           if high[i] > price and (i + 1 >= len(high) or high[i + 1:].max() < high[i])]
    ssl = [float(low[i]) for i in swing_lows
           if low[i] < price and (i + 1 >= len(low) or low[i + 1:].min() > low[i])]
    return {'bsl': sorted(set(bsl)), 'ssl': sorted(set(ssl), reverse=True)}


def find_order_block(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
                     atr_values: np.ndarray, swing_highs: List[int],
                     rules: Rules) -> Optional[Dict[str, int]]:
    """
    Step 6: the most recent displacement break of structure and its order block.

    Walks back from today looking for a close that is the FIRST close above a
    prior swing high (a break of structure). The rally behind it starts at the
    lowest low between that swing high and the break; it only counts as
    "massive" if origin-to-break covers `displacement_atr` ATRs. The order
    block is the last down-close candle at or just before that origin.
    """
    n = len(c)
    start = max(1, n - rules.bos_lookback)
    for b in range(n - 1, start - 1, -1):
        broken = None
        for s in reversed(swing_highs):
            if s >= b:
                continue
            level = h[s]
            between = c[s + 1:b]
            if c[b] > level and (between.size == 0 or between.max() <= level):
                broken = s
            break       # only the nearest swing high before b is the one in play
        if broken is None:
            continue
        origin = broken + int(np.argmin(l[broken:b + 1]))
        if c[b] - l[origin] < rules.displacement_atr * atr_values[b]:
            continue
        ob = next((k for k in range(origin, max(origin - rules.ob_search, 0) - 1, -1)
                   if c[k] < o[k]), None)
        if ob is None:
            continue
        return {'bos': b, 'broken': broken, 'origin': origin, 'ob': ob}
    return None


def fair_value_gaps(h: np.ndarray, l: np.ndarray, first: int, last: int) -> List[Dict[str, Any]]:
    """
    Step 7: bullish three-candle gaps inside [first, last], with fill state.

    Candle 1's high below candle 3's low leaves an untraded gap. Anything
    later that trades back into it fills it from the top down; a gap traded
    all the way through is spent and dropped.
    """
    gaps = []
    for i in range(max(first + 1, 1), min(last, len(h) - 1)):
        bottom, top = h[i - 1], l[i + 1]
        if bottom >= top:
            continue
        later = l[i + 2:]
        deepest = float(later.min()) if later.size else math.inf
        if deepest <= bottom:
            continue
        gaps.append({
            'index': i,
            'bottom': float(bottom),
            'top': float(min(top, deepest)),
            'state': 'open' if deepest > top else 'partial',
        })
    return gaps


# ---------------------------------------------------------------------------
# The daily screen
# ---------------------------------------------------------------------------
STRUCTURE_TEXT = {
    # bias of the (possibly mirrored) chart -> (reported bias, reason), per side
    'long': {'bullish': ('bullish', 'higher high and higher low'),
             'broken': ('broken', 'closed below the last higher low'),
             'bearish': ('bearish', 'lower high and lower low — a short setup, not a long'),
             'range': ('range', 'mixed swings'),
             'unclear': ('unclear', 'fewer than two swing highs and lows')},
    'short': {'bullish': ('bearish', 'lower high and lower low'),
              'broken': ('broken', 'closed above the last lower high'),
              'bearish': ('bullish', 'higher high and higher low — a long setup, not a short'),
              'range': ('range', 'mixed swings'),
              'unclear': ('unclear', 'fewer than two swing highs and lows')},
}


def evaluate(frame: pd.DataFrame, rules: Rules = Rules(), side: str = 'long') -> Dict[str, Any]:
    """
    Run Phases 1-3 and a provisional Phase 5 on one stock's daily candles,
    for a long (bullish structure, discount, target above) or a short
    (bearish structure, premium, target below).

    Always returns a dict. `passed` is True only if every step passed;
    otherwise `failed_at` names the first step that did not, and every
    measurement computed up to that point is still included so the dashboard
    can say why. Prices are always real prices, whichever side.
    """
    df = clean(frame)
    short = side == 'short'
    out: Dict[str, Any] = {'passed': False, 'failed_at': None, 'reason': '', 'bars': len(df), 'side': side}

    def fail(step, reason):
        out['failed_at'], out['reason'] = step, reason
        return out

    need = max(rules.avg_volume_days, rules.atr_len * 3, rules.swing_len * 6)
    if len(df) < need:
        return fail('history', f'{len(df)} sessions, need {need}')

    # The working chart: the real one for a long, the mirrored one for a short.
    k = mirror_k(df) if short else 0.0
    P = (lambda v: k - v) if short else (lambda v: v)        # working price -> real price
    work = mirror(df, k) if short else df
    o, h, l, c = (work[x].to_numpy(dtype=float) for x in ('Open', 'High', 'Low', 'Close'))
    vol = df['Volume'].to_numpy(dtype=float)
    atr_values = atr(work, rules.atr_len).to_numpy(dtype=float)    # ranges: identical either way
    price, a = float(c[-1]), float(atr_values[-1])
    real_price = P(price)
    out.update({
        'date': _date(df, len(df) - 1),
        'close': real_price,
        'day_low': float(df['Low'].iloc[-1]),
        'day_high': float(df['High'].iloc[-1]),
        'atr': a,
        'atr_pct': a / real_price * 100 if real_price else 0.0,
        'avg_volume': avg_volume(vol[-rules.avg_volume_days:]),
    })

    # Phase 1
    if out['avg_volume'] < rules.min_avg_volume:
        return fail('liquidity', f"avg volume {out['avg_volume']:,.0f} < {rules.min_avg_volume:,.0f}")
    if out['atr_pct'] <= rules.min_atr_pct:
        return fail('volatility', f"ATR {out['atr_pct']:.2f}% ≤ {rules.min_atr_pct}%")

    # Phase 2
    sh, sl = swing_points(h, l, rules.swing_len)
    st = structure(h, l, c, sh, sl)
    bias, reason = STRUCTURE_TEXT[side][st['bias']]
    real_st = {'bias': bias, 'reason': reason}
    if 'highs' in st and short:                 # mirrored swing lows are the real highs
        real_st.update(highs=[P(x) for x in st['lows']], lows=[P(x) for x in st['highs']])
    elif 'highs' in st:
        real_st.update(highs=st['highs'], lows=st['lows'])
    out['structure'] = real_st
    pools = liquidity_pools(h, l, price, sh, sl)
    out['pools'] = ({'bsl': [P(x) for x in pools['ssl']], 'ssl': [P(x) for x in pools['bsl']]}
                    if short else pools)
    out['swings'] = ({'highs': sl[-6:], 'lows': sh[-6:]} if short else {'highs': sh[-6:], 'lows': sl[-6:]})
    if st['bias'] != 'bullish':
        return fail('structure', reason)

    # Phase 3
    found = find_order_block(o, h, l, c, atr_values, sh, rules)
    if not found:
        return fail('order_block', f'no break of structure with a ≥{rules.displacement_atr} ATR '
                                   f'{"decline" if short else "rally"} in {rules.bos_lookback} sessions')
    ob, origin = found['ob'], found['origin']
    ob_low, ob_high = float(min(o[ob], c[ob])), float(max(o[ob], c[ob]))
    leg_low = float(l[origin])
    leg_top_idx = origin + int(np.argmax(h[origin:]))
    leg_high = float(h[leg_top_idx])
    eq = (leg_low + leg_high) / 2
    position = (price - leg_low) / (leg_high - leg_low) if leg_high > leg_low else 1.0

    gaps = fair_value_gaps(h, l, origin, leg_top_idx)
    # Gaps that sit in the discount half are the ones that matter for entry.
    discount_gaps = [g for g in gaps if g['top'] <= eq + 1e-9 and g['top'] >= ob_low]
    zone_low = ob_low
    zone_high = max([ob_high] + [g['top'] for g in discount_gaps])

    def lohi(lo, hi):                       # a working-chart band as a real {low, high}
        return {'low': P(hi), 'high': P(lo)} if short else {'low': lo, 'high': hi}
    origin_price, end_price = P(leg_low), P(leg_high)
    out.update({
        'bos_date': _date(df, found['bos']),
        'broken_level': P(float(h[found['broken']])),
        'order_block': {**lohi(ob_low, ob_high), 'date': _date(df, ob), 'index': ob},
        # The origin is where the leg started (a low for a long, a high for a short);
        # the end is its far extreme, the target side. `low`/`high` stay real.
        'origin': {'price': origin_price, 'date': _date(df, origin), 'index': origin,
                   **({} if short else {'low': origin_price})},
        'leg': {'low': min(origin_price, end_price), 'high': max(origin_price, end_price),
                'origin': origin_price, 'end': end_price,
                'end_date': _date(df, leg_top_idx), 'end_index': leg_top_idx,
                'high_date': _date(df, leg_top_idx), 'high_index': leg_top_idx,
                'equilibrium': P(eq)},
        'position': position,
        'fvgs': [{'index': g['index'], 'bottom': lohi(g['bottom'], g['top'])['low'],
                  'top': lohi(g['bottom'], g['top'])['high'], 'state': g['state']} for g in gaps],
        'zone': lohi(zone_low, zone_high),
    })

    after = c[ob + 1:]
    if after.size and after.min() < ob_low:
        worst = P(after.min())
        edge = P(ob_low)
        return fail('intact', f'closed above the order block ({worst:.2f} > {edge:.2f})' if short
                    else f'closed below the order block ({worst:.2f} < {edge:.2f})')

    if position > rules.max_discount:
        where = 'premium' if short else 'discount'
        return fail('discount', f'price {position * 100:.0f}% of the way back from the leg\'s origin, '
                                f'not yet in {where} (≤ {rules.max_discount * 100:.0f}%)')

    # Provisional Phase 5: entry at the zone (or here, if already inside it),
    # stop beyond the leg origin, target the leg's far extreme (BSL / SSL).
    entry = min(price, zone_high)
    stop = leg_low - rules.stop_buffer_atr * a
    target = leg_high
    risk = entry - stop
    rr = (target - entry) / risk if risk > 0 else 0.0
    in_zone = bool(l[-1] <= zone_high and price >= zone_low)
    distance_atr = 0.0 if price <= zone_high else (price - zone_high) / a if a else 0.0
    out.update({
        'plan': {'entry': P(entry), 'stop': P(stop), 'target': P(target), 'rr': rr},
        'in_zone': in_zone,
        'distance_atr': distance_atr,
        'distance_pct': max(0.0, (price - zone_high) / real_price * 100) if real_price else 0.0,
    })
    if rr < rules.pre_min_rr:
        return fail('rr', f'provisional R:R 1:{rr:.1f} < 1:{rules.pre_min_rr}')

    out['passed'] = True
    out['score'] = score(out, rules)
    return out


def evaluate_both(frame: pd.DataFrame, rules: Rules = Rules(), shorts: bool = True,
                  short_block: Optional[str] = None) -> Dict[str, Any]:
    """
    The one verdict per stock that the funnel counts. A chart is either
    HH/HL or LH/LL, never both, so after the structure step at most one side
    is still in play: the long, or -- when the long stopped at a bearish
    structure and shorts are on -- the short. `short_block` is a reason this
    stock may not be shorted (swing shorts need F&O); it fails the stock at
    the structure step with that reason.
    """
    r = evaluate(frame, rules, 'long')
    if r['failed_at'] == 'structure' and r['structure']['bias'] == 'bearish' and shorts:
        if short_block:
            r['reason'] = f'lower high and lower low — {short_block}'
            return r
        return evaluate(frame, rules, 'short')
    return r


def score(r: Dict[str, Any], rules: Rules) -> int:
    """
    0-100, for ordering only. Proximity and R:R lead because they decide
    whether a setup is actionable this week; depth, confluence and range
    break ties.
    """
    def clamp(x):
        return max(0.0, min(1.0, x))
    proximity = 1.0 if r['in_zone'] else clamp(1 - r['distance_atr'] / 3)
    rr = clamp(r['plan']['rr'] / (2 * rules.min_rr))
    depth = clamp((rules.max_discount - r['position']) / rules.max_discount)
    ob = r['order_block']
    confluence = 1.0 if any(g['bottom'] <= ob['high'] and g['top'] >= ob['low'] for g in r['fvgs']) \
        else (0.5 if r['fvgs'] else 0.0)
    rng = clamp((r['atr_pct'] - rules.min_atr_pct) / 2.5)
    return int(round(100 * (0.30 * proximity + 0.25 * rr + 0.20 * depth
                            + 0.15 * confluence + 0.10 * rng)))


def _date(df: pd.DataFrame, i: int) -> str:
    """The bar's date; with its time too on intraday bars."""
    v = df.index[i]
    if not hasattr(v, 'date'):
        return str(v)
    if getattr(v, 'hour', 0) or getattr(v, 'minute', 0):
        return v.strftime('%Y-%m-%d %H:%M')
    return str(v.date())


# ---------------------------------------------------------------------------
# The 15-minute trigger
# ---------------------------------------------------------------------------
def choch_trigger(frame15: pd.DataFrame, zone_low: float, zone_high: float,
                  target: float, rules: Rules = Rules(),
                  now: Optional[pd.Timestamp] = None, bar_minutes: int = 15,
                  sessions: Optional[int] = None, since=None, side: str = 'long') -> Dict[str, Any]:
    """
    The entry trigger for either side. A short is the long trigger on the
    mirrored chart: price rallies up into the zone, sweeps a swing HIGH, then
    a candle closes BELOW the last minor swing low. The result carries real
    prices; for a short, `sweep_low`/`low` hold the sweep's and the move's
    HIGH (named by the long they mirror) and `extreme` says which way.
    """
    if side != 'short':
        t = _choch_long(frame15, zone_low, zone_high, target, rules, now, bar_minutes, sessions, since)
        t['side'] = 'long'
        return t
    df = clean(frame15)
    k = mirror_k(df) if len(df) else 0.0
    t = _choch_long(mirror(df, k), k - zone_high, k - zone_low, k - target, rules, now,
                    bar_minutes, sessions, since)
    for key in ('last_close', 'low', 'sweep_low', 'swept_level', 'choch_level', 'entry', 'stop', 'target'):
        if t.get(key) is not None:
            t[key] = k - t[key]
    t['side'] = 'short'
    if t.get('triggered'):
        # The stop buffer is a % of the REAL price, which mirroring does not preserve.
        t['stop'] = t['sweep_low'] * (1 + rules.stop_buffer_pct / 100)
        risk = t['stop'] - t['entry']
        rr = (t['entry'] - t['target']) / risk if risk > 0 else 0.0
        t['rr'], t['valid'] = rr, rr >= rules.min_rr
        t['reason'] = trigger_reason(rr, rules.min_rr)
    return t


def trigger_reason(rr: float, min_rr: float) -> str:
    if rr >= min_rr:
        return f'CHoCH confirmed, R:R 1:{rr:.1f}'
    if rr <= 0:
        return 'CHoCH, but price had already passed the target'
    return f'CHoCH, but R:R 1:{rr:.1f} is under 1:{min_rr:g}'


def context_gate(ctx: Dict[str, Any], side: str, rules: Dict[str, Any]) -> Optional[str]:
    """
    Why a valid entry is skipped by the context gates, or None to take it.
    `ctx` is the entry's context (features.trade_context / market_context:
    rr, zone_age_days, nifty_ret20), `rules` the rule values as a dict. A gate
    at its 'off' value never blocks, and a missing number never blocks: the
    gate only acts on what it can see. The backtester and the live watcher
    both call this, so a gate the autopilot learns means the same in both.
    """
    def on(k):
        v = rules.get(k)
        return v is not None and v != TUNABLE[k]['off']

    rr = ctx.get('rr')
    if on('max_rr') and rr is not None and rr > rules['max_rr'] + 1e-9:
        return f"R:R 1:{rr:.1f} is over the 1:{rules['max_rr']:g} cap: a stop that tight is usually noise"
    age = ctx.get('zone_age_days')
    if on('min_zone_age') and age is not None and age < rules['min_zone_age']:
        return f"CHoCH {age} day(s) after the zone was set (gate: at least {rules['min_zone_age']:g})"
    run = ctx.get('nifty_ret20')
    if on('max_market_run') and run is not None:
        run = -run if side == 'short' else run
        if run > rules['max_market_run'] + 1e-9:
            return (f"Nifty moved {run:+.1f}% the trade's way in 20 days "
                    f"(gate: at most {rules['max_market_run']:+g}%): the market is already extended")
    return None


def _choch_long(frame15: pd.DataFrame, zone_low: float, zone_high: float,
                target: float, rules: Rules = Rules(),
                now: Optional[pd.Timestamp] = None, bar_minutes: int = 15,
                sessions: Optional[int] = None, since=None) -> Dict[str, Any]:
    """
    Phase 4 / Step 10 on 15-minute candles, plus Steps 11-12. The intraday
    mode runs the same trigger on 5-minute candles (`bar_minutes`) over
    today's session only (`sessions=1`).

    Within the last `sessions` (default `intraday_sessions`) sessions:
      1. TAP    -- a bar trades down into the daily zone (low <= zone top).
      2. SWEEP  -- from the tap on, price takes out a confirmed 15m swing low.
      3. CHoCH  -- a candle then CLOSES above the most recent minor 15m swing
                   high that formed before the sweep low.
    Only completed candles are used: a bar still forming has not closed above
    anything yet. With `since`, only bars from then on can be the tap (an
    intraday zone set at 11:00 was not "tapped" by the 09:15 candle); earlier
    bars still count as swing points.

    Returns {'tapped': bool, 'triggered': bool, ...}; when triggered it
    includes entry (the CHoCH close), stop (under the sweep low), target and
    R:R, and `valid` says whether the R:R clears `min_rr`.
    """
    df = clean(frame15)
    if now is not None and len(df):
        idx = df.index
        bar_end = idx + pd.Timedelta(minutes=bar_minutes)
        now_ts = pd.Timestamp(now)
        if idx.tz is not None and now_ts.tz is None:
            now_ts = now_ts.tz_localize(idx.tz)
        df = df[bar_end <= now_ts]
    if df.empty:
        return {'tapped': False, 'triggered': False, 'reason': f'no completed {bar_minutes}m bars'}

    days = sorted(set(df.index.date))[-(sessions or rules.intraday_sessions):]
    df = df[np.isin(df.index.date, days)]
    h, l, c = (df[k].to_numpy(dtype=float) for k in ('High', 'Low', 'Close'))
    n = rules.intraday_swing_len

    eligible = l <= zone_high
    if since is not None:
        since_ts = pd.Timestamp(since)
        if df.index.tz is not None and since_ts.tz is None:
            since_ts = since_ts.tz_localize(df.index.tz)
        eligible &= np.asarray(df.index >= since_ts)
    taps = np.nonzero(eligible)[0]
    if taps.size == 0:
        return {'tapped': False, 'triggered': False, 'reason': 'has not reached the zone',
                'last_close': float(c[-1]), 'low': float(l.min())}
    t0 = int(taps[0])
    out = {'tapped': True, 'triggered': False, 'tap_time': _ts(df, t0),
           'last_close': float(c[-1]), 'low': float(l[t0:].min())}

    sh, sl = swing_points(h, l, n)
    for cbar in range(t0 + 1, len(c)):
        m = t0 + int(np.argmin(l[t0:cbar]))          # lowest point since the tap
        swept = [s for s in sl if s + n < m and l[m] < l[s]]
        if not swept:
            continue
        minor = [s for s in sh if s < m and s + n < cbar]
        if not minor:
            continue
        level = h[minor[-1]]
        if c[cbar] <= level:
            continue
        entry = float(c[cbar])
        stop = float(l[m]) * (1 - rules.stop_buffer_pct / 100)
        risk = entry - stop
        rr = (target - entry) / risk if risk > 0 else 0.0
        out.update({
            'triggered': True,
            'sweep_low': float(l[m]), 'sweep_time': _ts(df, m),
            'swept_level': float(l[swept[-1]]),
            'choch_level': float(level), 'choch_time': _ts(df, cbar),
            'entry': entry, 'stop': stop, 'target': float(target), 'rr': rr,
            'valid': rr >= rules.min_rr,
            'reason': trigger_reason(rr, rules.min_rr),
        })
        return out
    out['reason'] = f'in the zone, waiting for a sweep and a {bar_minutes}m CHoCH'
    return out


def _ts(df: pd.DataFrame, i: int) -> str:
    return pd.Timestamp(df.index[i]).isoformat()


# ---------------------------------------------------------------------------
# Paper trades: a swing entry followed to its stop or target
# ---------------------------------------------------------------------------
TZ = 'Asia/Kolkata'
SESSION = (dtime(9, 15), dtime(15, 30))          # NSE cash session, IST


def _ist(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize(TZ) if ts.tz is None else ts.tz_convert(TZ)


def _session_bounds(day) -> Tuple[pd.Timestamp, pd.Timestamp]:
    d = pd.Timestamp(day).date()
    return (pd.Timestamp.combine(d, SESSION[0]).tz_localize(TZ),
            pd.Timestamp.combine(d, SESSION[1]).tz_localize(TZ))


def _trading_between(a: pd.Timestamp, b: pd.Timestamp) -> bool:
    """Could anything have traded between `a` and `b`? Is some weekday's
    session partly inside them (a holiday counts: its candle just won't exist)."""
    d = a.normalize()
    while d < b:
        if d.weekday() < 5:
            start, end = _session_bounds(d)
            if end > a and start < b:
                return True
        d += pd.Timedelta(days=1)
    return False


def follow_trade(t: Dict[str, Any], frame15: Optional[pd.DataFrame], daily: Optional[pd.DataFrame],
                 now, bar_minutes: int = 15) -> Dict[str, Any]:
    """
    Advance a swing paper trade from where it was last followed to `now`.

    The trade is the alert taken as given: in at the CHoCH candle's close, out
    at the stop or the target, no costs. A swing trade has no square-off; it
    runs until one of the two. `t` is the trigger (entry, stop, target, side,
    choch_time) and `followed_to`, the end of the last candle already
    examined (at first, the CHoCH candle's close).

    Candles are examined oldest first: the completed 15m candles from
    `followed_to` on. If those do not reach back that far (the app was off
    longer than the data source keeps 15m candles), daily candles fill the
    sessions in between. Any touch of a level a daily candle shows happened in
    its unexamined part, because the examined part never touched it. That
    does not hold on the entry day, whose candle also covers the price action
    before the entry, so a gap there is skipped and the result marked `approx`.

    Fills: the target at the target; the stop at the stop, or at the candle's
    open when price gapped through it (an overnight gap is a swing trade's
    real risk). A candle that touches both counts as stopped: it cannot say
    which came first, so the worse is assumed.

    Returns {'status': 'open' | 'won' | 'lost', 'followed_to', 'r'} with
    'last' while open and 'exit', 'exit_time' once closed; 'approx' when daily
    candles decided it or part of the entry day went unexamined; 'need_daily'
    when a gap needs daily candles that were not given; 'wait' when there are
    no 15m candles to follow with this time.
    """
    entry, stop, target = float(t['entry']), float(t['stop']), float(t['target'])
    short = t.get('side') == 'short' or stop > entry
    sign = -1 if short else 1
    risk = (entry - stop) * sign
    r_of = (lambda px: (px - entry) * sign / risk) if risk > 0 else (lambda px: 0.0)
    step = pd.Timedelta(minutes=bar_minutes)
    entry_at = _ist(t['choch_time']) + step                     # the CHoCH candle's close
    cursor = max(_ist(t['followed_to']), entry_at) if t.get('followed_to') else entry_at
    now_ts = _ist(now)
    approx = bool(t.get('approx'))
    last = float(t.get('last') or entry)

    def still_open(**extra) -> Dict[str, Any]:
        return {'status': 'open', 'followed_to': cursor.isoformat(), 'last': last, 'r': r_of(last),
                'approx': approx, **extra}

    f15 = clean(frame15) if frame15 is not None and len(frame15) else None
    if f15 is not None:
        if f15.index.tz is None:
            f15 = f15.tz_localize(TZ)
        f15 = f15[f15.index + step <= now_ts]                   # completed candles only
    if f15 is None or f15.empty:
        return still_open(wait=True)

    # Sessions between the last examined candle and the first 15m candle held.
    candles: List[Tuple[pd.Timestamp, pd.Timestamp, float, float, float, float, str]] = []
    covered_from = f15.index[0]
    if cursor < covered_from and _trading_between(cursor, covered_from):
        if daily is None or not len(daily):
            return still_open(need_daily=True)
        for day, row in clean(daily).iterrows():
            start, end = _session_bounds(day)
            if not (end > cursor and start < covered_from):
                continue
            if pd.Timestamp(day).date() == entry_at.date():
                approx = True                                   # can't split the entry day's candle
                continue
            candles.append((start, end, float(row['Open']), float(row['High']), float(row['Low']),
                            float(row['Close']), 'daily'))
    for ts, row in f15[f15.index >= cursor].iterrows():
        candles.append((ts, ts + step, float(row['Open']), float(row['High']), float(row['Low']),
                        float(row['Close']), '15m'))

    for start, end, o, hi, lo, c, src in candles:
        stopped = hi >= stop if short else lo <= stop
        reached = lo <= target if short else hi >= target
        if stopped or reached:
            if stopped:
                px = max(stop, o) if short else min(stop, o)     # gapped through: out at the open
            else:
                px = target
            return {'status': 'lost' if stopped else 'won', 'exit': float(px), 'exit_time': start.isoformat(),
                    'r': r_of(px), 'followed_to': end.isoformat(), 'approx': approx or src == 'daily',
                    'source': src}
        cursor = end
    last = float(f15['Close'].iloc[-1])                         # the latest completed close
    return still_open()

