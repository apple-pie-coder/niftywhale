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

    def test_a_bad_value_keeps_the_current_one(self):
        cur = execution.settings_from({'swing': 'shadow', 'buffer_bps': 15, 'risk_pct': 0.5})
        new = execution.settings_from({'buffer_bps': 'x', 'swing': 'nope', 'risk_pct': 0.3}, base=cur)
        self.assertEqual((new['buffer_bps'], new['swing'], new['risk_pct']), (15.0, 'shadow', 0.3))

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


class FixedLots(unittest.TestCase):
    st = {**execution.settings_from({}), 'sizing': 'lots'}

    def test_each_instrument(self):
        opt = execution.fixed_size({'mode': 'options', 'entry': 120.0, 'stop': 90.0}, 1, 75, 1e6, self.st)
        self.assertEqual((opt['product'], opt['qty'], opt['lots'], opt['margin'], opt['risk']), ('OPT', 75, 1, 9000.0, 2250.0))
        fut = execution.fixed_size({'mode': 'swing', 'side': 'short', 'entry': 1000.0, 'stop': 1030.0}, 2, 250, 1e6, self.st)
        self.assertEqual((fut['product'], fut['qty'], fut['margin']), ('FUT', 500, 100000.0))       # 20 % of the contract
        mis = execution.fixed_size({'mode': 'intraday', 'entry': 500.0, 'stop': 497.0}, 1, None, 1e6, self.st)
        self.assertEqual((mis['product'], mis['qty'], mis['margin'], mis['risk']), ('MIS', 1, 100.0, 3.0))
        cnc = execution.fixed_size({'mode': 'swing', 'entry': 126.5, 'stop': 122.68}, 1, None, 1e6, self.st)
        self.assertEqual((cnc['product'], cnc['qty'], cnc['margin']), ('CNC', 1, 126.5))

    def test_what_cannot_be_paid_for_is_skipped(self):
        r = execution.fixed_size({'mode': 'options', 'entry': 120.0, 'stop': 90.0}, 1, 75, 1000.18, self.st)
        self.assertIn('1 lot need ₹9,000.00, more than the ₹1,000.18 available', r['skip'])
        self.assertIn('futures', execution.fixed_size({'mode': 'swing', 'side': 'short', 'entry': 10.0, 'stop': 11.0}, 1, None, 1e6, self.st)['skip'])

    def test_the_setting(self):
        st = execution.settings_from({'sizing': 'lots', 'lots': 3.4})
        self.assertEqual((st['sizing'], st['lots']), ('lots', 3))
        self.assertEqual(execution.settings_from({'sizing': 'all-in', 'lots': 500}, base=st)['sizing'], 'lots')
        self.assertEqual(execution.settings_from({'lots': 500})['lots'], 100)


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
        good = {'bt': {'trades': 90}, 'rules_ok': True, 'account': '1 lot a trade',
                'live': {'account': '1 lot a trade', 'avg_r': 0.08, 'hold_r': 1.5}, 'paper': {'trades': 40, 'total_r': 3.0}}
        self.assertTrue(execution.gate('swing', good)['ok'])
        g = execution.gate('swing', {**good, 'live': {**good['live'], 'hold_r': None}, 'paper': {'trades': 4, 'total_r': 1}})
        self.assertEqual([c['label'] for c in g['checks'] if not c['ok']],
                         ['Backtest R on the last 3 months, at your size', 'Paper trades closed on these rules'])
        # Evidence about other rules, or costed at another size, does not count.
        g = execution.gate('swing', {**good, 'rules_ok': False, 'account': '2 lots a trade'})
        self.assertEqual([c['label'] for c in g['checks'] if not c['ok']],
                         ['Backtest on the rules in force', 'Backtest R a trade, after costs at your size',
                          'Backtest R on the last 3 months, at your size'])
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

    def test_an_error_inside_a_list_is_a_refusal(self):
        store.set_settings({'exec_send': '0'})
        r = mock.Mock(ok=True, status_code=200, text='[{"message":"Something went wrong","status":"ERROR"}]')
        r.json.return_value = [{'message': 'Something went wrong', 'status': 'ERROR'}]
        with mock.patch.object(broker.requests, 'request', return_value=r), mock.patch.object(broker.dhan, '_headers', return_value={}):
            ok, _, err = broker._call('GET', '/ip/getIP')
            self.assertIsNone(broker.static_ip())
        self.assertFalse(ok)
        self.assertIn('Something went wrong', err)
        self.assertEqual(store.broker_log(1)[0]['ok'], 0)

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

    def test_fixed_lots_take_one_share(self):
        store.set_settings({'exec': {'swing': 'shadow', 'capital': 1_000_000, 'sizing': 'lots', 'lots': 1}})
        app.exec_sync()
        r = self.row()
        self.assertEqual((r['status'], r['qty'], r['body']['quantity']), ('shadow', 1, 1))

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
        good = {'bt': {'trades': 90}, 'rules_ok': True, 'account': 'x', 'live': {'account': 'x', 'avg_r': 0.08, 'hold_r': 1.5},
                'paper': {'trades': 40, 'total_r': 3.0}}
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


