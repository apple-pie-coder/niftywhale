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
from niftywhale import config, data, demo, session, smart, store, ticker  # noqa: E402

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


# The shape of NSE's api/holiday-master?type=trading (fetched 20 Oct 2026), cut down.
NSE_HOLIDAYS = {
    'CM': [{'tradingDate': '02-Oct-2026', 'weekDay': 'Friday', 'description': 'Mahatma Gandhi Jayanti'},
           {'tradingDate': '20-Oct-2026', 'weekDay': 'Tuesday', 'description': 'Dussehra'},
           {'tradingDate': '08-Nov-2026', 'weekDay': 'Sunday', 'description': 'Diwali Laxmi Pujan*'},
           {'tradingDate': '24-Nov-2026', 'weekDay': 'Tuesday', 'description': 'Prakash Gurpurb Sri Guru Nanak Dev'},
           {'tradingDate': 'soon', 'description': 'a row that does not parse'},
           {'tradingDate': '02-Mar-2024', 'weekDay': 'Saturday', 'description': 'Special Live Trading'}],
    'FO': [{'tradingDate': '20-Oct-2026', 'weekDay': 'Tuesday', 'description': 'Dussehra'}],
    'CD': [{'tradingDate': '21-Oct-2026', 'weekDay': 'Wednesday', 'description': 'not a stock-market holiday'}],
}


class Holidays(unittest.TestCase):
    def setUp(self):
        store.init()
        session.save_holidays(session.parse_holidays(NSE_HOLIDAYS))

    def tearDown(self):
        store.set_json(session.HOLIDAYS_KEY, {})            # other tests' days are sessions
        session._held.update(at=None)

    def test_parsed_from_the_cash_and_fo_lists(self):
        self.assertEqual(session.parse_holidays(NSE_HOLIDAYS), {
            '2026-10-02': 'Mahatma Gandhi Jayanti', '2026-10-20': 'Dussehra', '2026-11-08': 'Diwali Laxmi Pujan*',
            '2026-11-24': 'Prakash Gurpurb Sri Guru Nanak Dev'})
        self.assertEqual(session.parse_holidays({}), {})

    def test_a_holiday_is_no_session(self):
        tue = datetime(2026, 10, 20, 11, 0, tzinfo=IST)
        self.assertFalse(session.trading_day(date(2026, 10, 20)))
        self.assertTrue(session.trading_day(date(2026, 10, 21)))                                    # CD only: stocks trade
        self.assertFalse(data.market_open(tue))
        self.assertFalse(session.fo_open(tue))
        self.assertEqual(data.last_close(tue).date(), date(2026, 10, 19))
        self.assertEqual(session.prev_trading_day(date(2026, 10, 5)), date(2026, 10, 1))           # back over Gandhi Jayanti
        self.assertEqual(app.intraday_phase(tue)['label'], 'Exchange holiday · Dussehra')
        self.assertEqual(app._tradable_why('swing', 'NOTFO', tue), 'the market is closed (Dussehra)')
        self.assertFalse(app.scheduled_scan_due(datetime(2026, 10, 20, 17, 0, tzinfo=IST),
                                                {'auto_scan': '1', 'scan_time': '16:15'}))

    def test_sessions_skip_it(self):
        self.assertEqual(ticker.session_date(datetime(2026, 10, 20, 11, 0, tzinfo=IST)), date(2026, 10, 19))
        self.assertEqual(ticker.session_date(datetime(2026, 10, 21, 8, 0, tzinfo=IST)), date(2026, 10, 19))
        self.assertEqual(app.last_session(datetime(2026, 10, 20, 19, 0, tzinfo=IST)), date(2026, 10, 19))
        self.assertEqual(smart.sessions_back(date(2026, 10, 21), 3),
                         [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 21)])
        self.assertEqual(demo.monthly_expiries(date(2026, 11, 1), date(2026, 11, 30)), [date(2026, 11, 23)])   # 24th: a holiday

    def test_the_page_names_it(self):
        self.assertEqual(app.holiday_view(datetime(2026, 10, 20, 11, 0, tzinfo=IST)),
                         {'day': '2026-10-20', 'name': 'Dussehra', 'next_session': '2026-10-21'})
        self.assertEqual(app.holiday_view(datetime(2026, 10, 2, 11, 0, tzinfo=IST))['next_session'], '2026-10-05')   # over the weekend
        self.assertIsNone(app.holiday_view(datetime(2026, 10, 21, 11, 0, tzinfo=IST)))
        self.assertIsNone(app.holiday_view(datetime(2026, 10, 24, 11, 0, tzinfo=IST)))                   # a Saturday

    def test_a_new_list_replaces_its_year_and_keeps_the_rest(self):
        session.save_holidays({'2025-12-25': 'Christmas'})
        days = session.save_holidays({'2026-10-20': 'Dussehra', '2026-12-25': 'Christmas'})
        self.assertEqual(days, {'2025-12-25': 'Christmas', '2026-10-20': 'Dussehra', '2026-12-25': 'Christmas'})
        self.assertTrue(session.trading_day(date(2026, 10, 2)))                                    # dropped by NSE


if __name__ == '__main__':
    unittest.main()
