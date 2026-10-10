"""
NSE's session clock (niftywhale/session.py): the closing auction for F&O stocks from
3 Aug 2026, the F&O segment's hours, and its times as App settings (config.py) over .env.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import config, data, session, store  # noqa: E402

IST = data.IST
CLOCK = ('intraday_square_off', 'nse_cts_end_fo', 'nse_closes_final', 'nse_fo_close', 'nse_cas_start')
ENV = ('INTRADAY_SQUARE_OFF', 'NSE_CTS_END_FO', 'NSE_CLOSES_FINAL', 'NSE_FO_CLOSE', 'NSE_CAS_START')


class Clock(unittest.TestCase):
    def setUp(self):
        store.init()
        config.save({k: '' for k in CLOCK})
        self.env = mock.patch.dict(os.environ, {k: '' for k in ENV})
        self.env.start()
        config._cache.clear()

    def tearDown(self):
        self.env.stop()
        config.save({k: '' for k in CLOCK})

    def test_continuous_trading_ends(self):
        self.assertEqual(session.cts_end(True, date(2026, 10, 9)).strftime('%H:%M'), '15:15')
        self.assertEqual(session.cts_end(False, date(2026, 10, 9)).strftime('%H:%M'), '15:30')
        self.assertEqual(session.cts_end(True, date(2026, 7, 31)).strftime('%H:%M'), '15:30')     # before the auction
        self.assertEqual(session.index_end(date(2026, 10, 9)).strftime('%H:%M'), '15:15')

    def test_the_fo_segment_trades_past_the_cash_close(self):
        self.assertTrue(session.fo_open(datetime(2026, 10, 9, 15, 35, tzinfo=IST)))
        self.assertFalse(session.fo_open(datetime(2026, 10, 9, 15, 40, tzinfo=IST)))
        self.assertFalse(session.fo_open(datetime(2026, 7, 31, 15, 35, tzinfo=IST)))
        self.assertFalse(session.fo_open(datetime(2026, 10, 10, 11, 0, tzinfo=IST)))             # Saturday
        self.assertFalse(data.market_open(datetime(2026, 10, 9, 15, 35, tzinfo=IST)))            # the cash market: 15:30

    def test_closes_settle_with_the_auction(self):
        self.assertEqual(data.settle_s(), 15 * 60)

    def test_app_settings_beat_env_and_apply_at_once(self):
        os.environ['NSE_CTS_END_FO'] = '15:20'
        config._cache.clear()
        self.assertEqual((session.cts_end_fo().strftime('%H:%M'), config.source('nse_cts_end_fo')), ('15:20', 'env'))
        # SEBI's Option A, saved on the dashboard: continuous trading to 15:30, F&O to 15:45.
        config.save({'nse_cts_end_fo': '15:30', 'nse_fo_close': '15:45', 'nse_closes_final': '15:55'})
        self.assertEqual(session.cts_end(True, date(2026, 10, 9)).strftime('%H:%M'), '15:30')
        self.assertTrue(session.fo_open(datetime(2026, 10, 9, 15, 44, tzinfo=IST)))
        self.assertEqual(data.settle_s(), 25 * 60)
        config.save({'nse_cts_end_fo': ''})                                                       # cleared: .env again
        self.assertEqual(session.cts_end_fo().strftime('%H:%M'), '15:20')

    def test_a_typo_in_env_keeps_the_default(self):
        os.environ.update({'NSE_CTS_END_FO': 'soon', 'NSE_CAS_START': 'x', 'INTRADAY_SQUARE_OFF': '16:30'})
        config._cache.clear()
        self.assertEqual(session.cts_end_fo().strftime('%H:%M'), '15:15')
        self.assertEqual(session.cas_start(), date(2026, 8, 3))
        self.assertEqual((config.get('intraday_square_off'), config.source('intraday_square_off')), ('15:10', 'default'))

    def test_validation(self):
        self.assertIsNone(config.validate('nse_cts_end_fo', '15:30'))
        self.assertIsNone(config.validate('nse_cts_end_fo', ''))                                   # clears
        self.assertIn('between', config.validate('nse_cts_end_fo', '16:00'))
        self.assertIn('a time like', config.validate('nse_fo_close', '15:4x'))
        self.assertIn('a date like', config.validate('nse_cas_start', '2026-13-01'))
        self.assertIn('between', config.validate('intraday_square_off', '09:00'))

    def test_intraday_square_off_from_app_settings(self):
        config.save({'intraday_square_off': '15:20'})
        r = app.irules()
        self.assertEqual(r.square_off, '15:20')
        self.assertEqual(r.square_off_at(True, date(2026, 10, 9)).strftime('%H:%M'), '15:15')     # F&O: never after 15:15
        self.assertEqual(app.square_off_for('NOTFO').strftime('%H:%M'), '15:20')


if __name__ == '__main__':
    unittest.main()
