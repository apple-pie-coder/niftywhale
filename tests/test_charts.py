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

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import charts, livefeed, store, studies  # noqa: E402


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

    def test_streamed_volume_makes_candle_volume(self):
        f = livefeed.Feed(lambda s: {}, lambda: 1)
        t0 = 1_800_000_000
        f.add({'A': 10.0}, t0 + 0.2, {'A': 1000})
        f.add({'A': 10.2}, t0 + 2.0, {'A': 1040})
        f.add({'A': 10.1}, t0 + 6.0, {'A': 1100})
        f.add({'A': 10.3}, t0 + 7.0, {'A': 1125})
        self.assertEqual([b[5] for b in f.bars('A', 5)], [40, 85])

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
               {'symbol': 'IDX:13', 'tf': '3600', 'levels': False, 'flow': True}, 'junk'] + [{'symbol': 'IDX:13', 'tf': 1}] * 20
        r = self.c.post('/api/charts/layout', json={'layout': bad}).get_json()
        self.assertEqual(r['layout'][:2], [{'symbol': 'IDX:25', 'tf': 60, 'levels': True, 'flow': False, 'studies': []},
                                           {'symbol': 'IDX:13', 'tf': 3600, 'levels': False, 'flow': True, 'studies': []}])
        self.assertEqual(len(r['layout']), app.CHART_MAX)
        self.assertEqual(self.c.get('/api/charts/config').get_json()['layout'], r['layout'])

    def test_orderflow_pane(self):
        with store.connect() as c:
            c.execute('DELETE FROM of_bars')
        now = int(time.time()) // 60 * 60
        bars = [{'key': 'NIFTY-FUT', 't': now - 120 + 60 * i, 'open': 1, 'high': 1, 'low': 1, 'close': 1, 'buy': 10 + i, 'sell': 5,
                 'unknown': 0, 'delta': 5 + i, 'cvd': 5 + i, 'poc': 1, 'step': 5.0, 'trades': 4, 'merged': 1, 'by_quote': 3,
                 'by_tick': 1, 'levels': [[1, 5, 10 + i, 0]]} for i in range(2)]
        store.add_of_bars(bars[:1])
        with mock.patch.object(charts.SOURCE, 'flow_bars', return_value=bars):           # the second not saved yet
            d = self.c.get('/api/charts/orderflow?symbol=IDX:13&tf=300').get_json()
        self.assertEqual(d['name'], 'NIFTY-FUT')                                          # the index's future
        self.assertEqual(d['bars'], [[now - 120 + charts.SHIFT, 5, 5], [now - 60 + charts.SHIFT, 6, 6]])
        self.assertEqual(self.c.get('/api/charts/orderflow?symbol=IDX:19&tf=300').get_json()['bars'], [])   # no future read
        self.assertEqual(self.c.get('/api/charts/orderflow?symbol=IDX:13&tf=86400').get_json()['bars'], [])
        self.assertEqual(self.c.get('/api/charts/orderflow?symbol=NOPE&tf=300').status_code, 400)

    def test_indicators_saved_with_the_layout(self):
        d = self.c.get('/api/charts/config').get_json()['studies']
        self.assertEqual((d['max'], d['max_panels']), (studies.MAX_STUDIES, studies.MAX_PANELS))
        self.assertIn('ema', [k['key'] for k in d['kinds']])
        ema = {'id': 'e9', 'type': 'ema', 'params': {'length': 9}, 'colors': {'ma': '#FF0000'}}
        r = self.c.post('/api/charts/layout', json={'layout': [{'symbol': 'IDX:13', 'tf': 300, 'studies': [ema]}]}).get_json()
        self.assertEqual(r['layout'][0]['studies'],
                         [{'id': 'e9', 'type': 'ema', 'params': {'length': 9, 'source': 'close'}, 'colors': {'ma': '#ff0000'}, 'hidden': False}])
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

    def test_panel_candles_keep_their_sessions_with_indicators(self):
        import pandas as pd
        idx = pd.DatetimeIndex([]).tz_localize(app.data.IST)
        for day in ('2026-10-08', '2026-10-09', '2026-10-12', '2026-10-13'):
            idx = idx.append(pd.date_range(f'{day} 09:15', periods=75, freq='5min', tz=app.data.IST))
        f = pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0, 'Close': 100.5, 'Volume': 10}, index=idx)
        app.PANEL_CANDLES.clear()
        with mock.patch.object(charts, 'frame', return_value=(f, 'dhan')) as fr:
            d = self.c.get('/api/candles/RELIANCE?tf=5').get_json()
        fr.assert_called_with('RELIANCE', 300)
        self.assertEqual((len(d['bars']), d['tf'], d['source']), (225, 5, 'dhan'))      # the last three sessions
        self.assertEqual(d['bars'][0]['d'], '10-09 09:15')
        self.assertEqual(len(d['ind']['bb_mid']), 225)
        self.assertEqual(self.c.get('/api/candles/RELIANCE?tf=7').status_code, 400)
        self.assertEqual(self.c.get('/api/candles/bad sym?tf=5').status_code, 400)

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



