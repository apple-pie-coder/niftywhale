"""
Market data: daily candles for the swing screen, 15- and 5-minute candles
for the triggers and the intraday scanner.

Daily data is batch-downloaded and cached in memory, because a scan touches
every stock but the dashboard's chart view re-reads the same frames. 15m data
is fetched only for the handful of names whose zone is in play.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import pandas as pd
import yfinance as yf

from niftywhale import dhan

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
BATCH = 50

_cache: Dict[str, tuple] = {}          # ticker -> (fetched_at, frame)
_lock = threading.Lock()


# ---------------------------------------------------------------- the clock
def now_ist() -> datetime:
    return datetime.now(IST)


def market_open(when: Optional[datetime] = None) -> bool:
    """Weekday session hours. Exchange holidays are not modelled: on one, the
    watcher simply finds no new candles."""
    t = when or now_ist()
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return MARKET_OPEN[0] * 60 + MARKET_OPEN[1] <= minutes < MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]


LIVE_TTL = 600             # during the session today's candle changes: refresh often
CLOSED_TTL = 3 * 3600      # after it the close is final, but refresh so a late print lands
SETTLE = 15 * 60           # how long after 15:30 the closing candle takes to be final


def last_close(when: Optional[datetime] = None) -> datetime:
    """The most recent weekday 15:30 IST at or before `when`."""
    t = (when or now_ist()).astimezone(IST)
    close = t.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    if t < close:
        close -= timedelta(days=1)
    while close.weekday() >= 5:
        close -= timedelta(days=1)
    return close


def _fresh(fetched_at: float, now: float) -> bool:
    """
    Is a daily frame fetched at `fetched_at` still good at `now`? During the
    session for LIVE_TTL. After it, only a frame fetched once the close had
    settled counts: one fetched at 14:35 holds a 14:35 candle, not the close,
    however young it is when the evening scan runs at 16:15.
    """
    age = now - fetched_at
    when = datetime.fromtimestamp(now, IST)
    if market_open(when):
        return age < LIVE_TTL
    settled = last_close(when).timestamp() + SETTLE
    if now < settled:                       # the close is still settling
        return age < LIVE_TTL
    return fetched_at >= settled and age < CLOSED_TTL


# ---------------------------------------------------------------- splitting
def _split(data: pd.DataFrame, tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """
    yfinance returns (ticker, field) or (field, ticker) MultiIndex columns
    depending on version and on whether one or many tickers were asked for.
    Normalise to one plain OHLCV frame per ticker.
    """
    out = {}
    if data is None or data.empty:
        return out
    cols = data.columns
    for t in tickers:
        try:
            if isinstance(cols, pd.MultiIndex):
                if t in cols.get_level_values(0):
                    frame = data[t]
                elif t in cols.get_level_values(1):
                    frame = data.xs(t, axis=1, level=1)
                else:
                    continue
            else:
                frame = data
            frame = frame[[c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c in frame.columns]]
            frame = frame.dropna(subset=['Close'])
            if not frame.empty:
                out[t] = frame
        except Exception as e:                         # one bad ticker never sinks the batch
            logger.debug(f'split {t}: {e}')
    return out


# ---------------------------------------------------------------- daily
def daily(tickers: List[str], should_stop: Callable[[], bool] = None,
          progress: Callable[[int, int], None] = None, force: bool = False,
          stale_ok: bool = True) -> Dict[str, pd.DataFrame]:
    """
    One year of daily candles per ticker, from cache where still fresh. A
    ticker whose download fails falls back to whatever frame is cached,
    however old -- unless `stale_ok` is False: a scan would sync the watch
    list from it, so there a failed download stays a gap (the scan leaves
    that stock's zone alone, and fails if the whole source is down).
    """
    now = time.time()
    wanted = set(tickers)
    with _lock:
        fresh = {t: f for t, (at, f) in _cache.items()
                 if t in wanted and not force and _fresh(at, now)}
    todo = [t for t in tickers if t not in fresh]
    done = len(fresh)
    if progress:
        progress(done, len(tickers))

    for i in range(0, len(todo), BATCH):
        if should_stop and should_stop():
            break
        chunk = todo[i:i + BATCH]
        try:
            data = yf.download(chunk, period='1y', interval='1d', group_by='ticker',
                               auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            logger.warning(f'daily batch of {len(chunk)} failed: {e}')
            data = None
        got = _split(data, chunk)
        with _lock:
            for t, f in got.items():
                _cache[t] = (time.time(), f)
        fresh.update(got)
        done += len(chunk)
        if progress:
            progress(min(done, len(tickers)), len(tickers))

    missing = [t for t in tickers if t not in fresh]
    if missing:
        # A failed refresh should not blank a chart we had a frame for.
        if stale_ok:
            with _lock:
                for t in missing:
                    if t in _cache:
                        fresh[t] = _cache[t][1]
        logger.info(f'daily: {len(missing)} ticker(s) without fresh data')
    return fresh


def frames_for(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """
    Whatever daily frames we hold, however old, downloading only what is
    missing. For the rule tuner: re-screening the same candles under new
    thresholds should take a moment, not another round trip to yfinance.
    """
    with _lock:
        have = {t: _cache[t][1] for t in tickers if t in _cache}
    missing = [t for t in tickers if t not in have]
    if missing:
        have.update(daily(missing))
    return have


def cached_daily(ticker: str) -> Optional[pd.DataFrame]:
    with _lock:
        hit = _cache.get(ticker)
    return hit[1] if hit else None


# ---------------------------------------------------------------- intraday
_intraday_cache: Dict[tuple, tuple] = {}     # (ticker, minutes) -> (fetched_at, frame)
INTRADAY_TTL = 55          # the live board polls once a minute


def intraday_cached(tickers: List[str], minutes: int = 15) -> Dict[str, pd.DataFrame]:
    """intraday(), but anything fetched in the last minute is reused -- the
    board, the stock panel and the watchers often want the same frames."""
    now = time.time()
    with _lock:
        have = {t: f for (t, m), (at, f) in _intraday_cache.items()
                if m == minutes and t in tickers and now - at < INTRADAY_TTL}
    missing = [t for t in tickers if t not in have]
    if missing:
        fresh = intraday(missing, minutes)
        with _lock:
            for t, f in fresh.items():
                _intraday_cache[(t, minutes)] = (time.time(), f)
        have.update(fresh)
    return have


def intraday(tickers: List[str], minutes: int = 15) -> Dict[str, pd.DataFrame]:
    """The last few sessions of `minutes`-minute candles: from Dhan when it is
    connected (real time), from yfinance for anything Dhan could not supply."""
    if not tickers:
        return {}
    frames: Dict[str, pd.DataFrame] = {}
    if dhan.available():
        try:
            frames = dhan.intraday(tickers, interval=minutes)
        except Exception as e:
            logger.warning(f'Dhan {minutes}m candles failed, using yfinance: {e}')
    rest = [t for t in tickers if t not in frames]
    if rest:
        frames.update(_yf_intraday(rest, minutes))
    return frames


def live_quotes(tickers: List[str]) -> Dict[str, Dict[str, float]]:
    """Real-time quotes from Dhan, keyed like the tickers; {} when not connected."""
    if not tickers or not dhan.available():
        return {}
    try:
        q = dhan.quotes([t.replace('.NS', '') for t in tickers])
        return {s + '.NS': v for s, v in q.items()}
    except Exception as e:
        logger.warning(f'Dhan quotes failed: {e}')
        return {}


def _yf_intraday(tickers: List[str], minutes: int = 15) -> Dict[str, pd.DataFrame]:
    """The last five sessions of intraday candles from yfinance, one request for all of them."""
    if not tickers:
        return {}
    try:
        data = yf.download(tickers, period='5d', interval=f'{minutes}m', group_by='ticker',
                           auto_adjust=False, progress=False, threads=True)
    except Exception as e:
        logger.warning(f'{minutes}m download failed: {e}')
        return {}
    frames = _split(data, tickers)
    for t, f in frames.items():
        if f.index.tz is None:
            frames[t] = f.tz_localize('UTC').tz_convert(IST)
        else:
            frames[t] = f.tz_convert(IST)
    return frames
