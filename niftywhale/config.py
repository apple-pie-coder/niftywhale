"""
Settings that used to live only in .env, now set on the dashboard (App settings). Each one is read
from the app's database first, then from the environment, then its default: a value saved in the
app wins, and clearing it there falls back to .env again.

Secrets (the Telegram bot token, the Dhan PIN and TOTP secret) are stored like the Dhan token
already is: written by the dashboard, never sent back to it. view() says only whether one is set.

Reads are cached for TTL seconds: dhan.available() asks for the data source many times a second.
"""
import logging
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from niftywhale import store

logger = logging.getLogger(__name__)

TTL = 3.0
LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
DATA_SOURCES = ('auto', 'yfinance')

# name: (database key, environment variable, default, secret)
FIELDS: Dict[str, Tuple[str, str, str, bool]] = {
    'telegram_token': ('secret:telegram_token', 'TELEGRAM_BOT_TOKEN', '', True),
    'telegram_chat_id': ('cfg:telegram_chat_id', 'TELEGRAM_CHAT_ID', '', False),
    'dhan_client_id': ('cfg:dhan_client_id', 'DHAN_CLIENT_ID', '', False),
    'dhan_pin': ('secret:dhan_pin', 'DHAN_PIN', '', True),
    'dhan_totp': ('secret:dhan_totp', 'DHAN_TOTP_SECRET', '', True),
    'watch_delay': ('cfg:watch_delay_s', 'WATCH_DELAY_SECONDS', '90', False),
    'live_watch_delay': ('cfg:live_watch_delay_s', 'LIVE_WATCH_DELAY_SECONDS', '10', False),
    'data_source': ('cfg:data_source', 'NIFTYWHALE_DATA', 'auto', False),
    'log_level': ('cfg:log_level', 'LOG_LEVEL', 'INFO', False),
    # NSE's session clock around the closing auction (session.py), and the intraday square-off.
    'intraday_square_off': ('cfg:intraday_square_off', 'INTRADAY_SQUARE_OFF', '15:10', False),
    'nse_cts_end_fo': ('cfg:nse_cts_end_fo', 'NSE_CTS_END_FO', '15:15', False),
    'nse_closes_final': ('cfg:nse_closes_final', 'NSE_CLOSES_FINAL', '15:45', False),
    'nse_fo_close': ('cfg:nse_fo_close', 'NSE_FO_CLOSE', '15:40', False),
    'nse_cas_start': ('cfg:nse_cas_start', 'NSE_CAS_START', '2026-08-03', False),
    # A watchdog off the Pi (healthchecks.io, or any address that alerts when its pings stop): pinged every minute
    # while the app runs, so a power or network cut, which takes the Pi's own Telegram alerts down with it, is told.
    'heartbeat_url': ('secret:heartbeat_url', 'HEARTBEAT_URL', '', True),
}
# What App settings calls each one (the change log shows them by these names).
LABELS = {'telegram_token': 'Telegram bot token', 'telegram_chat_id': 'Telegram chat ID', 'dhan_client_id': 'Dhan client ID',
          'dhan_pin': 'Dhan PIN', 'dhan_totp': 'Dhan TOTP secret', 'watch_delay': 'Wait after a candle, with yfinance (s)',
          'live_watch_delay': 'Wait after a candle, with Dhan (s)', 'data_source': 'Price data', 'log_level': 'Log level',
          'intraday_square_off': 'Intraday square-off', 'nse_cts_end_fo': 'F&O stocks stop trading at',
          'nse_closes_final': 'Official closes are out by', 'nse_fo_close': 'F&O segment closes at',
          'nse_cas_start': 'Closing auction began on', 'heartbeat_url': 'Watchdog ping address'}
# Times (HH:MM) and the range each may take; a value out of it from .env falls back to the default.
CLOCKS: Dict[str, Tuple[str, str, str]] = {
    'intraday_square_off': ('09:30', '15:30', 'the intraday square-off'),
    'nse_cts_end_fo': ('14:00', '15:30', "F&O stocks' continuous trading end"),
    'nse_closes_final': ('15:30', '17:00', 'the time official closes are out'),
    'nse_fo_close': ('15:15', '16:00', 'the F&O close'),
}

_cache: Dict[str, Tuple[float, str, str]] = {}      # name -> (read at, value, source)
_lock = threading.Lock()


