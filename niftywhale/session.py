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
"""
from datetime import date, datetime, time as dtime

from niftywhale import config

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
    """The F&O segment's hours (weekdays; exchange holidays are not modelled)."""
    return when.weekday() < 5 and OPEN <= when.time() < fo_close(when.date())
