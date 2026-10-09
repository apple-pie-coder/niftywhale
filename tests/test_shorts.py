"""
Short setups: the long protocol on the mirrored chart. Each test flips a
known long chart upside down and checks the short is its exact mirror image.

Run:  python -m unittest discover -s tests
"""
import unittest

from niftywhale import intraday, patterns, smc
from niftywhale.intraday import IntradayRules

from tests.test_intraday import AFTER, m5, session_frame
from tests.test_smc import BULLISH, CHOCH, daily_frame

K = 266.0           # mirrors the last close (133) onto itself, so ATR% is unchanged


def flip(frame, k=K):
    out = frame.copy()
    out['Open'], out['Close'] = k - frame['Open'], k - frame['Close']
    out['High'], out['Low'] = k - frame['Low'], k - frame['High']
    return out


def flip_bars(bars, k=K):
    return [(k - o, k - l, k - h, k - c) for o, h, l, c in bars]


class DailyShort(unittest.TestCase):
    def setUp(self):
        self.up = daily_frame(BULLISH)
        self.down = flip(self.up)
        self.long = smc.evaluate(self.up)
        self.short = smc.evaluate(self.down, side='short')

    def test_bearish_chart_is_a_short_setup(self):
        s = self.short
        self.assertTrue(s['passed'], s.get('reason'))
        self.assertEqual(s['side'], 'short')
        self.assertEqual(s['structure']['bias'], 'bearish')
        # Premium zone above price, stop above the zone, target below.
        self.assertGreater(s['zone']['high'], s['zone']['low'])
        self.assertLess(s['close'], s['zone']['high'] + 1e-9)
        self.assertGreater(s['plan']['stop'], s['zone']['high'])
        self.assertLess(s['plan']['target'], s['close'])

    def test_short_is_the_exact_mirror_of_the_long(self):
        lg, s = self.long, self.short
        self.assertAlmostEqual(s['zone']['low'], K - lg['zone']['high'])
        self.assertAlmostEqual(s['zone']['high'], K - lg['zone']['low'])
        self.assertAlmostEqual(s['plan']['target'], K - lg['plan']['target'])
        self.assertAlmostEqual(s['plan']['rr'], lg['plan']['rr'])
        self.assertAlmostEqual(s['position'], lg['position'])
        self.assertAlmostEqual(s['leg']['equilibrium'], K - lg['leg']['equilibrium'])
        self.assertEqual(len(s['fvgs']), len(lg['fvgs']))
        for a, b in zip(s['fvgs'], lg['fvgs']):
            self.assertAlmostEqual(a['top'], K - b['bottom'])
            self.assertLess(a['bottom'], a['top'])

    def test_short_side_of_a_bullish_chart_fails_structure(self):
        r = smc.evaluate(self.up, side='short')
        self.assertEqual(r['failed_at'], 'structure')
        self.assertIn('long setup', r['reason'])

    def test_evaluate_both_picks_the_side(self):
        self.assertEqual(smc.evaluate_both(self.up)['side'], 'long')
        self.assertEqual(smc.evaluate_both(self.down)['side'], 'short')
        off = smc.evaluate_both(self.down, shorts=False)
        self.assertEqual(off['failed_at'], 'structure')
        blocked = smc.evaluate_both(self.down, short_block='not an F&O stock')
        self.assertEqual(blocked['failed_at'], 'structure')
        self.assertIn('F&O', blocked['reason'])


class TriggerShort(unittest.TestCase):
    def test_sweep_of_a_high_then_choch_below(self):
        frame = flip(m5(CHOCH))
        t = smc.choch_trigger(frame, K - 125, K - 122, K - 151, side='short', bar_minutes=5)
        self.assertTrue(t['triggered'], t.get('reason'))
        self.assertEqual(t['side'], 'short')
        self.assertAlmostEqual(t['entry'], K - 126.5)
        self.assertAlmostEqual(t['sweep_low'], K - 122.8)          # the sweep's high
        self.assertGreater(t['stop'], t['sweep_low'])               # stop above that high
        self.assertAlmostEqual(t['stop'], (K - 122.8) * 1.001)      # 0.1% of the real price
        self.assertGreater(t['rr'], 3)

    def test_intraday_short_invalidated_above_the_origin(self):
        frame = flip(m5(CHOCH))
        t = intraday.trigger(frame, K - 125, K - 122, K - 135, IntradayRules(), AFTER,
                             invalid=K - 123.0, side='short')
        self.assertTrue(t['invalid'])
        self.assertIn('above', t['reason'])

    def test_short_outcomes(self):
        r = IntradayRules()
        t = intraday.trigger(flip(m5(CHOCH)), K - 125, K - 122, K - 135, r, AFTER, side='short')
        self.assertTrue(t['valid'], t.get('reason'))
        win = flip(m5(CHOCH + [(126.8, 130, 126.5, 129), (129, 135.5, 128.8, 135)]))
        o = intraday.outcome(win, t, r, AFTER)
        self.assertEqual(o['status'], 'won')
        self.assertGreater(o['r'], 2)
        lose = flip(m5(CHOCH + [(126.8, 127, 121, 121.5)]))
        o = intraday.outcome(lose, t, r, AFTER)
        self.assertEqual(o['status'], 'lost')
        self.assertAlmostEqual(o['r'], -1.0)


class IntradayShort(unittest.TestCase):
    def test_15m_short_targets_the_nearest_pool_below(self):
        f = flip(session_frame(BULLISH))
        r = intraday.evaluate(f, IntradayRules(), AFTER, shorts=True)
        self.assertTrue(r['passed'], r.get('reason'))
        self.assertEqual(r['side'], 'short')
        self.assertIn(r['target_kind'], intraday.POOLS['short'])
        self.assertLess(r['plan']['target'], r['plan']['entry'])
        self.assertGreaterEqual(r['plan']['target'], r['leg']['low'] - 1e-9)

    def test_shorts_off_means_long_only(self):
        f = flip(session_frame(BULLISH))
        r = intraday.evaluate(f, IntradayRules(), AFTER)
        self.assertEqual(r['failed_at'], 'structure')


class PatternsShort(unittest.TestCase):
    def test_bearish_twins(self):
        up = daily_frame(BULLISH)
        long_hits = patterns.detect(up)
        short_hits = patterns.detect(flip(up), side='short')
        self.assertEqual(len(long_hits), len(short_hits))
        twin = {k: v[0] for k, v in patterns.BEARISH.items()}
        for a, b in zip(long_hits, short_hits):
            self.assertEqual(b['key'], twin[a['key']])
            self.assertAlmostEqual(b['close'], K - a['close'])
            self.assertIn(b['key'], patterns.PATTERNS)


class IntradayTuner(unittest.TestCase):
    def test_overrides_clamped_and_unknown_ignored(self):
        r = IntradayRules().with_overrides({'min_rr': 99, 'swing_len': 2.6, 'square_off': '11:00', 'bogus': 1})
        self.assertEqual(r.min_rr, intraday.TUNABLE['min_rr']['max'])
        self.assertEqual(r.swing_len, 3)
        self.assertEqual(r.square_off, '15:20')

    def test_looser_discount_admits_a_shallow_pullback(self):
        f = session_frame(BULLISH[:-1] + [(6, 143)])
        self.assertEqual(intraday.evaluate(f, IntradayRules(), AFTER)['failed_at'], 'discount')
        loose = IntradayRules().with_overrides({'max_discount': 0.8})
        self.assertNotEqual(intraday.evaluate(f, loose, AFTER)['failed_at'], 'discount')


if __name__ == '__main__':
    unittest.main()
