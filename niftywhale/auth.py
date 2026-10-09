"""
Signing in to NiftyWhale: one owner account, two ways in, and the gate every request passes.

  1. Password, then a second factor: a 6-digit code from an authenticator app (TOTP, RFC 6238), or one of
     ten one-time recovery codes.
  2. A passkey (Face ID / Touch ID / a security key) alone: possession of the device plus its biometric
     check is itself two factors, and it cannot be phished (the browser binds it to this site's address).

Sessions are server-side: the cookie holds a random token whose SHA-256 is stored, so a session can be
listed and revoked, and a stolen database holds no usable cookies. Personal API tokens (for curl and
scripts) are stored the same way. Passwords use werkzeug's scrypt hashing.

Guards: a per-address limit on failed attempts, an account lock after repeated failures, a used TOTP
step is never accepted twice, cookie-authenticated writes must come from one of the app's own origins
(CSRF), and first-time setup needs a one-time code that only someone with access to the Pi can read
(`python -m niftywhale.auth setup-code`), so nobody else on the network can claim the account first.

The routes live here as a blueprint (bp); app.py registers it and calls gate() before every request.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

from flask import Blueprint, g, jsonify, make_response, redirect, request
from werkzeug.security import check_password_hash, generate_password_hash

from niftywhale import store

logger = logging.getLogger(__name__)
bp = Blueprint('auth', __name__)

# Off only for the feature tests (their command sets NIFTYWHALE_AUTH=0); app.py logs a warning if so.
ENABLED = os.getenv('NIFTYWHALE_AUTH', '1') != '0'
# The addresses the app is served on. Passkeys are bound to the first one's host name (the RP ID).
ORIGINS = [o.strip().rstrip('/') for o in os.getenv('NIFTYWHALE_ORIGINS', 'https://pandorasbox.local:5443').split(',') if o.strip()]
RP_ID = urlparse(ORIGINS[0]).hostname if ORIGINS else 'localhost'
RP_NAME = 'NiftyWhale'

SESSION_COOKIE, PRE_COOKIE = 'nw_session', 'nw_pre'
SESSION_IDLE_S = 12 * 3600            # a session unused this long ends
REMEMBER_S = 30 * 86400               # "keep me signed in": this long, renewed by use
PRE_S = 300                           # between the password and the second factor
FRESH_S = 600                         # sensitive changes need a sign-in this recent (or the password)
MIN_PASSWORD = 10
LOCK_AFTER, LOCK_S = 8, 900           # failures in a row before the account locks, and for how long
IP_WINDOW_S, IP_MAX_FAILS = 900, 20   # failures from one address within the window before it is refused
RECOVERY_CODES = 10
TOKEN_PREFIX = 'nwt_'
PUBLIC_PATHS = ('/healthz', '/login', '/trust', '/favicon.ico', '/static/', '/api/auth/')
COMMON = {'password', 'password1', 'password123', '1234567890', 'qwertyuiop', 'niftywhale', 'letmein123', 'iloveyou12'}

_lock = threading.Lock()
_pending: Dict[str, Dict[str, Any]] = {}      # short-lived state keyed by the nw_pre cookie: setup, MFA, challenges
_fails: Dict[str, deque] = {}                 # address -> times of failed attempts
_schema_ready = False


# ---------------------------------------------------------------- storage
def _db():
    global _schema_ready
    c = store.connect()
    if not _schema_ready:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS auth_user (
                id INTEGER PRIMARY KEY CHECK (id = 1), username TEXT, password_hash TEXT, totp_secret TEXT,
                totp_last_step INTEGER DEFAULT 0, user_handle TEXT, created TEXT, updated TEXT,
                failed INTEGER DEFAULT 0, locked_until REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_recovery (id INTEGER PRIMARY KEY AUTOINCREMENT, code_hash TEXT, used_at TEXT);
            CREATE TABLE IF NOT EXISTS auth_passkeys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, credential_id TEXT UNIQUE, public_key TEXT, sign_count INTEGER,
                transports TEXT, name TEXT, aaguid TEXT, backed_up INTEGER, created TEXT, last_used TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE, created TEXT, last_seen REAL,
                expires REAL, remember INTEGER, method TEXT, ip TEXT, user_agent TEXT, revoked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, token_hash TEXT UNIQUE, prefix TEXT, created TEXT,
                last_used TEXT, revoked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, ok INTEGER, ip TEXT, user_agent TEXT, detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_auth_events ON auth_events(id);
        ''')
        _schema_ready = True
    return c


def _rows(cur) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def owner() -> Optional[Dict[str, Any]]:
    with _db() as c:
        rows = _rows(c.execute('SELECT * FROM auth_user WHERE id = 1'))
    return rows[0] if rows else None


def _event(kind: str, ok: bool = True, detail: str = '') -> None:
    ip, ua = _client()
    with _db() as c:
        c.execute('INSERT INTO auth_events (ts, kind, ok, ip, user_agent, detail) VALUES (?, ?, ?, ?, ?, ?)',
                  (_now(), kind, int(ok), ip, ua, detail[:200]))
        c.execute('DELETE FROM auth_events WHERE id < (SELECT MAX(id) - 500 FROM auth_events)')


def _client():
    try:
        return request.remote_addr or '', (request.headers.get('User-Agent') or '')[:200]
    except RuntimeError:                      # no request (the command line)
        return 'shell', 'command line'


# ---------------------------------------------------------------- TOTP (RFC 6238: SHA-1, 6 digits, 30 s)
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return '%06d' % ((struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000)


def totp_check(secret: str, code: str, last_step: int = 0, at: float = None) -> Optional[int]:
    """The time step `code` matches (this one or one either side, for clock drift), or None. A step at or
    before `last_step` is refused: a code seen once cannot be replayed."""
    code = re.sub(r'\D', '', code or '')
    if len(code) != 6 or not secret:
        return None
    now = int((at or time.time()) // 30)
    for step in (now - 1, now, now + 1):
        if step > last_step and hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, username: str) -> str:
    return f'otpauth://totp/{quote(RP_NAME)}:{quote(username)}?secret={secret}&issuer={quote(RP_NAME)}&digits=6&period=30'


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return img.to_string(encoding='unicode')


# ---------------------------------------------------------------- recovery codes
def _new_recovery() -> List[str]:
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'                 # no 0/o, 1/l/i: read off paper without doubt
    codes = ['-'.join(''.join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2)) for _ in range(RECOVERY_CODES)]
    with _db() as c:
        c.execute('DELETE FROM auth_recovery')
        c.executemany('INSERT INTO auth_recovery (code_hash) VALUES (?)', [(_sha(x),) for x in codes])
    return codes


def _use_recovery(code: str) -> bool:
    norm = re.sub(r'[^a-z0-9]', '', (code or '').lower())
    if len(norm) != 10:
        return False
    h = _sha(norm[:5] + '-' + norm[5:])
    with _db() as c:
        cur = c.execute('UPDATE auth_recovery SET used_at = ? WHERE code_hash = ? AND used_at IS NULL', (_now(), h))
        return cur.rowcount == 1


def recovery_left() -> int:
    with _db() as c:
        return c.execute('SELECT COUNT(*) FROM auth_recovery WHERE used_at IS NULL').fetchone()[0]


# ---------------------------------------------------------------- setup code (first visit)
def setup_code() -> Optional[str]:
    """The one-time code first-time setup asks for; made on first need, gone once the owner exists."""
    if owner():
        return None
    code = store.get_kv('secret:auth_setup_code')
    if not code:
        code = '-'.join(''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(4)) for _ in range(3))
        store.set_kv('secret:auth_setup_code', code)
        logger.warning(f'No owner account yet. First-time setup code: {code}')
    return code


# ---------------------------------------------------------------- rate limits and locks
def _ip_blocked() -> Optional[int]:
    ip, _ = _client()
    q = _fails.get(ip)
    if not q:
        return None
    cut = time.time() - IP_WINDOW_S
    while q and q[0] < cut:
        q.popleft()
    return int(q[0] + IP_WINDOW_S - time.time()) + 1 if len(q) >= IP_MAX_FAILS else None


def _fail(kind: str, detail: str = '', account: bool = True) -> None:
    ip, _ = _client()
    _fails.setdefault(ip, deque(maxlen=200)).append(time.time())
    _event(kind, False, detail)
    if account and owner():
        with _db() as c:
            c.execute('UPDATE auth_user SET failed = failed + 1 WHERE id = 1')
            n = c.execute('SELECT failed FROM auth_user WHERE id = 1').fetchone()[0]
            if n >= LOCK_AFTER:
                c.execute('UPDATE auth_user SET locked_until = ?, failed = 0 WHERE id = 1', (time.time() + LOCK_S,))
                locked = True
            else:
                locked = False
        if locked:
            _event('locked', False, f'{LOCK_AFTER} failed attempts in a row')
            try:
                store.add_notice(f'auth-lock:{int(time.time())}', 'system', 'warn', 'Sign-in locked for 15 minutes',
                                 f'{LOCK_AFTER} failed attempts in a row, the last from {ip}.', link={'tab': 'security'})
            except Exception:
                pass


def _locked_for() -> int:
    u = owner()
    return max(0, int((u or {}).get('locked_until') or 0) - int(time.time())) if u else 0


def _refuse() -> Optional[tuple]:
    wait = _ip_blocked()
    if wait:
        return jsonify({'error': f'Too many attempts from this address. Try again in {wait // 60 + 1} min.', 'retry_after': wait}), 429
    wait = _locked_for()
    if wait:
        return jsonify({'error': f'Sign-in is locked after repeated failures. Try again in {wait // 60 + 1} min.', 'retry_after': wait}), 423
    return None


def _clear_fails() -> None:
    with _db() as c:
        c.execute('UPDATE auth_user SET failed = 0, locked_until = 0 WHERE id = 1')


# ---------------------------------------------------------------- short-lived state (setup, MFA, challenges)
def _pend_new(kind: str, **data) -> str:
    pid = secrets.token_urlsafe(24)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if v['until'] < now]:
            del _pending[k]
        _pending[pid] = {'kind': kind, 'until': now + PRE_S, **data}
    return pid


def _pend_get(kind: str) -> Optional[Dict[str, Any]]:
    pid = request.cookies.get(PRE_COOKIE)
    with _lock:
        p = _pending.get(pid or '')
        if not p or p['until'] < time.time() or p['kind'] != kind:
            return None
        return p


def _pend_drop() -> None:
    with _lock:
        _pending.pop(request.cookies.get(PRE_COOKIE) or '', None)


def _with_pre(resp, pid: str):
    resp.set_cookie(PRE_COOKIE, pid, max_age=PRE_S, httponly=True, secure=request.is_secure, samesite='Strict', path='/')
    return resp


# ---------------------------------------------------------------- sessions and tokens
def _start_session(method: str, remember: bool):
    token = secrets.token_urlsafe(32)
    ip, ua = _client()
    life = REMEMBER_S if remember else SESSION_IDLE_S
    with _db() as c:
        c.execute('INSERT INTO auth_sessions (token_hash, created, last_seen, expires, remember, method, ip, user_agent) '
                  'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (_sha(token), _now(), time.time(), time.time() + life, int(remember), method, ip, ua))
    _clear_fails()
    _event('signed_in', True, method)
    try:
        store.add_notice(f'auth-in:{_sha(token)[:12]}', 'system', 'info', 'New sign-in',
                         f'{_device(ua)} · {method} · {ip}', link={'tab': 'security'})
    except Exception:
        pass
    resp = make_response(jsonify({'ok': True, 'next': _safe_next(request.args.get('next') or (request.get_json(silent=True) or {}).get('next'))}))
    resp.set_cookie(SESSION_COOKIE, token, max_age=life if remember else None, httponly=True, secure=request.is_secure,
                    samesite='Lax', path='/')
    resp.delete_cookie(PRE_COOKIE, path='/')
    _pend_drop()
    return resp


def _device(ua: str) -> str:
    ua = ua or ''
    dev = 'iPhone' if 'iPhone' in ua else 'iPad' if 'iPad' in ua else 'Android' if 'Android' in ua else \
        'Mac' if 'Macintosh' in ua else 'Windows' if 'Windows' in ua else 'Linux' if 'Linux' in ua else 'a device'
    br = 'Edge' if 'Edg/' in ua else 'Chrome' if ('Chrome/' in ua or 'CriOS' in ua) else 'Firefox' if ('Firefox/' in ua or 'FxiOS' in ua) else \
        'Safari' if 'Safari/' in ua else ''
    return f'{br} on {dev}' if br else dev


def _session() -> Optional[Dict[str, Any]]:
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return None
    with _db() as c:
        rows = _rows(c.execute('SELECT * FROM auth_sessions WHERE token_hash = ? AND revoked = 0', (_sha(tok),)))
        if not rows:
            return None
        s = rows[0]
        now = time.time()
        idle_end = s['last_seen'] + (REMEMBER_S if s['remember'] else SESSION_IDLE_S)
        if now > s['expires'] or now > idle_end:
            c.execute('UPDATE auth_sessions SET revoked = 1 WHERE id = ?', (s['id'],))
            return None
        if now - s['last_seen'] > 60:                            # touched at most once a minute
            c.execute('UPDATE auth_sessions SET last_seen = ?, expires = MAX(expires, ?) WHERE id = ?',
                      (now, now + (REMEMBER_S if s['remember'] else SESSION_IDLE_S), s['id']))
    return s


def _token() -> Optional[Dict[str, Any]]:
    h = request.headers.get('Authorization') or ''
    tok = h[7:].strip() if h.lower().startswith('bearer ') else (request.headers.get('X-API-Key') or '').strip()
    if not tok.startswith(TOKEN_PREFIX):
        return None
    with _db() as c:
        rows = _rows(c.execute('SELECT * FROM api_tokens WHERE token_hash = ? AND revoked = 0', (_sha(tok),)))
        if not rows:
            return None
        t = rows[0]
        if not t['last_used'] or t['last_used'][:16] != _now()[:16]:
            c.execute('UPDATE api_tokens SET last_used = ? WHERE id = ?', (_now(), t['id']))
    return t


def create_token(name: str) -> Dict[str, Any]:
    tok = TOKEN_PREFIX + secrets.token_urlsafe(30)
    with _db() as c:
        tid = c.execute('INSERT INTO api_tokens (name, token_hash, prefix, created) VALUES (?, ?, ?, ?)',
                        (name[:60], _sha(tok), tok[:12], _now())).lastrowid
    _event('token_created', True, name)
    return {'id': tid, 'name': name, 'token': tok, 'prefix': tok[:12]}


def _safe_next(n) -> str:
    n = str(n or '/')
    return n if n.startswith('/') and not n.startswith('//') and not n.startswith('/api/') else '/'


# ---------------------------------------------------------------- the gate (app.before_request)
def _same_origin() -> bool:
    """A cookie-authenticated write must come from one of the app's own pages (CSRF)."""
    src = request.headers.get('Origin') or ''
    if not src:
        ref = request.headers.get('Referer') or ''
        src = '{0.scheme}://{0.netloc}'.format(urlparse(ref)) if ref else ''
    own = {o.lower() for o in ORIGINS} | {request.host_url.rstrip('/').lower()}
    return src.rstrip('/').lower() in own


