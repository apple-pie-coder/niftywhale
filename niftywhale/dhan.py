"""
Real-time market data from Dhan (DhanHQ v2), for the live board and the 15m watcher.

yfinance stays the source for the evening scan (completed daily candles; its
delay does not matter there). When Dhan is connected, the two places where
minutes matter switch to it:

  quotes()        last price, day OHLC and previous close     POST /marketfeed/ohlc
  intraday()      1/5/15-minute candles, last few sessions    POST /charts/intraday
  option_chain()  one expiry's chain: OI, volume, IV, greeks  POST /optionchain
  expiries()      an underlying's option expiries             POST /optionchain/expirylist

Everything here is read-only market data. Nothing calls an order API.

Access tokens last 24 hours. Three ways to keep one:
  1. Auto      client ID + PIN + TOTP secret (App settings, or .env) -- a token is
               generated from a TOTP code and renewed before it expires.
  2. Pasted    a token from web.dhan.co -> My Profile -> Access DhanHQ APIs,
               pasted into the dashboard (or DHAN_ACCESS_TOKEN in .env).
               Renewed through the API while still valid, so a token pasted
               once keeps going as long as the app is running.
  3. None      the app uses yfinance, exactly as before.

Data APIs need Dhan's Data API subscription (free with 25+ trades in 30 days,
otherwise paid); `status()` reports it from the profile endpoint.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from niftywhale import config, store

logger = logging.getLogger(__name__)

API = 'https://api.dhan.co/v2'
AUTH = 'https://auth.dhan.co/app/generateAccessToken'
SCRIP_URL = 'https://images.dhan.co/api-data/api-scrip-master.csv'
IDS_PATH = Path(os.getenv('DHAN_IDS_PATH', os.path.join(os.path.dirname(store.DB_PATH) or '.', 'dhan_ids.json')))
LOTS_PATH = IDS_PATH.with_name('dhan_lots.json')     # option lot sizes, from the same download
OPTS_PATH = IDS_PATH.with_name('dhan_opts.json')     # option contract -> security ID, same download
IDS_MAX_AGE = 7 * 86400          # new listings appear; refresh the map weekly
RENEW_BEFORE = 2 * 3600          # renew a token this long before it expires
IST = timezone(timedelta(hours=5, minutes=30))

ENV_TOKEN = os.getenv('DHAN_ACCESS_TOKEN', '').strip()
# The auto-login credentials: App settings first, .env as the fallback (config.py).
_cid = lambda: config.get('dhan_client_id')          # noqa: E731
_pin = lambda: config.get('dhan_pin')                # noqa: E731
_totp_secret = lambda: config.get('dhan_totp')       # noqa: E731

# Stored outside the settings the dashboard reads back, so a token never
# leaves the server once saved.
KV_TOKEN, KV_EXPIRY, KV_CLIENT = 'secret:dhan_token', 'dhan_token_expiry', 'dhan_client_id'

_lock = threading.Lock()
_ids: Dict[str, str] = {}
_ids_at = 0.0
_quote_gate = threading.Lock()   # quote API: 1 request/second
_last_quote = 0.0
_chart_gate = threading.Lock()   # data APIs: 5 requests/second, shared by the swing
_last_chart = 0.0                # watcher, the intraday scanner and the dashboard
_oc_gate = threading.Lock()      # option chain: 1 request / 3 seconds, its own limit
_last_oc = 0.0
OC_SPACING = 3.5                 # Dhan documents 3 s, but answers 429 at 3.2 s now and then
_expiries: Dict[str, Any] = {}   # (security id, segment) -> (date fetched, [expiries])
_lots: Dict[str, int] = {}
_opt_ids: Dict[str, Any] = {}    # option contract key -> (security id, segment), or (None, retry at)
_state: Dict[str, Any] = {'error': None, 'profile': None, 'profile_at': 0.0, 'next_login': 0.0,
                          'next_renew': 0.0, 'ids_tried': 0.0,
                          'rejected': False}     # Dhan answered 401/403 to the current token
PROFILE_TTL = 600                # a good profile is re-read every 10 minutes
PROFILE_RETRY = 60               # a failed check waits this long: every data call asks
                                 # available(), and must not re-ask Dhan (or wait 15 s) each time
RENEW_BACKOFF = 900              # after a refused renewal, try again in 15 minutes
IDS_RETRY = 900                  # after a failed instrument-list download
LOGIN_BACKOFF = 900              # after a refused login, wait 15 min: retrying a bad PIN/TOTP
                                 # every tick could get the Dhan account locked


# ---------------------------------------------------------------- TOTP
def totp(secret_b32: str, at: Optional[float] = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 time-based one-time password (SHA-1), what authenticator apps show."""
    key = base64.b32decode(secret_b32.upper() + '=' * (-len(secret_b32) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# ---------------------------------------------------------------- tokens
def _redact(err) -> str:
    """An error's text without secrets. requests puts the whole URL in its
    connection errors, query string included, and the login call carries the
    PIN and a one-time code there; this text is logged and shown on the
    dashboard."""
    msg = re.sub(r'([?&](?:pin|totp|dhanClientId)=)[^&\s\'")]*', r'\1***', str(err), flags=re.I)
    for secret in (_pin(), _totp_secret(), token()):
        if secret and len(secret) >= 4:
            msg = msg.replace(secret, '***')
    return msg


def client_id() -> str:
    return _cid() or store.get_kv(KV_CLIENT) or ''


def mode() -> str:
    if _cid() and _pin() and _totp_secret():
        return 'auto'
    if token():
        return 'token'
    return 'off'


def token() -> str:
    return store.get_kv(KV_TOKEN) or ENV_TOKEN


def expiry() -> Optional[datetime]:
    raw = store.get_kv(KV_EXPIRY)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _parse_time(raw) -> Optional[datetime]:
    """Dhan returns expiry in a few shapes depending on the endpoint."""
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / (1000 if raw > 1e11 else 1), IST)
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S'):
        try:
            return datetime.strptime(str(raw)[:26], fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _claims(tok: str) -> Dict[str, Any]:
    """The payload of a JWT (not verified -- only read for expiry and client ID)."""
    try:
        payload = tok.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}


def _jwt_expiry(tok: str) -> Optional[datetime]:
    """Dhan tokens are JWTs; their `exp` claim is the most reliable expiry."""
    try:
        return datetime.fromtimestamp(int(_claims(tok)['exp']), IST)
    except (KeyError, ValueError, TypeError):
        return None


def save_token(tok: str, cid: str = None, exp: datetime = None) -> None:
    exp = exp or _jwt_expiry(tok) or (datetime.now(IST) + timedelta(hours=23))
    cid = cid or str(_claims(tok).get('dhanClientId') or '') or None
    store.set_kv(KV_TOKEN, tok)
    store.set_kv(KV_EXPIRY, exp.isoformat(timespec='seconds'))
    if cid:
        store.set_kv(KV_CLIENT, cid)
    _state.update(profile=None, profile_at=0.0, next_renew=0.0, rejected=False)   # re-read with the new token


def forget_token() -> None:
    for k in (KV_TOKEN, KV_EXPIRY):
        store.set_kv(k, '')
    _state.update(profile=None, profile_at=0.0, error=None)


def _headers(tok: str = None) -> Dict[str, str]:
    return {'access-token': tok or token(), 'client-id': client_id(),
            'Content-Type': 'application/json', 'Accept': 'application/json'}


def generate_token() -> bool:
    """Auto mode: a fresh 24h token from client ID + PIN + TOTP."""
    if time.time() < _state['next_login']:
        return False
    _state['next_login'] = time.time() + LOGIN_BACKOFF   # cleared below on success
    try:
        r = requests.post(AUTH, params={'dhanClientId': _cid(), 'pin': _pin(), 'totp': totp(_totp_secret())},
                          timeout=20)
        body = r.json() if r.content else {}
        tok = body.get('accessToken')
        if not r.ok or not tok:
            _state['error'] = _redact(f"token generation refused: {body.get('message') or body or r.status_code}")
            logger.warning(f"Dhan: {_state['error']}")
            return False
        save_token(tok, _cid(), _parse_time(body.get('expiryTime')))
        _state['error'] = None
        _state['next_login'] = 0.0
        logger.info(f'Dhan: new access token, valid until {expiry()}')
        return True
    except (requests.RequestException, ValueError) as e:
        _state['error'] = f'token generation failed: {_redact(e)}'
        logger.warning(f"Dhan: {_state['error']}")
        return False


def renew_token() -> bool:
    """Swap a still-valid token for a fresh 24h one (expired tokens cannot be renewed).
    A refusal backs off for RENEW_BACKOFF: the scheduler calls this every 20 seconds."""
    if time.time() < _state['next_renew']:
        return False
    _state['next_renew'] = time.time() + RENEW_BACKOFF   # cleared below on success
    try:
        r = requests.get(f'{API}/RenewToken', headers={'access-token': token(), 'dhanClientId': client_id()},
                         timeout=20)
        body = r.json() if r.content else {}
        tok = body.get('accessToken') or body.get('token')
        if not r.ok or not tok:
            logger.warning(f'Dhan: renew refused: {body or r.status_code}')
            return False
        save_token(tok, client_id(), _parse_time(body.get('expiryTime')))
        _state['next_renew'] = 0.0
        logger.info(f'Dhan: token renewed, valid until {expiry()}')
        return True
    except (requests.RequestException, ValueError) as e:
        logger.warning(f'Dhan: renew failed: {_redact(e)}')
        return False


def keep_alive() -> None:
    """Called by the scheduler: keep a valid token whenever we can."""
    if mode() == 'auto' and token() and _state.get('rejected'):
        # Dhan refused a token that has not expired (revoked: say, a new one was
        # generated elsewhere). Log in again now rather than near its expiry, a day
        # of yfinance later. generate_token() itself backs off 15 minutes.
        generate_token()
        return
    exp, now = expiry(), datetime.now(IST)
    if token() and exp and exp - now > timedelta(seconds=RENEW_BEFORE):
        return
    if token() and exp and exp > now and renew_token():
        return
    if mode() == 'auto':
        generate_token()


# ---------------------------------------------------------------- status
def profile(force: bool = False) -> Optional[Dict[str, Any]]:
    """The profile endpoint: token validity and Data API subscription. Cached 10 min."""
    if not token():
        return None
    age = time.time() - _state['profile_at']
    if not force and age < (PROFILE_TTL if _state['profile'] else PROFILE_RETRY):
        return _state['profile']             # also caches a failure, for PROFILE_RETRY
    try:
        r = requests.get(f'{API}/profile', headers=_headers(), timeout=10)
        if r.status_code in (401, 403):
            _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(),
                          rejected=True)
            return None
        r.raise_for_status()
        _state.update(profile=r.json(), profile_at=time.time(), error=None, rejected=False)
        if _state['profile'].get('dhanClientId') and not store.get_kv(KV_CLIENT):
            store.set_kv(KV_CLIENT, str(_state['profile']['dhanClientId']))
    except (requests.RequestException, ValueError) as e:
        # Dhan unreachable: keep the last good profile (the token is probably fine),
        # but don't ask again for PROFILE_RETRY.
        _state['error'] = f'profile check failed: {_redact(e)}'
        _state['profile_at'] = time.time() - (PROFILE_TTL - PROFILE_RETRY if _state['profile'] else 0)
        if force:
            return None          # a forced check must never answer from another token's cache
    return _state['profile']


