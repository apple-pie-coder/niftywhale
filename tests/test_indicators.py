"""
Indicators against small hand-worked examples.

Run:  python -m unittest discover -s tests
"""
import unittest

import numpy as np
import pandas as pd

from niftywhale import indicators as ind


def bars(rows, start='2026-10-05 09:15', freq='5min'):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz='Asia/Kolkata')
    o, h, l, c, v = zip(*rows)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': v}, index=idx)


class Delta(unittest.TestCase):
    def test_close_location_splits_volume(self):
        f = bars([(10, 12, 10, 12, 100),      # closes at the high: all buying
                  (12, 12, 10, 10, 100),      # at the low: all selling
                  (10, 12, 10, 11, 100),      # mid-range: balanced
                  (11, 12, 10, 11.5, 100)])   # 3/4 up the range: 75 buy - 25 sell
        self.assertEqual(list(ind.clv_delta(f)), [100, -100, 0, 50])

    def test_no_range_uses_the_tick_rule(self):
        f = bars([(10, 10, 10, 10, 50), (11, 11, 11, 11, 80), (11, 11, 11, 11, 30), (9, 9, 9, 9, 20)])
        self.assertEqual(list(ind.clv_delta(f)), [0, 80, 0, -20])

    def test_minute_deltas_sum_into_their_bar(self):
        fine = bars([(10, 11, 10, 11, 10)] * 5 + [(11, 11, 10, 10, 4)] * 5, freq='1min')
        five = pd.date_range('2026-10-05 09:15', periods=2, freq='5min', tz='Asia/Kolkata')
        self.assertEqual(list(ind.bucket_delta(fine, five, 5)), [50, -20])

    def test_cumulative_restarts_each_session(self):
        d = pd.Series([1.0, 2.0, 3.0, 4.0], index=pd.DatetimeIndex(
            ['2026-10-01 15:00', '2026-10-01 15:15', '2026-10-05 09:15', '2026-10-05 09:30'], tz='Asia/Kolkata'))
        self.assertEqual(list(ind.cumulative(d)), [1, 3, 3, 7])
        self.assertEqual(list(ind.cumulative(d, by_session=False)), [1, 3, 6, 10])


class Vwap(unittest.TestCase):
    def test_volume_weighted_typical_price_anchored_daily(self):
        f = bars([(10, 12, 8, 10, 100), (10, 16, 14, 15, 300)])       # typical 10 then 15
        self.assertEqual(list(ind.session_vwap(f)), [10, (10 * 100 + 15 * 300) / 400])
        two = pd.concat([f, bars([(20, 21, 19, 20, 10)], start='2026-10-06 09:15')])
        self.assertEqual(ind.session_vwap(two).iloc[-1], 20)          # new session, new anchor


class Bands(unittest.TestCase):
    def test_bollinger_matches_the_definition(self):
        closes = list(range(1, 31))
        f = bars([(c, c + 1, c - 1, c, 1) for c in closes])
        b = ind.bollinger(f)
        last = np.array(closes[-20:], float)
        self.assertAlmostEqual(b['mid'].iloc[-1], last.mean())
        self.assertAlmostEqual(b['upper'].iloc[-1], last.mean() + 2 * last.std())
        self.assertTrue(np.isnan(b['mid'].iloc[18]))                  # needs 20 bars


class OpeningRange(unittest.TestCase):
    def test_range_and_first_breakout(self):
        f = bars([(100, 102, 99, 101, 1), (101, 103, 100, 102, 1), (102, 103, 101, 102, 1),   # 09:15-09:25
                  (102, 103, 101, 102.5, 1), (102, 105, 102, 104, 1), (104, 104, 98, 98, 1)])
        o = ind.opening_ranges(f, 15)['2026-10-05']
        self.assertEqual((o['high'], o['low']), (103, 99))
        self.assertEqual(o['breakout']['dir'], 'up')
        self.assertTrue(o['breakout']['time'].startswith('2026-10-05T09:35'))

    def test_series_line_up_with_bars(self):
        f = bars([(100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(30)])
        out = ind.tail(ind.for_bars(f, 5), 12)
        for k in ('delta', 'cum_delta', 'vwap', 'bb_mid', 'volume'):
            self.assertEqual(len(out[k]), 12, k)
        self.assertEqual(out['delta_source'], 'bar')


if __name__ == '__main__':
    unittest.main()
