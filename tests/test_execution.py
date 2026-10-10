"""
Live trading (niftywhale/execution.py, broker.py, app.py exec_*): the order a trade becomes (sized as the demo
account sizes, limits on the tick grid, a super order with its legs, a future's prices moved by its basis); the
guards and the evidence gate; nothing sent while real sending is off; shadow rows followed to their exit in
rupees; an offer placed once, by a signed-in click, and never twice (the correlation ID); Dhan's fills and
closes reconciled; a refused IP or the kill switch halting everything.

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

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import broker, execution, store  # noqa: E402

from tests.test_changelog import call  # noqa: E402
from tests.test_demo import paper_trade  # noqa: E402
from tests.test_regressions import _reset  # noqa: E402

IST = app.data.IST
NOW = datetime(2026, 10, 8, 11, 0, tzinfo=IST)          # a Thursday, in the session
ST = execution.settings_from({})
TRADE = {'ref': 'swing:7', 'mode': 'swing', 'symbol': 'ABC', 'side': 'long', 'entry': 126.5, 'stop': 122.68, 'target': 151.0}


class Plans(unittest.TestCase):
    def test_settings_are_clamped(self):
        st = execution.settings_from({'swing': 'auto', 'intraday': 'yolo', 'risk_pct': 50, 'max_open': 2.6, 'shorts': '1'})
        self.assertEqual((st['swing'], st['intraday'], st['risk_pct'], st['max_open'], st['shorts']), ('auto', 'off', 2.0, 3, True))

    def test_correlation_id_fits_dhan(self):
        cid = execution.correlation_id('intraday:12345678901234567890123:x/y')
        self.assertLessEqual(len(cid), 30)
        self.assertRegex(cid, r'^[A-Za-z0-9_-]+$')

    def test_limits_on_the_tick_grid(self):
        self.assertEqual(execution.entry_limit(126.5, True, 10), 126.65)      # 126.6265 up to 5 paise
        self.assertEqual(execution.entry_limit(126.5, False, 10), 126.35)     # 126.3735 down

    def test_a_long_is_one_super_order(self):
        p = execution.plan(TRADE, {'product': 'CNC', 'qty': 654}, {'security_id': '500'}, ST, 'C1')
        b = p['body']
        self.assertEqual((b['transactionType'], b['exchangeSegment'], b['productType'], b['orderType']), ('BUY', 'NSE_EQ', 'CNC', 'LIMIT'))
        self.assertEqual((b['quantity'], b['price'], b['stopLossPrice'], b['targetPrice'], b['trailingJump']), (654, 126.65, 122.65, 151.0, 0))
        self.assertEqual(b['correlationId'], 'nw-swing7')

    def test_a_short_trades_the_future_at_its_basis(self):
        short = {**TRADE, 'side': 'short', 'entry': 100.0, 'stop': 103.0, 'target': 90.0}
        p = execution.plan(short, {'product': 'FUT', 'qty': 500}, {'security_id': '9', 'segment': 'NSE_FNO', 'basis': 0.8}, ST, 'C1')
        b = p['body']
        self.assertEqual((b['transactionType'], b['productType'], b['exchangeSegment']), ('SELL', 'MARGIN', 'NSE_FNO'))
        self.assertEqual((b['price'], b['stopLossPrice'], b['targetPrice']), (100.65, 103.8, 90.8))   # 100.8 less 10 bps, down

    def test_a_plan_without_room_is_skipped(self):
        self.assertIn('skip', execution.plan({**TRADE, 'target': 126.6}, {'product': 'CNC', 'qty': 1}, {'security_id': '5'}, ST, 'C1'))
        self.assertIn('skip', execution.plan(TRADE, {'product': 'CNC', 'qty': 1}, {}, ST, 'C1'))

    def test_protection_holds_target_then_stop(self):
        p = {'qty': 10, 'segment': 'NSE_EQ', 'security_id': '5', 'target': 151.0, 'stop': 122.65, 'correlation_id': 'nw-swing7'}
        b = execution.protect_body(p, 'C1')
        self.assertEqual((b['orderFlag'], b['transactionType'], b['price'], b['triggerPrice1'], b['price1']), ('OCO', 'SELL', 151.0, 122.65, 122.0))


class Guards(unittest.TestCase):
    book = {'open': 0, 'today': 0, 'day_pnl': 0.0}

    def why(self, trade=TRADE, book=None, st=ST, last=126.6, open_=True, risk=2000):
        return ' | '.join(execution.guards({'risk': risk}, trade, book or self.book, st, 1_000_000, last, open_))

    def test_clear(self):
        self.assertEqual(self.why(), '')

    def test_each_limit(self):
        self.assertIn('market is closed', self.why(open_=False))
        self.assertIn('most at once', self.why(book={**self.book, 'open': 5}))
        self.assertIn("day's limit", self.why(book={**self.book, 'today': 5}))
        self.assertIn('daily', self.why(book={**self.book, 'day_pnl': -10_000}).replace("today's loss", 'daily'))
        self.assertIn('a trade may', self.why(risk=6000))
        self.assertIn('has run', self.why(last=127.5))
        self.assertIn('through the stop', self.why(last=122.0))
        self.assertIn('shorts are not live', self.why(trade={**TRADE, 'side': 'short', 'stop': 130, 'target': 110}, last=None))

    def test_the_gate(self):
        good = {'bt': {'trades': 90, 'avg_r': 0.08}, 'hold_r': 1.5, 'paper': {'trades': 40, 'total_r': 3.0}}
        self.assertTrue(execution.gate('swing', good)['ok'])
        g = execution.gate('swing', {**good, 'hold_r': None, 'paper': {'trades': 4, 'total_r': 1}})
        self.assertEqual([c['label'] for c in g['checks'] if not c['ok']], ['Backtest R on the last 3 months', 'Paper trades closed'])
        self.assertEqual(execution.hold_r([['2026-06-01', 2.0], ['2026-08-01', 3.5], ['2026-09-01', 1.0]], '2026-07-10'), -1.0)


class Broker(unittest.TestCase):
    def test_nothing_is_sent_while_sending_is_off(self):
        store.set_settings({'exec_send': '0'})
        with mock.patch.object(broker.requests, 'request') as req, mock.patch.object(broker.dhan, 'available', return_value=True):
            ok, _, err = broker.place_super({'x': 1})
            self.assertFalse(ok)
            self.assertIn('real sending is off', err)
            self.assertFalse(broker.cancel_super('1')[0])
            self.assertFalse(broker.exit_all()[0])
        req.assert_not_called()

    def test_calls_are_logged_without_the_token(self):
        store.set_settings({'exec_send': '1'})
        r = mock.Mock(ok=True, status_code=200, text='{"orderId": "11", "orderStatus": "PENDING"}')
        r.json.return_value = {'orderId': '11', 'orderStatus': 'PENDING'}
        with mock.patch.object(broker.requests, 'request', return_value=r), mock.patch.object(broker.dhan, 'available', return_value=True), \
                mock.patch.object(broker.dhan, '_headers', return_value={'access-token': 'SECRET'}):
            ok, ans, _ = broker.place_super({'quantity': 1})
        self.assertTrue(ok)
        last = store.broker_log(1)[0]
        self.assertEqual((last['path'], last['ok']), ('/super/orders', 1))
        self.assertNotIn('SECRET', str(last))
        store.set_settings({'exec_send': '0'})


class Flow(unittest.TestCase):
    def setUp(self):
        _reset()
        with store.connect() as c:
            c.execute('DELETE FROM exec_orders')
        store.set_settings({'exec_since': '2026-10-08T09:00:00+05:30', 'exec_send': '1', 'exec_halt': '',
                            'exec': {'swing': 'shadow', 'capital': 1_000_000}})
        self.patches = [mock.patch.object(app.data, 'now_ist', return_value=NOW),
                        mock.patch.object(app.dhan, 'available', return_value=True),
                        mock.patch.object(app.dhan, 'security_id', return_value='500'),
                        mock.patch.object(app.dhan, 'lot_size', return_value=None),
                        mock.patch.object(app.dhan, 'client_id', return_value='C1'),
                        mock.patch.object(app.dhan, 'quotes_full', side_effect=lambda q: {('NSE_EQ', 500): {'ltp': self.last}}),
                        mock.patch.object(app.notify, 'send', return_value=True),
                        # No test reaches Dhan: every read the reconciler and the view make answers here.
                        mock.patch.object(app.broker, 'super_orders', return_value=[]),
                        mock.patch.object(app.broker, 'positions', return_value=[]),
                        mock.patch.object(app.broker, 'holdings', return_value=[]),
                        mock.patch.object(app.broker, 'static_ip', return_value={'primaryIP': '203.0.113.7'}),
                        mock.patch.object(app.broker, 'funds', return_value=500_000.0),
                        mock.patch.object(broker.requests, 'request', side_effect=AssertionError('a test reached Dhan'))]
        for p in self.patches:
            p.start()
        self.last = 126.6
        self.zid = paper_trade('ABC', entry=126.5, stop=122.68, at='2026-10-08T10:58:00+05:30')

    def tearDown(self):
        for p in self.patches:
            p.stop()
        store.set_settings({'exec': {}, 'exec_since': '', 'exec_send': '0', 'exec_halt': ''})

    def level(self, lvl):
        store.set_settings({'exec': {'swing': lvl, 'capital': 1_000_000}})

    def row(self):
        return store.exec_orders()[0]

    def test_shadow_plans_and_follows_in_rupees(self):
        with mock.patch.object(app.broker, 'place_super') as send:
            app.exec_sync()
            send.assert_not_called()
        r = self.row()
        # 0.25 % of ₹10 lakh between 126.5 and 122.68: 654 shares.
        self.assertEqual((r['status'], r['qty'], r['body']['price'], r['body']['productType']), ('shadow', 654, 126.65, 'CNC'))
        with store.connect() as c:
            c.execute("UPDATE zones SET status = 'won', trigger = json_set(trigger, '$.exit', 151.0, '$.exit_time', "
                      "'2026-10-09T11:00:00+05:30', '$.r', 6.41) WHERE id = ?", (self.zid,))
        app.exec_sync()
        r = self.row()
        self.assertEqual(r['status'], 'shadow_closed')
        self.assertEqual(r['gross'], round((151.0 - 126.5) * 654, 2))
        self.assertLess(r['net'], r['gross'])

    def test_confirm_offers_then_places_once(self):
        self.level('confirm')
        app.exec_sync()
        r = self.row()
        self.assertEqual(r['status'], 'offered')
        with mock.patch.object(app.broker, 'order_by_correlation', return_value=None), \
                mock.patch.object(app.broker, 'place_super', return_value=(True, {'orderId': '77', 'orderStatus': 'PENDING'}, None)) as send:
            call(lambda: app.api_exec_offer(r['id']), '/api/exec/offer', {'action': 'place'})
            again = call(lambda: app.api_exec_offer(r['id']), '/api/exec/offer', {'action': 'place'})
        send.assert_called_once()
        self.assertEqual((self.row()['status'], self.row()['order_id']), ('placed', '77'))
        self.assertIn('no longer waiting', again['error'])

    def test_a_lost_answer_is_never_sent_twice(self):
        self.level('auto')
        with mock.patch.object(app.broker, 'order_by_correlation', return_value={'orderId': '55'}), \
                mock.patch.object(app.broker, 'place_super') as send:
            app.exec_sync()
        send.assert_not_called()
        self.assertEqual((self.row()['status'], self.row()['order_id']), ('placed', '55'))

    def test_a_guard_blocks_and_says_why(self):
        self.level('auto')
        self.last = 128.0                                      # ran 1.2 % past the entry
        with mock.patch.object(app.broker, 'place_super') as send:
            app.exec_sync()
        send.assert_not_called()
        self.assertEqual(self.row()['status'], 'blocked')
        self.assertIn('has run', self.row()['note'])

    def test_fills_and_closes_reconciled(self):
        self.level('auto')
        with mock.patch.object(app.broker, 'order_by_correlation', return_value=None), \
                mock.patch.object(app.broker, 'place_super', return_value=(True, {'orderId': '77'}, None)):
            app.exec_sync()
        legs = [{'legName': 'TARGET_LEG', 'orderStatus': 'TRADED', 'price': 151.0}, {'legName': 'STOP_LOSS_LEG', 'orderStatus': 'CANCELLED'}]
        so = {'orderId': '77', 'orderStatus': 'CLOSED', 'averageTradedPrice': 126.6, 'filledQty': 654, 'legDetails': legs}
        with mock.patch.object(app.broker, 'super_orders', return_value=[so]), \
                mock.patch.object(app.broker, 'positions', return_value=[]), mock.patch.object(app.broker, 'holdings', return_value=[]):
            app.exec_reconcile(app.exec_settings())
        r = self.row()
        self.assertEqual((r['status'], r['fill'], r['exit'], r['note']), ('closed', 126.6, 151.0, 'target'))
        self.assertEqual(r['gross'], round((151.0 - 126.6) * 654, 2))

    def test_a_refused_ip_halts_everything(self):
        self.level('auto')
        with mock.patch.object(app.broker, 'order_by_correlation', return_value=None), \
                mock.patch.object(app.broker, 'place_super', return_value=(False, None, 'DH-905 Invalid IP')):
            app.exec_sync()
        self.assertEqual(self.row()['status'], 'rejected')
        self.assertIn('static IP', app.exec_halted())

    def test_arming_needs_sending_and_evidence(self):
        store.set_settings({'exec_send': '0'})
        r = call(app.api_exec_settings, '/api/exec/settings', {'settings': {'swing': 'confirm'}})
        self.assertIn('real sending is off', r['error'])
        store.set_settings({'exec_send': '1'})
        r = call(app.api_exec_settings, '/api/exec/settings', {'settings': {'swing': 'confirm'}})
        self.assertIn('Backtest trades', r['error'])
        good = {'bt': {'trades': 90, 'avg_r': 0.08}, 'hold_r': 1.5, 'paper': {'trades': 40, 'total_r': 3.0}}
        with mock.patch.object(app, 'exec_evidence', return_value=good):
            r = call(app.api_exec_settings, '/api/exec/settings', {'settings': {'swing': 'confirm'}})
        self.assertEqual(r['levels']['swing'], 'confirm')

    def test_the_kill_switch(self):
        self.level('auto')
        with mock.patch.object(app.broker, 'order_by_correlation', return_value=None), \
                mock.patch.object(app.broker, 'place_super', return_value=(True, {'orderId': '77'}, None)):
            app.exec_sync()
        with mock.patch.object(app.broker, 'cancel_super', return_value=(True, None, None)) as cancel, \
                mock.patch.object(app.broker, 'exit_all', return_value=(True, None, None)) as flat:
            r = call(app.api_exec_kill, '/api/exec/kill', {'flatten': True})
        cancel.assert_called_once_with('77', 'ENTRY_LEG')
        flat.assert_called_once()
        self.assertEqual((r['levels']['swing'], self.row()['status']), ('shadow', 'cancelled'))
        self.assertIn('kill switch', r['halt'])

    def test_settings_api_cannot_touch_live_trading(self):
        r = call(app.api_settings, '/api/settings', {'exec_send': '1'})
        self.assertIn('own endpoints', r['error'])


if __name__ == '__main__':
    unittest.main()