def gate():
    """Before every request: public paths pass; otherwise a valid session cookie or API token, or a 401
    (API) / a redirect to the sign-in page (pages)."""
    if not ENABLED:
        return None
    p = request.path
    if p == '/' and not owner():
        return redirect('/login')
    if any(p == x or (x.endswith('/') and p.startswith(x)) for x in PUBLIC_PATHS):
        return None
    tok = _token()
    if tok:
        g.auth = {'via': 'token', 'token': tok['id']}
        return None
    s = _session()
    if s:
        # A WebSocket handshake is a GET, but it opens a two-way channel: another site's page must not
        # open one with the cookie (cross-site WebSocket hijacking), so it is checked like a write.
        upgrade = (request.headers.get('Upgrade') or '').lower() == 'websocket'
        if (upgrade or request.method not in ('GET', 'HEAD', 'OPTIONS')) and not _same_origin():
            return jsonify({'error': 'cross-site request refused'}), 403
        g.auth = {'via': 'session', 'session': s}
        return None
    socket = (request.headers.get('Upgrade') or '').lower() == 'websocket'      # no redirect for a WebSocket
    if p.startswith('/api/') or socket or 'application/json' in (request.headers.get('Accept') or ''):
        return jsonify({'error': 'sign in first', 'login': '/login'}), 401
    return redirect('/login?next=' + quote(request.full_path.rstrip('?')))