class LiveBook(unittest.TestCase):
    """What the limits see. Every unfinished row however old (a swing holding outlives the newest 500 rows), and
    the follow-up goes on with every mode off; Shadow meets the guards Automatic would, on its own book; money tied
    up is margin, not the full value, and not counted twice against Dhan's balance; the daily loss limit measures
    what today lost (open rows marked, what they carried in left out) against the capital the day started with."""
    tearDown, row = Flow.tearDown, Flow.row                              # Flow's world, without running its tests again

    def setUp(self):
        Flow.setUp(self)
        app._exec_cache['funds'] = (0.0, None)                           # Dhan's balance is read afresh in each test
        store.set_kv('exec_day', '')

    def add(self, ref, status, level='auto', **kw):
        row = {'ref': ref, 'mode': 'swing', 'symbol': ref.split(':')[-1].upper(), 'side': 'long', 'level': level,
               'status': status, 'product': 'CNC', 'qty': 10, 'price': 100.0, 'entry_time': '2026-10-08T10:00:00+05:30', **kw}
        return store.add_exec_order(row)

    def age(self, *ids):
        with store.connect() as c:
            c.execute(f"UPDATE exec_orders SET updated = '2026-09-01T10:00:00' WHERE id IN ({','.join('?' * len(ids))})", ids)

    def test_an_old_holding_is_never_out_of_sight(self):
        held = self.add('swing:old', 'open', entry_time='2026-09-01T10:00:00+05:30')
        filler = [self.add(f'swing:f{k}', 'skipped', entry_time='2026-10-07T10:00:00+05:30') for k in range(600)]
        self.age(held, *filler)
        self.assertNotIn(held, [r['id'] for r in store.exec_orders()])          # the newest 500 had lost it
        rows = app.exec_rows()
        self.assertIn(held, [r['id'] for r in rows])
        self.assertEqual(len(rows), 1)
        self.assertEqual(app.exec_book(rows)['open'], 1)

    def test_every_mode_off_still_follows_what_is_open(self):
        self.add('swing:old', 'open')
        store.set_settings({'exec': {'swing': 'off', 'capital': 1_000_000}})
        with mock.patch.object(app, 'exec_reconcile', return_value={}) as rec:
            app.exec_sync()
        rec.assert_called_once()
        self.assertEqual([r['ref'] for r in store.exec_orders()], ['swing:old'])   # nothing new taken

    def test_shadow_meets_the_limits(self):
        store.set_settings({'exec': {'swing': 'shadow', 'capital': 1_000_000, 'max_open': 1}})
        self.add('swing:other', 'shadow', level='shadow')
        app.exec_sync()
        r = next(r for r in store.exec_orders() if r['ref'] != 'swing:other')
        self.assertEqual(r['status'], 'blocked')
        self.assertTrue(r['note'].startswith('Automatic would not have sent it'))
        self.assertIn('positions open', r['note'])

    def test_shadow_meets_the_price_checks(self):
        self.last = 128.0                                                      # ran 1.2 % past the entry
        app.exec_sync()
        self.assertEqual(self.row()['status'], 'blocked')
        self.assertIn('has run', self.row()['note'])

    def test_money_tied_up(self):
        st = execution.settings_from({})
        margin = lambda **kw: app.exec_margin({'mode': 'swing', 'qty': 10, 'price': 100.0, **kw}, st)   # noqa: E731
        self.assertEqual((margin(product='CNC'), margin(product='INTRADAY'), margin(product='MARGIN'),
                          margin(product='MARGIN', mode='options')), (1000.0, 200.0, 200.0, 1000.0))
        for k, status in enumerate(('offered', 'placing', 'placed', 'open')):
            self.add(f'swing:t{k}', status)
        self.add('swing:s', 'shadow', level='shadow')
        rows = store.exec_orders()
        # Dhan's available balance already leaves out what its orders hold: only the offers and sends in flight.
        self.assertEqual(app.exec_tied(rows, execution.settings_from({'capital': 0}), shadow=False), 2000.0)
        self.assertEqual(app.exec_tied(rows, execution.settings_from({'capital': 500_000}), shadow=False), 4000.0)
        self.assertEqual(app.exec_tied(rows, st, shadow=True), 1000.0)

    def test_the_day_starts_once(self):
        store.set_kv('exec_day', '')
        st = execution.settings_from({'capital': 0})
        carried = self.add('swing:held', 'open', entry_time='2026-10-07T10:00:00+05:30', unreal=-500.0)
        shut = self.add('swing:shut', 'open', entry_time='2026-10-07T10:00:00+05:30', unreal=-400.0)
        day = app.exec_day(st, store.exec_orders())
        self.assertEqual(day['capital'], 500_000 + 2 * 1000.0)                 # Dhan's balance plus what its orders hold
        self.assertEqual(day['base'], {str(carried): -500.0, str(shut): -400.0})
        app._exec_cache['funds'] = (0.0, None)
        with mock.patch.object(app.broker, 'funds', return_value=100_000.0):   # money out at Dhan mid-day
            self.assertEqual(app.exec_day(st)['capital'], 502_000.0)
        # Today: the held one fell 200 more, the other closed 900 down in all (500 of it today).
        store.update_exec_order(carried, unreal=-700.0)
        store.update_exec_order(shut, status='closed', net=-900.0, exit_time='2026-10-08T10:30:00+05:30')
        self.assertEqual(app.exec_book(store.exec_orders(), day=day)['day_pnl'], -700.0)

    def test_a_day_without_dhan_asks_again(self):
        st = execution.settings_from({'capital': 0})
        store.set_kv('exec_day', '{"day": "2026-10-08", "capital": 0, "base": {"9": -50}}')
        with mock.patch.object(app.broker, 'funds', return_value=300_000.0):
            app._exec_cache['funds'] = (0.0, None)
            day = app.exec_day(st, [])
        self.assertEqual((day['capital'], day['base']), (300_000.0, {'9': -50}))

    def test_open_rows_are_marked(self):
        sid = self.add('swing:m', 'shadow', level='shadow', qty=654)
        rid = self.add('swing:r', 'open', qty=10, fill=126.0)
        rows = store.exec_orders()
        trade = {'entry': 126.5, 'stop': 122.68, 'last': 130.0}
        app.exec_mark(rows, {'swing:m': trade, 'swing:r': trade})
        got = {r['id']: r['unreal'] for r in store.exec_orders()}
        self.assertEqual((got[sid], got[rid]), (round(3.5 * 654, 2), 40.0))    # a real row from its fill