def _data_plan_active(p: Optional[Dict[str, Any]]) -> bool:
    """From the profile's dataPlan field. If Dhan does not report it, assume
    yes and let a refused data call fall back to yfinance."""
    if p is None:                # no profile = no valid token; an empty one is still a valid token
        return False
    if p.get('dataPlan') in (None, ''):
        return True
    plan = str(p['dataPlan']).strip().lower()
    if any(w in plan for w in ('inactive', 'deactive', 'not', 'expired', 'false', 'none')):
        return False
    return 'active' in plan or plan in ('true', 'subscribed', 'yes')


def available() -> bool:
    """Is Dhan usable right now? Callers fall back to yfinance when not."""
    if config.get('data_source') == 'yfinance' or not token():
        return False
    exp = expiry()
    if exp and exp <= datetime.now(IST):
        return False
    return _data_plan_active(profile())


def status() -> Dict[str, Any]:
    """For the dashboard. Never includes the token itself."""
    p = profile() if token() else None
    exp = expiry()
    live = available()
    if mode() == 'off':
        msg = 'Not connected'
    elif not live and p and not _data_plan_active(p):
        msg = 'Connected, but the Data API subscription is not active'
    elif not live:
        msg = _state['error'] or 'Token expired'
    else:
        msg = 'Live'
    return {
        'provider': 'dhan' if live else 'yfinance',
        'live': live, 'mode': mode(), 'message': msg,
        'client_id': client_id() or None,
        'token_expiry': exp.isoformat(timespec='minutes') if exp else None,
        'data_plan': (p or {}).get('dataPlan'), 'data_validity': (p or {}).get('dataValidity'),
        'error': _state['error'],
    }