def still_valid(info: Optional[Dict[str, Any]]) -> bool:
    """For a long-lived connection (the page's WebSocket): is the sign-in it opened with still good?
    A session in use stays alive, as each request would keep it."""
    if not ENABLED:
        return True
    if not info:
        return False
    with _db() as c:
        if info.get('via') == 'token':
            return bool(c.execute('SELECT 1 FROM api_tokens WHERE id = ? AND revoked = 0', (info.get('token'),)).fetchone())
        sid = (info.get('session') or {}).get('id')
        rows = _rows(c.execute('SELECT * FROM auth_sessions WHERE id = ? AND revoked = 0', (sid,)))
        if not rows:
            return False
        s, now = rows[0], time.time()
        if now > s['expires']:
            return False
        c.execute('UPDATE auth_sessions SET last_seen = ?, expires = MAX(expires, ?) WHERE id = ?',
                  (now, now + (REMEMBER_S if s['remember'] else SESSION_IDLE_S), s['id']))
    return True


def _need_session(fresh: bool = False):
    """For the account routes: a signed-in browser (not a token); with `fresh`, signed in within FRESH_S
    or the current password in the body."""
    if not ENABLED:
        return None
    s = _session()
    if not s:
        return jsonify({'error': 'sign in first'}), 401
    if request.method == 'POST' and not _same_origin():
        return jsonify({'error': 'cross-site request refused'}), 403
    g.auth = {'via': 'session', 'session': s}
    if fresh:
        recent = time.time() - datetime.fromisoformat(s['created']).timestamp() < FRESH_S
        pw = (request.get_json(silent=True) or {}).get('password')
        u = owner()
        if not recent and not (pw and u and check_password_hash(u['password_hash'], pw)):
            return jsonify({'error': 'enter your password to confirm', 'need_password': True}), 403
    return None


