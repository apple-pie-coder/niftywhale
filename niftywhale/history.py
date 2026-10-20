"""
Candle history for the lab (backtests and the autopilot): years of daily,
15-minute and 5-minute candles per stock, plus Nifty 50 and India VIX daily
closes for the market context.

Stored as gzipped pickles under LAB_DIR/history/<interval>/<SYMBOL>.pkl.gz and
topped up incrementally: a refresh only fetches what is missing at the end.

Sources: daily candles from yfinance (what the live scan uses), 15m / 5m from
Dhan's intraday charts (90 days per request, ~5 years back), index dailies from
Dhan's historical charts. Downloads are paced well under Dhan's limits and run
outside market hours, so they never compete with the live watcher.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import requests

from niftywhale import data, dhan, session

logger = logging.getLogger(__name__)

LAB_DIR = Path(os.getenv('LAB_DIR', os.path.join(os.path.dirname(os.getenv('DB_PATH', 'var/niftywhale.db')) or '.', 'lab')))
TZ = 'Asia/Kolkata'
CHUNK_DAYS = 88                 # Dhan: 90 days of intraday candles per request
PACE_S = 0.6                    # ~1.6 requests a second: the live app keeps the rest of the 5/s
NIFTY_ID, VIX_ID = 13, 21


def path(interval: str, symbol: str) -> Path:
    return LAB_DIR / 'history' / interval / f'{symbol.replace("/", "_")}.pkl.gz'


def load(interval: str, symbol: str) -> Optional[pd.DataFrame]:
    p = path(interval, symbol)
    if not p.exists():
        return None
    try:
        return pd.read_pickle(p)
    except Exception as e:                       # a torn write: refetch rather than crash a backtest
        logger.warning(f'history {interval}/{symbol} unreadable ({e}); will refetch')
        return None


def save(interval: str, symbol: str, frame: pd.DataFrame) -> None:
    p = path(interval, symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    frame.to_pickle(tmp, compression='gzip')
    tmp.replace(p)


def merge(old: Optional[pd.DataFrame], new: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if old is None or old.empty:
        return new
    if new is None or new.empty:
        return old
    out = pd.concat([old, new])
    return out[~out.index.duplicated(keep='last')].sort_index()


# ---------------------------------------------------------------- downloads
def _intraday_chunk(sid: str, interval: int, start: datetime, end: datetime) -> pd.DataFrame:
    body = {'securityId': sid, 'exchangeSegment': 'NSE_EQ', 'instrument': 'EQUITY', 'interval': str(interval),
            'oi': False, 'fromDate': start.strftime('%Y-%m-%d 09:00:00'), 'toDate': end.strftime('%Y-%m-%d 15:30:00')}
    for attempt in range(3):
        time.sleep(PACE_S)
        r = requests.post(f'{dhan.API}/charts/intraday', headers=dhan._headers(), json=body, timeout=40)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        d = r.json()
        if not d.get('timestamp'):
            return pd.DataFrame()
        idx = pd.to_datetime(d['timestamp'], unit='s', utc=True).tz_convert(TZ)
        return pd.DataFrame({'Open': d['open'], 'High': d['high'], 'Low': d['low'], 'Close': d['close'],
                             'Volume': d.get('volume') or [0] * len(idx)}, index=idx).sort_index()
    raise RuntimeError('Dhan kept answering 429')


def update_intraday(symbol: str, interval: int, years: float, should_stop: Callable[[], bool] = lambda: False) -> int:
    """Fetch what is missing of `years` of `interval`-minute candles. Returns new bars."""
    sid = dhan.security_id(symbol)
    if not sid:
        return 0
    key = f'{interval}m'
    old = load(key, symbol)
    today = data.now_ist().date()
    want_from = today - timedelta(days=int(365 * years))
    have_from = old.index[0].date() if old is not None and len(old) else None
    have_to = old.index[-1].date() if old is not None and len(old) else None
    spans = []
    if have_from is None:
        spans.append((want_from, today))
    else:
        if want_from < have_from - timedelta(days=3):
            spans.append((want_from, have_from))
        spans.append((have_to, today))
    got = []
    for a, b in spans:
        cur = a
        while cur <= b and not should_stop():
            end = min(b, cur + timedelta(days=CHUNK_DAYS))
            got.append(_intraday_chunk(sid, interval, datetime.combine(cur, datetime.min.time()),
                                       datetime.combine(end, datetime.min.time())))
            cur = end + timedelta(days=1)
    new = pd.concat([g for g in got if len(g)]) if any(len(g) for g in got) else None
    merged = merge(old, new)
    if merged is None:
        return 0
    # Only completed sessions: today's candles are still forming during the session.
    if data.market_open():
        merged = merged[merged.index.date < today]
    save(key, symbol, merged)
    return len(merged) - (len(old) if old is not None else 0)


def held(interval: str) -> List[str]:
    """The symbols with `interval` candles on disk."""
    return sorted(p.name[:-len('.pkl.gz')] for p in (LAB_DIR / 'history' / interval).glob('*.pkl.gz'))


def update_daily(symbols: List[str], years: float) -> int:
    """Daily candles from yfinance, as the live scan reads them (unadjusted)."""
    import yfinance as yf
    changed = 0
    for i in range(0, len(symbols), 25):
        chunk = symbols[i:i + 25]
        tickers = [s + '.NS' for s in chunk]
        try:
            raw = yf.download(tickers, period=f'{max(2, int(years + 1.5))}y', interval='1d', group_by='ticker',
                              auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            logger.warning(f'daily history batch failed: {e}')
            continue
        for sym, t in zip(chunk, tickers):
            try:
                # yfinance groups by ticker (even for one ticker, in recent versions) or not at all.
                f = raw[t] if isinstance(raw.columns, pd.MultiIndex) and t in raw.columns.get_level_values(0) else raw
                f = f[['Open', 'High', 'Low', 'Close', 'Volume']].dropna(subset=['Close'])
            except (KeyError, TypeError):
                continue
            if len(f):
                save('1d', sym, merge(load('1d', sym), f))
                changed += 1
        time.sleep(1)
    return changed


def update_market(years: float = 8) -> Optional[pd.DataFrame]:
    """Nifty 50 and India VIX daily closes, the market context of every trade. Dhan's daily history adds a
    session only the next day, so once the market has closed, today's close is taken from today's 1-minute
    candles (session_close) when the daily history has not got it yet; the next night's download replaces it.
    Without that, every trade the next day would read the market as of two sessions back."""
    frames = {}
    for sid, name in ((NIFTY_ID, 'nifty'), (VIX_ID, 'vix')):
        time.sleep(PACE_S)
        r = requests.post(f'{dhan.API}/charts/historical', headers=dhan._headers(), timeout=40, json={
            'securityId': str(sid), 'exchangeSegment': 'IDX_I', 'instrument': 'INDEX', 'expiryCode': 0, 'oi': False,
            'fromDate': (data.now_ist().date() - timedelta(days=int(365 * years))).isoformat(),
            'toDate': (data.now_ist().date() + timedelta(days=1)).isoformat()})
        r.raise_for_status()
        d = r.json()
        idx = pd.to_datetime(d['timestamp'], unit='s', utc=True).tz_convert(TZ).normalize().tz_localize(None)
        frames[name] = pd.Series(d['close'], index=idx)
    m = pd.DataFrame(frames).sort_index()
    m = m[~m.index.duplicated(keep='last')]
    now = data.now_ist()
    day = pd.Timestamp(now.date())
    if session.trading_day(now.date()) and now.hour * 60 + now.minute >= 15 * 60 + 30 and (m.empty or m.index[-1] < day):
        today = {}
        for sid, name in ((NIFTY_ID, 'nifty'), (VIX_ID, 'vix')):
            try:
                today[name] = session_close(sid, now.date())
            except Exception as e:                       # the daily history alone, a day behind, as before
                logger.warning(f'{name}: today\'s close from minute candles failed: {dhan._redact(e)}')
        if today.get('nifty') is not None:                # no Nifty candles today: a holiday, nothing to add
            m = pd.concat([m, pd.DataFrame([today], index=[day])]).sort_index()
    if not m.empty and session.trading_day(now.date()) and now.hour >= 16 and m.index[-1] < day:
        logger.warning(f"Nifty / VIX daily closes end {m.index[-1].date()}: today's close is not in yet")
    save('market', 'NIFTY_VIX', m)
    return m


def session_close(sid: int, day: date) -> Optional[float]:
    """An index's close on `day` from its 1-minute candles: the last candle of that day (after the close Dhan
    adds one with the closing value). None when it has no candles that day."""
    f = dhan.chart_candles(str(sid), 'IDX_I', 'INDEX', 1, 1)
    f = f[f.index.date == day] if len(f) else f
    return round(float(f['Close'].iloc[-1]), 2) if len(f) else None


CONTEXT = 'CONTEXT'          # breadth and sector series, built from the daily candles held (update_context)
BREADTH_MIN_STOCKS = 50      # fewer stocks with a 50-day average that day, and breadth is not counted


def update_context() -> Optional[pd.DataFrame]:
    """The market's breadth and each sector's own index, day by day, from every stock's daily candles
    held (every stock ever in a lab universe, all topped up nightly: lab.update_history): `breadth50`, the share of stocks closing above their 50-day
    average (%), and `sec_<index>`, an equal-weighted index of the sector's members (100 at the start).
    Built by the lab after the daily candles; the live app and the backtester read the same file, so a
    trade's breadth and sector strength mean the same in both."""
    from niftywhale import universe
    closes = {}
    for p in sorted((LAB_DIR / 'history' / '1d').glob('*.pkl.gz')):
        sym = p.name[:-len('.pkl.gz')]
        f = load('1d', sym)
        if f is not None and len(f) >= 60 and 'Close' in f:
            s = f['Close'].astype(float)
            closes[sym] = s[~s.index.duplicated(keep='last')]
    if not closes:
        return None
    px = pd.DataFrame(closes).sort_index()
    ma50 = px.rolling(50, min_periods=50).mean()
    counted = ma50.notna().sum(axis=1)
    above = ((px > ma50) & ma50.notna()).sum(axis=1)
    out = pd.DataFrame({'breadth50': (above / counted.where(counted >= BREADTH_MIN_STOCKS) * 100).round(2)})
    rets = px.pct_change(fill_method=None)
    for key, members in universe.sector_members().items():
        have = [m for m in members if m in rets]
        if len(have) >= 3:
            out[f'sec_{key}'] = ((1 + rets[have].mean(axis=1, skipna=True).fillna(0)).cumprod() * 100).round(3)
    out = out.dropna(how='all')
    save('market', CONTEXT, out)
    return out


def load_market() -> Optional[pd.DataFrame]:
    """Nifty and VIX (update_market) with breadth and the sector series (update_context) beside them."""
    m, c = load('market', 'NIFTY_VIX'), load('market', CONTEXT)
    if m is None or c is None:
        return m
    return m.join(c, how='left')


def coverage(intervals=('1d', '15m', '5m')) -> Dict[str, Dict[str, object]]:
    """What is on disk: per interval, symbols, first and last date, size."""
    out = {}
    for iv in intervals:
        d = LAB_DIR / 'history' / iv
        files = sorted(d.glob('*.pkl.gz')) if d.exists() else []
        first = last = None
        for f in files[:3] + files[-3:]:
            try:
                fr = pd.read_pickle(f)
                a, b = fr.index[0], fr.index[-1]
                first = a if first is None or a < first else first
                last = b if last is None or b > last else last
            except Exception:
                continue
        out[iv] = {'symbols': len(files), 'first': str(first)[:10] if first is not None else None,
                   'last': str(last)[:10] if last is not None else None,
                   'mb': round(sum(f.stat().st_size for f in files) / 1e6, 1)}
    return out