# ---------------------------------------------------------------- symbols
def _load_ids() -> Dict[str, str]:
    """NSE symbol -> Dhan security ID, from Dhan's instrument master (cached weekly)."""
    global _ids, _ids_at
    if _ids and time.time() - _ids_at < IDS_MAX_AGE:
        return _ids
    with _lock:
        if _ids and time.time() - _ids_at < IDS_MAX_AGE:
            return _ids
        fresh = (IDS_PATH.exists() and LOTS_PATH.exists() and OPTS_PATH.exists()
                 and time.time() - IDS_PATH.stat().st_mtime < IDS_MAX_AGE)
        # A failed download is retried every IDS_RETRY, not on every call: it can take 2 minutes.
        if not fresh and time.time() - _state['ids_tried'] > IDS_RETRY:
            _state['ids_tried'] = time.time()
            try:
                r = requests.get(SCRIP_URL, timeout=120)
                r.raise_for_status()
                ids, fallback, lots, opts = {}, {}, {}, {}
                for row in csv.DictReader(io.StringIO(r.text)):
                    if row.get('SEM_INSTRUMENT_NAME') in ('OPTIDX', 'OPTSTK'):
                        _lot_row(row, lots)
                        _opt_row(row, opts)
                        continue
                    if row.get('SEM_EXM_EXCH_ID') != 'NSE' or row.get('SEM_SEGMENT') != 'E':
                        continue
                    sym, sid = row.get('SEM_TRADING_SYMBOL', '').strip(), row.get('SEM_SMST_SECURITY_ID', '').strip()
                    if not sym or not sid:
                        continue
                    # The EQ series is the normal market; BE (trade-for-trade) only if there is no EQ.
                    if row.get('SEM_SERIES') == 'EQ':
                        ids[sym] = sid
                    elif row.get('SEM_SERIES') == 'BE':
                        fallback.setdefault(sym, sid)
                ids = {**fallback, **ids}
                IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
                IDS_PATH.write_text(json.dumps(ids))
                LOTS_PATH.write_text(json.dumps(lots))
                OPTS_PATH.write_text(json.dumps(opts, separators=(',', ':')))
                _opt_ids.clear()
                fresh = True
                logger.info(f'Dhan: {len(ids)} NSE equity IDs refreshed')
            except (requests.RequestException, OSError) as e:
                logger.warning(f'Dhan: instrument list download failed: {e}')
        try:
            _lots.clear()
            _lots.update(json.loads(LOTS_PATH.read_text()))
        except (OSError, ValueError):
            pass
        try:
            _ids = json.loads(IDS_PATH.read_text())
            # Fresh: good for a week. Stale (the refresh failed): use it, retry in IDS_RETRY.
            _ids_at = time.time() if fresh else time.time() - IDS_MAX_AGE + IDS_RETRY
        except (OSError, ValueError):
            _ids = {}
    return _ids


