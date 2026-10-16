"""
Learning: performance statistics, the backtester's zone lifecycle (live rules
replayed, with the trigger and trade functions stubbed so each rule is pinned
on its own), the autopilot's decisions (walk-forward finds a real edge and
refuses noise; shadow, rollback, pause), options candidates, the lab's job
queue and promotions, and the API around them.

Run:  python -m unittest discover -s tests
"""
import json
import os
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
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
from niftywhale import autopilot, backtest, data, intraday, lab, options, perf, smc, store  # noqa: E402

IST = data.IST


def trade(day, r, **f):
    return {'symbol': 'X', 'side': f.pop('side', 'long'), 'entry_time': f'{day}T10:00:00+05:30',
            'exit_time': f'{day}T11:00:00+05:30', 'status': 'won' if r > 0 else 'lost', 'r': r, 'f': f}


class Perf(unittest.TestCase):
    def test_summary(self):
        ts = [trade('2026-01-01', 2), trade('2026-01-02', -1), trade('2026-01-03', -1), trade('2026-01-04', 3),
              {**trade('2026-01-05', 0), 'status': 'open', 'r': None}]
        s = perf.summary(ts)
        self.assertEqual((s['trades'], s['open'], s['wins'], s['total_r'], s['win_rate']), (4, 1, 2, 3.0, 50.0))
        self.assertEqual(s['max_drawdown'], -2.0)           # +2, +1, 0: two below the peak
        self.assertEqual(s['profit_factor'], 2.5)
        self.assertLess(s['lcb'], s['avg_r'])
        self.assertEqual(perf.summary([])['trades'], 0)

    def test_equity_is_cumulative_by_exit(self):
        self.assertEqual([p[1] for p in perf.equity([trade('2026-01-02', -1), trade('2026-01-01', 2)])], [2.0, 1.0])

    def test_breakdown_flags_a_real_difference(self):
        rnd = random.Random(3)
        ts = [trade('2026-01-01', rnd.gauss(0.8, 1), side='long') for _ in range(40)] + \
             [trade('2026-01-01', rnd.gauss(-0.8, 1), side='short') for _ in range(40)]
        side = next(d for d in perf.breakdown(ts) if d['key'] == 'side')
        sig = {r['bucket']: r['signal'] for r in side['rows']}
        self.assertEqual(sig, {'Long': 'good', 'Short': 'bad'})
        self.assertEqual(len(perf.highlights(perf.breakdown(ts))), 2)

    def test_small_buckets_are_never_flagged(self):
        ts = [trade('2026-01-01', 5, side='long') for _ in range(5)] + [trade('2026-01-01', -1, side='short') for _ in range(5)]
        side = next(d for d in perf.breakdown(ts) if d['key'] == 'side')
        self.assertTrue(all(r['signal'] is None and r['small'] for r in side['rows']))


SPEC = {'min_rr': {'step': 0.5, 'min': 1, 'max': 6}, 'max_discount': {'step': 0.05, 'min': 0.2, 'max': 0.8}}