def _check_password_rules(pw: str, username: str = '') -> Optional[str]:
    if len(pw or '') < MIN_PASSWORD:
        return f'use at least {MIN_PASSWORD} characters'
    if pw.lower() in COMMON or (username and username.lower() in pw.lower()):
        return 'that password is too easy to guess'
    return None


# ---------------------------------------------------------------- WebAuthn (passkeys)
def _wa():
    import webauthn
    from webauthn.helpers import structs
    return webauthn, structs


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def passkeys() -> List[Dict[str, Any]]:
    with _db() as c:
        return _rows(c.execute('SELECT id, credential_id, name, created, last_used, transports, backed_up FROM auth_passkeys ORDER BY id'))


def _origin_ok() -> bool:
    """Passkeys work only where the page's host is the RP ID (pandorasbox.local), never on a bare IP."""
    here = request.host_url.rstrip('/').lower()
    return here in {o.lower() for o in ORIGINS} and urlparse(here).hostname == RP_ID


# ---------------------------------------------------------------- routes: state, setup, sign-in
@bp.route('/api/auth/state')
def api_state():
    u = owner()
    s = _session() if u else None
    return jsonify({'setup': not u, 'signed_in': bool(s), 'username': u['username'] if (u and s) else None,
                    'passkeys': len(passkeys()) if u else 0, 'passkey_origin': _origin_ok(), 'rp_id': RP_ID,
                    'origins': ORIGINS, 'locked_for': _locked_for()})


