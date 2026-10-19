"""
The stock page (/api/stock/<symbol>/profile): the price and the year's numbers read from daily
candles, and the stock's own zones, alerts and events.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import pandas as pd  # noqa: E402

import app  # noqa: E402
from niftywhale import data, store  # noqa: E402

from tests.test_regressions import _reset, _zone  # noqa: E402

# Friday 16 Oct 2026, after the close: the last candle is the session being priced.
AFTER_CLOSE = datetime(2026, 10, 16, 18, 0, tzinfo=data.IST)


def _frame(n=250, last_day='2026-10-16'):
    """`n` weekday candles ending on `last_day`, closing 100, 101, 102 ... with a steady volume."""
    days = pd.bdate_range(end=last_day, periods=n, tz=data.IST)
    close = [100.0 + i for i in range(n)]
    return pd.DataFrame({'Open': [c - 0.5 for c in close], 'High': [c + 1 for c in close], 'Low': [c - 1 for c in close],
                         'Close': close, 'Volume': [1000.0] * (n - 1) + [3000.0]}, index=days)


class Profile(unittest.TestCase):
    def setUp(self):
        _reset()
        self.client = app.app.test_client()

    def get(self, sym, frame):
        with mock.patch.object(app.data, 'daily', return_value={sym + '.NS': frame} if frame is not None else {}), \
                mock.patch.object(app.data, 'live_quotes', return_value={}), \
                mock.patch.object(app.data, 'now_ist', return_value=AFTER_CLOSE), \
                mock.patch.object(app.universe, 'load', return_value={'stocks': [
                    {'symbol': 'AAA', 'name': 'Aaa Ltd', 'industry': 'Metals', 'fo': True, 'nifty100': True, 'indices': ['nifty50']},
                    {'symbol': 'BBB', 'name': 'Bbb Ltd', 'industry': 'Metals', 'fo': False, 'nifty100': False, 'indices': []}]}):
            return self.client.get(f'/api/stock/{sym}/profile')

    def test_price_and_the_years_numbers(self):
        d = self.get('AAA', _frame()).get_json()
        q, st = d['quote'], d['stats']
        self.assertEqual((q['last'], q['prev_close'], q['open']), (349.0, 348.0, 348.5))
        self.assertAlmostEqual(q['change_pct'], 1 / 348 * 100)
        self.assertEqual((st['high_52w'], st['low_52w']), (350.0, 99.0))
        self.assertAlmostEqual(st['returns']['1W'], (349 / 344 - 1) * 100)          # five sessions back
        self.assertAlmostEqual(st['sma']['20']['v'], sum(range(330, 350)) / 20)
        self.assertEqual(st['streak'], 249)                                         # every close higher
        self.assertAlmostEqual(st['volume_ratio'], 3.0)                             # today against the 20 before
        p = st['pivots']                                                            # from the session before
        self.assertEqual(p['from'], '2026-10-15')
        self.assertAlmostEqual(p['P'], (349 + 347 + 348) / 3)
        self.assertEqual((d['industry'], d['fo'], d['indices']), ('Metals', True, ['Nifty 50']))
        self.assertEqual([x['symbol'] for x in d['peers']], ['BBB'])

    def test_a_session_with_no_candle_yet_has_no_move(self):
        with mock.patch.object(app.data, 'market_open', return_value=True):
            d = self.get('AAA', _frame(last_day='2026-10-15')).get_json()
        self.assertEqual((d['quote']['last'], d['quote']['prev_close']), (349.0, 349.0))
        self.assertIsNone(d['quote']['change_pct'])

    def test_its_zones_alerts_and_events(self):
        zid = _zone('AAA', status='won', mode='intraday')
        _zone('BBB')
        with store.connect() as c:
            c.execute("INSERT INTO alerts (zone_id, symbol, created_at, kind, mode) VALUES (?, 'AAA', '2026-10-15T10:00:00', 'entry', 'intraday')", (zid,))
        store.add_events([{'symbol': 'AAA', 'day': '2026-10-30', 'purpose': 'Financial Results'},
                          {'symbol': 'AAA', 'day': '2026-07-17', 'purpose': 'Dividend'}])
        d = self.get('AAA', _frame()).get_json()
        self.assertEqual([(z['id'], z['mode'], z['status']) for z in d['zones']], [(zid, 'intraday', 'won')])
        self.assertEqual([a['zone_id'] for a in d['alerts']], [zid])
        self.assertEqual([e['day'] for e in d['events']['upcoming']], ['2026-10-30'])
        self.assertEqual([e['day'] for e in d['events']['past']], ['2026-07-17'])

    def test_a_live_price_past_the_years_high_dates_it_today(self):
        with mock.patch.object(app.data, 'live_quotes', return_value={'AAA.NS': {'last': 400.0, 'prev_close': 348.0}}):
            with mock.patch.object(app.data, 'daily', return_value={'AAA.NS': _frame()}), \
                    mock.patch.object(app.data, 'now_ist', return_value=AFTER_CLOSE), \
                    mock.patch.object(app.universe, 'load', return_value={'stocks': []}):
                st = self.client.get('/api/stock/AAA/profile').get_json()['stats']
        self.assertEqual((st['high_52w'], st['high_52w_date']), (400.0, '2026-10-16'))

    def test_a_live_close_the_candles_lack_yet_is_its_own_session(self):
        # Friday after the close, the daily frame still ending Thursday: Dhan's price is Friday's, on top of Thursday.
        live = {'AAA.NS': {'last': 360.0, 'prev_close': 349.0, 'high': 365.0, 'low': 348.0}}
        with mock.patch.object(app.data, 'live_quotes', return_value=live), \
                mock.patch.object(app.data, 'daily', return_value={'AAA.NS': _frame(last_day='2026-10-15')}), \
                mock.patch.object(app.data, 'now_ist', return_value=AFTER_CLOSE), \
                mock.patch.object(app.universe, 'load', return_value={'stocks': []}):
            d = self.client.get('/api/stock/AAA/profile').get_json()
        q, st = d['quote'], d['stats']
        self.assertEqual((q['session'], q['today_bar'], q['last']), ('2026-10-16', False, 360.0))
        self.assertAlmostEqual(st['returns']['1W'], (360 / 345 - 1) * 100)       # Thursday's close is history now
        self.assertEqual(st['pivots']['from'], '2026-10-15')                     # Friday's levels: from Thursday
        self.assertEqual((st['high_52w'], st['high_52w_date']), (365.0, '2026-10-16'))   # the day's high, not only its last
        f = _frame(last_day='2026-10-15')
        sept = float(f['Close'][f.index.month == 9].iloc[-1])
        self.assertEqual(st['months'][-1]['m'], '2026-10')                      # October runs to the live price
        self.assertAlmostEqual(st['months'][-1]['r'], (360 / sept - 1) * 100)

    def test_no_futures_read_without_the_oi_change(self):
        rows = [{'day': '2026-10-15', 'oi': 100, 'oi_chg': 5, 'close': 10.0}, {'day': '2026-10-16', 'oi': 110, 'oi_chg': None, 'close': 11.0}]
        self.assertIsNone(app._futures_read(rows)['read'])
        rows[-1]['oi_chg'] = 10
        self.assertEqual(app._futures_read(rows)['read'], 'long build-up')

    def test_iv_rank_stays_on_its_scale_and_orders_are_the_stocks_own(self):
        with store.connect() as c:
            c.execute("DELETE FROM exec_orders WHERE symbol IN ('AAA', 'BBB')")
            c.execute("DELETE FROM oc_daily WHERE symbol = 'AAA'")
            c.execute("DELETE FROM oc_snapshots WHERE symbol = 'AAA'")
        for i in range(25):                                                         # past IVs 20 to 30
            store.save_daily('AAA', f'2026-09-{i + 1:02d}', 340.0, 20 + i * 10 / 24, 1.0, 340.0)
        store.add_snapshot('AAA', '2026-10-29', '2026-10-16T15:20:00', '2026-10-16', 349.0, {'atm_iv': 45.0, 'spot': 349.0}, {})
        store.add_exec_order({'ref': 'sp:a1', 'mode': 'swing', 'symbol': 'AAA', 'status': 'closed'})
        for i in range(510):                                                        # newer orders on another stock
            store.add_exec_order({'ref': f'sp:b{i}', 'mode': 'swing', 'symbol': 'BBB', 'status': 'closed'})
        d = self.get('AAA', _frame()).get_json()
        self.assertEqual(d['options']['iv_rank'], 100.0)
        self.assertEqual([o['ref'] for o in d['orders']], ['sp:a1'])

    def test_bad_symbol_and_no_data(self):
        self.assertEqual(self.get('%3Cx%3E', None).status_code, 400)
        self.assertEqual(self.get('ZZZ', None).status_code, 404)


if __name__ == '__main__':
    unittest.main()
