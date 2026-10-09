"""
Swing context gates (smc.context_gate): the entry filters the autopilot learns
-- Nifty's 20-day run the trade's way, the zone's age, an R:R cap. Off by
default; the same function decides in the backtester and the live watcher;
they never change the stored records; a gated CHoCH spends its setup like an
R:R rejection; a zone you set yourself is never gated.

Run:  python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
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
from niftywhale import autopilot, backtest, lab, smc, store  # noqa: E402

from tests import test_learn  # noqa: E402
from tests.test_learn import THR, setup_numbers  # noqa: E402
from tests.test_regressions import FRESH_15M, _reset, _zone  # noqa: E402
from tests.test_smc import CHOCH, m15  # noqa: E402

OFF = {k: smc.TUNABLE[k]['off'] for k in smc.GATES}


def market(ret20: float, first=date(2025, 10, 1), n=120) -> pd.DataFrame:
    """Nifty closes flat at 100, then moving `ret20` % over the last 20 sessions."""
    idx = pd.DatetimeIndex([pd.Timestamp(first + timedelta(days=i)) for i in range(n)])
    closes = [100.0] * (n - 20) + [100.0 * (1 + ret20 / 100 * (k + 1) / 20) for k in range(20)]
    return pd.DataFrame({'nifty': closes, 'vix': [14.0] * n}, index=idx)


class Gate(unittest.TestCase):
    ctx = {'rr': 12.0, 'zone_age_days': 1, 'nifty_ret20': 4.0}

    def test_off_never_blocks(self):
        self.assertIsNone(smc.context_gate(self.ctx, 'long', OFF))
        self.assertIsNone(smc.context_gate(self.ctx, 'long', smc.Rules().as_dict()))
        self.assertIsNone(smc.context_gate(self.ctx, 'long', THR))           # rule sets from before the gates

    def test_each_gate(self):
        self.assertIn('cap', smc.context_gate(self.ctx, 'long', {**OFF, 'max_rr': 10}))
        self.assertIsNone(smc.context_gate(self.ctx, 'long', {**OFF, 'max_rr': 12}))
        self.assertIn('after the zone', smc.context_gate(self.ctx, 'long', {**OFF, 'min_zone_age': 2}))
        self.assertIsNone(smc.context_gate(self.ctx, 'long', {**OFF, 'min_zone_age': 1}))
        self.assertIn('+4.0%', smc.context_gate(self.ctx, 'long', {**OFF, 'max_market_run': 3}))
        self.assertIsNone(smc.context_gate(self.ctx, 'long', {**OFF, 'max_market_run': 4}))

    def test_market_run_is_the_trades_way(self):
        # Nifty up 4 %: extended for a long, not for a short; down 4 %: the other way round.
        self.assertIsNone(smc.context_gate(self.ctx, 'short', {**OFF, 'max_market_run': 0}))
        down = {**self.ctx, 'nifty_ret20': -4.0}
        self.assertIsNone(smc.context_gate(down, 'long', {**OFF, 'max_market_run': 0}))
        self.assertIsNotNone(smc.context_gate(down, 'short', {**OFF, 'max_market_run': 0}))
        self.assertIsNotNone(smc.context_gate({**self.ctx, 'nifty_ret20': 0.5}, 'long', {**OFF, 'max_market_run': -1}))

    def test_missing_context_never_blocks(self):
        strict = {'max_rr': 6, 'min_zone_age': 5, 'max_market_run': -2}
        self.assertIsNone(smc.context_gate({}, 'long', strict))

    def test_tuner_clamps_and_casts(self):
        r = smc.Rules().with_overrides({'max_market_run': 50, 'min_zone_age': 2.7, 'max_rr': 1})
        self.assertEqual((r.max_market_run, r.min_zone_age, r.max_rr), (10, 3, 4))
        self.assertIsInstance(r.min_zone_age, int)


class Records(unittest.TestCase):
    def test_gates_never_make_records_stale(self):
        key = backtest.structural_key('swing', smc.Rules())
        self.assertEqual(backtest.structural_key('swing', smc.Rules(max_rr=8, min_zone_age=3, max_market_run=0)), key)
        self.assertNotEqual(backtest.structural_key('swing', smc.Rules(swing_len=4)), key)

    def test_key_matches_records_built_before_the_gates(self):
        # The same dict the key was hashed from before the gates existed.
        old = {k: v for k, v in smc.Rules().as_dict().items() if k not in backtest.FILTERS and k not in smc.GATES}
        import hashlib
        want = hashlib.sha1(json.dumps([backtest.RECORDS_VERSION, 'swing', old], sort_keys=True,
                                       default=str).encode()).hexdigest()[:12]
        self.assertEqual(backtest.structural_key('swing', smc.Rules()), want)

    def test_thresholds_carry_the_gates_for_swing_only(self):
        self.assertEqual(set(backtest.thresholds('swing', smc.Rules())), set(backtest.FILTERS) | set(smc.GATES))
        self.assertEqual(set(backtest.thresholds('intraday', app.intraday.IntradayRules())), set(backtest.FILTERS))


class GatedLifecycle(test_learn.SwingLifecycle):
    """The backtester's swing lifecycle with the gates on (inherits the lifecycle tests: gates off)."""
    MARKET = None

    def run_sim(self, passes, triggers, outcome=None, thr=THR):
        if self.MARKET is None:
            return super().run_sim(passes, triggers, outcome, thr)
        return self._run_with(passes, triggers, outcome, thr)

    def _run_with(self, passes, triggers, outcome, thr):
        rec = {'days': self.days, 'pass': {self.days[i]: s for i, s in passes.items()}}
        test = self

        def choch(window, zl, zh, tg, rules, sessions, since, side):
            return dict(triggers.get(test._today, {'tapped': False, 'triggered': False}))

        class Tracking:
            def sessions(self, symbol, a, b):
                test._today = test.days.index(b.isoformat())
                return pd.DataFrame({'Close': [1.0]})

            def after(self, symbol, d):
                return pd.DataFrame({'Close': [1.0]})

        out = outcome or {'status': 'won', 'exit': 130.0, 'exit_time': '2026-01-20T11:00:00+05:30', 'r': 3.0}
        with mock.patch.object(backtest.smc, 'choch_trigger', choch), \
                mock.patch.object(backtest.smc, 'follow_trade', lambda t, f, d, now: dict(out)):
            trades = backtest.simulate_swing('X', rec, thr, Tracking(), smc.Rules(), {}, market=self.MARKET)
        return trades, [], []

    def test_rr_cap_spends_the_setup(self):
        passes = {i: setup_numbers() for i in range(10)}
        trigs = {2: self.trig(2, rr=12.0), 4: self.trig(4, rr=4.0)}
        self.assertEqual(len(self.run_sim(passes, trigs, thr={**THR, **OFF})[0]), 1)
        # Capped at 1:10: the 1:12 CHoCH is skipped and the setup spent, so day 4's never comes.
        self.assertEqual(self.run_sim(passes, trigs, thr={**THR, **OFF, 'max_rr': 10})[0], [])

    def test_zone_age(self):
        passes = {i: setup_numbers() for i in range(10)}
        trades, _, _ = self.run_sim(passes, {2: self.trig(2)}, thr={**THR, **OFF, 'min_zone_age': 2})
        self.assertEqual(len(trades), 1)                 # 2 days after the zone: old enough
        trades, _, _ = self.run_sim(passes, {2: self.trig(2)}, thr={**THR, **OFF, 'min_zone_age': 3})
        self.assertEqual(trades, [])


