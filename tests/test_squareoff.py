"""
Squaring off by hand (app.py, square off): open paper trades and demo positions closed now at the live
price, one, a selection or all; a paper trade's demo position following at the same price; a demo position
closed alone leaving its trade running; nothing closed outside trading hours or without a live price; a
trade the watcher closed meanwhile left alone; your exits marked, and kept out of the autopilot's judging.

Run:  python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import store  # noqa: E402

from tests.test_changelog import call  # noqa: E402
from tests.test_demo import paper_trade  # noqa: E402
from tests.test_regressions import _reset  # noqa: E402

IST = app.data.IST
OPEN_AT = datetime(2026, 10, 8, 11, 0, tzinfo=IST)          # a Thursday, in the session


class SquareOff(unittest.TestCase):
    def setUp(self):
        _reset()
        store.clear_demo()
        store.set_settings({'demo': {}, 'demo_since': '2026-10-08T09:00:00+05:30'})
        store.add_demo_ledger('deposit', 1_000_000, '', '2026-10-08T09:00:00+05:30')
        self.zid = paper_trade('ABC', entry=126.5, stop=122.68, at='2026-10-08T10:00:00+05:30')
        app.demo_sync()
        self.now = mock.patch.object(app.data, 'now_ist', return_value=OPEN_AT)
        self.now.start()

    def tearDown(self):
        self.now.stop()
        for k in ('ABC', 'FOSTOCK'):
            app.charts.FEED._px.pop(k, None)
            app.charts.FEED._ticks.pop(k, None)

    def price(self, px, sym='ABC'):
        app.charts.FEED.add({sym: px})

    def post(self, body):
        return call(app.api_squareoff, '/api/squareoff', body)

    def trade(self):
        z = store.get_zone(self.zid)
        return z['status'], json.loads(z['trigger'])

    def position(self):
        return next(p for p in store.demo_positions() if p['ref'] == f'swing:{self.zid}')

    def test_listed_with_the_live_price(self):
        self.price(130.32)
        d = call(app.api_squareoff_list, '/api/squareoff')
        [t], [p] = d['paper'], d['demo']
        self.assertEqual((t['ref'], t['price'], t['r'], t['why']), (f'swing:{self.zid}', 130.32, 1.0, None))
        self.assertEqual((p['ref'], p['price'], p['why']), (f'swing:{self.zid}', 130.32, None))
        self.assertEqual(p['unreal'], round((130.32 - 126.5) * p['qty'], 2))

    def test_a_paper_trade_and_its_demo_position_close_together(self):
        self.price(130.32)
        r = self.post({'paper': [f'swing:{self.zid}']})
        self.assertEqual((r['closed'], r['failed']), (1, 0))
        status, t = self.trade()
        self.assertEqual((status, t['exit'], t['r'], t['by']), ('closed', 130.32, 1.0, 'you'))
        p = self.position()
        self.assertEqual((p['status'], p['exit'], p['note']), ('closed', 130.32, store.MANUAL_NOTE))
        self.assertEqual(r['paper'], [])                                   # nothing left open
        lt = store.live_trades('swing')[0]
        self.assertEqual((lt['status'], lt['r'], lt['manual']), ('closed', 1.0, True))
        self.assertEqual(app.swing_trades_state()['stats']['trades'], 1)   # it counts in your journal

    def test_a_demo_position_alone_leaves_its_trade_running(self):
        self.price(124.0)
        pid = self.position()['id']
        r = self.post({'demo': [pid]})
        self.assertEqual(r['closed'], 1)
        p = self.position()
        self.assertEqual((p['status'], p['exit'], p['note']), ('closed', 124.0, store.MANUAL_NOTE))
        self.assertLess(p['net'], p['gross'])                              # charges on the way out
        self.assertEqual(self.trade()[0], 'triggered')
        app.demo_sync()
        self.assertEqual(self.position()['status'], 'closed')             # the sync leaves it closed

    def test_square_off_all(self):
        other = paper_trade('XYZ', entry=50, stop=48, at='2026-10-08T10:30:00+05:30')
        app.demo_sync()
        app.charts.FEED.add({'ABC': 128.0, 'XYZ': 51.0})
        try:
            r = self.post({'all': 'both'})
            self.assertEqual((r['closed'], r['failed']), (4, 0))          # two trades, two positions
            self.assertEqual({store.get_zone(z)['status'] for z in (self.zid, other)}, {'closed'})
            self.assertEqual(store.demo_positions('open'), [])
        finally:
            app.charts.FEED._px.pop('XYZ', None)
            app.charts.FEED._ticks.pop('XYZ', None)

    def test_closed_meanwhile_is_left_alone(self):
        self.price(130.0)
        items = app.squareoff_items(OPEN_AT)
        store.update_zone(self.zid, status='won')                         # the watcher got there first
        r = app._close_paper(items['paper'][0], OPEN_AT)
        self.assertEqual(r['ok'], False)
        self.assertIn('meanwhile', r['why'])
        self.assertEqual(store.get_zone(self.zid)['status'], 'won')

    def test_not_outside_trading_hours_or_without_a_price(self):
        r = self.post({'paper': [f'swing:{self.zid}']})                   # no live price
        self.assertEqual((r['closed'], r['results']['paper'][0]['why']), (0, 'no live price (the Dhan feed gives it)'))
        self.price(130.0)
        with mock.patch.object(app.data, 'now_ist', return_value=datetime(2026, 10, 10, 11, 0, tzinfo=IST)):
            self.assertEqual(self.post({'paper': [f'swing:{self.zid}']})['results']['paper'][0]['why'], 'the market is closed')
        late = datetime(2026, 10, 8, 15, 20, tzinfo=IST)
        self.assertIsNone(app._tradable_why('swing', 'ABC', late))                       # no F&O: trades to 15:30
        with mock.patch.object(app.universe, 'fo_symbols', return_value=frozenset({'FOSTOCK'})):
            self.assertEqual(app._tradable_why('swing', 'FOSTOCK', late), 'FOSTOCK stopped trading at 15:15')
        self.assertIsNone(app._tradable_why('options', 'NIFTY', late))                   # F&O to 15:40
        self.assertEqual(self.trade()[0], 'triggered')

    def test_bad_requests(self):
        self.assertIn('error', self.post({}))
        self.assertIn('error', self.post({'all': 'everything'}))
        self.assertIn('error', self.post({'paper': 'swing:1'}))
        self.assertIn('error', self.post({'demo': ['1']}))
        r = self.post({'paper': ['swing:999999'], 'demo': [999999]})
        self.assertEqual([x['why'] for x in r['results']['paper'] + r['results']['demo']],
                         ['not an open paper trade', 'not an open demo position'])

    def test_your_exits_do_not_judge_the_rules(self):
        self.price(120.0)
        self.post({'paper': [f'swing:{self.zid}']})
        from niftywhale import lab
        seen = {}
        with mock.patch.object(lab.autopilot, 'pause_due', side_effect=lambda trades, pol, mode: seen.setdefault('n', len(trades))), \
                mock.patch.object(lab, 'tg'):
            lab.shadow_and_guards('swing', mock.Mock(ready=lambda: False))
        self.assertEqual(seen['n'], 0)


if __name__ == '__main__':
    unittest.main()