def _lot_row(row: Dict[str, str], lots: Dict[str, int]) -> None:
    """Lot size per option underlying. Trading symbols look like NIFTY-Oct2026-23000-CE or
    BAJAJ-AUTO-Oct2026-9000-PE, so the underlying is everything before the last three parts.
    NSE's contracts win over BSE's (stocks trade options on both); Sensex only on BSE."""
    parts = (row.get('SEM_TRADING_SYMBOL') or '').rsplit('-', 3)
    if len(parts) != 4:
        return
    und, exch = parts[0], row.get('SEM_EXM_EXCH_ID')
    try:
        lot = int(float(row.get('SEM_LOT_UNITS') or 0))
    except ValueError:
        return
    if lot > 0 and (und not in lots or exch == 'NSE'):
        lots[und] = lot


def _opt_key(und: str, expiry: str, strike: float, opt_side: str) -> str:
    return f"{und}|{str(expiry)[:10]}|{float(strike):g}|{opt_side}"


def _opt_row(row: Dict[str, str], opts: Dict[str, str]) -> None:
    """One option contract's security ID: 'NIFTY|2026-10-13|22450|CE' -> '44608:N' (N = NSE_FNO,
    B = BSE_FNO). NSE's contract wins where a stock trades options on both, as in _lot_row."""
    parts = (row.get('SEM_TRADING_SYMBOL') or '').rsplit('-', 3)
    sid, typ, exch = (row.get('SEM_SMST_SECURITY_ID') or '').strip(), row.get('SEM_OPTION_TYPE'), row.get('SEM_EXM_EXCH_ID')
    if len(parts) != 4 or not sid or typ not in ('CE', 'PE') or exch not in ('NSE', 'BSE'):
        return
    try:
        key = _opt_key(parts[0], row.get('SEM_EXPIRY_DATE') or '', float(row.get('SEM_STRIKE_PRICE') or 0), typ)
    except ValueError:
        return
    if key not in opts or exch == 'NSE':
        opts[key] = sid + (':N' if exch == 'NSE' else ':B')