class MarketGate(GatedLifecycle):
    MARKET = market(5.0, first=date(2025, 10, 1), n=96)      # Nifty up 5 % into early January 2026

    def test_market_run(self):
        passes = {i: setup_numbers() for i in range(6)}
        trades, _, _ = self.run_sim(passes, {2: self.trig(2)}, thr={**THR, **OFF})
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0]['f']['nifty_ret20'], 4)
        self.assertEqual(self.run_sim(passes, {2: self.trig(2)}, thr={**THR, **OFF, 'max_market_run': 3})[0], [])
        short = {i: setup_numbers(side='short') for i in range(6)}
        trig = {**self.trig(2), 'side': 'short'}
        self.assertEqual(len(self.run_sim(short, {2: trig}, thr={**THR, **OFF, 'max_market_run': 3})[0]), 1)

    # The inherited lifecycle tests ran in GatedLifecycle already.
    test_zone_set_watched_and_traded = test_rr_below_the_rule_is_rejected_and_not_rearmed = None
    test_filters_decide_which_setups_become_zones = test_one_trade_per_stock_and_new_setup_after = None
    test_late_choch_is_rejected = test_lifetime_and_screen_exit = test_refresh_keeps_since = None
    test_rr_cap_spends_the_setup = test_zone_age = None