@bp.route('/api/auth/setup/begin', methods=['POST'])
def api_setup_begin():
    if owner():
        return jsonify({'error': 'the owner account already exists'}), 409
    bad = _refuse()
    if bad:
        return bad
    b = request.get_json(silent=True) or {}
    code = re.sub(r'[^A-Z0-9]', '', str(b.get('setup_code') or '').upper())
    want = re.sub(r'[^A-Z0-9]', '', (setup_code() or '').upper())
    if not want or not hmac.compare_digest(code, want):
        _fail('setup_code', 'wrong setup code', account=False)
        return jsonify({'error': 'that setup code is not right'}), 400
    username = str(b.get('username') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._@-]{3,40}', username):
        return jsonify({'error': 'a username is 3-40 letters, digits or . _ @ -'}), 400
    why = _check_password_rules(str(b.get('password') or ''), username)
    if why:
        return jsonify({'error': f'Password: {why}.'}), 400
    secret = new_totp_secret()
    pid = _pend_new('setup', username=username, pw_hash=generate_password_hash(str(b['password'])), secret=secret)
    uri = otpauth_uri(secret, username)
    return _with_pre(make_response(jsonify({'secret': secret, 'uri': uri, 'qr': qr_svg(uri)})), pid)


@bp.route('/api/auth/setup/finish', methods=['POST'])
def api_setup_finish():
    if owner():
        return jsonify({'error': 'the owner account already exists'}), 409
    p = _pend_get('setup')
    if not p:
        return jsonify({'error': 'setup took too long: start again'}), 400
    code = (request.get_json(silent=True) or {}).get('code')
    step = totp_check(p['secret'], code)
    if step is None:
        _fail('setup_totp', 'wrong code', account=False)
        return jsonify({'error': 'that code does not match: check the phone\'s clock, then try the next code'}), 400
    with _db() as c:
        c.execute('INSERT INTO auth_user (id, username, password_hash, totp_secret, totp_last_step, user_handle, created, updated) '
                  'VALUES (1, ?, ?, ?, ?, ?, ?, ?)', (p['username'], p['pw_hash'], p['secret'], step, _b64u(secrets.token_bytes(32)), _now(), _now()))
    store.set_kv('secret:auth_setup_code', '')
    codes = _new_recovery()
    _event('account_created', True, p['username'])
    resp = _start_session('password + authenticator', remember=True)
    data = json.loads(resp.get_data(as_text=True))
    resp.set_data(json.dumps({**data, 'recovery_codes': codes}))
    return resp


@bp.route('/api/auth/login', methods=['POST'])
def api_login():
    bad = _refuse()
    if bad:
        return bad
    u = owner()
    if not u:
        return jsonify({'error': 'no account yet: finish the setup first', 'setup': True}), 409
    b = request.get_json(silent=True) or {}
    name_ok = hmac.compare_digest(str(b.get('username') or '').strip().lower(), u['username'].lower())
    pw_ok = check_password_hash(u['password_hash'], str(b.get('password') or ''))      # always run: equal timing
    if not (name_ok and pw_ok):
        _fail('password', 'wrong username or password')
        return jsonify({'error': 'wrong username or password'}), 401
    pid = _pend_new('mfa', remember=bool(b.get('remember')))
    return _with_pre(make_response(jsonify({'mfa': True, 'methods': ['totp', 'recovery'] + (['passkey'] if passkeys() else [])})), pid)


@bp.route('/api/auth/mfa', methods=['POST'])
def api_mfa():
    bad = _refuse()
    if bad:
        return bad
    p = _pend_get('mfa')
    if not p:
        return jsonify({'error': 'that took too long: sign in again', 'restart': True}), 400
    b = request.get_json(silent=True) or {}
    u = owner()
    if b.get('recovery_code'):
        if not _use_recovery(b['recovery_code']):
            _fail('recovery', 'wrong or used recovery code')
            return jsonify({'error': 'that recovery code is not right, or was used already'}), 401
        left = recovery_left()
        _event('recovery_used', True, f'{left} left')
        if left <= 3:
            store.add_notice(f'auth-recovery:{left}', 'system', 'warn', f'{left} recovery code{"s" if left != 1 else ""} left',
                             'Make new ones in Account & security.', link={'tab': 'security'})
        return _start_session('password + recovery code', p['remember'])
    step = totp_check(u['totp_secret'], b.get('code'), int(u['totp_last_step'] or 0))
    if step is None:
        _fail('totp', 'wrong authenticator code')
        return jsonify({'error': 'that code is not right (or was already used): wait for the next one'}), 401
    with _db() as c:
        c.execute('UPDATE auth_user SET totp_last_step = ? WHERE id = 1', (step,))
    return _start_session('password + authenticator', p['remember'])


