"""
The intraday mode against hand-built 15m and 5m charts.

Run:  python -m unittest discover -s tests
"""
import os
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from niftywhale import intraday
from niftywhale.intraday import IntradayRules

from tests.test_smc import BULLISH, CHOCH, daily_frame

R = IntradayRules()
DAYS = ['2026-09-29', '2026-09-30', '2026-10-01', '2026-10-03', '2026-10-05']


def session_frame(segments, start=80.0, per_day=25):
    """The test_smc daily geometry laid out as 15m candles, `per_day` a session."""
    d = daily_frame(segments, start)
    idx = []
    for day in DAYS:
        idx.extend(pd.date_range(f'{day} 09:15', periods=per_day, freq='15min', tz='Asia/Kolkata'))
    d = d.iloc[-len(idx):] if len(d) > len(idx) else d
    d.index = pd.DatetimeIndex(idx[-len(d):])
    return d


def m5(bars, start='2026-10-05 10:00'):
    idx = pd.date_range(start, periods=len(bars), freq='5min', tz='Asia/Kolkata')
    o, h, l, c = zip(*bars)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': [1e4] * len(bars)}, index=idx)


AFTER = pd.Timestamp('2026-10-05 15:29', tz='Asia/Kolkata')


class Screen(unittest.TestCase):
    def test_bullish_15m_setup_gets_the_nearest_pool_as_target(self):
        f = session_frame(BULLISH)
        r = intraday.evaluate(f, R, AFTER)
        self.assertTrue(r['passed'], r.get('reason'))
        plan = r['plan']
        self.assertIn(r['target_kind'], intraday.POOLS['long'])
        # The nearest pool: never beyond the leg high, never within the cost floor.
        self.assertLessEqual(plan['target'], r['leg']['high'] + 1e-9)
        self.assertGreater(plan['target'], plan['entry'] * (1 + R.min_target_pct / 100))
        self.assertGreaterEqual(plan['rr'], R.pre_min_rr)
        lv = r['levels']
        self.assertEqual(lv['session'], '2026-10-05')
        self.assertIn('pdh', lv)
        self.assertEqual(lv['or_high'], float(f[f.index.date == f.index[-1].date()]['High'].iloc[0]))

    def test_candles_still_forming_are_not_used(self):
        f = session_frame(BULLISH)
        # Ten minutes into the last candle: it has not closed, so it is not the close.
        now = f.index[-1] + pd.Timedelta(minutes=10)
        r = intraday.evaluate(f, R, now)
        self.assertAlmostEqual(r['close'], float(f['Close'].iloc[-2]))

    def test_shallow_pullback_fails_discount(self):
        r = intraday.evaluate(session_frame(BULLISH[:-1] + [(6, 143)]), R, AFTER)
        self.assertEqual(r['failed_at'], 'discount', r.get('reason'))

    def test_no_pool_far_enough_fails_rr(self):
        strict = IntradayRules(min_target_pct=50)
        r = intraday.evaluate(session_frame(BULLISH), strict, AFTER)
        self.assertEqual(r['failed_at'], 'rr')
        self.assertIn('no liquidity pool', r['reason'])


class DailyFilter(unittest.TestCase):
    def test_thin_volume(self):
        self.assertEqual(intraday.daily_filter(daily_frame(BULLISH, volume=300_000), R)['failed_at'], 'liquidity')

    def test_quiet_stock(self):
        quiet = daily_frame([(80, 101)], start=100.0)
        quiet[['High', 'Low']] = np.column_stack([quiet['Close'] + 0.2, quiet['Close'] - 0.2])
        self.assertEqual(intraday.daily_filter(quiet, R)['failed_at'], 'volatility')

    def test_liquid_and_moving(self):
        self.assertIsNone(intraday.daily_filter(daily_frame(BULLISH), R)['failed_at'])


