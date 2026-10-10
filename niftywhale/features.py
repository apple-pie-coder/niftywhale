"""
The context recorded with every trade, live or backtested, so results can be
broken down by the conditions they happened in. One function per source of
context, all pure; the same code runs for live alerts and for the backtester,
so the two are comparable.
"""
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd

MARKET_COLUMNS = ('nifty', 'vix')


def market_context(market: Optional[pd.DataFrame], day: date) -> Dict[str, Any]:
    """The market as of the close BEFORE `day` (what was known when the day began):
    Nifty against its 20 and 50-day averages, its 20-day return, India VIX."""
    out: Dict[str, Any] = {}
    if market is None or market.empty:
        return out
    m = market[market.index.date < day] if hasattr(market.index, 'date') else market
    if len(m) < 55:
        return out
    n = m['nifty'].astype(float)
    last = float(n.iloc[-1])
    ma20, ma50 = float(n.iloc[-20:].mean()), float(n.iloc[-50:].mean())
    out.update(nifty_vs_ma50=round((last / ma50 - 1) * 100, 2), nifty_vs_ma20=round((last / ma20 - 1) * 100, 2),
               nifty_ret20=round((last / float(n.iloc[-21]) - 1) * 100, 2),
               market_trend='up' if last > ma50 and ma20 > ma50 else 'down' if last < ma50 and ma20 < ma50 else 'mixed')
    if 'vix' in m and not math.isnan(float(m['vix'].iloc[-1])):
        out['vix'] = round(float(m['vix'].iloc[-1]), 2)
    if 'breadth50' in m:                           # history.update_context: % of stocks above their 50-day average
        b = m['breadth50'].dropna()
        if len(b):
            out['breadth50'] = round(float(b.iloc[-1]), 1)
    return out


def _before(frame: pd.DataFrame, day: date) -> pd.DataFrame:
    idx = pd.DatetimeIndex(frame.index)
    dates = idx.tz_convert(None).date if idx.tz is not None else idx.date
    return frame[dates < day]


def relative_context(daily: Optional[pd.DataFrame], market: Optional[pd.DataFrame], sector: Optional[str],
                     day: date) -> Dict[str, Any]:
    """The stock's 20-day return against Nifty's (`rs_nifty20`) and against its own sector's
    (`rs_sector20`, from history.update_context's equal-weighted series), in percentage points, as of
    the close before `day`."""
    out: Dict[str, Any] = {}
    if daily is None or daily.empty or market is None or market.empty:
        return out
    c = _before(daily, day)['Close'].astype(float)
    m = _before(market, day)
    if len(c) < 21 or len(m) < 21:
        return out
    ret = (float(c.iloc[-1]) / float(c.iloc[-21]) - 1) * 100
    n = m['nifty'].astype(float)
    out['rs_nifty20'] = round(ret - (float(n.iloc[-1]) / float(n.iloc[-21]) - 1) * 100, 2)
    col = f'sec_{sector}' if sector else None
    if col and col in m:
        sec = m[col].dropna().astype(float)
        if len(sec) >= 21:
            out['rs_sector20'] = round(ret - (float(sec.iloc[-1]) / float(sec.iloc[-21]) - 1) * 100, 2)
            out['sector'] = sector
    return out


def weekly_context(daily: Optional[pd.DataFrame], day: date) -> Dict[str, Any]:
    """The stock's weekly trend as of the close before `day`: 'up' when the weekly close is above its
    10-week average and that average is rising (against four weeks before), 'down' the mirror, else 'mixed'."""
    if daily is None or daily.empty:
        return {}
    before = _before(daily, day)
    if len(before) < 75:
        return {}
    idx = pd.DatetimeIndex(before.index)
    closes = pd.Series(before['Close'].astype(float).values, index=idx.tz_convert(None) if idx.tz is not None else idx)
    w = closes.resample('W-FRI').last().dropna()
    if len(w) < 15:
        return {}
    ma = w.rolling(10).mean()
    last, now, then = float(w.iloc[-1]), float(ma.iloc[-1]), float(ma.iloc[-5])
    trend = 'up' if last > now and now > then else 'down' if last < now and now < then else 'mixed'
    return {'weekly_trend': trend}