@bp.route('/api/auth/passkey/login/options', methods=['POST'])
def api_passkey_login_options():
    bad = _refuse()
    if bad:
        return bad
    if not owner() or not passkeys():
        return jsonify({'error': 'no passkey yet: sign in with your password, then add one in Account & security'}), 409
    webauthn, s = _wa()
    opts = webauthn.generate_authentication_options(
        rp_id=RP_ID, user_verification=s.UserVerificationRequirement.REQUIRED,
        allow_credentials=[s.PublicKeyCredentialDescriptor(id=_unb64u(pk['credential_id'])) for pk in passkeys()])
    remember = bool((request.get_json(silent=True) or {}).get('remember'))
    pid = _pend_new('passkey_login', challenge=_b64u(opts.challenge), remember=remember)
    return _with_pre(make_response(webauthn.options_to_json(opts), 200, {'Content-Type': 'application/json'}), pid)


@bp.route('/api/auth/passkey/login/verify', methods=['POST'])
def api_passkey_login_verify():
    bad = _refuse()
    if bad:
        return bad
    p = _pend_get('passkey_login')
    if not p:
        return jsonify({'error': 'that took too long: try again'}), 400
    cred = (request.get_json(silent=True) or {}).get('credential') or {}
    with _db() as c:
        rows = _rows(c.execute('SELECT * FROM auth_passkeys WHERE credential_id = ?', (str(cred.get('id') or ''),)))
    if not rows:
        _fail('passkey', 'unknown passkey', account=False)
        return jsonify({'error': 'this passkey is not one of yours (or was removed)'}), 401
    pk = rows[0]
    webauthn, _ = _wa()
    try:
        v = webauthn.verify_authentication_response(
            credential=cred, expected_challenge=_unb64u(p['challenge']), expected_rp_id=RP_ID, expected_origin=ORIGINS,
            credential_public_key=_unb64u(pk['public_key']), credential_current_sign_count=int(pk['sign_count'] or 0),
            require_user_verification=True)
    except Exception as e:
        _fail('passkey', f'not verified: {str(e)[:80]}', account=False)
        return jsonify({'error': 'the passkey could not be verified'}), 401
    with _db() as c:
        c.execute('UPDATE auth_passkeys SET sign_count = ?, last_used = ? WHERE id = ?', (v.new_sign_count, _now(), pk['id']))
    return _start_session(f'passkey ({pk["name"]})', p['remember'])


@bp.route('/api/auth/logout', methods=['POST'])
def api_logout():
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        with _db() as c:
            c.execute('UPDATE auth_sessions SET revoked = 1 WHERE token_hash = ?', (_sha(tok),))
        _event('signed_out', True)
    resp = make_response(jsonify({'ok': True}))
    resp.delete_cookie(SESSION_COOKIE, path='/')
    return resp


# ---------------------------------------------------------------- routes: Account & security
@bp.route('/api/auth/account')
def api_account():
    bad = _need_session()
    if bad:
        return bad
    u = owner()
    cur = g.auth['session']['id'] if ENABLED else None
    with _db() as c:
        sessions = _rows(c.execute('SELECT id, created, last_seen, expires, remember, method, ip, user_agent FROM auth_sessions '
                                   'WHERE revoked = 0 AND expires > ? ORDER BY last_seen DESC', (time.time(),)))
        tokens = _rows(c.execute('SELECT id, name, prefix, created, last_used FROM api_tokens WHERE revoked = 0 ORDER BY id DESC'))
        events = _rows(c.execute('SELECT ts, kind, ok, ip, user_agent, detail FROM auth_events ORDER BY id DESC LIMIT 40'))
    for s in sessions:
        s['device'] = _device(s['user_agent'])
        s['current'] = s['id'] == cur
        s['last_seen'] = datetime.fromtimestamp(s['last_seen']).isoformat(timespec='seconds')
    for e in events:
        e['device'] = _device(e['user_agent'])
    return jsonify({'username': u['username'], 'created': u['created'], 'recovery_left': recovery_left(),
                    'passkeys': passkeys(), 'sessions': sessions, 'tokens': tokens, 'events': events,
                    'rp_id': RP_ID, 'passkey_origin': _origin_ok(), 'origins': ORIGINS})


