"""
The funnel's promise: on every card, "−N here" is exactly the number of
stocks its "who stopped here" list shows. Checked for both modes with stocks
that fail at each kind of step, including the ones that are easy to misfile
(too little history; intraday stocks never sent for 15m candles).

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

# Never touch the live database: the store reads DB_PATH once, at its first
# import, so whichever test module imports it first decides. Refuse to run
# unless that is a temporary file.
from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import intraday, smc  # noqa: E402
from niftywhale.intraday import IntradayRules  # noqa: E402

from tests.test_intraday import AFTER, session_frame  # noqa: E402
from tests.test_smc import BULLISH, daily_frame  # noqa: E402


def stock(sym, fo=True):
    return {'symbol': sym, 'name': sym, 'ticker': sym + '.NS', 'fo': fo}


def check(test, funnel, results, steps):
    """Each displayed card's drop equals the size of its click-list."""
    fails = {}
    for r in results:
        fails[r['failed_at']] = fails.get(r['failed_at'], 0) + 1
    keys = ['universe', 'data'] + [k for k, _ in steps if k != 'history']
    for prev, k in zip(keys, keys[1:]):
        listed = fails.get(k, 0) + (fails.get('history', 0) if k == 'data' else 0)
        test.assertEqual(funnel[prev] - funnel[k], listed, f'card {k}: {funnel}')
        test.assertGreaterEqual(funnel[prev], funnel[k])
    test.assertEqual(funnel['universe'], len(results))


class SwingFunnel(unittest.TestCase):
    def test_every_card_matches_its_list(self):
        good = daily_frame(BULLISH)
        stocks = [stock('GOOD'), stock('THIN'), stock('YOUNG'), stock('NODATA'), stock('SHALLOW')]
        frames = {'GOOD.NS': good,
                  'THIN.NS': daily_frame(BULLISH, volume=200_000),
                  'YOUNG.NS': good.tail(30),                       # too little history
                  'SHALLOW.NS': daily_frame(BULLISH[:-1] + [(6, 143)])}
        funnel, passed, results = app.screen(stocks, frames, smc.Rules())
        check(self, funnel, results, smc.STEPS)
        self.assertEqual(funnel['data'], 3)                         # NODATA and YOUNG drop there
        self.assertEqual([p['symbol'] for p in passed], ['GOOD'])


class IntradayFunnel(unittest.TestCase):
    def test_daily_filter_failures_count_at_their_own_step(self):
        good15 = session_frame(BULLISH)
        quiet = daily_frame([(80, 101)], start=100.0)
        quiet[['High', 'Low']] = quiet[['Close', 'Close']].values + [[0.2, -0.2]]
        stocks = [stock(s) for s in ('GOOD', 'THIN', 'QUIET', 'NO15', 'NODAILY', 'FEW15')]
        dailies = {'GOOD.NS': daily_frame(BULLISH), 'THIN.NS': daily_frame(BULLISH, volume=200_000),
                   'QUIET.NS': quiet, 'NO15.NS': daily_frame(BULLISH), 'FEW15.NS': daily_frame(BULLISH)}
        # As in a real scan: only stocks that pass the daily filters get 15m candles.
        frames15 = {'GOOD.NS': good15, 'FEW15.NS': good15.tail(20)}
        funnel, passed, results = app.intraday_screen(stocks, dailies, frames15, IntradayRules(), AFTER)
        check(self, funnel, results, intraday.STEPS)
        by = {r['symbol']: r['failed_at'] for r in results}
        self.assertEqual(by['THIN'], 'liquidity')                  # not "no 15m candles"
        self.assertEqual(by['QUIET'], 'volatility')
        self.assertEqual(by['NO15'], 'data')
        self.assertEqual(by['NODAILY'], 'data')
        self.assertEqual(by['FEW15'], 'data')
        self.assertEqual(funnel['liquidity'], funnel['data'] - 1)


if __name__ == '__main__':
    unittest.main()
