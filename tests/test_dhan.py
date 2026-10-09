"""Dhan helpers that need no network: TOTP, token parsing, plan status."""
import base64
import json
import os
import tempfile
import unittest

os.environ.setdefault('DB_PATH', os.path.join(tempfile.mkdtemp(), 'test.db'))

from niftywhale import dhan  # noqa: E402

# Never touch the live database: the store reads DB_PATH once, at its first
# import, so whichever test module imports it first decides. Refuse to run
# unless that is a temporary file.
from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

# RFC 6238 appendix B: SHA-1 secret "12345678901234567890".
RFC_SECRET = base64.b32encode(b'12345678901234567890').decode()


def fake_jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
    return f"{enc({'alg': 'HS512'})}.{enc(claims)}.signature"


class Totp(unittest.TestCase):
    def test_rfc6238_vectors(self):
        for at, code in ((59, '94287082'), (1111111109, '07081804'), (1234567890, '89005924'),
                         (2000000000, '69279037')):
            self.assertEqual(dhan.totp(RFC_SECRET, at=at, digits=8), code)

    def test_six_digits_and_spaces_tolerated(self):
        self.assertEqual(len(dhan.totp(RFC_SECRET.lower(), at=59)), 6)


class Tokens(unittest.TestCase):
    def test_jwt_expiry_and_client(self):
        tok = fake_jwt({'exp': 1790000000, 'dhanClientId': '1100123456'})
        self.assertEqual(int(dhan._jwt_expiry(tok).timestamp()), 1790000000)
        self.assertEqual(dhan._claims(tok)['dhanClientId'], '1100123456')

    def test_garbage_token(self):
        self.assertIsNone(dhan._jwt_expiry('not-a-jwt'))
        self.assertEqual(dhan._claims('a.b'), {})

    def test_time_formats(self):
        for raw in ('2026-10-06T09:15:00', '2026-10-06 09:15:00', '06/10/2026 09:15'):
            t = dhan._parse_time(raw)
            self.assertEqual((t.year, t.month, t.day, t.hour, t.minute), (2026, 10, 6, 9, 15), raw)
        self.assertIsNone(dhan._parse_time('soon'))


class Plan(unittest.TestCase):
    def test_plan_status(self):
        on = [{'dataPlan': 'Active'}, {'dataPlan': 'active'}, {'dataPlan': None}, {}]
        off = [{'dataPlan': 'Deactive'}, {'dataPlan': 'Inactive'}, {'dataPlan': 'Not Subscribed'},
               {'dataPlan': 'Expired'}]
        for p in on:
            self.assertTrue(dhan._data_plan_active(p), p)
        for p in off:
            self.assertFalse(dhan._data_plan_active(p), p)
        self.assertFalse(dhan._data_plan_active(None))


if __name__ == '__main__':
    unittest.main()