@bp.route('/api/auth/password', methods=['POST'])
def api_password():
    b = request.get_json(silent=True) or {}
    bad = _need_session()
    if bad:
        return bad
    u = owner()
    if not check_password_hash(u['password_hash'], str(b.get('current') or '')):
        _fail('password_change', 'wrong current password')
        return jsonify({'error': 'the current password is not right'}), 401
    why = _check_password_rules(str(b.get('new') or ''), u['username'])
    if why:
        return jsonify({'error': f'New password: {why}.'}), 400
    with _db() as c:
        c.execute('UPDATE auth_user SET password_hash = ?, updated = ? WHERE id = 1', (generate_password_hash(str(b['new'])), _now()))
        c.execute('UPDATE auth_sessions SET revoked = 1 WHERE id != ?', (g.auth['session']['id'],))   # every other device signs in again
    _event('password_changed', True)
    return jsonify({'ok': True})


@bp.route('/api/auth/totp/begin', methods=['POST'])
def api_totp_begin():
    bad = _need_session(fresh=True)
    if bad:
        return bad
    secret = new_totp_secret()
    pid = _pend_new('totp_reset', secret=secret)
    uri = otpauth_uri(secret, owner()['username'])
    return _with_pre(make_response(jsonify({'secret': secret, 'uri': uri, 'qr': qr_svg(uri)})), pid)


@bp.route('/api/auth/totp/finish', methods=['POST'])
def api_totp_finish():
    bad = _need_session()
    if bad:
        return bad
    p = _pend_get('totp_reset')
    if not p:
        return jsonify({'error': 'that took too long: start again'}), 400
    step = totp_check(p['secret'], (request.get_json(silent=True) or {}).get('code'))
    if step is None:
        return jsonify({'error': 'that code does not match the new key'}), 400
    with _db() as c:
        c.execute('UPDATE auth_user SET totp_secret = ?, totp_last_step = ?, updated = ? WHERE id = 1', (p['secret'], step, _now()))
    _pend_drop()
    _event('authenticator_changed', True)
    return jsonify({'ok': True})


@bp.route('/api/auth/recovery/new', methods=['POST'])
def api_recovery_new():
    bad = _need_session(fresh=True)
    if bad:
        return bad
    codes = _new_recovery()
    _event('recovery_renewed', True)
    return jsonify({'recovery_codes': codes})


@bp.route('/api/auth/passkey/register/options', methods=['POST'])
def api_passkey_register_options():
    bad = _need_session(fresh=True)
    if bad:
        return bad
    if not _origin_ok():
        return jsonify({'error': f'passkeys work on {ORIGINS[0]} only: open the app there to add one'}), 409
    u = owner()
    webauthn, s = _wa()
    opts = webauthn.generate_registration_options(
        rp_id=RP_ID, rp_name=RP_NAME, user_id=_unb64u(u['user_handle']), user_name=u['username'], user_display_name=u['username'],
        exclude_credentials=[s.PublicKeyCredentialDescriptor(id=_unb64u(pk['credential_id'])) for pk in passkeys()],
        authenticator_selection=s.AuthenticatorSelectionCriteria(resident_key=s.ResidentKeyRequirement.REQUIRED,
                                                                 user_verification=s.UserVerificationRequirement.REQUIRED))
    pid = _pend_new('passkey_register', challenge=_b64u(opts.challenge))
    return _with_pre(make_response(webauthn.options_to_json(opts), 200, {'Content-Type': 'application/json'}), pid)


@bp.route('/api/auth/passkey/register/verify', methods=['POST'])
def api_passkey_register_verify():
    bad = _need_session()
    if bad:
        return bad
    p = _pend_get('passkey_register')
    if not p:
        return jsonify({'error': 'that took too long: try again'}), 400
    b = request.get_json(silent=True) or {}
    webauthn, _ = _wa()
    try:
        v = webauthn.verify_registration_response(credential=b.get('credential') or {}, expected_challenge=_unb64u(p['challenge']),
                                                  expected_rp_id=RP_ID, expected_origin=ORIGINS, require_user_verification=True)
    except Exception as e:
        return jsonify({'error': f'the passkey could not be added: {str(e)[:120]}'}), 400
    name = str(b.get('name') or '').strip()[:40] or _device(request.headers.get('User-Agent') or '')
    transports = (b.get('credential') or {}).get('response', {}).get('transports') or []
    with _db() as c:
        c.execute('INSERT INTO auth_passkeys (credential_id, public_key, sign_count, transports, name, aaguid, backed_up, created) '
                  'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (_b64u(v.credential_id), _b64u(v.credential_public_key), v.sign_count,
                                                     json.dumps(transports), name, str(v.aaguid), int(bool(v.credential_backed_up)), _now()))
    _pend_drop()
    _event('passkey_added', True, name)
    return jsonify({'ok': True, 'passkeys': passkeys()})