def option_ids(und: str, expiry: str, contracts: List[tuple]) -> Dict[tuple, tuple]:
    """{(strike, 'CE' | 'PE'): (security id, 'NSE_FNO' | 'BSE_FNO')} for one underlying and expiry,
    for live quotes. The map (~120k contracts, ~5 MB) is read from disk once per call that has a
    miss, and not kept: a few new contracts a day. One not in it is asked about again after IDS_RETRY."""
    out, missing = {}, []
    for strike, side in contracts:
        hit = _opt_ids.get(_opt_key(und, expiry, strike, side))
        if hit and hit[0]:
            out[(strike, side)] = hit
        elif not hit or time.time() >= hit[1]:
            missing.append((strike, side))
    if missing:
        _load_ids()
        try:
            known = json.loads(OPTS_PATH.read_text())
        except (OSError, ValueError):
            known = {}
        for strike, side in missing:
            key = _opt_key(und, expiry, strike, side)
            raw = known.get(key)
            if not raw:
                _opt_ids[key] = (None, time.time() + IDS_RETRY)
                continue
            sid, seg = raw.split(':')
            _opt_ids[key] = out[(strike, side)] = (sid, 'NSE_FNO' if seg == 'N' else 'BSE_FNO')
    return out


def option_id(und: str, expiry: str, strike: float, opt_side: str) -> Optional[tuple]:
    """(security id, segment) of one option contract, or None."""
    return option_ids(und, expiry, [(strike, opt_side)]).get((strike, opt_side))


def lot_size(symbol: str) -> Optional[int]:
    _load_ids()
    return _lots.get(symbol)


def security_id(symbol: str) -> Optional[str]:
    return _load_ids().get(symbol.upper().replace('.NS', ''))


# ---------------------------------------------------------------- data
def quotes(symbols: List[str]) -> Dict[str, Dict[str, float]]:
    """symbol -> {last, open, high, low, prev_close}, one request for all of them."""
    global _last_quote
    ids = {security_id(s): s for s in symbols}
    ids.pop(None, None)
    if not ids:
        return {}
    with _quote_gate:                              # 1 request/second, across all callers
        wait = 1.05 - (time.time() - _last_quote)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.post(f'{API}/marketfeed/ohlc', headers=_headers(),
                              json={'NSE_EQ': [int(i) for i in ids]}, timeout=10)
        finally:
            _last_quote = time.time()
    r.raise_for_status()
    out = {}
    for sid, q in (r.json().get('data', {}).get('NSE_EQ') or {}).items():
        sym, o = ids.get(str(sid)), q.get('ohlc') or {}
        if sym and q.get('last_price'):
            # In this response `close` is the previous session's close.
            out[sym] = {'last': float(q['last_price']), 'open': o.get('open'), 'high': o.get('high'),
                        'low': o.get('low'), 'prev_close': o.get('close')}
    return out


