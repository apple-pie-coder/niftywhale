"""
The protocol against hand-built charts whose answer is known.

Run:  python -m unittest discover -s tests
"""
import unittest

import numpy as np
import pandas as pd

from niftywhale import smc


def daily_frame(segments, start=80.0, volume=2_000_000):
    """
    Build daily candles from (bars, target_close) segments. Each candle opens
    at the previous close and wicks 1 point beyond its body, so the geometry
    (pivots, gaps, the down candle at a turn) is predictable.
    """
    closes, price = [], start
    for bars, target in segments:
        closes.extend(np.linspace(price, target, bars + 1)[1:])
        price = target
    c = np.array(closes)
    o = np.concatenate([[start], c[:-1]])
    h = np.maximum(o, c) + 1
    l = np.minimum(o, c) - 1
    idx = pd.bdate_range('2025-01-01', periods=len(c))
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c,
                         'Volume': np.full(len(c), float(volume))}, index=idx)


# A clean bullish leg: HH/HL, a pullback (the down candles that become the
# order block), a 29-point rally through the prior high, then a pullback
# into the discount half of that rally.
BULLISH = [(40, 100), (20, 120), (8, 112), (15, 130), (8, 122), (10, 151), (12, 133)]


class DailyScreen(unittest.TestCase):
    def test_bullish_setup_passes_every_step(self):
        r = smc.evaluate(daily_frame(BULLISH))
        self.assertTrue(r['passed'], r.get('reason'))
        self.assertEqual(r['structure']['bias'], 'bullish')
        # Rally origin is the pullback low near 121, leg top near 152.
        self.assertAlmostEqual(r['leg']['low'], 121, delta=1.5)
        self.assertAlmostEqual(r['leg']['high'], 152, delta=1.5)
        self.assertLess(r['position'], 0.5)
        # Order block is a down-close candle at the bottom of the pullback.
        ob = r['order_block']
        self.assertLess(ob['low'], ob['high'])
        self.assertLess(ob['high'], r['leg']['equilibrium'])
        # The rally moved ~2.9/bar against 1-point wicks: gaps must be found.
        self.assertTrue(r['fvgs'])
        self.assertGreaterEqual(r['plan']['rr'], smc.Rules().pre_min_rr)
        self.assertEqual(r['plan']['target'], r['leg']['high'])
        self.assertTrue(0 <= r['score'] <= 100)

    def test_shallow_pullback_fails_discount(self):
        frame = daily_frame(BULLISH[:-1] + [(6, 143)])
        r = smc.evaluate(frame)
        self.assertEqual(r['failed_at'], 'discount', r.get('reason'))

    def test_thin_volume_fails_liquidity(self):
        r = smc.evaluate(daily_frame(BULLISH, volume=400_000))
        self.assertEqual(r['failed_at'], 'liquidity')

    def test_mirrored_chart_is_bearish(self):
        up = daily_frame(BULLISH)
        down = up.copy()
        for k in ('Open', 'Close'):
            down[k] = 300 - up[k]
        down['High'], down['Low'] = 300 - up['Low'], 300 - up['High']
        r = smc.evaluate(down)
        self.assertEqual(r['failed_at'], 'structure')
        self.assertEqual(r['structure']['bias'], 'bearish')

    def test_close_under_order_block_fails_intact(self):
        # Pull back hard enough to close through the order block body.
        frame = daily_frame(BULLISH[:-1] + [(14, 119)])
        r = smc.evaluate(frame)
        self.assertIn(r['failed_at'], ('intact', 'structure'), r.get('reason'))

    def test_too_little_history(self):
        r = smc.evaluate(daily_frame([(20, 100)]))
        self.assertEqual(r['failed_at'], 'history')


class Pivots(unittest.TestCase):
    def test_peak_and_trough(self):
        high = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3], dtype=float)
        low = np.array([5, 4, 3, 2, 3, 4, 0.5, 4, 5], dtype=float)
        hs, ls = smc.swing_points(high, low, 2)
        self.assertEqual(hs, [3])
        self.assertEqual(ls, [3, 6])      # 2 at bar 3 is a trough too; 0.5 at bar 6 the deeper one

    def test_fvg_fill_state(self):
        h = np.array([10, 13, 16, 17, 15], dtype=float)
        l = np.array([9, 12, 14, 15, 11.5], dtype=float)
        gaps = smc.fair_value_gaps(h, l, 0, 4)
        # candle 0 high 10 < candle 2 low 14 -> gap 10..14, later low 11.5 -> partial
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['state'], 'partial')
        self.assertEqual(gaps[0]['top'], 11.5)


