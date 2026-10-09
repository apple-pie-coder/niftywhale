"""
The Charts tab: candles for any stock or index at any timeframe from one second to a week, and the
live price behind them (livefeed.py).

  timeframe   with Dhan                              without (yfinance)
  1s-30s      ticks, from when a chart opens          ticks (yfinance every 15 s: steps, not ticks)
  1m-1h       Dhan 1 / 5 / 15 / 60-minute candles     yfinance 1m (5 days), 5m-15m (1 month), 1h (6 months)
  30m, 4h     made from 15m / 1h, from 09:15
  1D          Dhan daily (5 years)                    yfinance daily (5 years)
  1W          weeks from the daily candles            yfinance weekly (10 years)

Instruments are the universe's stocks (by symbol) and the ticker's indices ('IDX:<Dhan id>'). Times go
to the browser as UTC seconds shifted to IST (the chart library draws UTC), daily and weekly candles as
their date. Live prices come from Dhan's WebSocket feed (dhanws.py), pushed as they trade, only while
something on screen wants them and the market is open. While that connection is down they are polled
once a second from Dhan's LTP API (its limit: one quote request a second, shared with the board).
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from niftywhale import data, dhan, dhanws, livefeed, ticker, universe

logger = logging.getLogger(__name__)

TFS: List[Tuple[int, str]] = [(1, '1s'), (5, '5s'), (15, '15s'), (30, '30s'), (60, '1m'), (300, '5m'), (900, '15m'),
                              (1800, '30m'), (3600, '1h'), (14400, '4h'), (86400, '1D'), (604800, '1W')]
TF_SECONDS = {s for s, _ in TFS}
# tf -> (source interval in minutes, days of history, resample rule)
DHAN = {60: (1, 5, None), 300: (5, 30, None), 900: (15, 60, None), 1800: (15, 60, '30min'), 3600: (60, 90, None),
        14400: (60, 90, '4h'), 86400: (1440, 5 * 365, None), 604800: (1440, 8 * 365, 'W')}
YF = {60: ('1m', '5d', None), 300: ('5m', '1mo', None), 900: ('15m', '1mo', None), 1800: ('15m', '1mo', '30min'),
      3600: ('60m', '6mo', None), 14400: ('60m', '1y', '4h'), 86400: ('1d', '5y', None), 604800: ('1wk', '10y', None)}
# Indices yfinance carries (the rest are charted only with Dhan).
YF_INDEX = {13: '^NSEI', 25: '^NSEBANK', 51: '^BSESN', 21: '^INDIAVIX', 27: 'NIFTY_FIN_SERVICE.NS', 29: '^CNXIT',
            14: '^CNXAUTO', 28: '^CNXFMCG', 32: '^CNXPHARMA', 31: '^CNXMETAL', 34: '^CNXREALTY', 33: '^CNXPSUBANK',
            42: '^CNXENERGY', 30: '^CNXMEDIA', 17: '^CNX100', 19: '^CRSLDX', 38: '^NSMIDCP', 37: '^NSEMDCP50'}
SHIFT = 19800
MAX_BARS = 1500
_cache: Dict[tuple, tuple] = {}
_lock = threading.Lock()


def instruments() -> List[Dict[str, Any]]:
    out = [{'symbol': f'IDX:{i}', 'label': lab, 'name': name, 'group': 'Indices'} for i, lab, name, _ in ticker.INDEXES]
    try:
        stocks = universe.load().get('stocks', [])
    except (OSError, ValueError):                       # no universe built yet: indices only
        stocks = []
    for s in sorted(stocks, key=lambda x: x['symbol']):
        if not universe.is_placeholder(s['symbol']):
            out.append({'symbol': s['symbol'], 'label': s['symbol'], 'name': s.get('name') or s['symbol'], 'group': 'Stocks'})
    return out


_known: Dict[str, Any] = {'at': 0.0, 'set': set()}


def known(sym: str) -> bool:
    if time.time() - _known['at'] > 600:
        _known['set'] = {i['symbol'] for i in instruments()}
        _known['at'] = time.time()
    return sym in _known['set']


def _index_id(sym: str) -> Optional[int]:
    return int(sym[4:]) if sym.startswith('IDX:') and sym[4:].isdigit() else None


def ttl(tf: int) -> float:
    return 2 if tf < 60 else 15 if tf < 86400 else 300


# ---------------------------------------------------------------- the live price
_yl: Dict[str, Any] = {'at': 0.0, 'px': {}}


def _yf_codes(symbols: List[str]) -> Dict[str, str]:
    out = {}
    for s in symbols:
        i = _index_id(s)
        if i is None:
            out[s + '.NS'] = s
        elif i in YF_INDEX:
            out[YF_INDEX[i]] = s
    return out


def _yf_last(symbols: List[str]) -> Dict[str, float]:
    import yfinance as yf
    if time.time() - _yl['at'] < 15 and set(symbols) <= set(_yl['px']):
        return {s: _yl['px'][s] for s in symbols if s in _yl['px']}
    codes = _yf_codes(symbols)
    if codes:
        raw = yf.download(list(codes), period='1d', interval='1m', group_by='ticker', auto_adjust=False,
                          progress=False, threads=True)
        for code, f in data._split(raw, list(codes)).items():
            c = f['Close'].dropna() if f is not None and 'Close' in f else None
            if c is not None and len(c):
                _yl['px'][codes[code]] = float(c.iloc[-1])
    _yl['at'] = time.time()
    return {s: _yl['px'][s] for s in symbols if s in _yl['px']}


def fetch_prices(symbols: List[str]) -> Dict[str, float]:
    """One poll's prices. 'OPT:<segment>:<id>' symbols are F&O contracts (open option
    positions): Dhan only, yfinance has no Indian option quotes."""
    contracts: Dict[str, List[int]] = {}
    for s in symbols:
        if s.startswith('OPT:'):
            _, seg, sid = s.split(':')
            contracts.setdefault(seg, []).append(int(sid))
    plain = [s for s in symbols if not s.startswith('OPT:')]
    if dhan.available():
        stocks = [s for s in plain if _index_id(s) is None]
        idx = [_index_id(s) for s in plain if _index_id(s) is not None]
        return dhan.ltp(stocks, idx, contracts)
    return _yf_last(plain) if plain else {}


def interval() -> float:
    return 1.05 if dhan.available() else 5.0


def feed_key(sym: str) -> Optional[tuple]:
    """A feed symbol's Dhan instrument: (segment, security id)."""
    if sym.startswith('OPT:'):
        parts = sym.split(':')
        return (parts[1], int(parts[2])) if len(parts) == 3 and parts[2].isdigit() else None
    i = _index_id(sym)
    if i is not None:
        return ('IDX_I', i)
    sid = dhan.security_id(sym)
    return ('NSE_EQ', int(sid)) if sid else None


