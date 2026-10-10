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