@bp.route('/api/auth/passkey/<int:pid>/delete', methods=['POST'])
def api_passkey_delete(pid):
    bad = _need_session(fresh=True)
    if bad:
        return bad
    with _db() as c:
        n = c.execute('DELETE FROM auth_passkeys WHERE id = ?', (pid,)).rowcount
    if not n:
        return jsonify({'error': 'no such passkey'}), 404
    _event('passkey_removed', True, str(pid))
    return jsonify({'ok': True, 'passkeys': passkeys()})


@bp.route('/api/auth/sessions/<int:sid>/revoke', methods=['POST'])
def api_session_revoke(sid):
    bad = _need_session()
    if bad:
        return bad
    with _db() as c:
        c.execute('UPDATE auth_sessions SET revoked = 1 WHERE id = ?', (sid,))
    _event('session_revoked', True, str(sid))
    return jsonify({'ok': True})


@bp.route('/api/auth/sessions/revoke-others', methods=['POST'])
def api_session_revoke_others():
    bad = _need_session()
    if bad:
        return bad
    with _db() as c:
        n = c.execute('UPDATE auth_sessions SET revoked = 1 WHERE revoked = 0 AND id != ?', (g.auth['session']['id'],)).rowcount
    _event('sessions_revoked', True, f'{n} other device(s)')
    return jsonify({'ok': True, 'revoked': n})


@bp.route('/api/auth/tokens', methods=['POST'])
def api_token_create():
    bad = _need_session(fresh=True)
    if bad:
        return bad
    name = str((request.get_json(silent=True) or {}).get('name') or '').strip()
    if not 1 <= len(name) <= 60:
        return jsonify({'error': 'give the token a name, up to 60 characters'}), 400
    return jsonify(create_token(name))


@bp.route('/api/auth/tokens/<int:tid>/revoke', methods=['POST'])
def api_token_revoke(tid):
    bad = _need_session()
    if bad:
        return bad
    with _db() as c:
        c.execute('UPDATE api_tokens SET revoked = 1 WHERE id = ?', (tid,))
    _event('token_revoked', True, str(tid))
    return jsonify({'ok': True})


# ---------------------------------------------------------------- the command line (inside the container)
USAGE = '''Usage: python -m niftywhale.auth <command>
  setup-code          the one-time code first-time setup asks for
  reset-password      set a new password (asks for it)
  reset-mfa           remove the authenticator, recovery codes and passkeys; the next sign-in sets them up again
  token <name>        create an API token and print it
  sign-out-all        end every session on every device
  status              the account, its second factors, sessions and tokens'''


def _cli(argv: List[str]) -> int:
    logging.basicConfig(level='WARNING')
    cmd = argv[0] if argv else ''
    u = owner()
    if cmd == 'setup-code':
        print(setup_code() or 'The owner account exists already: no setup code.')
    elif cmd == 'reset-password':
        if not u:
            print('No account yet: open the app and use the setup code.'); return 1
        import getpass
        pw = getpass.getpass('New password: ')
        why = _check_password_rules(pw, u['username'])
        if why:
            print('Password not changed:', why); return 1
        with _db() as c:
            c.execute('UPDATE auth_user SET password_hash = ?, failed = 0, locked_until = 0, updated = ? WHERE id = 1', (generate_password_hash(pw), _now()))
            c.execute('UPDATE auth_sessions SET revoked = 1')
        _event('password_reset', True, 'command line'); print('Password changed; every device signs in again.')
    elif cmd == 'reset-mfa':
        if not u:
            print('No account yet.'); return 1
        with _db() as c:
            c.execute('DELETE FROM auth_user'); c.execute('DELETE FROM auth_recovery'); c.execute('DELETE FROM auth_passkeys')
            c.execute('UPDATE auth_sessions SET revoked = 1')
        store.set_kv('secret:auth_setup_code', '')
        _event('mfa_reset', True, 'command line')
        print('The account was removed (data, settings and API tokens are kept). Open the app and set it up again with this code:')
        print(setup_code())
    elif cmd == 'token' and len(argv) > 1:
        t = create_token(' '.join(argv[1:])); print(t['token'])
    elif cmd == 'sign-out-all':
        with _db() as c:
            n = c.execute('UPDATE auth_sessions SET revoked = 1 WHERE revoked = 0').rowcount
        print(f'{n} session(s) ended.')
    elif cmd == 'status':
        with _db() as c:
            ns = c.execute('SELECT COUNT(*) FROM auth_sessions WHERE revoked = 0 AND expires > ?', (time.time(),)).fetchone()[0]
            nt = c.execute('SELECT COUNT(*) FROM api_tokens WHERE revoked = 0').fetchone()[0]
        print(f"Owner: {u['username'] if u else '(none: setup code ' + str(setup_code()) + ')'}; passkeys {len(passkeys())}; "
              f"recovery codes left {recovery_left()}; sessions {ns}; API tokens {nt}; addresses {', '.join(ORIGINS)}; "
              f"sign-in {'required' if ENABLED else 'OFF (NIFTYWHALE_AUTH=0)'}")
    else:
        print(USAGE); return 2
    return 0


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