def ws_url() -> Optional[str]:
    if not dhan.available():
        return None
    return dhanws.URL.format(token=dhan.token(), client=dhan.client_id())


class DhanSource:
    """The live feed's stream: feed symbols to Dhan instruments and back, and the latest packet of
    each (open / high / low, OI, best bid and ask) for what wants more than the price."""
    QUOTE_KEEP_S = 600

    def __init__(self, feed_fn):
        self._feed = feed_fn                    # -> the Feed the prices go to
        self._lock = threading.Lock()
        self._sym: Dict[tuple, str] = {}
        self._quotes: Dict[tuple, dict] = {}
        self.stream = dhanws.Stream(ws_url, self._packet, name='dhan-feed')

    def cover(self, want: Dict[str, str]) -> set:
        if not want or not dhan.available():
            self.stream.want({})
            return set()
        keys, syms = {}, {}
        for s, m in want.items():
            k = feed_key(s)
            if k:
                keys[k], syms[k] = m, s
        with self._lock:
            self._sym = syms
        self.stream.want(keys)
        have = self.stream.subscribed()
        return {s for k, s in syms.items() if k in have}

    def _packet(self, p: dict) -> None:
        k, now = (p['seg'], p['sid']), time.time()
        with self._lock:
            q = self._quotes.get(k)
            if q is None:
                q = self._quotes[k] = {}
            q.update({a: b for a, b in p.items() if a not in ('code', 'seg', 'sid')})
            q['at'] = now
            sym = self._sym.get(k)
        ltp = p.get('ltp')
        if sym and ltp and ltp > 0:
            self._feed().add({sym: float(ltp)}, now)

    def quote(self, sym: str, fresh_s: float = 15) -> Optional[dict]:
        """The latest packet's fields for a feed symbol, if one came in the last `fresh_s` seconds."""
        k = feed_key(sym)
        with self._lock:
            q = self._quotes.get(k) if k else None
            return dict(q) if q and time.time() - q['at'] <= fresh_s else None

    def carried(self, syms) -> Dict[str, dict]:
        """The latest packet of each symbol the open connection carries and has priced since it
        opened. An instrument that has not traded since is still current: the stream sends a
        packet on subscribing and on every change."""
        have, since = self.stream.subscribed(), self.stream.since or 0
        out = {}
        with self._lock:
            for s in syms:
                k = feed_key(s)
                q = self._quotes.get(k) if k in have else None
                if q and q['at'] >= since:
                    out[s] = dict(q)
        return out

    def quotes(self, syms, fresh_s: float = 15) -> Dict[str, dict]:
        out = {}
        for s in syms:
            q = self.quote(s, fresh_s)
            if q:
                out[s] = q
        return out

    def prune(self) -> None:
        cut = time.time() - self.QUOTE_KEEP_S
        with self._lock:
            for k in [k for k, q in self._quotes.items() if q['at'] < cut]:
                del self._quotes[k]

    def status(self) -> dict:
        return self.stream.status()