class Evidence(unittest.TestCase):
    """Live trading's evidence is about what would trade: costs at the size live trading trades (a flat brokerage
    weighs far more on one lot than on the nominal ₹10 lakh account), an option's charges over its whole lot, the
    rules in force, and the paper trades taken since those rules were set."""
    OPT = {'symbol': 'NIFTY', 'entry': 100.0, 'stop': 60.0, 'exit': 140.0, 'status': 'won', 'r': 1.0, 'lot': 75}

    def test_costs_at_your_size(self):
        from niftywhale import costs
        one = costs.live_account({'sizing': 'lots', 'lots': 1})
        self.assertEqual(one['label'], '1 lot a trade (a share for stocks)')
        self.assertEqual(costs.live_account({'risk_pct': 0.5}, 200_000)['label'], '0.5% risk a trade on ₹200,000')
        self.assertEqual(costs.live_account({'risk_pct': 0.5, 'capital': 300_000}, 200_000)['capital'], 300_000)
        nominal = costs.cost_r(self.OPT, 'options', lot=75)
        at_one = costs.cost_r(self.OPT, 'options', lot=75, account=one)
        self.assertGreater(at_one, nominal)                      # the same trade costs more R on one lot
        # Swing at one share: the DP charge alone is a large part of R.
        self.assertGreater(costs.cost_r({'entry': 126.5, 'stop': 122.68, 'exit': 151.0, 'status': 'won'}, 'swing', account=one), 1.0)

    def test_an_option_is_costed_over_its_lot(self):
        from niftywhale import costs
        with_lot = costs.apply([self.OPT], 'options')[0]['cost_r']
        self.assertEqual(with_lot, round(costs.cost_r(self.OPT, 'options', lot=75), 4))
        self.assertLess(with_lot, costs.cost_r({**self.OPT, 'lot': None}, 'options'))   # one unit carried the whole brokerage

    def test_rules_since_follows_a_change(self):
        store.set_kv('rules_since:rules', '2026-01-01T00:00:00+05:30')
        store.set_settings({'rules': store.settings()['rules']})                          # saved again, unchanged
        self.assertEqual(store.rules_since('rules'), '2026-01-01T00:00:00+05:30')
        store.set_settings({'rules': {'min_rr': 2.5}})
        self.assertGreater(store.rules_since('rules'), '2026-01-01T00:00:00+05:30')
        store.set_settings({'rules': {}})

    def test_paper_counts_from_the_rules_in_force(self):
        _reset()
        paper_trade('ABC', status='won', exit_=151.0, at='2026-10-06T10:58:00+05:30', r=6.4)
        paper_trade('DEF', status='won', exit_=151.0, at='2026-10-08T10:58:00+05:30', r=6.4)
        store.set_kv('rules_since:rules', '2026-10-07T09:00:00+05:30')
        with mock.patch('niftywhale.lab.current_thresholds', return_value={'min_rr': 2.0}), \
                mock.patch.object(app.store, 'latest_run', return_value={'params': {'min_rr': 3.0}, 'summary': {'trades': 90}}):
            ev = app.exec_evidence('swing')
        self.assertEqual((ev['paper']['trades'], ev['rules_ok']), (1, False))


