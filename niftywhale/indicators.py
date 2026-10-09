"""
Chart indicators: order-flow delta and cumulative delta, session VWAP, the
opening range (ORB) and Bollinger Bands.

Pure functions over OHLCV frames, like smc.py.

About delta. True order-flow delta is volume traded at the ask minus volume
traded at the bid, which needs every trade tagged with its aggressor side.
Candle APIs (Dhan's and yfinance's) give only OHLCV, so delta here is an
ESTIMATE: each candle's volume is split by where it closed within its range
(close at the high = all buying, at the low = all selling, mid-range =
balanced). Done on 1-minute candles and summed into each 5m/15m bar, that
follows real order flow closely; done on a daily bar it is only a coarse hint.
Every delta value says which it is (`delta_source`).
"""
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

BB_LEN = 20
BB_K = 2.0
ORB_MINUTES = 15

# What the indicator settings may change, with their defaults: the values each indicator was
# designed around and is read with everywhere (Bollinger's own 20 / 2, the first 15 minutes for
# the opening range). They draw the chart; no rule trades on them.
SETTINGS: Dict[str, Dict[str, Any]] = {
    'bb_len':      {'default': BB_LEN, 'min': 5, 'max': 100, 'step': 1, 'label': 'Bollinger length', 'unit': 'candles'},
    'bb_k':        {'default': BB_K, 'min': 1.0, 'max': 4.0, 'step': 0.1, 'label': 'Bollinger width', 'unit': 'std devs'},
    'orb_minutes': {'default': ORB_MINUTES, 'values': [5, 15, 30, 60], 'label': 'Opening range', 'unit': 'minutes'},
    'delta_view':  {'default': 'both', 'values': ['both', 'bars', 'cumulative'], 'label': 'Delta panel', 'unit': ''},
    # Colours: None follows the dashboard's light / dark theme; a #rrggbb colour is used in both.
    'color_vwap':       {'default': None, 'color': True, 'label': 'VWAP line'},
    'color_bb':         {'default': None, 'color': True, 'label': 'Bollinger bands'},
    'color_orb':        {'default': None, 'color': True, 'label': 'Opening range'},
    'color_delta_up':   {'default': None, 'color': True, 'label': 'Delta: buying'},
    'color_delta_down': {'default': None, 'color': True, 'label': 'Delta: selling'},
    'color_cum':        {'default': None, 'color': True, 'label': 'Cumulative delta'},
}
COLOR_RE = __import__('re').compile(r'^#[0-9a-fA-F]{6}$')


def settings_from(saved: Any) -> Dict[str, Any]:
    """Saved indicator settings made safe: each value cast, clamped or checked against its list;
    anything unknown or broken falls back to the default, so a bad setting never breaks a chart."""
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for k, spec in SETTINGS.items():
        v = saved.get(k, spec['default'])
        if spec.get('color'):
            out[k] = v.lower() if isinstance(v, str) and COLOR_RE.match(v) else None
            continue
        if 'values' in spec:
            if isinstance(spec['default'], str):
                out[k] = v if v in spec['values'] else spec['default']
            else:
                try:
                    v = float(v)
                    out[k] = min(spec['values'], key=lambda x: abs(x - v))
                except (TypeError, ValueError):
                    out[k] = spec['default']
            continue
        try:
            v = float(v)
            if not np.isfinite(v):
                raise ValueError
        except (TypeError, ValueError):
            v = spec['default']
        v = min(spec['max'], max(spec['min'], v))
        out[k] = int(round(v)) if isinstance(spec['default'], int) else round(v, 2)
    return out


def _ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame[['Open', 'High', 'Low', 'Close']].copy()
    df['Volume'] = frame['Volume'] if 'Volume' in frame.columns else 0.0
    df['Volume'] = df['Volume'].fillna(0.0).astype(float)
    return df.dropna(subset=['Open', 'High', 'Low', 'Close']).sort_index()


def clv_delta(frame: pd.DataFrame) -> pd.Series:
    """
    Estimated delta per candle: volume × (2·close − high − low) / (high − low),
    i.e. buying volume (close − low)/range minus selling volume (high − close)/range.
    A candle with no range is all buying or all selling by its close against
    the previous close (the tick rule), or balanced if unchanged.
    """
    df = _ohlcv(frame)
    h, l, c, v = (df[k].to_numpy(float) for k in ('High', 'Low', 'Close', 'Volume'))
    rng = h - l
    with np.errstate(divide='ignore', invalid='ignore'):
        d = np.where(rng > 0, v * (2 * c - h - l) / np.where(rng > 0, rng, 1), 0.0)
    prev = np.concatenate([[np.nan], c[:-1]])
    flat = rng <= 0
    d[flat] = (v * np.sign(np.nan_to_num(c - prev)))[flat]
    return pd.Series(d, index=df.index)