def _chart_slot() -> None:
    """Wait for this process's next data-API slot (5 requests/second, all threads)."""
    global _last_chart
    with _chart_gate:
        wait = 0.21 - (time.time() - _last_chart)
        if wait > 0:
            time.sleep(wait)
        _last_chart = time.time()


def intraday(symbols: List[str], days: int = 7, interval: int = 15) -> Dict[str, pd.DataFrame]:
    """
    `interval`-minute candles per symbol for the last `days` calendar days, as
    OHLCV frames indexed by candle start time in IST (the same shape yfinance gives).
    """
    now = datetime.now(IST)
    body_base = {'exchangeSegment': 'NSE_EQ', 'instrument': 'EQUITY', 'interval': str(interval), 'oi': False,
                 'fromDate': (now - timedelta(days=days)).strftime('%Y-%m-%d 09:00:00'),
                 'toDate': now.strftime('%Y-%m-%d %H:%M:%S')}
    out = {}
    for sym in symbols:
        sid = security_id(sym)
        if not sid:
            continue
        try:
            _chart_slot()
            r = requests.post(f'{API}/charts/intraday', headers=_headers(),
                              json={**body_base, 'securityId': sid}, timeout=15)
            if r.status_code == 429:                # over the limit anyway: back off once, retry
                time.sleep(1.5)
                _chart_slot()
                r = requests.post(f'{API}/charts/intraday', headers=_headers(),
                                  json={**body_base, 'securityId': sid}, timeout=15)
            if r.status_code in (401, 403):
                # The token is no good for any symbol: asking for the rest of
                # the list would only repeat the refusal. available() now
                # says no (for PROFILE_RETRY), so callers use yfinance.
                _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(),
                              rejected=True)
                logger.warning(f'Dhan: token rejected while fetching {interval}m candles; using yfinance')
                break
            r.raise_for_status()
            d = r.json()
            if not d.get('timestamp'):
                continue
            idx = pd.to_datetime(d['timestamp'], unit='s', utc=True).tz_convert(IST)
            frame = pd.DataFrame({'Open': d['open'], 'High': d['high'], 'Low': d['low'],
                                  'Close': d['close'], 'Volume': d.get('volume') or [0] * len(idx)},
                                 index=idx).sort_index()
            out[sym if sym.endswith('.NS') else sym + '.NS'] = frame
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning(f'Dhan: {interval}m candles for {sym} failed: {_redact(e)}')
    return out


# ---------------------------------------------------------------- the Charts tab
def ltp(stocks: List[str], index_ids: List[int], contracts: Dict[str, List[int]] = None) -> Dict[str, float]:
    """Last traded prices of stocks, indices and F&O contracts together, one request (the quote
    API's 1-a-second limit, shared with quotes()). `contracts`: {'NSE_FNO': [security id, ...],
    'BSE_FNO': [...]}. Keys: the stock symbol, 'IDX:<id>', or 'OPT:<segment>:<id>'."""
    global _last_quote
    ids = {security_id(s): s for s in stocks}
    ids.pop(None, None)
    body = {}
    if ids:
        body['NSE_EQ'] = [int(i) for i in ids]
    if index_ids:
        body['IDX_I'] = [int(i) for i in index_ids]
    for seg, cids in (contracts or {}).items():
        if cids:
            body[seg] = [int(i) for i in cids]
    if not body:
        return {}
    with _quote_gate:
        wait = 1.05 - (time.time() - _last_quote)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.post(f'{API}/marketfeed/ltp', headers=_headers(), json=body, timeout=10)
        finally:
            _last_quote = time.time()
    if r.status_code in (401, 403):
        _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(), rejected=True)
    r.raise_for_status()
    d = r.json().get('data', {})
    out = {}
    for sid, q in (d.get('NSE_EQ') or {}).items():
        if ids.get(str(sid)) and q.get('last_price'):
            out[ids[str(sid)]] = float(q['last_price'])
    for sid, q in (d.get('IDX_I') or {}).items():
        if q.get('last_price'):
            out[f'IDX:{int(sid)}'] = float(q['last_price'])
    for seg in contracts or {}:
        for sid, q in (d.get(seg) or {}).items():
            if q.get('last_price'):
                out[f'OPT:{seg}:{int(sid)}'] = float(q['last_price'])
    return out