class Records(unittest.TestCase):
    """Totals over the whole record, not the newest rows; the deepest fall in the order trades closed."""
    def test_totals_count_every_row(self):
        with store.connect() as c:
            c.execute('DELETE FROM exec_orders')
            c.executemany("INSERT INTO exec_orders (ref, level, status, net) VALUES (?, 'shadow', 'shadow_closed', 10)",
                          [(f'swing:{k}',) for k in range(700)])
        self.assertEqual(store.exec_totals()['shadow'], {'trades': 700, 'net': 7000.0})
        with store.connect() as c:
            c.execute('DELETE FROM exec_orders')

    def test_demo_positions_are_not_capped(self):
        with store.connect() as c:
            c.execute('DELETE FROM demo_positions')
            c.executemany("INSERT INTO demo_positions (ref, status, net, entry_time) VALUES (?, 'closed', 1, '2026-10-08')",
                          [(f'x:{k}',) for k in range(5_050)])
        self.assertEqual(len(store.demo_positions()), 5_050)
        with store.connect() as c:
            c.execute('DELETE FROM demo_positions')

    def test_drawdown_in_exit_order(self):
        from niftywhale import perf
        t = lambda e, x, r: {'status': 'won' if r > 0 else 'lost', 'r': r, 'entry_time': e, 'exit_time': x}   # noqa: E731
        # Entered A, B, C; closed B (-1), C (-1), A (+3): two losses in a row before the win.
        trades = [t('2026-10-01', '2026-10-09', 3.0), t('2026-10-02', '2026-10-03', -1.0), t('2026-10-04', '2026-10-05', -1.0)]
        self.assertEqual(perf.summary(trades)['max_drawdown'], -2.0)
        self.assertEqual(perf.summary(trades)['first'], '2026-10-01')

if __name__ == '__main__':
    unittest.main()