class Autopilot(unittest.TestCase):
    pol = autopilot.policy({})

    def test_policy_merges_and_ignores_junk(self):
        p = autopilot.policy({'mode': 'approve', 'max_steps': 3, 'pause_r': {'swing': 9}, 'bogus': 1, 'cooldown_days': 'x'})
        self.assertEqual((p['mode'], p['max_steps'], p['pause_r']['swing'], p['pause_r']['intraday']), ('approve', 3, 9, 10.0))
        self.assertEqual(p['cooldown_days'], 14)
        self.assertEqual(autopilot.policy({'mode': 'yolo'})['mode'], 'auto')

    def test_grid_and_limits(self):
        self.assertEqual(autopilot.grid('min_rr', 3.0, SPEC['min_rr'], self.pol, 'swing', span=2), [2.0, 2.5, 3.0, 3.5, 4.0])
        pol = autopilot.policy({'limits': {'swing': {'min_rr': [2.5, 3.5]}}})
        self.assertEqual(autopilot.grid('min_rr', 3.0, SPEC['min_rr'], pol, 'swing', span=4), [2.5, 3.0, 3.5])
        self.assertEqual(autopilot.grid('stop_pct', 25, {'values': [15, 20, 25, 30, 40]}, self.pol, 'options', span=1), [20, 25, 30])

    def test_clamp_steps(self):
        out = autopilot.clamp_steps({'min_rr': 5.0, 'max_discount': 0.45}, {'min_rr': 3.0, 'max_discount': 0.5}, SPEC, 2)
        self.assertEqual(out, {'min_rr': 4.0, 'max_discount': 0.45})
        out = autopilot.clamp_steps({'stop_pct': 40}, {'stop_pct': 15}, {'stop_pct': {'values': [15, 20, 25, 30, 40]}}, 2)
        self.assertEqual(out['stop_pct'], 25)

    def _market(self, edge: bool, seed: int = 1):
        """Two years of candidate trades with an R:R; with `edge`, trades under 3.5 lose."""
        rnd = random.Random(seed)
        pool = []
        d = date(2024, 1, 1)
        while d < date(2026, 1, 1):
            for _ in range(3):
                rr = rnd.choice([1.5, 2.5, 3.0, 3.5, 4.5, 5.5])
                mu = (0.6 if rr >= 3.5 else -0.5) if edge else 0.0
                pool.append(trade(d.isoformat(), round(rnd.gauss(mu, 1.0), 3), rr=rr))
            d += timedelta(days=2)
        return lambda thr: [t for t in pool if t['f']['rr'] >= thr['min_rr']]

    def test_walk_forward_finds_a_real_edge(self):
        evaluate = self._market(edge=True)
        wf = autopilot.walk_forward(evaluate, {'min_rr': 1.5}, {'min_rr': SPEC['min_rr']}, self.pol, 'swing',
                                    date(2024, 1, 1), date(2025, 12, 31))
        self.assertEqual(wf['reasons'], [])
        self.assertGreater(wf['gain_r'], 0.3)
        self.assertEqual(wf['proposal'], {'min_rr': 2.5})        # moved at most 2 steps from 1.5

    def test_walk_forward_refuses_noise(self):
        evaluate = self._market(edge=False, seed=7)
        wf = autopilot.walk_forward(evaluate, {'min_rr': 1.5}, {'min_rr': SPEC['min_rr']}, self.pol, 'swing',
                                    date(2024, 1, 1), date(2025, 12, 31))
        self.assertIsNone(wf['proposal'])
        self.assertTrue(wf['reasons'])

    def test_walk_forward_reports_each_fold_and_its_checks(self):
        evaluate = self._market(edge=True)
        seen = []
        wf = autopilot.walk_forward(evaluate, {'min_rr': 1.5}, {'min_rr': SPEC['min_rr']}, self.pol, 'swing',
                                    date(2024, 1, 1), date(2025, 12, 31), on_step=lambda n, total, what: seen.append((n, total, what)))
        total = seen[0][1]
        self.assertEqual([n for n, _, _ in seen], list(range(1, total + 1)))   # every fold, then all history
        self.assertEqual(seen[0][2][:8], 'testing ')
        self.assertEqual(seen[-1][2], 'tuning on all history')
        self.assertEqual(len(wf['folds']), total - 1)
        self.assertTrue(all(c['ok'] for c in wf['checks']))                  # no reasons: every check passed
        refused = autopilot.walk_forward(self._market(edge=False, seed=7), {'min_rr': 1.5}, {'min_rr': SPEC['min_rr']}, self.pol,
                                         'swing', date(2024, 1, 1), date(2025, 12, 31))
        self.assertTrue(any(not c['ok'] for c in refused['checks']))
        self.assertEqual({c['key'] for c in refused['checks']}, {'folds', 'trades', 'gain', 'total', 'drawdown'})

    def test_walk_forward_needs_history(self):
        wf = autopilot.walk_forward(lambda thr: [], {'min_rr': 3.0}, {'min_rr': SPEC['min_rr']}, self.pol, 'swing',
                                    date(2025, 10, 1), date(2025, 12, 31))
        self.assertIsNone(wf['proposal'])
        self.assertIn('not enough history', wf['reasons'][0])

    def test_shadow_decision(self):
        good = [trade('2026-01-01', 1)] * 5
        bad = [trade('2026-01-01', -1)] * 5
        self.assertEqual(autopilot.shadow_decision(good, bad, 5, self.pol, 'swing')[0], 'wait')
        self.assertEqual(autopilot.shadow_decision(good, bad, 20, self.pol, 'swing')[0], 'promote')
        self.assertEqual(autopilot.shadow_decision(bad, good, 3, self.pol, 'swing')[0], 'drop')      # clearly worse: early
        self.assertEqual(autopilot.shadow_decision([], [], 20, self.pol, 'intraday')[0], 'wait')      # too few trades yet
        self.assertEqual(autopilot.shadow_decision([], [], 60, self.pol, 'intraday')[0], 'promote')   # never worse

    def test_shadow_counts_only_the_trades_that_differ(self):
        day = lambda d, r: trade(f'2026-01-{d:02d}', r)
        shared = [day(d, 1) for d in range(1, 21)]
        # 20 trades both take and 3 the current rules add: 3 differ, under swing's 4.
        self.assertEqual(autopilot.differing(shared, shared + [day(d, -1) for d in (22, 23, 24)]), 3)
        verdict, why = autopilot.shadow_decision(shared, shared + [day(d, 2) for d in (22, 23, 24)], 25, self.pol, 'swing')
        self.assertEqual(verdict, 'wait')
        self.assertIn('3 of them differ (need 4)', why)
        # The same entry with another result (an options exit) is one differing trade, not two.
        self.assertEqual(autopilot.differing([day(1, 2)], [day(1, -1)]), 1)
        # Open trades are not evidence yet.
        self.assertEqual(autopilot.differing([{**day(1, 0), 'status': 'open'}], []), 0)

    def test_rollback_and_pause(self):
        lose = [trade('2026-01-01', -1)] * 5
        self.assertIsNotNone(autopilot.rollback_due(lose, lose, [trade('2026-01-01', 1)] * 2, self.pol, 'swing'))
        self.assertIsNone(autopilot.rollback_due(lose, lose, lose, self.pol, 'swing'))     # old rules no better
        self.assertIsNone(autopilot.rollback_due(lose[:2], [], [trade('2026-01-01', 3)], self.pol, 'swing'))
        self.assertIsNotNone(autopilot.pause_due([trade('2026-01-01', -1)] * 12, self.pol, 'swing'))
        self.assertIsNone(autopilot.pause_due([trade('2026-01-01', -1)] * 5, self.pol, 'swing'))        # too few
        self.assertIsNotNone(autopilot.resume_due([trade('2026-01-01', 1)] * 5))
        self.assertIsNone(autopilot.resume_due([trade('2026-01-01', 1)] * 3))


class FakeData:
    def sessions(self, symbol, a, b):
        return pd.DataFrame({'Close': [1.0]})

    def after(self, symbol, d):
        return pd.DataFrame({'Close': [1.0]})


THR = {'min_avg_volume': 0, 'min_atr_pct': 0, 'max_discount': 0.5, 'pre_min_rr': 1.5, 'min_rr': 3.0}


def setup_numbers(**kw):
    s = {'side': 'long', 'zl': 95.0, 'zh': 100.0, 'tg': 130.0, 'stop': 90.0, 'ob': '2026-01-01', 'obe': 95.0,
         'pos': 0.3, 'prr': 2.0, 'atr': 2.0, 'vol': 2e6, 'sc': 60, 'iz': False, 'tk': None}
    s.update(kw)
    return s