class Drawings(unittest.TestCase):
    """The Charts tab's drawings: per instrument, cleaned, every device."""
    def setUp(self):
        store.init()
        store.set_json('charts:drawings', {})
        self.c = app.app.test_client()

    def test_saved_cleaned_and_listed(self):
        ok = [{'id': 'a1', 'type': 'trend', 'p1': {'t': 1791545100, 'p': 22500.123456}, 'p2': {'t': 1791548700, 'p': 22600}},
              {'id': 'b2', 'type': 'hline', 'p1': {'t': 1791545100, 'p': 22450}, 'p2': {'t': 1, 'p': 1}},
              {'id': 'c3', 'type': 'rect', 'p1': {'t': 1791545100, 'p': 22400}, 'p2': {'t': 1791552300, 'p': 22550}}]
        bad = [{'id': 'x', 'type': 'circle', 'p1': {'t': 1, 'p': 1}}, {'id': 'bad id!', 'type': 'hline', 'p1': {'t': 1791545100, 'p': 1}},
               {'id': 'y', 'type': 'trend', 'p1': {'t': 1791545100, 'p': 1}}, {'id': 'z', 'type': 'hline', 'p1': {'t': 'soon', 'p': 1}}, 'junk']
        r = self.c.post('/api/charts/drawings', json={'symbol': 'idx:13', 'drawings': ok + bad}).get_json()
        self.assertEqual([d['id'] for d in r['drawings']], ['a1', 'b2', 'c3'])
        self.assertNotIn('p2', r['drawings'][1])                        # a horizontal line is one point
        self.assertEqual(r['drawings'][0]['p1']['p'], 22500.1235)
        got = self.c.get('/api/charts/config').get_json()['drawings']
        self.assertEqual(list(got), ['IDX:13'])
        self.c.post('/api/charts/drawings', json={'symbol': 'IDX:13', 'drawings': []})
        self.assertEqual(self.c.get('/api/charts/config').get_json()['drawings'], {})

    def test_unknown_instrument_and_cap(self):
        self.assertEqual(self.c.post('/api/charts/drawings', json={'symbol': 'NOPE', 'drawings': []}).status_code, 400)
        many = [{'id': f'h{i}', 'type': 'hline', 'p1': {'t': 1791545100, 'p': 100 + i}} for i in range(80)]
        r = self.c.post('/api/charts/drawings', json={'symbol': 'IDX:13', 'drawings': many}).get_json()
        self.assertEqual(len(r['drawings']), app.DRAW_MAX)

    def test_colours_kept_when_valid(self):
        d = [{'id': 'a', 'type': 'hline', 'p1': {'t': 1791545100, 'p': 1}, 'color': '#E5484D'},
             {'id': 'b', 'type': 'hline', 'p1': {'t': 1791545100, 'p': 2}, 'color': 'red; background:url(x)'}]
        r = self.c.post('/api/charts/drawings', json={'symbol': 'IDX:13', 'drawings': d}).get_json()
        self.assertEqual(r['drawings'][0]['color'], '#e5484d')
        self.assertNotIn('color', r['drawings'][1])


