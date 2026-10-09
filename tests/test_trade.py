"""
One trade in full (/api/trade/<ref>): what a row in any trade table opens. A swing trade with its
setup, alerts, demo position and charges; an option idea; refs that are not trades; the candles of
the trade's window with the entry and exit candles marked.

Run:  python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

import app  # noqa: E402
from niftywhale import store  # noqa: E402

from tests.test_demo import paper_trade  # noqa: E402
from tests.test_regressions import _reset  # noqa: E402


class Trade(unittest.TestCase):
    def setUp(self):
        _reset()
        store.clear_demo()
        with store.connect() as c:
            c.execute('DELETE FROM oc_ideas')
            c.execute('DELETE FROM notices')
        store.set_settings({'demo': {}, 'demo_since': ''})
        app.TRADE_CANDLES.clear()
        self.client = app.app.test_client()

    def closed_swing(self):
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T09:00:00+05:30')):
            self.client.post('/api/demo/funds', json={'kind': 'deposit', 'amount': 1_000_000})
        zid = paper_trade()
        z = store._zone_rows([store.get_zone(zid)])[0]
        store.add_alert(z, 'entry', z['trigger'], True, 'entry message')
        app.demo_sync()
        with store.connect() as c:
            t = json.loads(c.execute('SELECT trigger FROM zones WHERE id = ?', (zid,)).fetchone()[0])
            t.update(exit=151.0, exit_time='2026-10-08T14:00:00+05:30', r=6.4)
            c.execute("UPDATE zones SET trigger = ?, status = 'won' WHERE id = ?", (json.dumps(t), zid))
        app.demo_sync()
        return zid

    def test_swing_trade_in_full(self):
        zid = self.closed_swing()
        d = self.client.get(f'/api/trade/swing:{zid}').get_json()
        self.assertEqual((d['symbol'], d['status'], d['word'], d['r']), ('ABC', 'won', 'Target hit', 6.4))
        self.assertEqual((d['entry'], d['stop'], d['exit']), (126.5, 122.68, 151.0))
        self.assertEqual(d['minutes'], 240)
        self.assertAlmostEqual(d['rr'], round(24.5 / 3.82, 2))
        self.assertEqual(d['setup']['zone_low'], store.get_zone(zid)['zone_low'])
        self.assertEqual([a['kind'] for a in d['alerts']], ['entry'])
        self.assertEqual((d['demo']['status'], d['demo']['qty']), ('closed', 1976))
        self.assertGreater(d['demo']['charges_detail']['stt'], 0)

    def test_option_idea_and_not_trades(self):
        iid = store.add_idea({'symbol': 'NIFTY', 'expiry': '2026-10-13', 'strike': 22450.0, 'side': 'CE', 'direction': 'long',
                              'created_at': '2026-10-09T10:00:00+05:30', 'session': '2026-10-09', 'status': 'lost',
                              'entry': 140.0, 'stop': 105.0, 'target': 210.0, 'spot': 22400.0, 'level': 22300.0, 'lot': 65,
                              'last': 100.0, 'r_open': -1.14, 'reason': 'put writers leaning in'})
        store.update_idea(iid, exit=100.0, exit_time='2026-10-09T11:00:00+05:30', r=-1.14, note='stop')
        d = self.client.get(f'/api/trade/options:{iid}').get_json()
        self.assertEqual((d['word'], d['option']['side'], d['option']['underlying']), ('Stopped out', 'CE', 'IDX:13'))
        self.assertEqual(d['rr'], 2.0)
        self.assertEqual(self.client.get('/api/trade/options:999999').status_code, 404)
        self.assertEqual(self.client.get('/api/trade/nope:1').status_code, 404)
        zid = paper_trade(status='watching')                     # a zone that never traded
        with store.connect() as c:
            c.execute('UPDATE zones SET trigger = NULL WHERE id = ?', (zid,))
        self.assertEqual(self.client.get(f'/api/trade/swing:{zid}').status_code, 404)

    def test_candles_mark_entry_and_exit(self):
        zid = self.closed_swing()
        idx = pd.date_range('2026-10-07 09:15', periods=26, freq='15min', tz=app.data.IST).append(
            pd.date_range('2026-10-08 09:15', periods=26, freq='15min', tz=app.data.IST))
        f = pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0, 'Close': 100.5, 'Volume': 0}, index=idx)
        with mock.patch.object(app.dhan, 'available', return_value=True), \
             mock.patch.object(app.dhan, 'security_id', return_value='123'), \
             mock.patch.object(app.dhan, 'chart_candles', return_value=f) as cc, \
             mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T16:00:00+05:30')):
            c = self.client.get(f'/api/trade/swing:{zid}/candles').get_json()
        self.assertEqual(cc.call_args[0][:4], ('123', 'NSE_EQ', 'EQUITY', 15))
        self.assertEqual(c['tf'], 15)
        self.assertEqual(len(c['bars']), 52)                     # the session before the entry, and the entry's
        self.assertEqual(c['bars'][c['entry_k']]['d'], '10-08 10:00')
        self.assertEqual(c['bars'][c['exit_k']]['d'], '10-08 14:00')
        with mock.patch.object(app.dhan, 'available', return_value=False):
            app.TRADE_CANDLES.clear()
            self.assertIn('needs Dhan', self.client.get(f'/api/trade/swing:{zid}/candles').get_json()['note'])


if __name__ == '__main__':
    unittest.main()
