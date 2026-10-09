"""The Charts tab: the live feed's ticks and candles, the routes, the saved layout and the levels."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

import app  # noqa: E402
from niftywhale import charts, livefeed, store  # noqa: E402


class Feed(unittest.TestCase):
    def test_ticks_make_candles(self):
        f = livefeed.Feed(lambda s: {}, lambda: 1)
        t0 = 1_800_000_000                      # a multiple of 5
        f.add({'A': 10.0}, t0 + 0.2)
        f.add({'A': 10.5}, t0 + 1.4)
        f.add({'A': 9.8}, t0 + 3.0)
        f.add({'A': 10.1}, t0 + 6.0)
        self.assertEqual(f.bars('A', 5), [[t0, 10.0, 10.5, 9.8, 9.8, 0], [t0 + 5, 10.1, 10.1, 10.1, 10.1, 0]])
        self.assertEqual(len(f.bars('A', 1)), 4)
        self.assertEqual(f.bars('A', 5, shift=100)[0][0], t0 + 100)
        self.assertEqual([p for _, p in f.ticks('A', since=t0 + 2)], [9.8, 10.1])
        f.add({'A': None, 'B': 0})                 # no price, no tick
        self.assertIsNone(f.last('B'))

    def test_marked_symbols_keep_only_the_last_price(self):
        f = livefeed.Feed(lambda s: {}, lambda: 1, active=lambda: False)
        f.mark(['OPT:NSE_FNO:1'])
        f.watch(['A'])
        f.add({'OPT:NSE_FNO:1': 5.0, 'A': 10.0}, 100.0)
        f.add({'OPT:NSE_FNO:1': 5.5, 'A': 10.5}, 101.0)
        self.assertEqual(f.last('OPT:NSE_FNO:1'), (101.0, 5.5))
        self.assertEqual(f.ticks('OPT:NSE_FNO:1'), [])               # no history for a position
        self.assertEqual(len(f.ticks('A')), 2)
        self.assertEqual(f.watched(), ['A', 'OPT:NSE_FNO:1'])

    def test_fetch_sends_contracts_to_dhan(self):
        with mock.patch.object(charts.dhan, 'available', return_value=True), \
                mock.patch.object(charts.dhan, 'ltp', return_value={}) as ltp:
            charts.fetch_prices(['TCS', 'IDX:13', 'OPT:NSE_FNO:44608', 'OPT:BSE_FNO:1117378'])
        self.assertEqual(ltp.call_args[0], (['TCS'], [13], {'NSE_FNO': [44608], 'BSE_FNO': [1117378]}))
        with mock.patch.object(charts.dhan, 'available', return_value=False):
            self.assertEqual(charts.fetch_prices(['OPT:NSE_FNO:44608']), {})   # yfinance has no option quotes

    def test_option_ids_from_the_instrument_list(self):
        from niftywhale import dhan
        rows = [{'SEM_TRADING_SYMBOL': 'NIFTY-Oct2026-22450-CE', 'SEM_SMST_SECURITY_ID': '44608', 'SEM_OPTION_TYPE': 'CE',
                 'SEM_EXM_EXCH_ID': 'NSE', 'SEM_EXPIRY_DATE': '2026-10-13 14:30:00', 'SEM_STRIKE_PRICE': '22450.00000'},
                {'SEM_TRADING_SYMBOL': 'NTPC-Oct2026-312.5-CE', 'SEM_SMST_SECURITY_ID': '9', 'SEM_OPTION_TYPE': 'CE',
                 'SEM_EXM_EXCH_ID': 'BSE', 'SEM_EXPIRY_DATE': '2026-10-27 14:30:00', 'SEM_STRIKE_PRICE': '312.5'},
                {'SEM_TRADING_SYMBOL': 'NTPC-Oct2026-312.5-CE', 'SEM_SMST_SECURITY_ID': '139738', 'SEM_OPTION_TYPE': 'CE',
                 'SEM_EXM_EXCH_ID': 'NSE', 'SEM_EXPIRY_DATE': '2026-10-27 14:30:00', 'SEM_STRIKE_PRICE': '312.50000'},
                {'SEM_TRADING_SYMBOL': 'SENSEX-Oct2026-72200-CE', 'SEM_SMST_SECURITY_ID': '1117378', 'SEM_OPTION_TYPE': 'CE',
                 'SEM_EXM_EXCH_ID': 'BSE', 'SEM_EXPIRY_DATE': '2026-10-15 15:30:00', 'SEM_STRIKE_PRICE': '72200'}]
        opts = {}
        for r in rows:
            dhan._opt_row(r, opts)
        path = os.path.join(_tmp, 'opts.json')
        with open(path, 'w') as fh:
            json.dump(opts, fh)
        with mock.patch.object(dhan, 'OPTS_PATH', __import__('pathlib').Path(path)), \
                mock.patch.object(dhan, '_load_ids', return_value={}), mock.patch.dict(dhan._opt_ids, clear=True):
            self.assertEqual(dhan.option_id('NIFTY', '2026-10-13', 22450.0, 'CE'), ('44608', 'NSE_FNO'))
            self.assertEqual(dhan.option_id('NTPC', '2026-10-27', 312.5, 'CE'), ('139738', 'NSE_FNO'))   # NSE wins
            self.assertEqual(dhan.option_id('SENSEX', '2026-10-15', 72200, 'CE'), ('1117378', 'BSE_FNO'))
            self.assertIsNone(dhan.option_id('NIFTY', '2026-10-13', 22450.0, 'PE'))

    def test_polls_only_while_watched_and_active(self):
        calls = []
        f = livefeed.Feed(lambda s: calls.append(list(s)) or {x: 1.0 for x in s}, lambda: 0.2, active=lambda: True)
        with mock.patch.object(livefeed, 'WATCH_S', 0.5):
            f.watch(['IDX:13', 'IDX:25'])
            deadline = time.time() + 3
            while time.time() < deadline and f._thread.is_alive():
                time.sleep(0.05)
        self.assertFalse(f._thread.is_alive())       # nobody watching: the thread ends
        self.assertTrue(calls)
        self.assertEqual(calls[0], ['IDX:13', 'IDX:25'])
        self.assertIsNotNone(f.last('IDX:25'))

    def test_market_closed_asks_nothing(self):
        calls = []
        f = livefeed.Feed(lambda s: calls.append(s) or {}, lambda: 0.2, active=lambda: False)
        with mock.patch.object(livefeed, 'WATCH_S', 0.4):
            f.watch(['IDX:13'])
            f._thread.join(3)
        self.assertEqual(calls, [])


class Routes(unittest.TestCase):
    def setUp(self):
        store.init()
        store.set_json('charts:layout', None)
        self.c = app.app.test_client()
        p = mock.patch.object(charts.FEED, 'watch')
        self.watch = p.start()
        self.addCleanup(p.stop)

    def test_config_default_and_saved_layout(self):
        d = self.c.get('/api/charts/config').get_json()
        self.assertEqual(d['layout'], app.CHART_DEFAULT)
        self.assertIn({'s': 1, 'label': '1s'}, d['tfs'])
        self.assertIn({'s': 604800, 'label': '1W'}, d['tfs'])
        self.assertIn('NIFTY 50', [i['label'] for i in d['instruments']])
        bad = [{'symbol': 'idx:25', 'tf': 60}, {'symbol': 'NOPE', 'tf': 60}, {'symbol': 'IDX:13', 'tf': 7},
               {'symbol': 'IDX:13', 'tf': '3600', 'levels': False}, 'junk'] + [{'symbol': 'IDX:13', 'tf': 1}] * 20
        r = self.c.post('/api/charts/layout', json={'layout': bad}).get_json()
        self.assertEqual(r['layout'][:2], [{'symbol': 'IDX:25', 'tf': 60, 'levels': True},
                                           {'symbol': 'IDX:13', 'tf': 3600, 'levels': False}])
        self.assertEqual(len(r['layout']), app.CHART_MAX)
        self.assertEqual(self.c.get('/api/charts/config').get_json()['layout'], r['layout'])

    def test_candles_watch_the_symbol_and_carry_levels(self):
        bars = {'bars': [[1, 1.1, 1.2, 1.0, 1.15, 0]], 'source': 'dhan', 'daily': False, 'ticks_from': None}
        with mock.patch.object(charts, 'candles', return_value=bars), \
                mock.patch.object(app, 'chart_levels', return_value=[{'price': 1.1, 'kind': 'zone', 'label': 'x'}]):
            d = self.c.get('/api/charts/candles?symbol=IDX:13&tf=900').get_json()
        self.assertEqual(d['bars'], bars['bars'])
        self.assertEqual(d['levels'][0]['price'], 1.1)
        self.watch.assert_called_with(['IDX:13'])
        self.assertEqual(self.c.get('/api/charts/candles?symbol=IDX:13&tf=7').status_code, 400)
        self.assertEqual(self.c.get('/api/charts/candles?symbol=FOO&tf=60').status_code, 400)

    def test_live_returns_ticks_since(self):
        charts.FEED.add({'IDX:13': 1.1}, 100.0)
        charts.FEED.add({'IDX:13': 1.2}, 200.0)
        d = self.c.get('/api/charts/live?symbols=IDX:13,FOO&since=150').get_json()
        self.assertEqual(list(d['ticks']), ['IDX:13'])
        self.assertEqual([p for _, p in d['ticks']['IDX:13']], [1.2])
        self.assertIn('poll_ms', d)

    def test_levels_from_zones(self):
        store.init()
        with store.connect() as c:
            c.execute("DELETE FROM zones")
            c.execute("INSERT INTO zones (symbol, zone_low, zone_high, target, status, mode, side, created_at, expires_at, meta) "
                      "VALUES ('RELIANCE', 1.30, 1.31, 1.35, 'watching', 'swing', 'long', ?, '2099-01-01', NULL)",
                      (app.data.now_ist().isoformat(),))
            c.execute("INSERT INTO zones (symbol, zone_low, zone_high, target, status, mode, side, created_at, expires_at, trigger) "
                      "VALUES ('RELIANCE', 1.32, 1.33, 1.36, 'triggered', 'intraday', 'short', ?, '2000-01-01', ?)",
                      (app.data.now_ist().isoformat(), json.dumps({'entry': 1.325, 'stop': 1.334, 'target': 1.30})))
        lv = app.chart_levels('RELIANCE')
        self.assertEqual([(x['kind'], x['price']) for x in lv], [('zone', 1.31), ('zone', 1.30), ('target', 1.35)])


class Marks(unittest.TestCase):
    def test_marked_symbols_keep_only_the_last_price(self):
        f = livefeed.Feed(lambda s: {}, lambda: 1)
        f._start = lambda: None
        f.mark(['OPT:NSE_FNO:1'])
        f.add({'OPT:NSE_FNO:1': 10.0}, 100.0)
        f.add({'OPT:NSE_FNO:1': 11.0}, 101.0)
        self.assertEqual(f.last('OPT:NSE_FNO:1'), (101.0, 11.0))
        self.assertEqual(f.ticks('OPT:NSE_FNO:1'), [])
        self.assertEqual(f.watched(), ['OPT:NSE_FNO:1'])
        f.watch(['OPT:NSE_FNO:1'])                     # a chart opens it: ticks from now on
        f.add({'OPT:NSE_FNO:1': 12.0}, 102.0)
        self.assertEqual([p for _, p in f.ticks('OPT:NSE_FNO:1')], [12.0])

    def test_contracts_go_to_their_segment(self):
        with mock.patch.object(charts.dhan, 'available', return_value=True), \
             mock.patch.object(charts.dhan, 'ltp', return_value={'X': 1.0}) as ltp:
            charts.fetch_prices(['ABC', 'IDX:13', 'OPT:NSE_FNO:44608', 'OPT:BSE_FNO:1117378'])
        ltp.assert_called_once_with(['ABC'], [13], {'NSE_FNO': [44608], 'BSE_FNO': [1117378]})
        with mock.patch.object(charts.dhan, 'available', return_value=False):
            self.assertEqual(charts.fetch_prices(['OPT:NSE_FNO:44608']), {})      # no option quotes off Dhan


if __name__ == '__main__':
    unittest.main()