def bucket_delta(fine: pd.DataFrame, bars: pd.DatetimeIndex, minutes: int) -> pd.Series:
    """Sum fine (e.g. 1-minute) candle deltas into the `minutes` bars starting at `bars`.
    NSE bars start at 09:15, a multiple of 5 and 15 minutes, so flooring the
    fine candle's start time gives the bar it belongs to. Bars with no fine
    candles get NaN."""
    d = clv_delta(fine)
    if d.empty:
        return pd.Series(np.nan, index=bars)
    keys = d.index.floor(f'{minutes}min')
    summed = d.groupby(keys).sum()
    return summed.reindex(bars)


def cumulative(delta: pd.Series, by_session: bool = True) -> pd.Series:
    """Running total of delta, restarting each session for intraday bars."""
    d = delta.fillna(0.0)
    if by_session:
        return d.groupby(d.index.date).cumsum()
    return d.cumsum()


def session_vwap(frame: pd.DataFrame) -> pd.Series:
    """Volume-weighted average of the typical price (H+L+C)/3, anchored at each
    session's open. NaN until a session has traded volume."""
    df = _ohlcv(frame)
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    day = df.index.date
    pv = (tp * df['Volume']).groupby(day).cumsum()
    vol = df['Volume'].groupby(day).cumsum()
    return (pv / vol.replace(0, np.nan)).astype(float)


def bollinger(frame: pd.DataFrame, n: int = BB_LEN, k: float = BB_K) -> pd.DataFrame:
    """Middle = n-bar simple average of the close; bands = middle ± k population
    standard deviations (the usual charting convention). NaN for the first n−1 bars."""
    c = _ohlcv(frame)['Close']
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    return pd.DataFrame({'upper': mid + k * sd, 'mid': mid, 'lower': mid - k * sd})


def opening_ranges(frame: pd.DataFrame, minutes: int = ORB_MINUTES) -> Dict[str, Dict[str, Any]]:
    """
    Per session: the high and low of its first `minutes` (from its first
    candle), and the first CLOSE outside that range after it formed -- the
    opening range breakout (up) or breakdown (down). Candles must be no longer
    than `minutes` for the range to be exact.
    """
    df = _ohlcv(frame)
    out: Dict[str, Dict[str, Any]] = {}
    for day, g in df.groupby(df.index.date):
        start = g.index[0]
        end = start + pd.Timedelta(minutes=minutes)
        rng = g[g.index < end]
        rest = g[g.index >= end]
        hi, lo = float(rng['High'].max()), float(rng['Low'].min())
        brk: Optional[Dict[str, Any]] = None
        for ts, row in rest.iterrows():
            if row['Close'] > hi or row['Close'] < lo:
                brk = {'time': ts.isoformat(), 'dir': 'up' if row['Close'] > hi else 'down',
                       'close': float(row['Close'])}
                break
        out[str(day)] = {'high': hi, 'low': lo, 'ends': end.isoformat(), 'breakout': brk}
    return out


def _r(x) -> Optional[float]:
    return None if x is None or not np.isfinite(x) else round(float(x), 2)


def for_bars(frame: pd.DataFrame, minutes: Optional[int], fine: Optional[pd.DataFrame] = None,
             intraday: bool = True, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Everything the chart draws, aligned with `frame`'s candles (call it on
    exactly the candles shown, plus earlier ones for the Bollinger warm-up:
    see `tail`). `minutes` is the candle length (None for daily); `fine` is
    1-minute candles for a better delta estimate; `settings` the indicator settings
    (settings_from), the defaults when not given.
    """
    st = settings_from(settings)
    df = _ohlcv(frame)
    if fine is not None and minutes and not fine.empty:
        delta = bucket_delta(fine, df.index, minutes)
        source = '1m'
        missing = delta.isna()
        if missing.any():                    # no 1m candles for some bars: estimate from the bar
            delta[missing] = clv_delta(df)[missing]
            source = '1m+bar' if (~missing).any() else 'bar'
    else:
        delta, source = clv_delta(df), 'bar'
    cd = cumulative(delta, by_session=intraday)
    bb = bollinger(df, st['bb_len'], st['bb_k'])
    out = {
        'volume': [float(x) for x in df['Volume']],
        'delta': [_r(x) for x in delta],
        'cum_delta': [_r(x) for x in cd],
        'bb_upper': [_r(x) for x in bb['upper']],
        'bb_mid': [_r(x) for x in bb['mid']],
        'bb_lower': [_r(x) for x in bb['lower']],
        'delta_source': source,
        'settings': st,
    }
    if intraday:
        out['vwap'] = [_r(x) for x in session_vwap(df)]
        out['orb'] = opening_ranges(df, st['orb_minutes'])
        out['orb_minutes'] = st['orb_minutes']
    return out


def tail(ind: Dict[str, Any], n: int) -> Dict[str, Any]:
    """The last n values of every per-bar series (the warm-up bars dropped)."""
    return {k: (v[-n:] if isinstance(v, list) else v) for k, v in ind.items()}
