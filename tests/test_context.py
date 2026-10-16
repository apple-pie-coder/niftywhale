"""
Context from the data already on disk (features.py, history.update_context) and the gates on it
(smc.context_gate): market breadth, strength against Nifty and the stock's sector, the weekly trend,
and the CHoCH candle (its close in its range, its volume against the same time slot).

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

from niftywhale import features, history, intraday, smc  # noqa: E402

IST = 'Asia/Kolkata'


def daily(closes, start=date(2025, 1, 1)):
    idx = pd.DatetimeIndex(pd.bdate_range(start, periods=len(closes)))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({'Open': c, 'High': c * 1.01, 'Low': c * 0.99, 'Close': c, 'Volume': 1e6})


class Series(unittest.TestCase):
    def test_breadth_and_sector_series(self):
        lab = tempfile.mkdtemp()
        with mock.patch.object(history, 'LAB_DIR', history.Path(lab)):
            # 60 stocks: 45 rising (above their 50-day), 15 falling.
            for i in range(60):
                trend = np.linspace(100, 160, 120) if i < 45 else np.linspace(160, 100, 120)
                history.save('1d', f'S{i}', daily(trend))
            members = {'niftybank': ['S0', 'S1', 'S2'], 'niftyit': ['S50', 'S51', 'S52']}
            with mock.patch.object(history, 'load', wraps=history.load), \
                    mock.patch('niftywhale.universe.sector_members', return_value={**{k: [] for k in ('niftyauto',)}, **members}):
                out = history.update_context()
            self.assertAlmostEqual(out['breadth50'].iloc[-1], 75.0)
            self.assertTrue(np.isnan(out['breadth50'].iloc[10]))               # no 50-day average yet
            self.assertGreater(out['sec_niftybank'].iloc[-1], 100)             # members rose
            self.assertLess(out['sec_niftyit'].iloc[-1], 100)                  # members fell
            self.assertNotIn('sec_niftyauto', out)                             # fewer than 3 members
            history.save('market', 'NIFTY_VIX', pd.DataFrame({'nifty': out['breadth50'] * 0 + 20000, 'vix': 14.0}))
            m = history.load_market()
            self.assertIn('breadth50', m)
            self.assertEqual(features.market_context(m, m.index[-1].date() + timedelta(days=1))['breadth50'], 75.0)


class MarketCloses(unittest.TestCase):
    """Nifty and VIX up to today's close: Dhan's daily history adds a session only the next day."""
    DAYS = ('2026-10-13', '2026-10-14', '2026-10-15')

    def setUp(self):
        _store.init()
        self.lab = tempfile.mkdtemp()
        self.minute_calls = []
        patches = [mock.patch.object(history, 'LAB_DIR', history.Path(self.lab)),
                   mock.patch.object(history.time, 'sleep', lambda s: None),
                   mock.patch.object(history.dhan, '_headers', lambda: {}),
                   mock.patch.object(history.requests, 'post', self.daily)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def daily(self, url, headers=None, timeout=None, json=None):
        base = 22000.0 if json['securityId'] == '13' else 14.0
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {'timestamp': [int(pd.Timestamp(d, tz=IST).timestamp()) for d in self.DAYS],
                          'close': [base + i for i in range(len(self.DAYS))]}
        return r

    def minutes(self, today_bars=True, fail_vix=False):
        def f(sid, seg, inst, interval, days):
            self.minute_calls.append(sid)
            if fail_vix and sid == '21':
                raise RuntimeError('Dhan is down')
            idx = pd.DatetimeIndex(['2026-10-15 15:29', '2026-10-16 15:29', '2026-10-16 17:44']).tz_localize(IST)
            close = [1.0, 2.0, 23146.15 if sid == '13' else 14.06]
            f = pd.DataFrame({'Close': close}, index=idx)
            return f if today_bars else f.iloc[:1]
        return mock.patch.object(history.dhan, 'chart_candles', f)

    def at(self, hh, mm=0):
        return mock.patch.object(history.data, 'now_ist', lambda: datetime(2026, 10, 16, hh, mm, tzinfo=history.dhan.IST))

    def test_todays_close_from_minute_candles_after_the_close(self):
        with self.minutes(), self.at(20, 30):
            m = history.update_market(years=0.1)
        self.assertEqual(str(m.index[-1].date()), '2026-10-16')
        self.assertEqual((m['nifty'].iloc[-1], m['vix'].iloc[-1]), (23146.15, 14.06))   # the day's last candle
        self.assertEqual(float(history.load_market()['nifty'].iloc[-2]), 22002.0)       # the daily history kept

    def test_not_during_the_session_and_not_when_the_daily_has_it(self):
        with self.minutes(), self.at(14, 0):
            self.assertEqual(str(history.update_market(years=0.1).index[-1].date()), '2026-10-15')
        self.DAYS = ('2026-10-14', '2026-10-15', '2026-10-16')
        with self.minutes(), self.at(20, 30):
            self.assertEqual(float(history.update_market(years=0.1)['nifty'].iloc[-1]), 22002.0)   # Dhan's own candle
        self.assertEqual(self.minute_calls, [])

    def test_a_holiday_or_a_failure(self):
        with self.minutes(today_bars=False), self.at(20, 30):
            self.assertEqual(str(history.update_market(years=0.1).index[-1].date()), '2026-10-15')   # no session today
        with self.minutes(fail_vix=True), self.at(20, 30):
            m = history.update_market(years=0.1)
        self.assertEqual(m['nifty'].iloc[-1], 23146.15)
        self.assertTrue(np.isnan(m['vix'].iloc[-1]))                                       # unknown, not made up

    def test_recorded_context_put_right_once_the_close_is_in(self):
        import json
        from niftywhale import context
        with _store.connect() as c:
            for t in ('zones', 'oc_ideas', 'oc_candidates'):
                c.execute(f'DELETE FROM {t}')
            c.execute("INSERT INTO zones (symbol, status, created_at, trigger, ctx) VALUES ('SBIN', 'won', '2026-10-15', ?, ?)",
                      (json.dumps({'choch_time': '2026-10-16T10:30:00+05:30', 'entry': 1}),
                       json.dumps({'vix': 13.0, 'market_trend': 'down', 'rr': 3.0})))
        iid = _store.add_idea({'symbol': 'NIFTY', 'created_at': '2026-10-16T09:45:00+05:30', 'session': '2026-10-16'})
        _store.update_idea(iid, features=json.dumps({'vix': 13.0, 'bias': 0.5}))
        cid = _store.add_candidate({'symbol': 'NIFTY', 'direction': 'long', 'side': 'CE', 'strike': 1.0, 'entry': 1.0,
                                    'level': None, 'f': {'vix': 13.0, 'ctx': 1}}, '2026-10-27', '2026-10-16',
                                   '2026-10-16T09:45:00+05:30', False)
        idx = pd.bdate_range(end='2026-10-15', periods=80)
        market = pd.DataFrame({'nifty': np.linspace(20000, 22000, 80), 'vix': [14.0] * 79 + [14.63]}, index=idx)
        self.assertEqual(context.refresh_market_context(market, days=3650), {'zones': 1, 'ideas': 1, 'candidates': 1})
        with _store.connect() as c:
            z = json.loads(c.execute('SELECT ctx FROM zones').fetchone()[0])
        self.assertEqual((z['vix'], z['market_trend'], z['rr']), (14.63, 'up', 3.0))      # the 15th's close; the rest kept
        self.assertEqual(json.loads(_store.ideas()[0]['features'])['vix'], 14.63)
        self.assertEqual(next(c for c in _store.option_candidates() if c['id'] == cid)['f']['vix'], 14.63)
        self.assertEqual(context.refresh_market_context(market, days=3650), {'zones': 0, 'ideas': 0, 'candidates': 0})

    def test_every_held_stock_stays_in_the_daily_update(self):
        from niftywhale import lab
        history.save('1d', 'OLDSTOCK', daily([100.0] * 60))
        history.save('1d', 'SBIN', daily([100.0] * 60))
        self.assertEqual(history.held('1d'), ['OLDSTOCK', 'SBIN'])
        got = {}
        with mock.patch.object(lab.dhan, 'available', lambda: True), \
                mock.patch.object(lab, 'universe_for', lambda m: [{'symbol': 'SBIN'}, {'symbol': 'TCS'}]), \
                mock.patch.object(lab.history, 'update_market', lambda: None), \
                mock.patch.object(lab.history, 'update_daily', lambda syms, yrs: got.setdefault('syms', syms)), \
                mock.patch.object(lab.history, 'update_context', lambda: None), \
                mock.patch.object(lab.history, 'update_intraday', lambda *a: 0), \
                mock.patch.object(lab, 'wait_for_close', lambda job: None):
            lab.update_history(None, lambda m: None)
        self.assertEqual(got['syms'], ['OLDSTOCK', 'SBIN', 'TCS'])          # breadth keeps its population


class Features(unittest.TestCase):
    def test_relative_strength_against_nifty_and_sector(self):
        stock = daily(np.r_[np.full(80, 100.0), np.linspace(100, 110, 21)])           # +10 % over 20 days
        idx = stock.index
        market = pd.DataFrame({'nifty': np.r_[np.full(80, 20000.0), np.linspace(20000, 20400, 21)],     # +2 %
                               'vix': 14.0, 'sec_niftybank': np.r_[np.full(80, 100.0), np.linspace(100, 105, 21)]}, index=idx)
        day = idx[-1].date() + timedelta(days=1)
        r = features.relative_context(stock, market, 'niftybank', day)
        self.assertEqual((r['rs_nifty20'], r['rs_sector20'], r['sector']), (8.0, 5.0, 'niftybank'))
        self.assertNotIn('rs_sector20', features.relative_context(stock, market, None, day))

    def test_weekly_trend(self):
        up = daily(np.linspace(100, 200, 200))
        down = daily(np.linspace(200, 100, 200))
        flat = daily(np.full(200, 100.0))                  # no trend at all
        day = up.index[-1].date() + timedelta(days=1)
        self.assertEqual([features.weekly_context(f, day).get('weekly_trend') for f in (up, down, flat)], ['up', 'down', 'mixed'])
        self.assertEqual(features.weekly_context(daily(np.linspace(1, 2, 30)), day), {})                # too short

    def test_choch_candle(self):
        idx = []
        for k in range(6):                                   # six sessions of 5m candles, 09:15-10:10
            d = datetime(2026, 10, 5) + timedelta(days=k)
            idx += [pd.Timestamp(d.replace(hour=9, minute=15)) + timedelta(minutes=5 * j) for j in range(12)]
        idx = pd.DatetimeIndex(idx).tz_localize(IST)
        f = pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0, 'Close': 100.0, 'Volume': 1000.0}, index=idx)
        at = idx[-3]                                         # the last session's 10:00 candle
        f.loc[at, ['Close', 'Volume']] = [100.6, 3000.0]     # closed 80 % up its range, three times the slot's volume
        t = {'choch_time': at.isoformat()}
        self.assertEqual(features.candle_context(f, t, 'long'), {'choch_close_loc': 0.8, 'choch_rvol': 3.0})
        self.assertEqual(features.candle_context(f, t, 'short')['choch_close_loc'], 0.2)
        self.assertEqual(features.candle_context(f, {'choch_time': '2020-01-01T10:00:00+05:30'}, 'long'), {})


class Gates(unittest.TestCase):
    def rules(self, **on):
        return {**smc.Rules().as_dict(), **on}

    def test_off_by_default(self):
        ctx = {'breadth50': 5, 'weekly_trend': 'down', 'rs_nifty20': -15, 'rs_sector20': -15, 'choch_rvol': 0.1, 'choch_close_loc': 0.0}
        self.assertIsNone(smc.context_gate(ctx, 'long', self.rules()))
        self.assertIsNone(smc.context_gate(ctx, 'long', intraday.IntradayRules().as_dict(), intraday.TUNABLE))

    def test_each_gate_both_ways(self):
        r = self.rules(min_breadth=50)
        self.assertIn('above their 50-day', smc.context_gate({'breadth50': 40}, 'long', r))
        self.assertIsNone(smc.context_gate({'breadth50': 40}, 'short', r))          # a short wants weak breadth
        self.assertIn('at most 50%', smc.context_gate({'breadth50': 60}, 'short', r))
        r = self.rules(weekly_align=1)
        self.assertIn('against the trade', smc.context_gate({'weekly_trend': 'down'}, 'long', r))
        self.assertIsNone(smc.context_gate({'weekly_trend': 'down'}, 'short', r))
        self.assertIsNone(smc.context_gate({'weekly_trend': 'mixed'}, 'long', r))
        r = self.rules(min_rs_nifty=0, min_rs_sector=2)
        self.assertIn('against Nifty', smc.context_gate({'rs_nifty20': -1}, 'long', r))
        self.assertIsNone(smc.context_gate({'rs_nifty20': -1}, 'short', r))         # lagging suits a short
        self.assertIn('its sector', smc.context_gate({'rs_nifty20': 3, 'rs_sector20': 1}, 'long', r))
        r = self.rules(min_choch_rvol=1.5, min_close_loc=0.6)
        self.assertIn('usual volume', smc.context_gate({'choch_rvol': 1.2, 'choch_close_loc': 0.9}, 'long', r))
        self.assertIn('through its range', smc.context_gate({'choch_rvol': 2, 'choch_close_loc': 0.5}, 'long', r))
        self.assertIsNone(smc.context_gate({'choch_rvol': 2, 'choch_close_loc': 0.7}, 'long', r))
        self.assertIsNone(smc.context_gate({}, 'long', self.rules(min_breadth=60, min_choch_rvol=2)))   # unseen: never blocks

    def test_intraday_has_its_own_gates_only(self):
        r = {**intraday.IntradayRules().as_dict(), 'min_choch_rvol': 1.5}
        self.assertIn('usual volume', smc.context_gate({'choch_rvol': 1.0}, 'long', r, intraday.TUNABLE))
        self.assertNotIn('weekly_align', intraday.IntradayRules().as_dict())
        self.assertEqual(intraday.IntradayRules().with_overrides({'min_close_loc': 5}).min_close_loc, 0.9)   # clamped


if __name__ == '__main__':
    unittest.main()