def quotes_full(contracts: Dict[str, List[int]]) -> Dict[tuple, Dict[str, float]]:
    """Full quotes (price, OI, volume, best bid / ask) of up to 1000 instruments in one request,
    {(segment, security id): {...}}: an option chain's OI between two chain reads. Shares the
    quote API's 1-a-second slot with ltp() and quotes(); a refusal (429) is retried once."""
    global _last_quote
    body = {seg: [int(i) for i in ids] for seg, ids in contracts.items() if ids}
    if not body:
        return {}
    for attempt in range(2):
        with _quote_gate:
            wait = 1.05 - (time.time() - _last_quote)
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.post(f'{API}/marketfeed/quote', headers=_headers(), json=body, timeout=10)
            finally:
                _last_quote = time.time()
        if r.status_code != 429:
            break
        time.sleep(1.2)
    if r.status_code in (401, 403):
        _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(), rejected=True)
    r.raise_for_status()
    out = {}
    for seg, rows in (r.json().get('data') or {}).items():
        if not isinstance(rows, dict):
            continue
        for sid, q in rows.items():
            depth = q.get('depth') or {}
            bid = ((depth.get('buy') or [{}])[0] or {}).get('price')
            ask = ((depth.get('sell') or [{}])[0] or {}).get('price')
            out[(seg, int(sid))] = {'ltp': float(q.get('last_price') or 0), 'oi': float(q.get('oi') or 0),
                                    'vol': float(q.get('volume') or 0), 'bid': float(bid or 0), 'ask': float(ask or 0)}
    return out


def chart_candles(sid: str, segment: str, instrument: str, interval: int, days: int) -> pd.DataFrame:
    """`interval`-minute candles (1, 5, 15, 25, 60) of any instrument for the last `days` (90 at
    most), indexed in IST; `interval` 1440 gives daily candles (years back), indexed by date."""
    now = datetime.now(IST)
    _chart_slot()
    if interval == 1440:
        r = requests.post(f'{API}/charts/historical', headers=_headers(), timeout=20, json={
            'securityId': str(sid), 'exchangeSegment': segment, 'instrument': instrument, 'expiryCode': 0, 'oi': False,
            'fromDate': (now - timedelta(days=days)).strftime('%Y-%m-%d'), 'toDate': (now + timedelta(days=1)).strftime('%Y-%m-%d')})
    else:
        r = requests.post(f'{API}/charts/intraday', headers=_headers(), timeout=20, json={
            'securityId': str(sid), 'exchangeSegment': segment, 'instrument': instrument, 'interval': str(interval), 'oi': False,
            'fromDate': (now - timedelta(days=min(days, 90))).strftime('%Y-%m-%d 09:00:00'), 'toDate': now.strftime('%Y-%m-%d %H:%M:%S')})
    if r.status_code in (401, 403):
        _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(), rejected=True)
    r.raise_for_status()
    d = r.json()
    if not d.get('timestamp'):
        return pd.DataFrame(columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    idx = pd.to_datetime(d['timestamp'], unit='s', utc=True).tz_convert(IST)
    if interval == 1440:
        idx = pd.DatetimeIndex(idx.tz_localize(None).normalize())
    f = pd.DataFrame({'Open': d['open'], 'High': d['high'], 'Low': d['low'], 'Close': d['close'],
                      'Volume': d.get('volume') or [0] * len(idx)}, index=idx).sort_index()
    return f[~f.index.duplicated(keep='last')]


# ---------------------------------------------------------------- options
def _oc_post(path: str, body: Dict[str, Any]) -> requests.Response:
    """One option-chain request, spaced OC_SPACING apart across all threads. Dhan allows
    one every 3 seconds, separately from the chart and quote limits above."""
    global _last_oc
    with _oc_gate:
        wait = OC_SPACING - (time.time() - _last_oc)
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.post(f'{API}{path}', headers=_headers(), json=body, timeout=20)
        finally:
            _last_oc = time.time()


def _underlying(symbol: str, index_ids: Dict[str, Any]):
    if symbol in index_ids:
        sid, seg = index_ids[symbol][0], index_ids[symbol][1]
        return int(sid), seg
    sid = security_id(symbol)
    return (int(sid), 'NSE_EQ') if sid else (None, None)


def _oc_refused(r: requests.Response) -> None:
    if r.status_code in (401, 403):
        _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(), rejected=True)
    r.raise_for_status()