class SwingLifecycle(unittest.TestCase):
    days = [(date(2026, 1, 5) + timedelta(days=i)).isoformat() for i in range(20)]

    def run_sim(self, passes, triggers, outcome=None, thr=THR):
        """passes: {day index: setup}; triggers: {day index: trigger dict} (what choch_trigger
        says when the zone is watched that day)."""
        rec = {'days': self.days, 'pass': {self.days[i]: s for i, s in passes.items()}}
        calls = []

        def choch(window, zl, zh, tg, rules, sessions, since, side):
            calls.append((zl, zh, tg, since))
            return dict(triggers.get(self._today, {'tapped': False, 'triggered': False}))

        sim_days = []

        def fake_trigger(*a, **k):
            return choch(*a, **k)
        orig = backtest.simulate_swing

        # Track which day the watcher is on by wrapping the day loop through sessions().
        test = self

        class Tracking(FakeData):
            def sessions(self, symbol, a, b):
                test._today = test.days.index(b.isoformat())
                sim_days.append(test._today)
                return super().sessions(symbol, a, b)

        out = outcome or {'status': 'won', 'exit': 130.0, 'exit_time': '2026-01-20T11:00:00+05:30', 'r': 3.0}
        with mock.patch.object(backtest.smc, 'choch_trigger', fake_trigger), \
             mock.patch.object(backtest.smc, 'follow_trade', lambda t, f, d, now: dict(out)):
            trades = orig('X', rec, thr, Tracking(), smc.Rules(), {})
        return trades, calls, sim_days

    def trig(self, day, rr=4.0, hh='10:00'):
        return {'tapped': True, 'triggered': True, 'choch_time': f'{self.days[day]}T{hh}:00+05:30', 'entry': 100.0,
                'stop': 97.0, 'target': 100.0 + 3 * rr, 'rr': rr, 'side': 'long'}

    def test_zone_set_watched_and_traded(self):
        # The setup keeps passing each evening (a zone that stops passing expires, as live).
        trades, calls, days = self.run_sim({i: setup_numbers() for i in range(4)}, {2: self.trig(2)})
        self.assertEqual(len(trades), 1)
        self.assertEqual(days[0], 1)                   # watched from the next session
        self.assertEqual(calls[0][3], f'{self.days[0]}T16:15:00+05:30')
        self.assertEqual(trades[0]['f']['zone_age_days'], 2)

    def test_rr_below_the_rule_is_rejected_and_not_rearmed(self):
        passes = {i: setup_numbers() for i in range(0, 10)}
        trades, calls, _ = self.run_sim(passes, {2: self.trig(2, rr=2.0), 4: self.trig(4)})
        self.assertEqual(trades, [])                   # 1:2 under 1:3; the same setup is spent afterwards
        trades, _, _ = self.run_sim(passes, {2: self.trig(2, rr=2.0)}, thr={**THR, 'min_rr': 1.5})
        self.assertEqual(len(trades), 1)               # a looser rule takes it

    def test_filters_decide_which_setups_become_zones(self):
        deep = {i: setup_numbers(pos=0.6) for i in range(4)}
        self.assertEqual(self.run_sim(deep, {2: self.trig(2)})[0], [])
        self.assertEqual(len(self.run_sim(deep, {2: self.trig(2)}, thr={**THR, 'max_discount': 0.65})[0]), 1)

    def test_one_trade_per_stock_and_new_setup_after(self):
        passes = {0: setup_numbers(), 1: setup_numbers(),
                  **{i: setup_numbers(ob='2026-01-08', obe=110, zl=110, zh=112) for i in range(3, 8)}}
        out = {'status': 'won', 'exit': 112, 'exit_time': f'{self.days[3]}T15:00:00+05:30', 'r': 3.0}
        trades, calls, _ = self.run_sim(passes, {1: self.trig(1), 6: self.trig(6)}, out)
        # Day 3's new setup came while the first trade was open until day 3's close: skipped
        # that evening, armed on day 4 once the trade had closed.
        self.assertEqual(len(trades), 2)
        self.assertEqual(calls[-1][:2], (110, 112))

    def test_late_choch_is_rejected(self):
        trades, _, _ = self.run_sim({i: setup_numbers() for i in range(4)},
                                    {3: {**self.trig(2), 'choch_time': f'{self.days[2]}T10:00:00+05:30'}})
        self.assertEqual(trades, [])

    def test_lifetime_and_screen_exit(self):
        trades, calls, _ = self.run_sim({0: setup_numbers()}, {})
        self.assertEqual(len(calls), 1)                # gone the next evening: no longer passes
        long_lived = {i: setup_numbers() for i in range(15)}
        _, calls, _ = self.run_sim(long_lived, {})
        self.assertEqual(len(calls), 10)               # watched days 1-10, expired at the lifetime

    def test_refresh_keeps_since(self):
        _, calls, _ = self.run_sim({0: setup_numbers(), 1: setup_numbers(zl=96)}, {})
        self.assertEqual(calls[1][0], 96)
        self.assertEqual(calls[1][3], calls[0][3])     # refreshed levels, same watch start


class IntradayLifecycle(unittest.TestCase):
    D = '2026-01-05'

    def run_sim(self, marks, triggers, thr=None, day_extra=None):
        """marks: {mark index: setup}; triggers: {check time 'HH:MM': trigger}."""
        thr = thr or {**THR, 'min_rr': 2.0, 'pre_min_rr': 1.0}
        rec = {'days': {self.D: {'vol': 2e6, 'atr': 1.5, 'marks': marks, **(day_extra or {})}}}
        calls = []

        def trig(frame, zl, zh, tg, rules, now, since, invalid, side):
            calls.append((now.strftime('%H:%M'), zl, since))
            return dict(triggers.get(now.strftime('%H:%M'), {'tapped': False, 'triggered': False}))
        out = {'status': 'won', 'exit': 1, 'exit_time': f'{self.D}T12:00:00+05:30', 'r': 2.0}
        with mock.patch.object(backtest.intraday, 'trigger', trig), \
             mock.patch.object(backtest.intraday, 'outcome', lambda *a, **k: dict(out)):
            trades = backtest.simulate_intraday('X', rec, thr, FakeData(), intraday.IntradayRules(), {})
        return trades, calls

    def t(self, rr=3.0, **kw):
        return {'tapped': True, 'triggered': True, 'choch_time': f'{self.D}T10:20:00+05:30', 'entry': 100.0,
                'stop': 99.0, 'target': 100 + rr, 'rr': rr, **kw}

    def test_zone_from_scan_then_trigger(self):
        trades, calls = self.run_sim({0: setup_numbers()}, {'09:45': self.t()})
        self.assertEqual(len(trades), 1)
        self.assertEqual(calls[0], ('09:45', 95.0, f'{self.D}T09:30:00+05:30'))
        self.assertEqual(len(calls), 1)                 # one trade a day: nothing after

    def test_moved_levels_restart_the_watch(self):
        _, calls = self.run_sim({0: setup_numbers(), 1: setup_numbers(zl=96)}, {})
        self.assertEqual(calls[1][2], f'{self.D}T09:45:00+05:30')

    def test_tapped_zone_is_frozen(self):
        _, calls = self.run_sim({0: setup_numbers(), 1: setup_numbers(zl=96)}, {'09:45': {'tapped': True, 'triggered': False}})
        self.assertEqual(calls[1][1], 95.0)             # not refreshed after the tap

    def test_rejected_and_failed_setups_are_not_rearmed_today(self):
        marks = {k: setup_numbers() for k in range(6)}
        _, calls = self.run_sim(marks, {'09:45': self.t(rr=1.5)})
        self.assertEqual(len(calls), 1)
        _, calls = self.run_sim(marks, {'09:45': {'tapped': True, 'triggered': False, 'invalid': True}})
        self.assertEqual(len(calls), 1)
        trades, calls = self.run_sim({0: setup_numbers(), 2: setup_numbers(ob='2026-01-05 10:00', obe=97.0)},
                                     {'09:45': self.t(rr=1.5), '10:15': self.t()})
        self.assertEqual(len(trades), 1)                # a new setup on the stock is

    def test_late_and_daily_filters(self):
        trades, _ = self.run_sim({0: setup_numbers()}, {'09:45': self.t(late=True)})
        self.assertEqual(trades, [])
        trades, calls = self.run_sim({0: setup_numbers()}, {'09:45': self.t()}, thr={**THR, 'min_rr': 2.0, 'pre_min_rr': 1.0,
                                                                                    'min_atr_pct': 2.0})
        self.assertEqual((trades, calls), ([], []))     # the day fails the ATR filter

    def test_last_check_before_cutoff(self):
        _, calls = self.run_sim({19: setup_numbers()}, {})
        self.assertEqual([c[0] for c in calls], ['14:35'])