class AutopilotLadder(unittest.TestCase):
    def test_specs_offer_the_gates_for_swing(self):
        sp = lab.specs('swing')
        self.assertTrue(set(smc.GATES) <= set(sp))
        self.assertFalse(set(smc.GATES) & set(lab.specs('intraday')))

    def test_ladder_from_off(self):
        pol = autopilot.policy({})
        sp = smc.TUNABLE['max_market_run']
        self.assertEqual(autopilot.grid('max_market_run', 10, sp, pol, 'swing'), [10, 6, 4, 3, 2])
        # A first change moves two rungs at most, however far the tuning went.
        out = autopilot.clamp_steps({'max_market_run': 0, 'max_rr': 6}, {'max_market_run': 10, 'max_rr': 30},
                                    {k: smc.TUNABLE[k] for k in ('max_market_run', 'max_rr')}, 2)
        self.assertEqual(out, {'max_market_run': 4, 'max_rr': 12})

    def test_your_limits_narrow_the_ladder(self):
        pol = autopilot.policy({'limits': {'swing': {'max_market_run': [2, 10]}}})
        self.assertEqual(autopilot.grid('max_market_run', 4, smc.TUNABLE['max_market_run'], pol, 'swing'), [10, 6, 4, 3, 2])
        self.assertNotIn(1, autopilot.grid('max_market_run', 2, smc.TUNABLE['max_market_run'], pol, 'swing'))

    def test_change_text_says_off(self):
        self.assertEqual(lab.describe({'max_rr': 12.0, 'min_rr': 2.0}, {'max_rr': 30.0, 'min_rr': 2.0}), 'max_rr off → 12')


class LiveWatcher(unittest.TestCase):
    """The CHOCH fixture: in at 126.5, stop 122.68; to a 151 target that is R:R 1:6.4."""

    def setUp(self):
        _reset()

    def check(self):
        with mock.patch.object(app.data, 'intraday', return_value={'ABC.NS': m15(CHOCH)}), \
                mock.patch.object(app.data, 'now_ist', return_value=FRESH_15M), \
                mock.patch.object(app.notify, 'send', return_value=True) as send:
            return app.check_zones('manual'), send

    def test_gate_off_alerts(self):
        zid = _zone(target=151.0)
        summary, send = self.check()
        self.assertEqual(summary['triggered'], 1)
        self.assertEqual(store.get_zone(zid)['status'], 'triggered')
        self.assertTrue(json.loads(store.get_zone(zid)['ctx']))

    def test_gated_choch_is_rejected_with_its_reason(self):
        store.set_settings({'telegram': '1', 'rules': {'max_rr': 6}})
        zid = _zone(target=151.0)
        summary, send = self.check()
        self.assertEqual((summary['triggered'], summary['rejected']), (0, 1))
        z = store.get_zone(zid)
        self.assertEqual(z['status'], 'rejected')
        self.assertIn('skipped', z['note'])
        self.assertIn('1:6', z['note'])
        self.assertTrue(z['trigger']['gated'] if isinstance(z['trigger'], dict) else json.loads(z['trigger'])['gated'])
        self.assertEqual([a['kind'] for a in store.alerts()], ['rejected'])
        send.assert_not_called()
        self.assertEqual(store.live_trades('swing'), [])           # never a paper trade

    def test_your_own_zone_is_never_gated(self):
        store.set_settings({'rules': {'max_rr': 6}})
        zid = _zone(target=151.0, source='manual')
        summary, _ = self.check()
        self.assertEqual(summary['triggered'], 1)
        self.assertEqual(store.get_zone(zid)['status'], 'triggered')


if __name__ == '__main__':
    unittest.main()
