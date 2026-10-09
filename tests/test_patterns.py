"""Candlestick patterns against hand-built candles."""
import unittest

import pandas as pd

from niftywhale import patterns


def frame(rows):
    idx = pd.date_range('2026-10-05 09:15', periods=len(rows), freq='15min', tz='Asia/Kolkata')
    o, h, l, c = zip(*rows)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c}, index=idx)


# Four falling candles to give the reversal patterns their "after a decline".
DECLINE = [(110, 110.5, 107.5, 108), (108, 108.5, 105.5, 106), (106, 106.5, 103.5, 104), (104, 104.5, 101.5, 102)]


def keys(rows, **kw):
    return [p['key'] for p in patterns.detect(frame(rows), **kw)]


class Patterns(unittest.TestCase):
    def test_bullish_engulfing(self):
        rows = DECLINE + [(102, 102.3, 99.8, 100), (99.8, 103.5, 99.5, 103)]
        self.assertIn('engulfing', keys(rows))

    def test_hammer(self):
        # Body 101.6-102, lower wick down to 98, nothing above.
        rows = DECLINE + [(101.6, 102.1, 98, 102)]
        self.assertIn('hammer', keys(rows))

    def test_hammer_after_a_rally_is_ignored(self):
        rally = [(90, 92.5, 89.5, 92), (92, 94.5, 91.5, 94), (94, 96.5, 93.5, 96), (96, 98.5, 95.5, 98)]
        rows = rally + [(101.6, 102.1, 98, 102)]
        self.assertNotIn('hammer', keys(rows))

    def test_inside_bar_breakout(self):
        rows = DECLINE + [(102, 106, 99, 100), (100.5, 103, 100, 101), (101, 104.5, 100.8, 104)]
        self.assertIn('inside_break', keys(rows))

    def test_morning_star(self):
        rows = DECLINE + [(102, 102.2, 97.8, 98), (98, 98.6, 97.2, 98.3), (98.4, 101.5, 98.2, 101.2)]
        self.assertIn('morning_star', keys(rows))

    def test_plain_downtrend_has_nothing(self):
        rows = DECLINE + [(102, 102.5, 99.5, 100), (100, 100.5, 97.5, 98)]
        self.assertEqual(keys(rows), [])

    def test_zone_flag(self):
        rows = DECLINE + [(101.6, 102.1, 98, 102)]
        at = patterns.detect(frame(rows), zone={'low': 97, 'high': 99})
        away = patterns.detect(frame(rows), zone={'low': 80, 'high': 85})
        self.assertTrue(at[-1]['at_zone'])
        self.assertFalse(away[-1]['at_zone'])

    def test_last_n_limits_the_window(self):
        rows = DECLINE + [(101.6, 102.1, 98, 102), (102, 102.6, 101.8, 102.4), (102.4, 103, 102.2, 102.8)]
        self.assertEqual(keys(rows, last_n=1), [])
        self.assertIn('hammer', keys(rows, last_n=3))

    def test_unfinished_candle_dropped(self):
        f = frame(DECLINE + [(101.6, 102.1, 98, 102)])
        now = f.index[-1] + pd.Timedelta(minutes=7)
        self.assertEqual(len(patterns.completed(f, now)), len(f) - 1)


if __name__ == '__main__':
    unittest.main()
