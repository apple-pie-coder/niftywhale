"""
Live charts: the option chain between two chain reads (live OI / LTP from one full-quote request,
the summary computed again), and the Performance curve's open trades with their feed symbols.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

import app  # noqa: E402
from niftywhale import options, store  # noqa: E402

from tests.test_demo import paper_trade  # noqa: E402
from tests.test_regressions import _reset  # noqa: E402

NOW = datetime.fromisoformat('2026-10-09T12:00:00+05:30')


def side(oi, ltp):
    return {'oi': oi, 'poi': oi, 'ltp': ltp, 'pc': ltp, 'vol': 10.0, 'pvol': 10.0, 'iv': 15.0, 'delta': 0.5, 'bid': ltp, 'ask': ltp}


class ChainLive(unittest.TestCase):
    def setUp(self):
        with store.connect() as c:
            c.execute('DELETE FROM oc_snapshots')
        app.CHAIN_LIVE.clear()
        app.charts.FEED._px.clear()
        rows = [{'k': k, 'ce': side(1000.0, 50.0), 'pe': side(1000.0, 50.0)} for k in (22400.0, 22450.0, 22500.0)]
        chain = {'spot': 22450.0, 'strikes': rows, 'all': {**options.totals(rows), 'max_pain': 22450.0}}
        chain['all'] = {k: v * 10 for k, v in chain['all'].items() if k != 'max_pain'} | {'max_pain': 22450.0}
        store.add_snapshot('NIFTY', '2026-10-13', '2026-10-09T11:57:00+05:30', '2026-10-09', 22450.0,
                           {**options.summarize(chain), 'iv_pct': 40.0, 'dte': 4}, options.compact(chain))

    def test_live_oi_and_summary(self):
        ids = {(22400.0, 'CE'): ('1', 'NSE_FNO'), (22450.0, 'CE'): ('2', 'NSE_FNO'), (22500.0, 'CE'): ('3', 'NSE_FNO'),
               (22400.0, 'PE'): ('4', 'NSE_FNO'), (22450.0, 'PE'): ('5', 'NSE_FNO'), (22500.0, 'PE'): ('6', 'NSE_FNO')}
        quotes = {('NSE_FNO', 3): {'ltp': 40.0, 'oi': 9000.0, 'vol': 50.0, 'bid': 39.9, 'ask': 40.1},   # call wall builds
                  ('NSE_FNO', 4): {'ltp': 60.0, 'oi': 3000.0, 'vol': 50.0, 'bid': 59.9, 'ask': 60.1}}
        app.charts.FEED.add({'IDX:13': 22480.0})
        with mock.patch.object(app.data, 'now_ist', return_value=NOW), \
             mock.patch.object(app.data, 'market_open', return_value=True), \
             mock.patch.object(app.dhan, 'available', return_value=True), \
             mock.patch.object(app.dhan, 'option_ids', side_effect=lambda s, e, pairs: {p: ids[p] for p in pairs if p in ids}), \
             mock.patch.object(app.dhan, 'quotes_full', return_value=quotes) as qf:
            d = self.client().get('/api/options/chain/NIFTY/live').get_json()
            self.client().get('/api/options/chain/NIFTY/live')                  # within 3 s: the cached answer
        self.assertEqual(qf.call_count, 1)
        self.assertEqual(sorted(qf.call_args[0][0]['NSE_FNO']), [1, 2, 3, 4, 5, 6])
        self.assertEqual(d['quoted'], 2)
        row = {r['k']: r for r in d['chain']}
        self.assertEqual((row[22500.0]['ce']['oi'], row[22500.0]['ce']['ltp']), (9000.0, 40.0))
        self.assertEqual(row[22450.0]['ce']['oi'], 1000.0)                          # not quoted: as read
        s = d['summary']
        self.assertEqual((s['spot'], s['resistance']), (22480.0, 22500.0))
        # Expiry totals moved by the window's change: calls 30000 + 8000, puts 30000 + 2000.
        self.assertEqual((s['ce_oi'], s['pe_oi']), (38000.0, 32000.0))
        self.assertEqual(s['pcr'], round(32000 / 38000, 3))
        self.assertEqual((s['iv_pct'], s['dte'], s['max_pain']), (40.0, 4, 22450.0))

    def test_nothing_live_after_hours(self):
        with mock.patch.object(app.data, 'now_ist', return_value=NOW), \
             mock.patch.object(app.data, 'market_open', return_value=False):
            self.assertEqual(self.client().get('/api/options/chain/NIFTY/live').status_code, 204)

    def client(self):
        return app.app.test_client()


class PerfLive(unittest.TestCase):
    def test_open_trades_carry_feed_symbols(self):
        _reset()
        paper_trade('ABC')
        d = app.app.test_client().get('/api/perf?mode=swing').get_json()
        self.assertEqual([(o['lp'], o['entry'], o['stop'], o['side']) for o in d['open_live']], [('ABC', 126.5, 122.68, 'long')])


if __name__ == '__main__':
    unittest.main()
