"""
Signing in (niftywhale/auth.py): the gate, first-time setup with its one-time code, password + authenticator
code (no replays), recovery codes (once each), the account lock, sessions and their cookies, cross-site
writes refused, API tokens, sign-out, a password change signing other devices out, and passkeys end to end
(a software authenticator registers one and signs in with it).

The other test files run with NIFTYWHALE_AUTH=0; these switch the gate on for themselves.

Run:  python -m unittest discover -s tests
"""
import base64
import json
import os
import tempfile
import time
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import auth, store  # noqa: E402

BASE = 'https://nw.test'
H = {'Origin': BASE}
PW = 'correct horse battery'


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


class Base(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(auth, 'ENABLED', True), mock.patch.object(auth, 'ORIGINS', [BASE]),
                        mock.patch.object(auth, 'RP_ID', 'nw.test')]
        for p in self.patches:
            p.start()
        with auth._db() as c:
            for t in ('auth_user', 'auth_recovery', 'auth_passkeys', 'auth_sessions', 'api_tokens', 'auth_events'):
                c.execute(f'DELETE FROM {t}')
        store.set_kv('secret:auth_setup_code', '')
        auth._pending.clear()
        auth._fails.clear()
        self.c = app.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def post(self, path, body=None, client=None, headers=H):
        return (client or self.c).post(path, json=body or {}, base_url=BASE, headers=headers)

    def get(self, path, client=None, headers=None):
        return (client or self.c).get(path, base_url=BASE, headers=headers or {})

    def setup_owner(self):
        code = auth.setup_code()
        r = self.post('/api/auth/setup/begin', {'setup_code': code, 'username': 'owner', 'password': PW})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.secret = r.get_json()['secret']
        now = int(time.time() // 30)
        r = self.post('/api/auth/setup/finish', {'code': auth.totp_at(self.secret, now)})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['recovery_codes']

    def sign_in(self, client, code_step=None):
        r = self.post('/api/auth/login', {'username': 'owner', 'password': PW, 'remember': True}, client=client)
        self.assertEqual(r.status_code, 200, r.get_json())
        step = code_step if code_step is not None else int(time.time() // 30) + 1
        return self.post('/api/auth/mfa', {'code': auth.totp_at(self.secret, step)}, client=client)


class Gate(Base):
    def test_closed_until_signed_in(self):
        self.assertEqual(self.get('/healthz').status_code, 200)
        self.assertEqual(self.get('/login').status_code, 200)
        self.assertEqual(self.get('/trust').status_code, 200)                                 # certificate help, public
        self.assertEqual(self.get('/static/favicon.svg').status_code, 200)
        r = self.get('/')
        self.assertEqual((r.status_code, r.headers['Location']), (302, '/login'))          # no owner: straight to setup
        r = self.get('/api/glossary')
        self.assertEqual((r.status_code, r.get_json()['login']), (401, '/login'))
        self.setup_owner()
        anon = app.app.test_client()
        r = self.get('/docs', client=anon)
        self.assertEqual((r.status_code, r.headers['Location']), (302, '/login?next=/docs'))
        self.assertEqual(self.get('/api/glossary').status_code, 200)                        # the setup's own session

    def test_setup_needs_the_code_and_happens_once(self):
        r = self.post('/api/auth/setup/begin', {'setup_code': 'WRONG-CODE-XXXX', 'username': 'owner', 'password': PW})
        self.assertEqual(r.status_code, 400)
        r = self.post('/api/auth/setup/begin', {'setup_code': auth.setup_code(), 'username': 'owner', 'password': 'short'})
        self.assertIn('at least 10', r.get_json()['error'])
        codes = self.setup_owner()
        self.assertEqual(len(codes), 10)
        self.assertIsNone(auth.setup_code())
        self.assertEqual(self.post('/api/auth/setup/begin', {'setup_code': 'x', 'username': 'a', 'password': PW}).status_code, 409)
        self.assertNotIn(PW, json.dumps(auth.owner()))                                      # stored hashed


class SignIn(Base):
    def setUp(self):
        super().setUp()
        self.codes = self.setup_owner()

    def test_password_then_code_no_replay(self):
        other = app.app.test_client()
        r = self.post('/api/auth/login', {'username': 'owner', 'password': 'nope nope nope'}, client=other)
        self.assertEqual(r.status_code, 401)
        r = self.post('/api/auth/login', {'username': 'OWNER', 'password': PW}, client=other)
        self.assertEqual((r.status_code, r.get_json()['mfa']), (200, True))
        self.assertEqual(self.get('/api/glossary', client=other).status_code, 401)            # not in yet
        step = int(time.time() // 30) + 1
        self.assertEqual(self.post('/api/auth/mfa', {'code': '000000'}, client=other).status_code, 401)
        r = self.post('/api/auth/mfa', {'code': auth.totp_at(self.secret, step)}, client=other)
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get_all('Set-Cookie')
        self.assertTrue(any('nw_session=' in c and 'HttpOnly' in c and 'Secure' in c and 'SameSite=Lax' in c for c in cookie))
        self.assertEqual(self.get('/api/glossary', client=other).status_code, 200)
        # The same code again, on a fresh sign-in: refused.
        third = app.app.test_client()
        self.post('/api/auth/login', {'username': 'owner', 'password': PW}, client=third)
        self.assertEqual(self.post('/api/auth/mfa', {'code': auth.totp_at(self.secret, step)}, client=third).status_code, 401)

    def test_recovery_code_works_once(self):
        a, b = app.app.test_client(), app.app.test_client()
        for cl in (a, b):
            self.post('/api/auth/login', {'username': 'owner', 'password': PW}, client=cl)
        self.assertEqual(self.post('/api/auth/mfa', {'recovery_code': self.codes[0].upper().replace('-', ' ')}, client=a).status_code, 200)
        self.assertEqual(self.post('/api/auth/mfa', {'recovery_code': self.codes[0]}, client=b).status_code, 401)
        self.assertEqual(auth.recovery_left(), 9)

    def test_account_locks_after_failures(self):
        cl = app.app.test_client()
        for _ in range(auth.LOCK_AFTER):
            self.post('/api/auth/login', {'username': 'owner', 'password': 'wrong password!'}, client=cl)
        r = self.post('/api/auth/login', {'username': 'owner', 'password': PW}, client=cl)
        self.assertEqual(r.status_code, 423)
        self.assertGreater(r.get_json()['retry_after'], 0)

    def test_cross_site_writes_are_refused(self):
        self.assertEqual(self.post('/api/notices/read', {}, headers={}).status_code, 403)
        self.assertEqual(self.post('/api/notices/read', {}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.post('/api/notices/read', {}).status_code, 200)

    def test_api_tokens(self):
        r = self.post('/api/auth/tokens', {'name': 'laptop', 'scope': 'write', 'days': 30})
        tok = r.get_json()['token']
        self.assertTrue(tok.startswith('nwt_'))
        anon = app.app.test_client()
        self.assertEqual(self.get('/api/glossary', client=anon, headers={'Authorization': f'Bearer {tok}'}).status_code, 200)
        self.assertEqual(anon.post('/api/notices/read', json={}, base_url=BASE, headers={'Authorization': f'Bearer {tok}'}).status_code, 200)
        acct = self.get('/api/auth/account').get_json()
        self.assertNotIn(tok, json.dumps(acct))
        self.post(f"/api/auth/tokens/{acct['tokens'][0]['id']}/revoke")
        self.assertEqual(self.get('/api/glossary', client=anon, headers={'Authorization': f'Bearer {tok}'}).status_code, 401)

    def test_sign_out_and_password_change(self):
        phone = app.app.test_client()
        self.assertEqual(self.sign_in(phone).status_code, 200)
        r = self.post('/api/auth/password', {'current': PW, 'new': 'another long passphrase'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.get('/api/glossary', client=phone).status_code, 401)            # signed out elsewhere
        self.assertEqual(self.get('/api/glossary').status_code, 200)                         # not here
        self.post('/api/auth/logout')
        self.assertEqual(self.get('/api/glossary').status_code, 401)

    def test_old_sessions_need_the_password(self):
        with auth._db() as c:
            c.execute("UPDATE auth_sessions SET created = '2020-01-01T00:00:00'")
        r = self.post('/api/auth/recovery/new')
        self.assertEqual((r.status_code, r.get_json().get('need_password')), (403, True))
        r = self.post('/api/auth/recovery/new', {'password': PW})
        self.assertEqual(len(r.get_json()['recovery_codes']), 10)


class Hardening(SignIn):
    """The review's fixes: token scopes and expiry, the account lock not shutting out passkeys, the
    caps on sign-ins in progress, one-use codes under a race, cross-site sign-out, fresh sign-in
    for alert and broker settings, response headers, the body limit and the trusted proxy."""
    def bearer(self, tok):
        return {'Authorization': f'Bearer {tok}'}

    def test_read_tokens_only_read_and_tokens_expire(self):
        tok = self.post('/api/auth/tokens', {'name': 'dashboard'}).get_json()['token']        # read, 90 days by default
        anon = app.app.test_client()
        self.assertEqual(self.get('/api/glossary', client=anon, headers=self.bearer(tok)).status_code, 200)
        self.assertEqual(anon.post('/api/notices/read', json={}, base_url=BASE, headers=self.bearer(tok)).status_code, 403)
        self.assertEqual(self.post('/api/auth/tokens', {'name': 'x', 'scope': 'admin'}).status_code, 400)
        self.assertEqual(self.post('/api/auth/tokens', {'name': 'x', 'days': 7}).status_code, 400)
        with auth._db() as c:
            c.execute('UPDATE api_tokens SET expires = ?', (time.time() - 1,))
        self.assertEqual(self.get('/api/glossary', client=anon, headers=self.bearer(tok)).status_code, 401)
        self.assertEqual(self.get('/api/auth/account').get_json()['tokens'], [])            # an ended one is not listed
        self.assertEqual(auth.create_token('cli')['scope'], 'write')                        # the Pi's command line: as before

    def test_the_account_lock_does_not_shut_out_passkeys(self):
        cl = app.app.test_client()
        for _ in range(auth.LOCK_AFTER):
            self.post('/api/auth/login', {'username': 'owner', 'password': 'wrong password!'}, client=cl)
        self.assertEqual(self.post('/api/auth/login', {'username': 'owner', 'password': PW}, client=cl).status_code, 423)
        # No passkey here, so 409 ("add one first"): the point is that it is not the lock's 423.
        self.assertEqual(self.post('/api/auth/passkey/login/options', client=cl).status_code, 409)

    def test_sign_ins_in_progress_are_capped(self):
        with mock.patch.object(auth, 'PENDING_PER_IP', 2):
            codes = [self.post('/api/auth/login', {'username': 'owner', 'password': PW}, client=app.app.test_client()).status_code
                     for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_a_code_claimed_by_a_racing_request_is_refused(self):
        cl = app.app.test_client()
        self.post('/api/auth/login', {'username': 'owner', 'password': PW}, client=cl)
        last = auth.owner()['totp_last_step']
        with mock.patch.object(auth, 'totp_check', return_value=last):    # read before the other request wrote its step
            self.assertEqual(self.post('/api/auth/mfa', {'code': '123456'}, client=cl).status_code, 401)

    def test_another_site_cannot_sign_you_out(self):
        self.assertEqual(self.post('/api/auth/logout', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.get('/api/glossary').status_code, 200)

    def test_alert_and_broker_settings_need_a_fresh_sign_in(self):
        with auth._db() as c:
            c.execute("UPDATE auth_sessions SET created = '2020-01-01T00:00:00'")
        r = self.post('/api/config', {'telegram_chat_id': '123456789'})
        self.assertEqual((r.status_code, r.get_json().get('need_password')), (403, True))
        self.assertEqual(self.post('/api/config', {'telegram_chat_id': '123456789', 'password': PW}).status_code, 200)
        self.assertEqual(self.post('/api/dhan/token', {'access_token': 'y' * 40}).get_json().get('need_password'), True)
        self.assertEqual(self.post('/api/config', {'log_level': 'INFO'}).status_code, 200)    # not sensitive
        tok = self.post('/api/auth/tokens', {'name': 's', 'scope': 'write', 'password': PW}).get_json()['token']
        anon = app.app.test_client()
        r = anon.post('/api/config', json={'telegram_chat_id': '1'}, base_url=BASE, headers=self.bearer(tok))
        self.assertEqual(r.status_code, 401)                                                 # a browser sign-in only
        self.post('/api/config', {'telegram_chat_id': '', 'password': PW})

    def test_headers_and_body_limit(self):
        r = self.get('/login')
        self.assertIn("frame-ancestors 'none'", r.headers['Content-Security-Policy'])
        self.assertEqual((r.headers['X-Frame-Options'], r.headers['X-Content-Type-Options']), ('DENY', 'nosniff'))
        big = app.app.test_client().post('/api/auth/login', data='x' * (2 * 1024 * 1024), base_url=BASE,
                                          headers={**H, 'Content-Type': 'application/json'})
        self.assertEqual(big.status_code, 413)

    def test_forwarded_headers_only_from_the_proxy(self):
        seen = {}

        def inner(environ, start):
            seen.update(environ)
            return []
        mw = app.TrustedProxy(inner, 'caddy')
        with mock.patch.object(app.socket, 'gethostbyname_ex', return_value=('caddy', [], ['10.0.0.9'])):
            mw({'REMOTE_ADDR': '10.0.0.9', 'HTTP_X_FORWARDED_FOR': '1.2.3.4'}, None)
            self.assertEqual(seen.get('HTTP_X_FORWARDED_FOR'), '1.2.3.4')
            seen.clear()
            mw({'REMOTE_ADDR': '10.0.0.1', 'HTTP_X_FORWARDED_FOR': '1.2.3.4', 'HTTP_X_FORWARDED_PROTO': 'https'}, None)
            self.assertNotIn('HTTP_X_FORWARDED_FOR', seen)
            self.assertNotIn('HTTP_X_FORWARDED_PROTO', seen)


class Passkeys(Base):
    def test_register_then_sign_in_with_it(self):
        import webauthn
        from soft_webauthn import SoftWebauthnDevice
        self.setup_owner()
        real_reg, real_auth = webauthn.verify_registration_response, webauthn.verify_authentication_response
        # The software authenticator has no biometric (it never sets "user verified"); everything else is real.
        no_uv = lambda real: (lambda **kw: real(**{**kw, 'require_user_verification': False}))   # noqa: E731
        dev = SoftWebauthnDevice()
        with mock.patch.object(webauthn, 'verify_registration_response', no_uv(real_reg)), \
             mock.patch.object(webauthn, 'verify_authentication_response', no_uv(real_auth)):
            o = json.loads(self.post('/api/auth/passkey/register/options').get_data(as_text=True))
            pk = {'publicKey': {**o, 'challenge': auth._unb64u(o['challenge']), 'user': {**o['user'], 'id': auth._unb64u(o['user']['id'])},
                                'attestation': 'none'}}
            att = dev.create(pk, BASE)
            cred = {'id': b64u(att['rawId']), 'rawId': b64u(att['rawId']), 'type': 'public-key',
                    'response': {k: b64u(v) for k, v in att['response'].items()}}
            r = self.post('/api/auth/passkey/register/verify', {'credential': cred, 'name': 'test key'})
            self.assertEqual(r.status_code, 200, r.get_json())
            # A new browser, no password: the passkey alone signs in.
            phone = app.app.test_client()
            self.assertTrue(self.get('/api/auth/state', client=phone).get_json()['passkeys'])
            o = json.loads(self.post('/api/auth/passkey/login/options', {'remember': True}, client=phone).get_data(as_text=True))
            asr = dev.get({'publicKey': {**o, 'challenge': auth._unb64u(o['challenge'])}}, BASE)
            cred = {'id': b64u(asr['rawId']), 'rawId': b64u(asr['rawId']), 'type': 'public-key',
                    'response': {k: (b64u(v) if v is not None else None) for k, v in asr['response'].items()}}
            r = self.post('/api/auth/passkey/login/verify', {'credential': cred}, client=phone)
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(self.get('/api/glossary', client=phone).status_code, 200)
            # From another site's page: the browser would put that origin in the signature; refused.
            o = json.loads(self.post('/api/auth/passkey/login/options', client=phone).get_data(as_text=True))
            asr = dev.get({'publicKey': {**o, 'challenge': auth._unb64u(o['challenge'])}}, 'https://evil.example')
            cred = {'id': b64u(asr['rawId']), 'rawId': b64u(asr['rawId']), 'type': 'public-key',
                    'response': {k: (b64u(v) if v is not None else None) for k, v in asr['response'].items()}}
            self.assertEqual(self.post('/api/auth/passkey/login/verify', {'credential': cred}, client=phone).status_code, 401)


class Cli(Base):
    def test_setup_code_and_token(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            auth._cli(['setup-code'])
        self.assertEqual(out.getvalue().strip(), auth.setup_code())
        self.setup_owner()
        out = io.StringIO()
        with redirect_stdout(out):
            auth._cli(['token', 'pi', 'scripts'])
        tok = out.getvalue().strip()
        self.assertEqual(self.get('/api/glossary', client=app.app.test_client(), headers={'Authorization': f'Bearer {tok}'}).status_code, 200)


if __name__ == '__main__':
    unittest.main()