class OptionsLearning(unittest.TestCase):
    NOW = datetime(2026, 10, 7, 11, 0, tzinfo=IST)
    R = options.rules_from({})

    def chain(self):
        return {'spot': 100.5, 'strikes': [{'k': 100.0, 'ce': {'ltp': 4.0, 'bid': 3.9, 'ask': 4.0, 'oi': 1}, 'pe': None}]}

    def test_candidate_measures_whatever_the_thresholds(self):
        s = {'bias_intraday': 0.2, 'spot': 100.5, 'atm': 100.0, 'pcr': 1.1, 'pcr_open': 1.0, 'support': 98, 'iv_pct': None}
        hist = [{'ts': (self.NOW - timedelta(minutes=30)).isoformat(), 'spot': 100.0}]
        c = options.candidate('NIFTY', self.chain(), s, hist, self.NOW, self.R)
        self.assertEqual((c['direction'], c['side'], c['entry'], c['level']), ('long', 'CE', 4.0, 98))
        self.assertEqual(c['f']['bias'], 0.2)               # below the 0.35 rule, still recorded
        self.assertTrue(c['f']['pcr_ok'])
        self.assertIsNone(options.candidate('NIFTY', self.chain(), {**s, 'bias_intraday': 0.1}, hist, self.NOW, self.R))
        self.assertIsNone(options.candidate('NIFTY', self.chain(), s, hist, self.NOW.replace(hour=9, minute=30), self.R))

    def test_path_outcome(self):
        p = [('2026-10-07T11:03:00', 4.5, 100.6), ('2026-10-07T11:06:00', 6.1, 101.0)]
        self.assertEqual(options.path_outcome(p, 4.0, 25, 50, 'long', 98)['status'], 'won')
        o = options.path_outcome([('2026-10-07T11:03:00', 2.9, 100)], 4.0, 25, 50, 'long', 98)
        self.assertEqual((o['status'], o['r']), ('lost', -1.1))
        o = options.path_outcome([('2026-10-07T11:03:00', 4.4, 97.5)], 4.0, 25, 50, 'long', 98)
        self.assertEqual(o['status'], 'won')                # support broke while ahead: out at market
        o = options.path_outcome([('2026-10-07T15:16:00', 4.2, 100)], 4.0, 25, 50, 'long', 98)
        self.assertEqual(o['status'], 'closed')

    def test_bars_outcome_sees_between_the_snapshots(self):
        # Bought at 4.0, stop 3.0 (25 %), target 6.0 (50 %). The premium reaches 6.2 inside 11:01 and is back at 4.1
        # by the 11:03 snapshot: the snapshots never see it, the minute candles do.
        touch = [('2026-10-07T11:01:00+05:30', 4.0, 6.2, 3.9, 4.1, 100.6)]
        o = options.bars_outcome(touch, 4.0, 25, 50, 'long', 98)
        self.assertEqual((o['status'], o['exit'], o['source']), ('won', 6.0, '1m'))
        self.assertEqual(options.path_outcome([('2026-10-07T11:03:00', 4.1, 100.6)], 4.0, 25, 50, 'long', 98)['status'], 'closed')
        # A minute that opens below the stop fills there; one that reaches both counts as stopped.
        o = options.bars_outcome([('2026-10-07T11:01:00+05:30', 2.5, 2.6, 2.4, 2.5, 100)], 4.0, 25, 50, 'long', 98)
        self.assertEqual((o['status'], o['exit'], o['r']), ('lost', 2.5, -1.5))
        o = options.bars_outcome([('2026-10-07T11:01:00+05:30', 4.0, 6.5, 2.9, 5.0, 100)], 4.0, 25, 50, 'long', 98)
        self.assertEqual((o['status'], o['exit']), ('lost', 3.0))
        # The support breaking, and the square-off minute, close at the minute's close.
        o = options.bars_outcome([('2026-10-07T11:01:00+05:30', 4.2, 4.6, 4.1, 4.5, 97.8)], 4.0, 25, 50, 'long', 98)
        self.assertEqual((o['status'], o['exit']), ('won', 4.5))
        o = options.bars_outcome([('2026-10-07T15:14:00+05:30', 4.2, 4.4, 4.1, 4.3, 100)], 4.0, 25, 50, 'long', 98)
        self.assertEqual((o['status'], o['exit']), ('closed', 4.3))

    def test_bars_outcome_with_a_trail(self):
        # Bought at 4.0, stop 3.0, target 6.0, trail 20 %: 11:01 reaches 6.5 (the trail starts at 5.2), 11:02 makes 8.0
        # (the stop goes to 6.4) and 11:03 dips to 6.0: out at the trailing stop, 6.4, not at the target.
        bars = [('2026-10-07T11:01:00+05:30', 4.0, 6.5, 3.9, 6.2, 100.6), ('2026-10-07T11:02:00+05:30', 6.2, 8.0, 6.0, 7.5, 101),
                ('2026-10-07T11:03:00+05:30', 7.0, 7.2, 6.0, 6.1, 101)]
        o = options.bars_outcome(bars, 4.0, 25, 50, 'long', 98, trail_pct=20)
        self.assertEqual((o['status'], o['exit'], o['r'], o['exit_time'][11:16]), ('won', 6.4, 2.4, '11:03'))
        self.assertEqual(options.bars_outcome(bars, 4.0, 25, 50, 'long', 98)['exit'], 6.0)          # no trail: the target
        # A minute that opens under the trailing stop fills at its open.
        gap = bars[:2] + [('2026-10-07T11:03:00+05:30', 6.2, 6.3, 6.0, 6.1, 101)]
        self.assertEqual(options.bars_outcome(gap, 4.0, 25, 50, 'long', 98, trail_pct=20)['exit'], 6.2)
        # The minute that reaches the target but closes under the trail's first stop: out at that stop.
        o = options.bars_outcome([('2026-10-07T11:01:00+05:30', 4.0, 6.5, 3.9, 5.0, 100.6)], 4.0, 25, 50, 'long', 98, trail_pct=20)
        self.assertEqual((o['exit'], o['r']), (5.2, 1.2))
        # The snapshots trail the same way, at the prices seen.
        path = [('2026-10-07T11:03:00', 6.5, 100.6), ('2026-10-07T11:06:00', 8.0, 101), ('2026-10-07T11:09:00', 6.3, 101)]
        self.assertEqual(options.path_outcome(path, 4.0, 25, 50, 'long', 98, trail_pct=20)['exit'], 6.3)

    def test_a_trail_reads_its_own_outcomes(self):
        c = {'symbol': 'NIFTY', 'direction': 'long', 'ts': '2026-10-07T11:00:00+05:30', 'entry': 4.0,
             'f': {'bias': 0.5, 'move_pct': 0.3, 'pcr_ok': True},
             'outcomes': {'25/50': {'status': 'won', 'r': 2.0, 'exit_time': '2026-10-07T11:30:00+05:30'},
                          '25/50/20': {'status': 'won', 'r': 3.4, 'exit_time': '2026-10-07T12:30:00+05:30'}}}
        rs = lambda tr: [t['r'] for t in options.simulate_candidates([c], OptionsMarketRules.thr(self, trail_pct=tr))]      # noqa: E731
        self.assertEqual((rs(0), rs(20), rs(10)), ([2.0], [3.4], []))

    def test_candidates_replayed_on_minute_candles(self):
        with store.connect() as c:
            c.execute('DELETE FROM oc_candidates')
            c.execute('DELETE FROM oc_snapshots')
        cand = {'symbol': 'NIFTY', 'direction': 'long', 'side': 'CE', 'strike': 100.0, 'entry': 4.0, 'level': 98, 'f': {}}
        cid = store.add_candidate(cand, '2026-10-13', '2026-10-07', '2026-10-07T11:00:30+05:30', False)
        snap = {'spot': 100.6, 'strikes': [{'k': 100.0, 'ce': {'ltp': 4.1}, 'pe': None}]}
        store.add_snapshot('NIFTY', '2026-10-13', '2026-10-07T11:03:00+05:30', '2026-10-07', 100.6, {}, options.compact(snap))
        idx = pd.DatetimeIndex(['2026-10-07 11:00', '2026-10-07 11:01', '2026-10-07 11:02']).tz_localize('Asia/Kolkata')
        opt = pd.DataFrame({'Open': [4.0, 4.0, 4.1], 'High': [4.1, 6.2, 4.2], 'Low': [3.95, 3.9, 4.0], 'Close': [4.0, 4.1, 4.1],
                            'Volume': [1, 1, 1]}, index=idx)
        und = pd.DataFrame({'Open': 100.5, 'High': 100.7, 'Low': 100.4, 'Close': 100.6, 'Volume': 1}, index=idx)
        later = datetime(2026, 10, 8, 20, 30, tzinfo=IST)

        def run(candles):
            with mock.patch.object(lab.data, 'now_ist', return_value=later), \
                    mock.patch.object(lab.dhan, 'available', return_value=True), \
                    mock.patch.object(lab.dhan, 'option_id', return_value=('4001', 'NSE_FNO')), \
                    mock.patch.object(lab.dhan, 'chart_candles', side_effect=candles):
                lab.candidate_outcomes(lambda m: None)
            return next(c for c in store.option_candidates() if c['id'] == cid)['outcomes']['25/50']

        def down(*a):
            raise RuntimeError('Dhan is down')
        # Dhan down: the snapshots, which miss the touch.
        o = run(down)
        self.assertEqual((o['status'], o['source']), ('closed', 'snapshots'))
        # The next evening, with candles: tried again, and the touch at 11:01 is a win (the 11:00 minute,
        # partly before the entry at 11:00:30, is left out).
        o = run(lambda sid, seg, inst, *a: opt if inst == 'OPTIDX' else und)
        self.assertEqual((o['status'], o['exit'], o['source']), ('won', 6.0, '1m'))

    def test_simulate_candidates_limits(self):
        def cand(ts, sym='NIFTY', bias=0.5, exit_t=None):
            return {'symbol': sym, 'ts': ts, 'direction': 'long', 'entry': 4.0,
                    'f': {'bias': bias, 'move_pct': 0.3, 'pcr_ok': True, 'iv_pct': None, 'spread_pct': 1.0},
                    'outcomes': {'25/50': {'status': 'won', 'r': 2.0, 'exit_time': exit_t or ts[:11] + '12:00:00'}}}
        thr = {**{k: float(v) for k, v in self.R.items() if k in options.LEARN_FILTERS}, 'stop_pct': 25.0, 'target_pct': 50.0,
               'max_per_day': 2}
        cs = [cand('2026-10-07T10:00:00'), cand('2026-10-07T10:30:00'), cand('2026-10-07T12:30:00'),
              cand('2026-10-07T13:00:00'), cand('2026-10-07T10:00:00', sym='BANKNIFTY', bias=0.2)]
        out = options.simulate_candidates(cs, thr)
        self.assertEqual([t['entry_time'][11:16] for t in out], ['10:00', '12:30'])   # busy until 12:00; 2 a day
        self.assertEqual(len(options.simulate_candidates(cs, {**thr, 'min_bias': 0.15})), 3)