def candle_context(frame: Optional[pd.DataFrame], t: Dict[str, Any], side: str, sessions: int = 10) -> Dict[str, Any]:
    """The CHoCH candle itself: where it closed in its range, measured the trade's way
    (`choch_close_loc`, 1 = at the high for a long, at the low for a short), and its volume against the
    same clock time's average over up to `sessions` earlier sessions (`choch_rvol`, from 3 of them)."""
    if frame is None or frame.empty or not t.get('choch_time'):
        return {}
    at = pd.Timestamp(t['choch_time'])
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is None and at.tz is not None:
        at = at.tz_localize(None)
    elif idx.tz is not None and at.tz is None:
        at = at.tz_localize(idx.tz)
    bar = frame[idx == at]
    if not len(bar):
        return {}
    h, l, c = (float(bar[k].iloc[0]) for k in ('High', 'Low', 'Close'))
    loc = (c - l) / (h - l) if h > l else 0.5
    out: Dict[str, Any] = {'choch_close_loc': round(1 - loc if side == 'short' else loc, 2)}
    if 'Volume' in frame:
        same = frame[(idx.time == at.time()) & (idx < at)].iloc[-sessions:]
        avg = float(same['Volume'].astype(float).mean()) if len(same) >= 3 else 0.0
        if avg > 0:
            out['choch_rvol'] = round(float(bar['Volume'].iloc[0]) / avg, 2)
    return out


def stock_day_context(daily: Optional[pd.DataFrame], day: date, open_price: Optional[float] = None) -> Dict[str, Any]:
    """The stock's gap on `day` (its open against the previous close) and its own trend."""
    out: Dict[str, Any] = {}
    if daily is None or daily.empty:
        return out
    idx = pd.DatetimeIndex(daily.index)
    dates = idx.tz_convert(None).date if idx.tz is not None else idx.date
    before = daily[dates < day]
    if len(before):
        prev = float(before['Close'].iloc[-1])
        today = daily[dates == day]
        o = open_price if open_price is not None else (float(today['Open'].iloc[0]) if len(today) else None)
        if o and prev:
            out['gap_pct'] = round((o / prev - 1) * 100, 2)
        if len(before) >= 50:
            c = before['Close'].astype(float)
            out['stock_vs_ma50'] = round((float(c.iloc[-1]) / float(c.iloc[-50:].mean()) - 1) * 100, 2)
    return out


FUT_DAYS = 5                 # futures buildup: the change over this many sessions
FUT_MIN_OI_CHG = 2.0         # open interest must move at least this % to count as a buildup or unwinding


def futures_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Stock futures over the last FUT_DAYS sessions before the entry (`rows`: sm_futoi, oldest first, all
    before the entry's day): open interest and price change in %, and what they say together:
      long buildup    price up, OI up       short buildup   price down, OI up
      short covering  price up, OI down     long unwinding  price down, OI down
    (neutral when OI moved less than FUT_MIN_OI_CHG %)."""
    rows = [r for r in rows if r.get('oi') and r.get('close')]
    if len(rows) <= FUT_DAYS:
        return {}
    last, then = rows[-1], rows[-1 - FUT_DAYS]
    oi = (last['oi'] / then['oi'] - 1) * 100
    px = (last['close'] / then['close'] - 1) * 100
    if abs(oi) < FUT_MIN_OI_CHG:
        kind = 'neutral'
    elif oi > 0:
        kind = 'long buildup' if px >= 0 else 'short buildup'
    else:
        kind = 'short covering' if px >= 0 else 'long unwinding'
    return {'fut_oi_chg5': round(oi, 2), 'fut_px_chg5': round(px, 2), 'fut_buildup': kind}


def results_context(days: List[str], day: date) -> Dict[str, Any]:
    """Calendar days to the stock's next results board meeting on or after `day`, and since its last one
    before it (`days`: ISO days, oldest first; NSE's event calendar lists them weeks ahead)."""
    if not days:
        return {}
    iso = day.isoformat()
    out: Dict[str, Any] = {}
    ahead = [d for d in days if d >= iso]
    behind = [d for d in days if d < iso]
    if ahead:
        out['days_to_results'] = (date.fromisoformat(ahead[0]) - day).days
    if behind:
        out['days_from_results'] = (day - date.fromisoformat(behind[-1])).days
    return out


def trade_context(t: Dict[str, Any], setup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The trade's own numbers: entry time, R:R, and the setup it came from."""
    out: Dict[str, Any] = {}
    at = t.get('choch_time')
    if at:
        ts = pd.Timestamp(at)
        out['entry_minute'] = ts.hour * 60 + ts.minute
        out['weekday'] = ts.weekday()
    for k in ('rr',):
        if t.get(k) is not None:
            out[k] = round(float(t[k]), 2)
    if t.get('entry') and t.get('stop'):
        out['risk_pct'] = round(abs(t['entry'] - t['stop']) / t['entry'] * 100, 3)
    for k in ('position', 'pre_rr', 'atr_pct', 'avg_volume', 'score', 'in_zone', 'zone_age_days', 'target_kind'):
        if setup and setup.get(k) is not None:
            v = setup[k]
            out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def combine(*parts: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for p in parts:
        out.update({k: v for k, v in (p or {}).items() if v is not None})
    return out


def now_stamp() -> str:
    return datetime.now().isoformat(timespec='seconds')