class Trigger(unittest.TestCase):
    def test_5m_sweep_and_choch(self):
        t = intraday.trigger(m5(CHOCH), 122, 125, 135, R, AFTER)
        self.assertTrue(t['triggered'], t.get('reason'))
        self.assertTrue(t['valid'])
        self.assertEqual(t['entry'], 126.5)
        self.assertGreaterEqual(t['rr'], R.min_rr)

    def test_after_the_cutoff_is_rejected(self):
        t = intraday.trigger(m5(CHOCH, start='2026-10-05 13:40'), 122, 125, 135, R, AFTER)
        self.assertTrue(t['triggered'])
        self.assertFalse(t['valid'])
        self.assertTrue(t.get('late'))
        self.assertIn('14:30', t['reason'])

    def test_price_there_before_the_zone_existed_is_not_a_tap(self):
        # The whole CHoCH sequence happened at 10:00-11:00; the zone was set at 12:00.
        quiet = [(127.0, 127.3, 126.8, 127.1)] * 6
        f = m5(CHOCH + quiet)
        t = intraday.trigger(f, 122, 125, 135, R, AFTER, since='2026-10-05T12:00:20')
        self.assertFalse(t['tapped'])
        t = intraday.trigger(f, 122, 125, 135, R, AFTER, since='2026-10-05T09:30:20')
        self.assertTrue(t['triggered'])

    def test_sweep_below_the_leg_origin_is_no_entry(self):
        t = intraday.trigger(m5(CHOCH), 122, 125, 135, R, AFTER, invalid=123.0)   # sweep low 122.8
        self.assertTrue(t['invalid'])
        self.assertFalse(t['valid'])
        self.assertIn('structure failed', t['reason'])
        t = intraday.trigger(m5(CHOCH), 122, 125, 135, R, AFTER, invalid=121.0)
        self.assertTrue(t['valid'])
        # Falling through it AFTER the entry is the trade being stopped, not the setup failing.
        later = m5(CHOCH + [(126.8, 127, 120, 120.5)])
        t = intraday.trigger(later, 122, 125, 135, R, AFTER, invalid=121.0)
        self.assertTrue(t['valid'])

    def test_only_today_counts(self):
        yesterday = m5(CHOCH, start='2026-10-03 10:00')
        today = m5([(127.0, 127.3, 126.8, 127.1)] * 3, start='2026-10-05 09:15')
        t = intraday.trigger(pd.concat([yesterday, today]), 122, 125, 135, R, AFTER)
        self.assertFalse(t['tapped'])


class Outcome(unittest.TestCase):
    def setUp(self):
        self.t = intraday.trigger(m5(CHOCH), 122, 125, 135, R, AFTER)
        self.entry_bars = len(CHOCH)

    def frame(self, after):
        return m5(CHOCH + after)

    def test_target(self):
        o = intraday.outcome(self.frame([(126.8, 130, 126.5, 129), (129, 135.5, 128.8, 135)]), self.t, R, AFTER)
        self.assertEqual(o['status'], 'won')
        self.assertEqual(o['exit'], 135)
        self.assertGreaterEqual(o['r'], R.min_rr)

    def test_stop(self):
        o = intraday.outcome(self.frame([(126.8, 127, 125, 125.2), (125.2, 125.3, 122, 122.5)]), self.t, R, AFTER)
        self.assertEqual(o['status'], 'lost')
        self.assertAlmostEqual(o['r'], -1.0)

    def test_both_in_one_candle_counts_as_stop(self):
        o = intraday.outcome(self.frame([(126.8, 136, 121, 130)]), self.t, R, AFTER)
        self.assertEqual(o['status'], 'lost')

    def test_open_then_squared_off(self):
        drift = [(127, 127.5, 126.6, 127.2)] * 70           # 10:00 + 83 bars passes 15:20
        f = self.frame(drift)
        mid = pd.Timestamp('2026-10-05 12:00', tz='Asia/Kolkata')
        self.assertEqual(intraday.outcome(f, self.t, R, mid)['status'], 'open')
        o = intraday.outcome(f, self.t, R, AFTER)
        self.assertEqual(o['status'], 'closed')
        self.assertEqual(o['exit_time'][11:16], '15:15')     # the candle that closes at 15:20
        self.assertGreater(o['r'], 0)


class Rules(unittest.TestCase):
    def test_env_overrides_and_typos(self):
        env = {'INTRADAY_MIN_RR': '2.5', 'INTRADAY_NO_ENTRY_AFTER': '14:00',
               'INTRADAY_SQUARE_OFF': 'soon', 'INTRADAY_SWING_LEN': '3'}
        with mock.patch.dict(os.environ, env):
            r = IntradayRules.from_env()
        self.assertEqual(r.min_rr, 2.5)
        self.assertEqual(r.no_entry_after, '14:00')
        self.assertEqual(r.square_off, '15:20')
        self.assertEqual(r.swing_len, 3)
        self.assertEqual(r.screen_rules().swing_len, 3)


if __name__ == '__main__':
    unittest.main()