class OptionsMarketRules(unittest.TestCase):
    """The market band, last entry hour and one direction a day, in the backtest and the autopilot."""
    R = options.rules_from({})

    def thr(self, **kw):
        return {**{k: float(v) for k, v in self.R.items() if k in options.LEARN_FILTERS}, 'stop_pct': 25.0,
                'target_pct': 50.0, 'max_per_day': 2, 'market_band_pct': 2.0, **kw}

    def cand(self, ts, direction='long', sym='NIFTY', mkt=None, r=1.0):
        h, m = int(ts[11:13]), int(ts[14:16])
        return {'symbol': sym, 'ts': ts, 'direction': direction, 'entry': 4.0,
                'f': {'bias': 0.5, 'move_pct': 0.3, 'pcr_ok': True, 'iv_pct': None, 'spread_pct': 1.0,
                      'entry_minute': h * 60 + m, 'mkt_move': mkt},
                'outcomes': {'25/50': {'status': 'won' if r > 0 else 'lost', 'r': r, 'exit_time': ts[:11] + f'{h:02d}:{m + 5:02d}:00'}}}

    def test_candidate_passes_the_band_and_the_hour(self):
        self.assertTrue(options.candidate_passes(self.cand('2026-10-14T10:00:00', mkt=-0.5), self.thr()))     # off
        self.assertFalse(options.candidate_passes(self.cand('2026-10-14T10:00:00', mkt=-0.5), self.thr(market_band_pct=0.3)))
        self.assertTrue(options.candidate_passes(self.cand('2026-10-14T10:00:00', mkt=-0.1), self.thr(market_band_pct=0.3)))
        self.assertTrue(options.candidate_passes(self.cand('2026-10-14T10:00:00', mkt=None), self.thr(market_band_pct=0.3)))
        self.assertFalse(options.candidate_passes(self.cand('2026-10-14T13:00:00'), self.thr(last_entry_hour=13.0)))
        self.assertTrue(options.candidate_passes(self.cand('2026-10-14T12:45:00'), self.thr(last_entry_hour=13.0)))

    def test_the_bounce_in_the_simulation(self):
        c = self.cand('2026-10-14T10:40:00', 'short')
        c['f'].update(mkt_off_low=0.88, mkt_off_high=0.0)
        self.assertTrue(options.candidate_passes(c, self.thr()))                          # off
        self.assertFalse(options.candidate_passes(c, self.thr(max_bounce_pct=0.5)))
        self.assertTrue(options.candidate_passes(c, self.thr(max_bounce_pct=1.0)))
        old = self.cand('2026-10-14T10:40:00', 'short')                                    # before it was recorded
        self.assertTrue(options.candidate_passes(old, self.thr(max_bounce_pct=0.5)))
        sp = lab.specs('options')
        self.assertEqual((sp['max_bounce_pct']['values'], sp['max_bounce_pct']['off']), (list(options.MARKET_GRID['max_bounce_pct']), 2.0))

    def test_one_direction_in_the_simulation(self):
        cs = [self.cand('2026-10-14T10:00:00', 'long'), self.cand('2026-10-14T11:00:00', 'short')]
        self.assertEqual(len(options.simulate_candidates(cs, self.thr())), 2)
        out = options.simulate_candidates(cs, self.thr(one_direction=1))
        self.assertEqual([t['side'] for t in out], ['long'])

    def test_ideas_open_one_way_and_the_pass_order(self):
        def look(ts, sym, iv=None, direction='short'):
            c = self.cand(ts, direction, sym)
            c['f']['atm_iv'] = iv
            return c
        one = [look('2026-10-14T10:00:10', 'NIFTY', 20.0), look('2026-10-14T10:00:20', 'SBIN', 15.0),
               look('2026-10-14T10:00:30', 'ITC', 12.0)]
        syms = lambda cs, **kw: [t['symbol'] for t in options.simulate_candidates(cs, self.thr(**kw))]     # noqa: E731
        self.assertEqual(syms(one), ['NIFTY', 'SBIN', 'ITC'])                       # off
        self.assertEqual(syms(one, max_open_side=2), ['NIFTY', 'SBIN'])             # in the order read
        self.assertEqual(syms(one, max_open_side=2, rank_by=1), ['SBIN', 'ITC'])    # the lowest IV, in entry order
        # The other way is not counted, and a slot frees when an idea closes (they exit 5 minutes on).
        later = [look('2026-10-14T10:03:10', 'TCS', direction='long'), look('2026-10-14T10:03:20', 'INFY'),
                 look('2026-10-14T10:06:10', 'LT')]
        self.assertEqual(syms(one + later, max_open_side=2), ['NIFTY', 'SBIN', 'TCS', 'LT'])

    def test_the_account_takes_what_its_limits_allow(self):
        cs = [self.cand(f'2026-10-14T10:00:{s}', 'long', sym) for s, sym in (('10', 'NIFTY'), ('20', 'SBIN'), ('30', 'ITC'))]
        cs.append(self.cand('2026-10-14T11:00:00', 'long', 'TCS'))
        run = lambda acct: [t['symbol'] for t in options.simulate_candidates(cs, self.thr(), acct)]       # noqa: E731
        self.assertEqual(run(None), ['NIFTY', 'SBIN', 'ITC', 'TCS'])
        self.assertEqual(run({'max_open': 2, 'max_per_day': 0}), ['NIFTY', 'SBIN', 'TCS'])     # 2 at once; TCS after
        self.assertEqual(run({'max_open': 0, 'max_per_day': 2}), ['NIFTY', 'SBIN'])
        self.assertEqual(run({'max_open': 0, 'max_per_day': 0}), ['NIFTY', 'SBIN', 'ITC', 'TCS'])

    def test_the_account_the_autopilot_tunes_for(self):
        store.init()
        with store.connect() as c:
            c.execute("DELETE FROM settings WHERE key IN ('demo', 'demo_since', 'exec', 'exec_since')")
        self.assertIsNone(lab.account_limits())
        store.set_settings({'demo_since': '2026-10-09T09:15:00+05:30', 'demo': {'options': True, 'max_open': 3, 'max_per_day': 0}})
        acct = lab.account_limits()
        self.assertEqual((acct['label'], acct['max_open'], acct['max_per_day']), ('demo', 3, 0))
        self.assertEqual(lab.account_note(acct), ' for the demo account (3 open at once, no daily limit)')
        store.set_settings({'exec_since': '2026-10-11T20:27:16+05:30', 'exec': {'options': 'shadow', 'max_open': 4, 'max_per_day': 6}})
        self.assertEqual(lab.account_limits()['label'], 'live trading')         # live trading's limits win
        with store.connect() as c:
            c.execute("DELETE FROM settings WHERE key IN ('demo', 'demo_since', 'exec', 'exec_since')")

    def test_autopilot_searches_the_book(self):
        sp = lab.specs('options')
        self.assertEqual(sp['max_open_side']['values'], list(options.BOOK_GRID['max_open_side']))
        self.assertEqual(sp['max_open_side']['off'], 50)
        self.assertTrue(sp['rank_by']['choice'])
        # From off, one change moves the limit two steps; the order is a choice, picked outright.
        out = autopilot.clamp_steps({'max_open_side': 1, 'rank_by': 3}, {'max_open_side': 50, 'rank_by': 0}, sp, 2)
        self.assertEqual(out, {'max_open_side': 6, 'rank_by': 3})
        self.assertEqual(lab.describe({'max_open_side': 3, 'rank_by': 1}, {'max_open_side': 50, 'rank_by': 0}),
                         'max_open_side off → 3, rank_by Scan order → Lowest IV first')

    def test_autopilot_searches_the_band_on_its_grid(self):
        sp = lab.specs('options')
        self.assertEqual(sp['market_band_pct']['values'], list(options.MARKET_GRID['market_band_pct']))
        self.assertIn('last_entry_hour', sp)
        self.assertIn('one_direction', sp)
        self.assertEqual(lab.describe({'market_band_pct': 0.3, 'one_direction': 1}, {'market_band_pct': 2.0, 'one_direction': 0}),
                         'market_band_pct off → 0.3, one_direction off → 1')

    def test_earlier_candidates_get_the_market_move(self):
        store.init()
        with store.connect() as c:
            c.execute('DELETE FROM oc_candidates')
            c.execute('DELETE FROM oc_snapshots')
        for t, spot in (('09:15', 100.0), ('10:00', 100.4)):
            store.add_snapshot('NIFTY', '2026-10-20', f'2026-10-14T{t}:00+05:30', '2026-10-14', spot, {}, None)
        cand = {'symbol': 'SBIN', 'direction': 'long', 'side': 'CE', 'strike': 950.0, 'entry': 14.0, 'level': None,
                'f': {'bias': 0.5}}
        cid = store.add_candidate(cand, '2026-10-27', '2026-10-14', '2026-10-14T10:01:00+05:30', False)
        late = store.add_candidate(cand, '2026-10-27', '2026-10-14', '2026-10-14T11:00:00+05:30', False)
        kept = store.add_candidate({**cand, 'f': {'bias': 0.5, 'mkt_move': 0.9}}, '2026-10-27', '2026-10-14',
                                   '2026-10-14T10:02:00+05:30', False)
        self.assertEqual(lab.candidate_market(lambda m: None), 3)
        got = {c['id']: c['f'] for c in store.option_candidates()}
        self.assertEqual(got[cid], {'bias': 0.5, 'mkt_move': 0.4, 'mkt_off_low': 0.4, 'mkt_off_high': 0.0})
        self.assertIsNone(got[late]['mkt_move'])                  # Nifty's last read an hour old: unknown
        self.assertIsNone(got[late]['mkt_off_low'])
        self.assertEqual((got[kept]['mkt_move'], got[kept]['mkt_off_low']), (0.9, 0.4))   # what the app recorded stays
        self.assertEqual(lab.candidate_market(lambda m: None), 0)  # done once