def _read(name: str) -> Tuple[str, str]:
    key, env, default, _ = FIELDS[name]
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < TTL:
        return hit[1], hit[2]
    try:
        saved = store.get_kv(key)
    except Exception:                       # no database yet (a test, a cold start): .env decides
        saved = None
    raw_env = os.getenv(env, '')
    if saved:
        value, source = saved, 'app'
    elif raw_env.strip():
        value, source = raw_env, 'env'
    else:
        value, source = default, 'default'
    value = clean(name, value)
    if (name in CLOCKS or name == 'nse_cas_start') and source != 'default' and validate(name, value):
        value, source = default, 'default'          # a typo in .env (or a stale value) keeps the default
    with _lock:
        _cache[name] = (time.time(), value, source)
    return value, source


def get(name: str) -> str:
    return _read(name)[0]


def source(name: str) -> str:
    """Where the value comes from: 'app', 'env' or 'default'."""
    return _read(name)[1]


def get_clock(name: str):
    """A time setting (CLOCKS) as a datetime.time."""
    return datetime.strptime(get(name), '%H:%M').time()


def get_int(name: str) -> int:
    try:
        return int(float(get(name)))
    except ValueError:
        return int(FIELDS[name][2])


def clean(name: str, value: Any) -> str:
    v = str(value if value is not None else '').strip()
    if name == 'dhan_totp':
        v = v.replace(' ', '').upper()
    elif name in ('data_source',):
        v = v.lower()
    elif name == 'log_level':
        v = v.upper()
    return v


def validate(name: str, value: Any) -> Optional[str]:
    """Why `value` can't be saved for `name`, or None. An empty value is always fine: it clears."""
    if name not in FIELDS:
        return f'unknown setting {name}'
    v = clean(name, value)
    if not v:
        return None
    if name == 'telegram_token' and not re.fullmatch(r'\d{5,12}:[A-Za-z0-9_-]{30,60}', v):
        return 'a bot token looks like 123456789:AA… (from @BotFather)'
    if name == 'telegram_chat_id' and not re.fullmatch(r'-?\d{3,20}|@[A-Za-z0-9_]{5,40}', v):
        return 'a chat ID is a number (negative for groups) or @channelname'
    if name == 'dhan_client_id' and not re.fullmatch(r'\d{6,15}', v):
        return 'the Dhan client ID is a number'
    if name == 'dhan_pin' and not re.fullmatch(r'\d{4,6}', v):
        return 'the Dhan PIN is 4 to 6 digits'
    if name == 'dhan_totp' and not re.fullmatch(r'[A-Z2-7]{16,64}=*', v):
        return 'the TOTP secret is the base32 key Dhan shows when you set up an authenticator (letters A-Z and digits 2-7)'
    if name == 'watch_delay' and not (v.isdigit() and 0 <= int(v) <= 600):
        return 'the delay is 0 to 600 seconds'
    if name == 'live_watch_delay' and not (v.isdigit() and 0 <= int(v) <= 120):
        return 'the delay is 0 to 120 seconds'
    if name == 'data_source' and v not in DATA_SOURCES:
        return 'data source is auto or yfinance'
    if name == 'log_level' and v not in LOG_LEVELS:
        return f'log level is one of {", ".join(LOG_LEVELS)}'
    if name in CLOCKS:
        lo, hi, what = CLOCKS[name]
        try:
            t = datetime.strptime(v, '%H:%M').strftime('%H:%M')
        except ValueError:
            return f'{what} is a time like {FIELDS[name][2]}'
        if not lo <= t <= hi:
            return f'{what} is between {lo} and {hi}'
    if name == 'heartbeat_url' and not re.fullmatch(r'https://[^\s/$.?#][^\s]{3,300}', v):
        return 'the watchdog address is an https:// link (healthchecks.io gives one per check)'
    if name == 'nse_cas_start':
        try:
            d = date.fromisoformat(v)
        except ValueError:
            return "the closing auction's first day is a date like 2026-08-03"
        if d.year < 2024:
            return "the closing auction's first day is 2024 or later"
    return None


def save(values: Dict[str, Any]) -> None:
    """Store the given settings (validated by the caller); '' clears one back to .env / default."""
    for name, value in values.items():
        store.set_kv(FIELDS[name][0], clean(name, value))
    with _lock:
        for name in values:
            _cache.pop(name, None)
    if 'log_level' in values:
        apply_log_level()


def apply_log_level() -> None:
    logging.getLogger().setLevel(get('log_level'))


def view() -> Dict[str, Any]:
    """For the dashboard: each setting's value and where it comes from; for a secret only whether it is set."""
    out = {}
    for name, (_, env, default, secret) in FIELDS.items():
        value, src = _read(name)
        item = {'source': src, 'env_set': bool(os.getenv(env, '').strip()), 'env': env, 'default': default}
        if secret:
            item['set'] = bool(value)
        else:
            item['value'] = value
        out[name] = item
    return out
