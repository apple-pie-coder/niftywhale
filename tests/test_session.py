"""
NSE's session clock (niftywhale/session.py): the closing auction for F&O stocks from
3 Aug 2026, the F&O segment's hours, and the times overridable from .env.

Run:  python -m unittest discover -s tests
"""
import importlib
import os
import unittest
from datetime import date, datetime
from unittest import mock

from niftywhale import data, session

IST = data.IST


class Clock(unittest.TestCase):
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
        self.assertEqual(data.SETTLE, 15 * 60)

    def test_env_overrides(self):
        # SEBI's Option A: continuous trading to 15:30, the auction to 15:40, F&O to 15:45.
        env = {'NSE_CTS_END_FO': '15:30', 'NSE_FO_CLOSE': '15:45', 'NSE_CLOSES_FINAL': 'late', 'NSE_CAS_START': 'x'}
        try:
            with mock.patch.dict(os.environ, env):
                s = importlib.reload(session)
                self.assertEqual((s.CTS_END_FO.strftime('%H:%M'), s.FO_CLOSE.strftime('%H:%M')), ('15:30', '15:45'))
                self.assertEqual((s.CLOSES_FINAL.strftime('%H:%M'), s.CAS_START), ('15:45', date(2026, 8, 3)))  # typos: defaults
        finally:
            importlib.reload(session)


if __name__ == '__main__':
    unittest.main()