class LabAndApi(unittest.TestCase):
    def setUp(self):
        store.init()
        with store.connect() as c:
            for t in ('lab_jobs', 'autopilot_log', 'bt_runs'):
                c.execute(f'DELETE FROM {t}')
            c.execute("DELETE FROM settings WHERE key LIKE 'autopilot%' OR key LIKE 'paused_%' OR key = 'rules' "
                      "OR key LIKE 'lab:%'")
        self.client = app.app.test_client()

    def test_job_queue(self):
        a = store.add_job('backtest')
        self.assertEqual(store.add_job('backtest'), a)             # not queued twice
        j = store.next_job()
        self.assertEqual((j['id'], j['kind']), (a, 'backtest'))
        self.assertIsNone(store.next_job())
        store.reset_running_jobs()
        self.assertEqual(store.next_job()['id'], a)               # a crashed run is retried

    def test_promotion_writes_rules_logs_and_resumes(self):
        store.set_settings({'rules': {'min_rr': 3.0}, 'paused_swing': '1'})
        lab.apply_thresholds('swing', {'min_rr': 3.5, 'max_discount': 0.45, 'bogus': 1}, 'test')
        saved = json.loads(store.settings()['rules'])
        self.assertEqual((saved['min_rr'], saved['max_discount']), (3.5, 0.45))
        self.assertNotIn('bogus', saved)
        self.assertEqual(app.current_rules().min_rr, 3.5)
        self.assertFalse(app.paused('swing'))
        self.assertEqual([r['action'] for r in store.autopilot_log()], ['resume', 'promote'])
        # Undo puts the previous overrides back.
        r = self.client.post('/api/autopilot/undo', json={'mode': 'swing'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(store.settings()['rules']), {'min_rr': 3.0})
        self.assertEqual(self.client.post('/api/autopilot/undo', json={'mode': 'swing'}).status_code, 409)

    def test_shadow_promotes_or_waits_for_approval(self):
        class Bench:
            mode = 'swing'

            def ready(self):
                return True

            def run(self, thr):
                r = 1.0 if thr['min_rr'] == 3.5 else -1.0
                return [trade((date(2026, 9, 1) + timedelta(days=i)).isoformat(), r) for i in range(10)]
        since = (data.now_ist().date() - timedelta(days=40)).isoformat()
        lab.save_state('swing', {'challenger': {'params': {**lab.current_thresholds('swing'), 'min_rr': 3.5},
                                                'since': '2026-08-01', 'change': 'min_rr 3 → 3.5', 'evidence': 'test'}})
        with mock.patch.object(lab, 'tg', lambda text: True):
            store.set_settings({'autopilot_policy': {'mode': 'approve'}})
            lab.shadow_and_guards('swing', Bench())
            st = lab.state('swing')
            self.assertIn('awaiting', st)
            self.assertNotIn('challenger', st)
            # Approve names the proposal shown (its since): without it, refused; with it, applied.
            self.assertEqual(self.client.post('/api/autopilot/approve', json={'mode': 'swing'}).status_code, 400)
            self.assertEqual(self.client.post('/api/autopilot/approve', json={'mode': 'swing', 'since': '2026-08-01'}).status_code, 200)
            self.assertEqual(json.loads(store.settings()['rules'])['min_rr'], 3.5)
        del since

    def test_pause_by_hand_and_api_validation(self):
        r = self.client.post('/api/autopilot/pause', json={'mode': 'intraday'})
        self.assertTrue(r.get_json()['paused'])
        self.assertTrue(app.paused('intraday'))
        self.client.post('/api/autopilot/pause', json={'mode': 'intraday', 'paused': False})
        self.assertFalse(app.paused('intraday'))
        self.assertEqual(self.client.post('/api/autopilot/pause', json={'mode': 'crypto'}).status_code, 400)
        self.assertEqual(self.client.post('/api/autopilot/fly', json={'mode': 'swing'}).status_code, 404)
        self.assertEqual(self.client.post('/api/lab/job', json={'kind': 'rm -rf'}).status_code, 400)
        self.assertEqual(self.client.post('/api/lab/job', json={'kind': 'tune', 'modes': ['swing', 'x']}).status_code, 200)
        r = self.client.post('/api/autopilot/policy', json={'mode': 'approve', 'limits': {'swing': {'min_rr': [2.5, 4], 'evil': [0, 1]}}})
        self.assertEqual(r.get_json()['policy']['mode'], 'approve')
        self.assertEqual(r.get_json()['policy']['limits'], {'swing': {'min_rr': [2.5, 4.0]}})

    def test_perf_endpoints(self):
        r = self.client.get('/api/perf?mode=swing').get_json()
        self.assertEqual(r['source'], 'live')
        self.assertIn('summary', r)
        self.assertIsNone(self.client.get('/api/perf?mode=intraday&source=backtest').get_json()['run'])
        store.add_run('intraday', 'baseline', {'min_rr': 2}, ('2024-01-01', '2026-01-01'), 'nifty50',
                      perf.summary([trade('2025-01-01', 1)]), [], [], {'highlights': []})
        r = self.client.get('/api/perf?mode=intraday&source=backtest').get_json()
        self.assertEqual(r['summary']['trades'], 1)
        self.assertEqual(self.client.get('/api/perf?mode=x').status_code, 400)
        lab_state = self.client.get('/api/lab').get_json()
        self.assertEqual(set(lab_state['modes']), {'swing', 'intraday', 'options'})
        self.assertIn('min_rr', lab_state['modes']['swing']['specs'])

    def test_weekly_report_text(self):
        text = lab.weekly_report()
        self.assertIn('weekly report', text)
        self.assertIn('Swing', text)

    def test_job_steps_recorded_and_shown(self):
        self.assertEqual(lab.step_of('15m candles 240/250 (VOGL)'), ('m15', 240, 250))
        self.assertEqual(lab.step_of('intraday records 30/250'), ('intraday_records', 30, 250))
        self.assertEqual(lab.step_of('options: walk-forward tuning over a – b')[0], 'tune_options')
        self.assertEqual(lab.step_of('swing: walk-forward 3/11 (testing 2024-07 to 2024-10)'), ('tune_swing', 3, 11))
        self.assertEqual(lab.step_of('options: outcomes for 4 candidates')[0], 'options')
        self.assertEqual(lab.step_of('waiting for the market to close'), (None, None, None))
        self.assertEqual(lab.plan('backtest', {'download': False})[0], 'swing_records')
        # A job started by the old lab (progress line only, no step record) still shows where it is.
        jid = store.add_job('nightly')
        job = store.next_job()
        store.update_job(jid, progress='5m candles 5/250 (ABREL)')
        v = lab.job_view(store.jobs(1)[0])
        self.assertEqual([s['state'] for s in v['steps'][:5]], ['done', 'done', 'done', 'running', 'pending'])
        self.assertEqual((v['step_index'], v['steps'][3]['n'], v['steps'][3]['total']), (3, 5, 250))
        # The new lab records each step's timing and results.
        log = lab.JobLog(job)
        log('market context (Nifty, VIX)')
        log('daily candles for 300 stocks')
        log('15m candles 1/2 (A)')
        log.note('history: 10 new candles')
        store.update_job(jid, progress='waiting for the market to close')
        v = lab.job_view(store.jobs(1)[0])
        self.assertTrue(v['waiting'])
        self.assertEqual(v['steps'][2]['state'], 'running')           # the open step, though the line says waiting
        self.assertEqual(v['steps'][2]['notes'], ['history: 10 new candles'])
        log.close()
        store.update_job(jid, status='done', finished=store._now())
        v = lab.job_view(store.jobs(1)[0])
        self.assertEqual([s['state'] for s in v['steps'][:4]], ['done', 'done', 'done', 'skipped'])
        self.assertIsNotNone(v['steps'][0]['secs'])
        # A tuning keeps its outcome as data with its step, for the job's detail.
        tid = store.add_job('tune', {'modes': ['swing']})
        tjob = store.next_job()
        tlog = lab.JobLog(tjob)
        tlog('swing: walk-forward tuning over a – b')
        tlog('swing: walk-forward 2/5 (testing 2025-01 to 2025-04)')
        running = [x for x in store.jobs(5) if x['id'] == tid][0]
        rv = lab.job_view(running)
        self.assertEqual((rv['steps'][0]['n'], rv['steps'][0]['total']), (2, 5))
        tlog.result('tune_swing', {'proposal': False, 'checks': [{'key': 'trades', 'ok': False}]})
        tlog.close()
        store.update_job(tid, status='done', finished=store._now())
        tv = lab.job_view([x for x in store.jobs(5) if x['id'] == tid][0])
        self.assertEqual(tv['steps'][0]['result']['checks'][0]['key'], 'trades')
        # A tuning run that passed over a mode says why.
        t = store.add_job('tune')
        tl = lab.JobLog(store.next_job())
        tl.skip('tune_swing', 'no backtest data yet')
        store.update_job(t, status='done', finished=store._now())
        v = lab.job_view(store.jobs(1)[0])
        self.assertEqual((v['steps'][0]['state'], v['steps'][0]['notes']), ('skipped', ['no backtest data yet']))
        self.assertEqual(self.client.get('/api/lab').get_json()['jobs'][0]['title'], 'Tuning')


if __name__ == '__main__':
    unittest.main()


class BugHunt(unittest.TestCase):
    """Regressions from the 2026-10-07 bug hunt on the learning code."""

    def setUp(self):
        store.init()
        with store.connect() as c:
            for t in ('lab_jobs', 'autopilot_log', 'bt_runs'):
                c.execute(f'DELETE FROM {t}')
            c.execute("DELETE FROM settings WHERE key LIKE 'autopilot%' OR key LIKE 'paused_%' OR key IN ('rules', 'intraday_rules')")

    def test_weekly_report_escapes_html(self):
        # A '< 12' bucket used to make Telegram refuse the whole report.
        store.add_run('swing', 'baseline', {}, ('2024-01-01', '2026-01-01'), 'nifty100',
                      perf.summary([trade('2025-01-01', 1)]), [], [],
                      {'highlights': [{'dimension': 'India VIX', 'bucket': '< 12', 'avg_r': 0.5, 'n': 20, 'signal': 'good'}]})
        text = lab.weekly_report()
        self.assertIn('&lt; 12', text)
        self.assertNotIn(' < 12', text)

    def test_promotion_leaves_hand_edits_alone(self):
        store.set_settings({'rules': {'min_rr': 3.0}})
        base = lab.current_thresholds('swing')                       # what the proposal was tuned against
        store.set_settings({'rules': {'min_rr': 3.0, 'max_discount': 0.4}})   # you edit another threshold since
        changed = lab.apply_thresholds('swing', {**base, 'min_rr': 3.5}, 'test', base=base)
        self.assertEqual(changed, {'min_rr': [3.0, 3.5]})
        self.assertEqual(json.loads(store.settings()['rules']), {'min_rr': 3.5, 'max_discount': 0.4})
        # Undo puts min_rr back, but not a value you have changed again since.
        store.set_settings({'rules': {'min_rr': 4.0, 'max_discount': 0.4}})
        lab.revert_change('swing', lab.state('swing')['last_change'])
        self.assertEqual(json.loads(store.settings()['rules']), {'min_rr': 4.0, 'max_discount': 0.4})
        store.set_settings({'rules': {'min_rr': 3.5}})
        lab.revert_change('swing', lab.state('swing')['last_change'])
        self.assertEqual(json.loads(store.settings()['rules']), {'min_rr': 3.0})

    def test_pause_does_not_flap_after_a_resume(self):
        losing = [{**trade('2026-09-01', -1), 'entry_time': f'2026-09-{d:02d}T10:00:00+05:30'} for d in range(1, 13)]

        class Bench:
            def ready(self):
                return False
        with mock.patch.object(store, 'live_trades', lambda mode: losing), mock.patch.object(lab, 'tg', lambda t: True):
            self.assertEqual(lab.shadow_and_guards('swing', Bench()), ['paused'])
            self.client = app.app.test_client()
            self.client.post('/api/autopilot/pause', json={'mode': 'swing', 'paused': False})     # you resume
            self.assertEqual(lab.shadow_and_guards('swing', Bench()), [])                        # the old streak no longer counts
            self.assertFalse(app.paused('swing'))

    def test_limits_apply_to_grid_values(self):
        pol = autopilot.policy({'limits': {'options': {'stop_pct': [20, 30]}}})
        self.assertEqual(autopilot.grid('stop_pct', 25, {'values': [15, 20, 25, 30, 40]}, pol, 'options', span=4), [20, 25, 30])

    def test_one_bad_stock_does_not_sink_the_records_job(self):
        with mock.patch.object(lab, '_build_one', side_effect=ValueError('corrupt pickle')):
            self.assertEqual(lab._build(('swing', 'X', {}, '2025-01-01', None)), -1)

    def test_ticker_never_holds_a_thread_while_a_call_is_in_flight(self):
        app.TICKER.update(at=0.0, rows=[{'id': 13, 'label': 'NIFTY 50', 'last': 1.0, 'prev_close': 1.0}], error=None,
                          session=None, prev={}, prev_session=None, loading_prev=True)
        calls = []
        with mock.patch.object(app.dhan, 'available', lambda: True), \
             mock.patch.object(app.dhan, 'index_quotes', lambda ids: calls.append(1) or {}):
            self.assertTrue(app.TICKER_LOCK.acquire())            # another request's call is in flight
            try:
                d = app.ticker_state()
            finally:
                app.TICKER_LOCK.release()
        self.assertEqual((calls, d['rows'][0]['label']), ([], 'NIFTY 50'))
