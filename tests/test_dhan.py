"""Dhan helpers that need no network: TOTP, token parsing, plan status, the futures map."""
import base64
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

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


def scrip(instrument, symbol, sid, expiry, exch='NSE'):
    """A row of Dhan's instrument master as csv.DictReader gives it (the columns _fut_row reads)."""
    return {'SEM_INSTRUMENT_NAME': instrument, 'SEM_TRADING_SYMBOL': symbol, 'SEM_SMST_SECURITY_ID': sid,
            'SEM_EXPIRY_DATE': expiry + ' 14:30:00', 'SEM_EXM_EXCH_ID': exch}


class Futures(unittest.TestCase):
    def setUp(self):
        futs = {}
        # The rows of 20 Oct 2026: November's expiry is Monday the 23rd (Tuesday the 24th is a holiday).
        for row in (scrip('FUTIDX', 'NIFTY-Oct2026-FUT', '48704', '2026-10-27'),
                    scrip('FUTIDX', 'NIFTY-Nov2026-FUT', '61471', '2026-11-23'),
                    scrip('FUTIDX', 'BANKNIFTY-Oct2026-FUT', '48699', '2026-10-27'),
                    scrip('FUTSTK', 'BAJAJ-AUTO-Oct2026-FUT', '52001', '2026-10-27'),
                    scrip('FUTIDX', 'SENSEX-Oct2026-FUT', '9', '2026-10-29', exch='BSE')):
            dhan._fut_row(row, futs)
        self.futs = futs
        path = Path(tempfile.mkdtemp()) / 'dhan_futures.json'
        path.write_text(json.dumps(futs))
        for p in (mock.patch.object(dhan, 'FUTS_PATH', path), mock.patch.object(dhan, '_load_ids', return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def test_index_and_stock_futures_mapped(self):
        self.assertEqual(self.futs, {'NIFTY': {'2026-10-27': '48704', '2026-11-23': '61471'},
                                     'BANKNIFTY': {'2026-10-27': '48699'}, 'BAJAJ-AUTO': {'2026-10-27': '52001'}})

    def test_an_index_future_rolls_on_its_expiry_day(self):
        self.assertEqual(dhan.index_future('NIFTY', date(2026, 10, 26)), ('48704', '2026-10-27'))
        self.assertEqual(dhan.index_future('NIFTY', date(2026, 10, 27)), ('61471', '2026-11-23'))
        self.assertEqual(dhan.future_id('NIFTY', date(2026, 10, 26)), ('61471', '2026-11-23'))       # a position: 3 days
        self.assertIsNone(dhan.index_future('BANKNIFTY', date(2026, 10, 27)))                      # November not listed here
