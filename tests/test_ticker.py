"""
The index ticker: which session the quotes belong to, previous closes from daily
candles, day position, GIFT Nifty's gap, and one Dhan call per interval however
many browsers ask.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import time
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
from niftywhale import data, ticker  # noqa: E402

IST = data.IST


class Pure(unittest.TestCase):
    def test_session_date(self):
        tue_eve = datetime(2026, 10, 6, 22, 0, tzinfo=IST)
        self.assertEqual(ticker.session_date(tue_eve), date(2026, 10, 6))
        self.assertEqual(ticker.session_date(datetime(2026, 10, 7, 0, 5, tzinfo=IST)), date(2026, 10, 6))   # just past midnight
        self.assertEqual(ticker.session_date(datetime(2026, 10, 7, 9, 0, tzinfo=IST)), date(2026, 10, 7))
        self.assertEqual(ticker.session_date(datetime(2026, 10, 11, 12, 0, tzinfo=IST)), date(2026, 10, 9))  # Sunday -> Friday
        self.assertEqual(ticker.session_date(datetime(2026, 10, 12, 8, 0, tzinfo=IST)), date(2026, 10, 9))  # Monday pre-open

    def test_prev_close_is_the_last_candle_before_the_session(self):
        daily = [(date(2026, 10, 5), 100.0), (date(2026, 10, 1), 90.0), (date(2026, 10, 6), 110.0)]
        self.assertEqual(ticker.prev_close(daily, date(2026, 10, 6)), 100.0)     # today's own candle is skipped
        self.assertEqual(ticker.prev_close(daily, date(2026, 10, 7)), 110.0)
        self.assertIsNone(ticker.prev_close([], date(2026, 10, 7)))

    def test_day_position(self):
        self.assertEqual(ticker.day_position(105, 100, 110), 50.0)
        self.assertEqual(ticker.day_position(110, 100, 110), 100.0)
        self.assertEqual(ticker.day_position(99, 100, 110), 0.0)                 # a tick outside the stale range
        self.assertIsNone(ticker.day_position(105, 0, 0))                        # GIFT Nifty: no day OHLC
        self.assertIsNone(ticker.day_position(105, 110, 110))

    def test_build(self):
        quotes = {13: {'last': 22776.1, 'open': 22603.25, 'high': 22776.1, 'low': 22561.6},
                  5024: {'last': 22657.0, 'open': 0, 'high': 0, 'low': 0},
                  21: {'last': 13.61, 'open': 14.78, 'high': 14.78, 'low': 13.52},
                  999: {'last': 1.0}}
        rows = ticker.build(quotes, {13: 22555.75, 21: 14.0})
        self.assertEqual([r['label'] for r in rows], ['NIFTY 50', 'INDIA VIX', 'GIFT NIFTY'])    # INDEXES order, unknown ids dropped
        n = rows[0]
        self.assertEqual((n['change'], n['change_pct'], n['position'], n['option']), (220.35, 0.98, 100.0, 'NIFTY'))
        gift = rows[2]
        self.assertEqual((gift['low'], gift['position'], gift['change'], gift['vs_nifty']), (None, None, None, -119.1))


class Endpoint(unittest.TestCase):
    def setUp(self):
        app.TICKER.update(at=0.0, rows=[], error=None, session=None, prev={}, prev_session=None, loading_prev=False,
                          prev_retry_at=None)
        self.calls = 0

        def quotes(ids):
            self.calls += 1
            return {13: {'last': 22776.1 + self.calls, 'open': 22600.0, 'high': 22800.0, 'low': 22500.0}}
        for p in (mock.patch.object(app.dhan, 'available', lambda: True),
                  mock.patch.object(app.dhan, 'index_quotes', quotes),
                  mock.patch.object(app.dhan, 'index_daily_closes', lambda sid: [(date(2026, 10, 5), 22555.75)]),
                  mock.patch.object(app.data, 'now_ist', lambda: datetime(2026, 10, 6, 11, 0, tzinfo=IST)),
                  # The previous-close loader paces its requests; not in tests.
                  mock.patch.object(app, '_load_prev_closes', self._prev_now)):
            p.start()
            self.addCleanup(p.stop)

    def _prev_now(self, session):
        with mock.patch.object(app.time, 'sleep', lambda s: None):
            self._real_load(session)

    _real_load = staticmethod(app._load_prev_closes)

    def test_one_dhan_call_per_interval_and_prev_closes_filled_in(self):
        c = app.app.test_client()
        first = c.get('/api/ticker').get_json()
        self.assertTrue(first['live'])
        for _ in range(5):
            c.get('/api/ticker')
        self.assertEqual(self.calls, 1)
        deadline = time.time() + 5                     # the previous closes load in the background
        while app.TICKER['prev_session'] is None and time.time() < deadline:
            time.sleep(0.05)
        row = c.get('/api/ticker').get_json()['rows'][0]
        self.assertEqual(self.calls, 1)                # filled in without another quote call
        self.assertEqual(row['prev_close'], 22555.75)
        app.TICKER['at'] -= app.TICKER_TTL_LIVE        # the interval has passed
        c.get('/api/ticker')
        self.assertEqual(self.calls, 2)

    def test_a_refused_index_is_retried(self):
        tries = {}

        def closes(sid):
            tries[sid] = tries.get(sid, 0) + 1
            if sid == 19 and tries[sid] < 3:
                raise RuntimeError('429 Client Error')
            return [(date(2026, 10, 5), 100.0)]
        with mock.patch.object(app.dhan, 'index_daily_closes', closes), mock.patch.object(app.time, 'sleep', lambda s: None):
            app._load_prev_closes(date(2026, 10, 6))
        self.assertEqual(app.TICKER['prev'][19], 100.0)
        self.assertIsNone(app.TICKER['prev_retry_at'])

    def test_still_missing_is_tried_again_later(self):
        def closes(sid):
            if sid == 19:
                raise RuntimeError('429')
            return [(date(2026, 10, 5), 100.0)]
        with mock.patch.object(app.dhan, 'index_daily_closes', closes), mock.patch.object(app.time, 'sleep', lambda s: None):
            app._load_prev_closes(date(2026, 10, 6))
        self.assertIsNone(app.TICKER['prev'].get(19))
        self.assertIsNotNone(app.TICKER['prev_retry_at'])

    def test_without_dhan(self):
        with mock.patch.object(app.dhan, 'available', lambda: False):
            d = app.app.test_client().get('/api/ticker').get_json()
        self.assertEqual(d['rows'], [])
        self.assertIn('needs Dhan', d['message'])


if __name__ == '__main__':
    unittest.main()


class Mood(unittest.TestCase):
    """The page's background follows ticker.mood: Nifty's move, breadth, India VIX."""
    def rows(self, nifty, others, vix=None, vix_chg=None):
        r = [{'id': 13, 'group': 'headline', 'change_pct': nifty, 'last': 22500.0}]
        r += [{'id': 100 + i, 'group': 'sector', 'change_pct': c, 'last': 1.0} for i, c in enumerate(others)]
        if vix is not None:
            r.append({'id': 21, 'group': 'volatility', 'change_pct': vix_chg, 'last': vix})
        r.append({'id': 5024, 'group': 'headline', 'change_pct': -5.0, 'last': 1.0})      # GIFT Nifty never counts
        return r

    def test_a_broad_rally_is_euphoric(self):
        m = ticker.mood(self.rows(1.4, [1.0] * 20, vix=11.5, vix_chg=-6.0))
        self.assertEqual(m['label'], 'Euphoric')
        self.assertGreater(m['score'], 0.6)
        self.assertEqual((m['up'], m['down'], m['count']), (21, 0, 21))

    def test_a_sell_off_with_vix_jumping_is_fearful(self):
        m = ticker.mood(self.rows(-1.6, [-1.2] * 20, vix=19.0, vix_chg=12.0))
        self.assertEqual(m['label'], 'Fearful')
        self.assertLess(m['score'], -0.6)
        self.assertGreater(m['fear'], 0.5)

    def test_flat_but_vix_high_is_uneasy(self):
        self.assertEqual(ticker.mood(self.rows(0.05, [0.1, -0.1] * 10, vix=22.0, vix_chg=2.0))['label'], 'Uneasy')
        self.assertEqual(ticker.mood(self.rows(0.05, [0.1, -0.1] * 10, vix=12.0, vix_chg=0.5))['label'], 'Calm')

    def test_mild_moves(self):
        self.assertEqual(ticker.mood(self.rows(0.5, [0.4] * 15 + [-0.2] * 5, vix=13, vix_chg=-1))['label'], 'Upbeat')
        self.assertEqual(ticker.mood(self.rows(-0.5, [-0.4] * 15 + [0.2] * 5, vix=13, vix_chg=3))['label'], 'Nervous')

    def test_no_previous_close_no_mood(self):
        self.assertIsNone(ticker.mood(self.rows(None, [1.0])))
        self.assertIsNone(ticker.mood([]))
