"""
The context recorded with every trade, live or backtested, so results can be
broken down by the conditions they happened in. One function per source of
context, all pure; the same code runs for live alerts and for the backtester,
so the two are comparable.
"""
import math
from datetime import date, datetime
from typing import Any, Dict, Optional

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
