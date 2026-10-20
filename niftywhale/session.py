"""
NSE's session clock, in one place.

Since 3 August 2026 a stock with F&O contracts ends continuous trading at 15:15 and gets its
closing price from a closing auction (CAS: a short transition, order entry to a random close
near 15:30, then one match by about 15:35). Nothing trades in it between 15:15 and that match,
so its candles stop at 15:15; and an index's value in that window is built from the auction's
indicative prices, not from trades. A stock without F&O trades on to 15:30 and closes on the
VWAP of its last half hour. The F&O segment itself trades to 15:40.

SEBI's consultation of 12 Sep 2026 proposes moving these again (continuous trading to 15:30
with the auction 15:31-15:40, or the auction 15:15-15:25 with F&O to 15:30), so each time is a
setting (config.py: App settings > Market session, else NSE_<NAME> in .env), read afresh each
time it is used. Days before the auction's first day keep the old session: one close at 15:30
for every stock.

Exchange holidays come from NSE's own list (api/holiday-master, fetched daily by the app:
smart.Fetcher.holidays), held in the database so the lab reads the same days. Until a list has
been fetched every weekday counts as a session, as it always did. Muhurat trading (an hour on a
Diwali evening) is not modelled: its day stays a holiday.
"""
import sqlite3
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, Optional

from niftywhale import config, store

OPEN = dtime(9, 15)
CLOSE = dtime(15, 30)                           # continuous trading ends: stocks without F&O


def cts_end_fo() -> dtime:
    """Continuous trading ends for stocks with F&O; their auction follows."""
    return config.get_clock('nse_cts_end_fo')


def closes_final() -> dtime:
    """Every official close is out (the auction's match included)."""
    return config.get_clock('nse_closes_final')


def cas_start() -> date:
    return date.fromisoformat(config.get('nse_cas_start'))


def auction(day: date) -> bool:
    """Whether F&O stocks closed by auction on `day`."""
    return day >= cas_start()


def cts_end(fo: bool, day: date) -> dtime:
    """When a stock's continuous trading (and so its last candle) ends on `day`."""
    return cts_end_fo() if fo and auction(day) else CLOSE


def index_end(day: date) -> dtime:
    """The last moment an index's value comes from trades on `day`: its constituents are F&O
    stocks, so from the auction's start it is their continuous trading's end."""
    return cts_end(True, day)


def fo_close(day: date) -> dtime:
    """When the F&O segment stops trading on `day`."""
    return config.get_clock('nse_fo_close') if auction(day) else CLOSE


def fo_open(when: datetime) -> bool:
    """The F&O segment's hours on a trading day."""
    return trading_day(when.date()) and OPEN <= when.time() < fo_close(when.date())


# ---------------------------------------------------------------- holidays
HOLIDAYS_KEY = 'nse:holidays'           # {'fetched': ISO time, 'days': {ISO date: name}}
HOLIDAYS_TTL = 60.0                     # seconds a read of the held list is reused (market_open is asked often)
_held: Dict[str, Any] = {'at': None, 'days': {}}


def parse_holidays(payload: Dict[str, Any]) -> Dict[str, str]:
    """NSE's trading holidays (api/holiday-master?type=trading) as {ISO date: name}, from the cash
    and F&O lists (the same days today; either one is enough). The list also carries the odd extra
    session ("Special Live Trading", Saturday 2 Mar 2024): a day named for trading is not a holiday.
    Its morning_session / evening_session fields were empty in every year from 2023 to 2026, the
    Muhurat days included, so extra sessions are not read from it."""
    out = {}
    for segment in ('CM', 'FO'):
        for h in (payload or {}).get(segment) or []:
            if 'trading' in (h.get('description') or '').lower():
                continue
            try:
                d = datetime.strptime(h['tradingDate'], '%d-%b-%Y').date()
            except (KeyError, TypeError, ValueError):
                continue
            out.setdefault(d.isoformat(), (h.get('description') or 'Exchange holiday').strip())
    return out


def save_holidays(fetched: Dict[str, str]) -> Dict[str, str]:
    """Hold a fetched list: the years it covers are replaced (NSE adds or moves a holiday during the
    year), earlier years are kept."""
    held = store.get_json(HOLIDAYS_KEY, {}) or {}
    years = {d[:4] for d in fetched}
    days = {d: n for d, n in (held.get('days') or {}).items() if d[:4] not in years}
    days.update(fetched)
    days = dict(sorted(days.items()))
    store.set_json(HOLIDAYS_KEY, {'fetched': store.now_local().isoformat(timespec='seconds'), 'days': days})
    _held.update(at=None)
    return days


def holidays() -> Dict[str, str]:
    """The exchange holidays held, {ISO date: name}; empty until a list has been fetched."""
    now = time.monotonic()
    if _held['at'] is None or now - _held['at'] > HOLIDAYS_TTL:
        try:
            days = (store.get_json(HOLIDAYS_KEY, {}) or {}).get('days') or {}
        except sqlite3.Error:
            days = _held['days']                # the database busy or not set up: the last read
        _held.update(at=now, days=days)
    return _held['days']


def holiday(day: date) -> Optional[str]:
    """The name of the exchange holiday on `day`, or None."""
    return holidays().get(day.isoformat())


def trading_day(day: date) -> bool:
    """Does NSE hold a session on `day`: a weekday that is not an exchange holiday."""
    return day.weekday() < 5 and day.isoformat() not in holidays()


def prev_trading_day(day: date) -> date:
    """The last trading day before `day`."""
    d = day - timedelta(days=1)
    while not trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(day: date) -> date:
    """The first trading day after `day`."""
    d = day + timedelta(days=1)
    while not trading_day(d):
        d += timedelta(days=1)
    return d