SOURCE = DhanSource(lambda: FEED)
FEED = livefeed.Feed(fetch_prices, interval, active=lambda: data.market_open(), name='chart-feed', stream=SOURCE)


def streaming() -> bool:
    return dhan.available() and SOURCE.stream.connected()


def live() -> Dict[str, Any]:
    on = dhan.available()
    ws = on and SOURCE.stream.connected()
    return {'source': 'dhan' if on else 'yfinance', 'realtime': on, 'stream': ws, 'poll_ms': 1000 if on else 5000,
            'note': 'Dhan live feed, every trade' if ws else 'Dhan prices, every second' if on
            else 'yfinance prices, every 15 s. Connect Dhan for real-time.'}


# ---------------------------------------------------------------- candles
def _resample(f: pd.DataFrame, rule: Optional[str]) -> pd.DataFrame:
    if not rule or f is None or f.empty:
        return f
    agg = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}
    if rule == 'W':                                     # weeks labelled by their Monday
        return f.resample('W-MON', label='left', closed='left').agg(agg).dropna(subset=['Open'])
    # The session opens at 09:15: 30-minute candles from 09:15, 4-hour ones at 09:15 and 13:15.
    offset = '15min' if rule == '30min' else '75min'
    return f.resample(rule, offset=offset).agg(agg).dropna(subset=['Open'])


def _dhan(sym: str, tf: int) -> Optional[pd.DataFrame]:
    if tf not in DHAN:
        return None
    minutes, days, rule = DHAN[tf]
    i = _index_id(sym)
    seg = (str(i), 'IDX_I', 'INDEX') if i is not None else (dhan.security_id(sym), 'NSE_EQ', 'EQUITY')
    if not seg[0]:
        return None
    f = dhan.chart_candles(*seg, minutes, days)
    if minutes == 1440:
        # Dhan's daily history lags a session (the latest day appears the next morning): the
        # days it lacks, today's forming one too, are made from its hourly candles.
        h = dhan.chart_candles(*seg, 60, 7)
        if len(h):
            d = h.groupby(h.index.tz_localize(None).normalize()).agg(
                {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
            d = d[d.index > f.index.max()] if len(f) else d
            f = pd.concat([f, d]) if len(f) else d
    return _resample(f, rule)


def _yf(sym: str, tf: int) -> Optional[pd.DataFrame]:
    import yfinance as yf
    if tf not in YF:
        return None
    codes = _yf_codes([sym])
    if not codes:
        return None
    code = next(iter(codes))
    iv, period, rule = YF[tf]
    raw = yf.download(code, period=period, interval=iv, auto_adjust=False, progress=False, threads=False)
    f = data._split(raw, [code]).get(code)
    if f is None or f.empty:
        return None
    if tf >= 86400:
        f.index = pd.DatetimeIndex(f.index).tz_localize(None).normalize() if getattr(f.index, 'tz', None) else f.index
    else:
        f = (f.tz_localize('UTC') if f.index.tz is None else f).tz_convert('Asia/Kolkata')
    return _resample(f, rule)


def _rows(f: pd.DataFrame, daily: bool) -> List[list]:
    if f is None or f.empty:
        return []
    f = f.dropna(subset=['Open', 'High', 'Low', 'Close']).tail(MAX_BARS)
    if daily:
        ts = [int(pd.Timestamp(i.date()).tz_localize('UTC').timestamp()) for i in f.index]
    else:
        idx = f.index if f.index.tz is not None else f.index.tz_localize('UTC')
        ts = [int(i.timestamp()) + SHIFT for i in idx]
    vol = f['Volume'].fillna(0) if 'Volume' in f else pd.Series(0, index=f.index)
    return [[t, round(float(o), 4), round(float(h), 4), round(float(l), 4), round(float(c), 4), float(v)]
            for t, o, h, l, c, v in zip(ts, f['Open'], f['High'], f['Low'], f['Close'], vol)]


def candles(sym: str, tf: int) -> Dict[str, Any]:
    key = (sym, tf, dhan.available())
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl(tf):
        return hit[1]
    daily = tf >= 86400
    f, src = None, 'ticks'
    if tf >= 60:
        if dhan.available():
            try:
                f, src = _dhan(sym, tf), 'dhan'
            except Exception as e:
                logger.warning(f'chart {sym} {tf}s from Dhan failed: {dhan._redact(e)}')
        if f is None or f.empty:
            try:
                f, src = _yf(sym, tf), 'yfinance'
            except Exception as e:
                logger.warning(f'chart {sym} {tf}s from yfinance failed: {e}')
    bars = _rows(f, daily) if f is not None else []
    if tf < 60:
        bars = FEED.bars(sym, tf, SHIFT)
    first = FEED.first_at(sym)
    out = {'bars': bars, 'source': src if bars else 'none', 'daily': daily,
           'ticks_from': int(first) + SHIFT if first else None}
    with _lock:
        _cache[key] = (time.time(), out)
    return out