def m15(bars, day='2026-10-05'):
    idx = pd.date_range(f'{day} 09:15', periods=len(bars), freq='15min', tz='Asia/Kolkata')
    o, h, l, c = zip(*bars)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c,
                         'Volume': [1e5] * len(bars)}, index=idx)


# Tap into a 122-125 zone, a swing low at 123.5, a minor high at 126.0, a
# sweep to 122.8 under that low, then a close at 126.5 above the minor high.
CHOCH = [
    (128.0, 128.5, 127.5, 127.8), (127.8, 128.0, 126.9, 127.0), (127.0, 127.2, 125.8, 126.0),
    (126.0, 126.2, 124.6, 124.8), (124.8, 125.0, 123.5, 123.8), (123.8, 125.2, 123.7, 125.0),
    (125.0, 126.0, 124.8, 125.8), (125.8, 125.9, 124.9, 125.0), (125.0, 125.1, 124.0, 124.2),
    (124.2, 124.3, 122.8, 123.0), (123.0, 124.5, 122.9, 124.4), (124.4, 126.6, 124.3, 126.5),
    (126.5, 127.0, 126.2, 126.8),
]


class Trigger(unittest.TestCase):
    def test_sweep_then_choch_triggers(self):
        t = smc.choch_trigger(m15(CHOCH), 122, 125, 151)
        self.assertTrue(t['tapped'])
        self.assertTrue(t['triggered'], t.get('reason'))
        self.assertEqual(t['choch_level'], 126.0)
        self.assertEqual(t['sweep_low'], 122.8)
        self.assertEqual(t['swept_level'], 123.5)
        self.assertEqual(t['entry'], 126.5)
        self.assertLess(t['stop'], 122.8)
        self.assertTrue(t['valid'])
        self.assertGreater(t['rr'], 3)

    def test_rr_under_minimum_is_rejected(self):
        t = smc.choch_trigger(m15(CHOCH), 122, 125, 130)      # target too close
        self.assertTrue(t['triggered'])
        self.assertFalse(t['valid'])

    def test_unfinished_candle_is_ignored(self):
        frame = m15(CHOCH)
        # "Now" is midway through the CHoCH candle: it has not closed yet.
        now = frame.index[11] + pd.Timedelta(minutes=7)
        t = smc.choch_trigger(frame, 122, 125, 151, now=now)
        self.assertFalse(t['triggered'])

    def test_no_tap(self):
        t = smc.choch_trigger(m15(CHOCH), 100, 110, 151)
        self.assertFalse(t['tapped'])

    def test_still_falling_has_no_trigger(self):
        falling = [(128 - i, 128.2 - i, 127 - i, 127.1 - i) for i in range(12)]
        t = smc.choch_trigger(m15(falling), 118, 125, 151)
        self.assertTrue(t['tapped'])
        self.assertFalse(t['triggered'])


class RulesOverrides(unittest.TestCase):
    def test_clamped_typed_and_unknown_ignored(self):
        r = smc.Rules().with_overrides({'swing_len': 99, 'min_rr': '2.5', 'max_discount': 'x', 'nope': 1})
        self.assertEqual(r.swing_len, smc.TUNABLE['swing_len']['max'])   # clamped
        self.assertIsInstance(r.swing_len, int)
        self.assertEqual(r.min_rr, 2.5)                                  # cast from text
        self.assertEqual(r.max_discount, smc.Rules().max_discount)       # bad value ignored

    def test_looser_discount_admits_shallow_pullback(self):
        frame = daily_frame(BULLISH[:-1] + [(6, 143)])
        self.assertEqual(smc.evaluate(frame)['failed_at'], 'discount')
        loose = smc.Rules().with_overrides({'max_discount': 0.8})
        self.assertNotEqual(smc.evaluate(frame, loose)['failed_at'], 'discount')


class RulesFromEnv(unittest.TestCase):
    def test_override_and_bad_value(self):
        import os
        os.environ['SMC_MIN_RR'] = '2.5'
        os.environ['SMC_SWING_LEN'] = 'oops'
        try:
            r = smc.Rules.from_env()
            self.assertEqual(r.min_rr, 2.5)
            self.assertEqual(r.swing_len, 3)
        finally:
            del os.environ['SMC_MIN_RR'], os.environ['SMC_SWING_LEN']


if __name__ == '__main__':
    unittest.main()
