"""
NSE's session clock, in one place.

Since 3 August 2026 a stock with F&O contracts ends continuous trading at 15:15 and gets its
closing price from a closing auction (CAS: a short transition, order entry to a random close
near 15:30, then one match by about 15:35). Nothing trades in it between 15:15 and that match,
so its candles stop at 15:15; and an index's value in that window is built from the auction's
indicative prices, not from trades. A stock without F&O trades on to 15:30 and closes on the
VWAP of its last half hour. The F&O segment itself trades to 15:40.

SEBI's consultation of 12 Sep 2026 proposes moving these again (continuous trading to 15:30
with the auction 15:31-15:40, or the auction 15:15-15:25 with F&O to 15:30), so each time can
be set in .env as NSE_<NAME>=HH:MM, and the auction's first day as NSE_CAS_START=YYYY-MM-DD.
Days before that keep the old session: one close at 15:30 for every stock.

Pure: no I/O beyond reading the environment once.
"""
import os
from datetime import date, datetime, time as dtime


def _clock(name: str, default: str) -> dtime:
    try:
        return datetime.strptime((os.getenv('NSE_' + name) or '').strip(), '%H:%M').time()
    except ValueError:
        return datetime.strptime(default, '%H:%M').time()      # unset or a typo: the default


def _day(name: str, default: str) -> date:
    try:
        return date.fromisoformat((os.getenv('NSE_' + name) or '').strip())
    except ValueError:
        return date.fromisoformat(default)


OPEN = dtime(9, 15)
CLOSE = dtime(15, 30)                           # continuous trading ends: stocks without F&O
CTS_END_FO = _clock('CTS_END_FO', '15:15')      # continuous trading ends: stocks with F&O; their auction follows
CLOSES_FINAL = _clock('CLOSES_FINAL', '15:45')  # every official close is out (the auction's match included)
FO_CLOSE = _clock('FO_CLOSE', '15:40')          # the F&O segment
CAS_START = _day('CAS_START', '2026-08-03')


def auction(day: date) -> bool:
    """Whether F&O stocks closed by auction on `day`."""
    return day >= CAS_START


def cts_end(fo: bool, day: date) -> dtime:
    """When a stock's continuous trading (and so its last candle) ends on `day`."""
    return CTS_END_FO if fo and auction(day) else CLOSE


def index_end(day: date) -> dtime:
    """The last moment an index's value comes from trades on `day`: its constituents are F&O
    stocks, so from the auction's start it is their continuous trading's end."""
    return cts_end(True, day)


def fo_close(day: date) -> dtime:
    """When the F&O segment stops trading on `day`."""
    return FO_CLOSE if auction(day) else CLOSE


def fo_open(when: datetime) -> bool:
    """The F&O segment's hours (weekdays; exchange holidays are not modelled)."""
    return when.weekday() < 5 and OPEN <= when.time() < fo_close(when.date())