class Spark(unittest.TestCase):
    """The open-position sparkline (app.spark): closes from the entry on, thinned to SPARK_POINTS with the latest kept;
    an option's own premium, or nothing when only the underlying has candles; nothing for a closed trade."""
    def bars(self, n):
        return [{'t': f'2026-10-12T{9 + k // 12:02d}:{(k % 12) * 5:02d}:00+05:30', 'c': 100.0 + k} for k in range(n)]

    def test_from_the_entry_thinned(self):
        d = {'mode': 'swing', 'status': 'open', 'entry': 105.0, 'stop': 100.0, 'target': 120.0, 'side': 'long', 'direction': 'long'}
        c = {'bars': self.bars(300), 'entry_k': 5, 'lp': 'ABC', 'tf': 15, 'label': 'ABC'}
        with mock.patch.object(app, 'trade_detail', return_value=d), mock.patch.object(app, 'trade_candles', return_value=c):
            sp = app.spark('swing:1')
        self.assertEqual(len(sp['pts']), app.SPARK_POINTS)
        self.assertEqual((sp['pts'][0][1], sp['pts'][-1][1]), (105.0, 399.0))       # from the entry bar to the latest
        self.assertEqual((sp['lp'], sp['up']), ('ABC', True))

    def test_options_need_their_premium(self):
        d = {'mode': 'options', 'status': 'open', 'entry': 50.0, 'stop': 37.5, 'target': 75.0, 'direction': 'bearish'}
        c = {'bars': self.bars(10), 'entry_k': 0, 'lp': '', 'label': "NIFTY (the option's own candles are not available)"}
        with mock.patch.object(app, 'trade_detail', return_value=d), mock.patch.object(app, 'trade_candles', return_value=c):
            self.assertEqual(app.spark('options:1')['pts'], [])
        with mock.patch.object(app, 'trade_detail', return_value={**d, 'status': 'won'}):
            self.assertEqual(app.spark('options:1'), {})


class Studies(unittest.TestCase):
    """A saved chart's indicators checked against the catalogue (niftywhale/studies.py)."""

    def test_settings_clamped_stepped_and_defaulted(self):
        got = studies.clean([{'id': 'a', 'type': 'bb', 'params': {'length': 9999, 'mult': '2.345', 'source': 'nope'}},
                             {'id': 'b', 'type': 'rsi', 'params': {'length': 'x', 'upper': float('nan')}},
                             {'id': 'c', 'type': 'vwap', 'params': {'anchor': 'week', 'band1': -3}}])
        self.assertEqual(got[0]['params'], {'length': 500, 'mult': 2.3, 'source': 'close'})
        self.assertEqual(got[1]['params'], {'length': 14, 'source': 'close', 'upper': 70, 'lower': 30})
        self.assertEqual(got[2]['params'], {'anchor': 'week', 'band1': 0.0, 'band2': 2.0})
        self.assertIsInstance(got[0]['params']['length'], int)

    def test_unknown_duplicate_and_bad_entries_dropped(self):
        got = studies.clean([{'id': 'a', 'type': 'sma'}, {'id': 'a', 'type': 'ema'}, {'id': 'bad id!', 'type': 'ema'},
                             {'id': 'z', 'type': 'nope'}, 'junk', {'type': 'ema'},
                             {'id': 'k', 'type': 'macd', 'colors': {'macd': '#123456', 'zzz': '#ffffff', 'signal': 'red'}, 'hidden': True}])
        self.assertEqual([(s['id'], s['type']) for s in got], [('a', 'sma'), ('k', 'macd')])
        self.assertEqual(got[1]['colors'], {'macd': '#123456'})
        self.assertTrue(got[1]['hidden'])
        self.assertEqual(studies.clean('junk'), [])
        # A kind that is not a string, an id or colour with a newline after it: dropped, not a crash or a saved newline.
        self.assertEqual(studies.clean([{'id': 'a', 'type': ['ema']}, {'id': 'b', 'type': {'k': 1}}, {'id': 'c\n', 'type': 'ema'}]), [])
        self.assertEqual(studies.clean([{'id': 'd', 'type': 'ema', 'colors': {'ma': '#123456\n'}}])[0]['colors'], {})

    def test_limits(self):
        many = [{'id': f'p{i}', 'type': 'rsi'} for i in range(6)] + [{'id': f'o{i}', 'type': 'ema'} for i in range(12)]
        got = studies.clean(many)
        self.assertEqual(len(got), studies.MAX_STUDIES)
        self.assertEqual(sum(s['type'] == 'rsi' for s in got), studies.MAX_PANELS)

    def test_every_kind_is_well_formed(self):
        for k in studies.CATALOG:
            self.assertIn(k['place'], ('price', 'panel'), k['key'])
            self.assertTrue(k['lines'], k['key'])
            for p in k['params']:
                if 'values' in p:
                    self.assertIn(p['default'], p['values'], (k['key'], p['key']))
                else:
                    self.assertTrue(p['min'] <= p['default'] <= p['max'], (k['key'], p['key']))
                    self.assertEqual(studies._value(p, p['default']), p['default'], (k['key'], p['key']))
            for f in k.get('fill', []):
                self.assertIn(f, [ln['key'] for ln in k['lines']], k['key'])


if __name__ == '__main__':
    unittest.main()
