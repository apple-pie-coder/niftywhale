"""
An honest backtest: costs and slippage taken off every simulated trade in R (niftywhale/costs.py,
applied by the lab's Bench), and a final holdout the tuning never sees (autopilot.walk_forward).

Run:  python -m unittest discover -s tests
"""
import random
import unittest
from datetime import date, timedelta

from niftywhale import autopilot, costs, demo


def trade(entry, stop, exit_, status, mode='intraday', side='long', r=None, symbol='ABC'):
    risk = abs(entry - stop)
    r = r if r is not None else round((exit_ - entry) * (1 if side == 'long' else -1) / risk, 3)
    return {'mode': mode, 'symbol': symbol, 'side': side, 'entry': entry, 'stop': stop, 'exit': exit_,
            'status': status, 'r': r, 'entry_time': '2026-01-05T10:00:00+05:30'}


class Costs(unittest.TestCase):
    def test_a_tight_intraday_stop_gives_away_a_large_part_of_r(self):
        # A 0.3 % stop: Dhan's MIS charges on a ₹10 lakh account's position plus 5 bps slippage a side.
        lost = costs.cost_r(trade(100.0, 99.7, 99.7, 'lost'), 'intraday')
        won = costs.cost_r(trade(100.0, 99.7, 100.6, 'won'), 'intraday')
        self.assertGreater(lost, 0.3)                      # two slippages and the charges: about a third of R
        self.assertLess(won, lost)                         # a target is a limit order: no slippage on the way out
        self.assertAlmostEqual(lost - won, (99.7 * 5 / 1e4 - (100.6 - 99.7) * 0) / 0.3, delta=0.05)

    def test_charges_follow_the_product(self):
        self.assertEqual([costs.product(m, s) for m, s in (('swing', 'long'), ('swing', 'short'), ('intraday', 'long'), ('options', 'long'))],
                         ['CNC', 'FUT', 'MIS', 'OPT'])
        # Delivery pays STT both ways and a DP charge: a wide swing stop still costs a few hundredths of R.
        c = costs.cost_r(trade(500.0, 470.0, 560.0, 'won', mode='swing'), 'swing', slippage_bps=0)
        sized = demo.size({'mode': 'swing', 'side': 'long', 'entry': 500.0, 'stop': 470.0}, costs.NOMINAL_CAPITAL,
                          costs.NOMINAL_CAPITAL, demo.settings_from({}))
        expect = demo.charges('CNC', 500.0 * sized['qty'], 560.0 * sized['qty'])['total'] / sized['qty'] / 30.0
        self.assertAlmostEqual(c, expect, places=6)

    def test_apply_keeps_gross_and_leaves_open_trades(self):
        ts = [trade(100.0, 99.0, 102.0, 'won'), {**trade(100.0, 99.0, 100.0, 'open'), 'r': None}]
        a, b = costs.apply(ts, 'intraday')
        self.assertEqual(a['r_gross'], 2.0)
        self.assertAlmostEqual(a['r'], round(2.0 - a['cost_r'], 3))
        self.assertGreater(a['cost_r'], 0)
        self.assertNotIn('r_gross', b)

    def test_options_use_the_premium_stop(self):
        t = {'mode': 'options', 'symbol': 'NIFTY', 'side': 'long', 'entry': 120.0, 'exit': 60.0, 'status': 'lost', 'r': -1.0,
             'entry_time': '2026-01-05T10:00:00+05:30'}
        [x] = costs.apply([t], 'options', stop_pct=50, lots={'NIFTY': 65})
        self.assertGreater(x['cost_r'], 0)
        self.assertLess(x['r'], -1.0)
        [y] = costs.apply([t], 'options', stop_pct=None)            # no stop to measure R against: kept as is
        self.assertEqual((y['r'], y['cost_r']), (-1.0, 0.0))


def market(holdout_turns: bool, seed: int = 1):
    """Two years of trades with an R:R; trades at 3.5 and over win, those under lose. With
    `holdout_turns`, from October 2025 on (the holdout) the winners lose: what tuning finds is luck."""
    rnd = random.Random(seed)
    pool = []
    d = date(2024, 1, 1)
    while d < date(2026, 1, 1):
        for _ in range(3):
            rr = rnd.choice([1.5, 2.5, 3.0, 3.5, 4.5, 5.5])
            good = rr >= 3.5
            if holdout_turns and d >= date(2025, 10, 1):
                good = not good
            mu = 0.6 if good else -0.5
            pool.append({'symbol': 'X', 'side': 'long', 'entry_time': f'{d}T10:00:00+05:30', 'exit_time': f'{d}T11:00:00+05:30',
                         'status': 'won', 'r': round(rnd.gauss(mu, 1.0), 3), 'f': {'rr': rr}})
        d += timedelta(days=2)
    return lambda thr: [t for t in pool if t['f']['rr'] >= thr['min_rr']]


SPEC = {'min_rr': {'step': 0.5, 'min': 1, 'max': 6}}


class Holdout(unittest.TestCase):
    def run_wf(self, evaluate, hold):
        pol = autopilot.policy({'holdout_months': hold})
        return autopilot.walk_forward(evaluate, {'min_rr': 1.5}, SPEC, pol, 'swing', date(2024, 1, 1), date(2025, 12, 31))

    def test_an_edge_that_holds_passes_its_holdout(self):
        wf = self.run_wf(market(holdout_turns=False), 3)
        self.assertEqual(wf['proposal'], {'min_rr': 2.5})
        self.assertEqual((wf['holdout']['from'], wf['holdout']['ok'], wf['holdout']['judged']), ('2025-10-01', True, True))
        self.assertIn('holdout', {c['key'] for c in wf['checks']})
        # The folds and the final tuning stop before the holdout.
        self.assertLess(max(f['test'][1] for f in wf['folds']), '2025-10-02')

    def test_luck_is_caught_by_the_holdout(self):
        wf = self.run_wf(market(holdout_turns=True), 3)
        self.assertIsNone(wf['proposal'])
        self.assertTrue(any('never saw' in r for r in wf['reasons']), wf['reasons'])
        self.assertFalse(next(c for c in wf['checks'] if c['key'] == 'holdout')['ok'])
        # Without the holdout the same history is proposed (the tuning cannot tell).
        self.assertIsNotNone(self.run_wf(market(holdout_turns=True), 0)['proposal'])

    def test_policy_range(self):
        self.assertEqual(autopilot.policy({'holdout_months': 99})['holdout_months'], 12)
        self.assertEqual(autopilot.policy({})['holdout_months'], 3)


if __name__ == '__main__':
    unittest.main()