def expiries(symbol: str, index_ids: Dict[str, Any]) -> List[str]:
    """An underlying's option expiries (YYYY-MM-DD), fetched once a day."""
    sid, seg = _underlying(symbol, index_ids)
    if sid is None:
        return []
    today = datetime.now(IST).date().isoformat()
    hit = _expiries.get((sid, seg))
    if hit and hit[0] == today:
        return hit[1]
    r = _oc_post('/optionchain/expirylist', {'UnderlyingScrip': sid, 'UnderlyingSeg': seg})
    if r.status_code == 429:
        time.sleep(2 * OC_SPACING)
        r = _oc_post('/optionchain/expirylist', {'UnderlyingScrip': sid, 'UnderlyingSeg': seg})
    _oc_refused(r)
    exp = sorted(str(e)[:10] for e in (r.json().get('data') or []))
    _expiries[(sid, seg)] = (today, exp)
    return exp


def option_chain(symbol: str, expiry: str, index_ids: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The raw chain for one expiry: {last_price, oc: {strike: {ce, pe}}}."""
    sid, seg = _underlying(symbol, index_ids)
    if sid is None:
        return None
    r = _oc_post('/optionchain', {'UnderlyingScrip': sid, 'UnderlyingSeg': seg, 'Expiry': expiry})
    if r.status_code == 429:                        # over the limit anyway: back off, retry once
        time.sleep(2 * OC_SPACING)
        r = _oc_post('/optionchain', {'UnderlyingScrip': sid, 'UnderlyingSeg': seg, 'Expiry': expiry})
    _oc_refused(r)
    return r.json().get('data') or None


# ---------------------------------------------------------------- indices
def index_quotes(ids: List[int]) -> Dict[int, Dict[str, float]]:
    """Index id -> {last, open, high, low}, one request for all of them (the quote API's
    1-a-second limit, shared with quotes()). The response's `close` is left out: after the
    session it is today's close, not the previous one."""
    global _last_quote
    with _quote_gate:
        wait = 1.05 - (time.time() - _last_quote)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.post(f'{API}/marketfeed/ohlc', headers=_headers(), json={'IDX_I': [int(i) for i in ids]}, timeout=10)
        finally:
            _last_quote = time.time()
    if r.status_code in (401, 403):
        _state.update(error='token rejected (expired or revoked)', profile=None, profile_at=time.time(), rejected=True)
    r.raise_for_status()
    out = {}
    for sid, q in (r.json().get('data', {}).get('IDX_I') or {}).items():
        o = q.get('ohlc') or {}
        if q.get('last_price'):
            out[int(sid)] = {'last': float(q['last_price']), 'open': o.get('open'), 'high': o.get('high'), 'low': o.get('low')}
    return out


def index_daily_closes(sid: int, days: int = 12) -> list:
    """[(date, close)] of an index's recent daily candles."""
    now = datetime.now(IST)
    _chart_slot()
    r = requests.post(f'{API}/charts/historical', headers=_headers(), timeout=15, json={
        'securityId': str(sid), 'exchangeSegment': 'IDX_I', 'instrument': 'INDEX', 'expiryCode': 0, 'oi': False,
        'fromDate': (now - timedelta(days=days)).strftime('%Y-%m-%d'), 'toDate': (now + timedelta(days=1)).strftime('%Y-%m-%d')})
    r.raise_for_status()
    d = r.json()
    return [(datetime.fromtimestamp(ts, IST).date(), c) for ts, c in zip(d.get('timestamp') or [], d.get('close') or [])]
