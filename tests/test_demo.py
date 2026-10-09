"""
Demo funds (niftywhale/demo.py, app.demo_sync): sizing on the instrument each trade would use
(shares for delivery and intraday, futures lots for swing shorts, option lots), the skip reasons,
Dhan's charges, settling longs and shorts, and the account mirroring the app's trades: a position
per trade sized from the account at its entry, marked while open, settled when the trade closes,
margin released, withdrawals limited to free funds, reset.

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
from niftywhale import data, demo, store  # noqa: E402

from tests.test_regressions import _reset, _zone  # noqa: E402

ST = demo.settings_from({})


class Sizing(unittest.TestCase):
    def test_swing_long_delivery(self):
        # Rs 10 lakh, 1 % risk = Rs 10,000; 5 a share at risk -> 2,000 shares, but 25 % cap = Rs 2.5 lakh -> 2,000 x 100 fits.
        s = demo.size({'mode': 'swing', 'side': 'long', 'entry': 100, 'stop': 95}, 1_000_000, 1_000_000, ST)
        self.assertEqual((s['product'], s['qty'], s['margin'], s['risk']), ('CNC', 2000, 200000.0, 10000.0))
        s = demo.size({'mode': 'swing', 'side': 'long', 'entry': 100, 'stop': 99}, 1_000_000, 1_000_000, ST)
        self.assertEqual(s['qty'], 2500)                                 # the 25 % cap binds before the risk
        s = demo.size({'mode': 'swing', 'side': 'long', 'entry': 100, 'stop': 99}, 1_000_000, 30_000, ST)
        self.assertEqual(s['qty'], 300)                                  # only free funds can be used

    def test_swing_short_is_futures_lots(self):
        t = {'mode': 'swing', 'side': 'short', 'entry': 260, 'stop': 268}
        s = demo.size(t, 1_000_000, 1_000_000, ST, lot=1150)
        # One lot risks 8 x 1150 = 9,200 <= 10,000: one lot; margin 20 % of 260 x 1150.
        self.assertEqual((s['product'], s['lots'], s['qty'], s['margin']), ('FUT', 1, 1150, 59800.0))
        self.assertIn('needs its futures', demo.size(t, 1_000_000, 1_000_000, ST, lot=None)['skip'])
        self.assertIn('one lot risks', demo.size({**t, 'stop': 280}, 1_000_000, 1_000_000, ST, lot=1150)['skip'])

    def test_intraday_mis(self):
        s = demo.size({'mode': 'intraday', 'side': 'short', 'entry': 500, 'stop': 502}, 200_000, 200_000, ST)
        # risk 2,000 / 2 = 1,000 shares; cap 50,000 x 5 / 500 = 500 shares; margin 500 x 500 / 5.
        self.assertEqual((s['product'], s['qty'], s['margin']), ('MIS', 500, 50000.0))

    def test_option_lots(self):
        t = {'mode': 'options', 'side': 'long', 'entry': 120, 'stop': 90}
        s = demo.size(t, 1_000_000, 1_000_000, ST, lot=75)
        # risk 10,000 / (30 x 75) = 4 lots; premium 4 x 75 x 120 = 36,000.
        self.assertEqual((s['product'], s['lots'], s['qty'], s['margin']), ('OPT', 4, 300, 36000.0))
        self.assertIn('one lot costs', demo.size(t, 1_000_000, 5_000, ST, lot=75)['skip'])
        self.assertIn('no demo funds', demo.size(t, 0, 0, ST, lot=75)['skip'])

    def test_settings_clamped(self):
        st = demo.settings_from({'risk_pct': 99, 'intraday_leverage': 0, 'options': '0', 'junk': 1})
        self.assertEqual((st['risk_pct'], st['intraday_leverage'], st['options']), (10.0, 1.0, False))
        self.assertNotIn('junk', st)


class Charges(unittest.TestCase):
    def test_delivery_round_trip(self):
        c = demo.charges('CNC', 100_000, 110_000)
        self.assertEqual((c['brokerage'], c['stt'], c['stamp'], c['dp']), (0.0, 210.0, 15.0, 12.5))
        # NSE Rs 306.99 a crore a side on 2.1 lakh; SEBI Rs 10 a crore; GST on all but STT and stamp.
        self.assertEqual((c['exchange'], c['sebi'], c['ipft']), (6.45, 0.21, 0.0))
        self.assertEqual(c['gst'], round((6.44679 + 0.21 + 0.00021 + 12.5) * 0.18, 2))
        self.assertAlmostEqual(c['total'], sum(c[k] for k in demo.CHARGE_KEYS), places=6)

    def test_options_and_intraday(self):
        c = demo.charges('OPT', 36_000, 48_000)
        self.assertEqual((c['brokerage'], c['stt'], c['stamp']), (40.0, 72.0, 1.08))  # Rs 20 an order; 0.15 % of the premium sold
        self.assertEqual(c['exchange'], round(84_000 * 3552.99 / 1e7, 2))
        c = demo.charges('MIS', 10_000, 10_400)
        self.assertEqual((c['brokerage'], c['stt']), (6.12, 2.6))          # 0.03 % under the Rs 20 cap; 0.025 % on the sell

    def test_futures_and_ipft(self):
        c = demo.charges('FUT', 10_000_000, 10_000_000)                   # a crore each way
        self.assertEqual((c['brokerage'], c['stt'], c['exchange'], c['ipft'], c['sebi'], c['stamp']),
                         (40.0, 5000.0, 365.98, 0.02, 20.0, 200.0))       # STT 0.05 % on the sell since 2026-04-01

    def test_monthly_expiries_and_rolls(self):
        from datetime import date
        self.assertEqual(demo.monthly_expiries(date(2026, 10, 1), date(2026, 12, 31)),
                         [date(2026, 10, 27), date(2026, 11, 24), date(2026, 12, 29)])   # last Tuesdays
        self.assertEqual(demo.rollovers('2026-10-20T10:00:00+05:30', '2026-11-05T11:00:00+05:30'), 1)
        self.assertEqual(demo.rollovers('2026-10-27T10:00:00+05:30', '2026-11-05T11:00:00+05:30'), 0)  # opened on expiry: next month
        self.assertEqual(demo.rollovers('2026-10-20T10:00:00+05:30', '2026-10-27T14:00:00+05:30'), 0)  # closed on expiry
        self.assertEqual(demo.rollovers('2026-10-20T10:00:00+05:30', '2026-12-01T11:00:00+05:30'), 2)
        pos = {'qty': 1150, 'entry': 260, 'side': 'short', 'product': 'FUT', 'entry_time': '2026-10-20T10:00:00+05:30'}
        held = demo.settle(pos, 250, exit_time='2026-12-01T11:00:00+05:30')
        plain = demo.settle(pos, 250, exit_time='2026-10-26T11:00:00+05:30')
        roll = demo.charges('FUT', 1150 * 260, 1150 * 260)['total']
        self.assertEqual(held['charges_detail']['rollovers'], 2)
        self.assertAlmostEqual(held['charges'], plain['charges'] + 2 * roll, delta=0.05)
        self.assertNotIn('rollovers', plain['charges_detail'])

    def test_settle_long_and_short(self):
        r = demo.settle({'qty': 100, 'entry': 100, 'side': 'long', 'product': 'CNC'}, 110)
        self.assertEqual(r['gross'], 1000.0)
        self.assertEqual(r['net'], round(1000 - r['charges'], 2))
        r = demo.settle({'qty': 1150, 'entry': 260, 'side': 'short', 'product': 'FUT'}, 250, with_charges=False)
        self.assertEqual((r['gross'], r['net']), (11500.0, 11500.0))
        self.assertEqual(demo.mark({'qty': 10, 'entry': 50, 'side': 'short'}, 48), 20.0)


def paper_trade(symbol='ABC', side='long', entry=126.5, stop=122.68, target=151.0, status='triggered', exit_=None,
                at='2026-10-08T10:00:00+05:30', r=None):
    zid = _zone(symbol, status=status, side=side, target=target)
    t = {'paper': True, 'entry': entry, 'stop': stop, 'target': target, 'choch_time': at, 'side': side, 'rr': 6.4}
    if exit_ is not None:
        t.update(exit=exit_, exit_time='2026-10-08T14:00:00+05:30', r=r)
    with store.connect() as c:
        c.execute('UPDATE zones SET trigger = ? WHERE id = ?', (json.dumps(t), zid))
    return zid


class Account(unittest.TestCase):
    def setUp(self):
        _reset()
        store.clear_demo()
        with store.connect() as c:
            c.execute('DELETE FROM oc_ideas')
        store.set_settings({'demo': {}, 'demo_since': ''})
        self.client = app.app.test_client()

    def start(self, amount=1_000_000, at='2026-10-08T09:00:00+05:30'):
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat(at)):
            r = self.client.post('/api/demo/funds', json={'kind': 'deposit', 'amount': amount})
        self.assertEqual(r.status_code, 200)

    def test_live_price_marks_open_positions(self):
        # The feed's price, while fresh, beats the watcher's candle close: in the views, the
        # quick /api/demo/live and what demo_sync stores. Stale, it falls back to the candle.
        self.start()
        zid = paper_trade()
        app.demo_sync()
        p = store.demo_positions('open')[0]
        app.charts.FEED.add({'ABC': 131.0})
        try:
            d = self.client.get('/api/demo/live').get_json()
            self.assertEqual(d['open'][0]['id'], p['id'])
            self.assertEqual((d['open'][0]['last'], d['open'][0]['unreal']), (131.0, round(4.5 * 1976, 2)))
            self.assertEqual(d['account']['unreal'], round(4.5 * 1976, 2))
            self.assertEqual(d['by_mode']['swing'], {'open': 1, 'unreal': round(4.5 * 1976, 2)})
            self.assertEqual(self.client.get('/api/demo').get_json()['open'][0]['last'], 131.0)
            app.demo_sync()
            self.assertEqual(store.demo_positions('open')[0]['last'], 131.0)
            # The open trade's journal row moves too: (131 - 126.5) / 3.82 R.
            z = next(z for z in app.swing_trades_state()['journal'] if z['id'] == zid)
            self.assertEqual((z['trigger']['last'], z['trigger']['r_open']), (131.0, round(4.5 / 3.82, 2)))
            with mock.patch.object(app.time, 'time', return_value=app.time.time() + app.LIVE_FRESH_S + 1):
                self.assertIsNone(app.live_px('ABC'))
        finally:
            app.charts.FEED._px.pop('ABC', None)
            app.charts.FEED._ticks.pop('ABC', None)

    def test_option_positions_use_the_contract_key(self):
        app._live_keys['options:77'] = 'OPT:NSE_FNO:44608'
        app.charts.FEED.add({'OPT:NSE_FNO:44608': 160.5})
        try:
            p = {'id': 1, 'ref': 'options:77', 'mode': 'options', 'symbol': 'NIFTY', 'status': 'open',
                 'side': 'long', 'entry': 138.7, 'qty': 130}
            app.live_demo([p])
            self.assertEqual((p['last'], p['unreal']), (160.5, round(21.8 * 130, 2)))
            i = app.live_idea({'id': 77, 'status': 'open', 'entry': 138.7, 'stop': 104.0})
            self.assertEqual((i['last'], i['r_open']), (160.5, round(21.8 / 34.7, 2)))
        finally:
            app._live_keys.pop('options:77', None)
            app.charts.FEED._px.pop('OPT:NSE_FNO:44608', None)
            app.charts.FEED._ticks.pop('OPT:NSE_FNO:44608', None)

    def test_trades_before_the_account_are_left_alone(self):
        paper_trade(at='2026-10-07T10:00:00+05:30')
        self.start()
        self.assertEqual(app.demo_sync()['opened'], 0)

    def test_open_mark_close(self):
        self.start()
        zid = paper_trade()
        got = app.demo_sync()
        self.assertEqual(got['opened'], 1)
        p = store.demo_positions('open')[0]
        # Risk 10,000 / 3.82 = 2,617 shares, capped at 25 % of 10 lakh / 126.5 = 1,976.
        self.assertEqual((p['product'], p['qty'], p['instrument']), ('CNC', 1976, 'ABC'))
        acct = app.demo_account()
        self.assertEqual(acct['blocked'], round(1976 * 126.5, 2))
        # The trade moves to 130 and the account marks it.
        with store.connect() as c:
            t = json.loads(c.execute('SELECT trigger FROM zones WHERE id = ?', (zid,)).fetchone()[0])
            t['last'] = 130.0
            c.execute('UPDATE zones SET trigger = ? WHERE id = ?', (json.dumps(t), zid))
        app.demo_sync()
        self.assertEqual(store.demo_positions('open')[0]['unreal'], round(3.5 * 1976, 2))
        # It hits the target: settled with charges, margin released.
        with store.connect() as c:
            t.update(exit=151.0, exit_time='2026-10-09T11:00:00+05:30', r=6.4)
            c.execute("UPDATE zones SET trigger = ?, status = 'won' WHERE id = ?", (json.dumps(t), zid))
        self.assertEqual(app.demo_sync()['closed'], 1)
        p = store.demo_positions('closed')[0]
        self.assertEqual(p['gross'], round(24.5 * 1976, 2))
        self.assertGreater(p['charges'], 0)
        acct = app.demo_account()
        self.assertEqual((acct['blocked'], acct['balance']), (0, round(1_000_000 + p['net'], 2)))
        self.assertEqual(app.demo_sync()['opened'], 0)                    # each trade is taken once
        # The statement books the trade's P&L and its charges as separate lines; the balance ties out.
        d = self.client.get('/api/demo').get_json()
        kinds = [x['kind'] for x in d['ledger']]
        self.assertEqual(kinds, ['charges', 'pnl', 'opening'])                # newest first
        self.assertEqual((d['ledger'][1]['amount'], d['ledger'][0]['amount']), (p['gross'], -p['charges']))
        self.assertEqual(d['ledger'][0]['balance'], acct['balance'])
        self.assertEqual(d['charges']['total'], p['charges'])
        self.assertEqual(d['charges']['lines']['dp'], 12.5)
        self.assertEqual(d['ledger'][1]['title'], 'Swing · ABC')
        self.assertEqual(d['ledger'][1]['detail'], 'bought 1,976 shares @ 126.50 → 151.00 · target hit')
        sm = d['statement']
        self.assertEqual((sm['opening'], sm['pnl'], sm['charges'], sm['trades']), (1_000_000, p['gross'], p['charges'], 1))
        self.assertEqual(sm['closing'], round(sm['opening'] + sm['pnl'] - sm['charges'], 2))
        csv = self.client.get('/api/demo/statement.csv')
        self.assertIn('attachment', csv.headers['Content-Disposition'])
        rows = csv.get_data(as_text=True).strip().splitlines()
        self.assertEqual(len(rows), 4)                                       # header + opening, P&L, charges
        self.assertTrue(rows[1].startswith('2026-10-08,09:00,Opening balance'))
        self.assertIn('Charges', rows[3])

    def test_option_idea_and_skips(self):
        self.start(50_000)
        iid = store.add_idea({'symbol': 'NIFTY', 'expiry': '2026-10-14', 'strike': 25000, 'side': 'CE', 'direction': 'long',
                              'created_at': '2026-10-08T10:30:00+05:30', 'session': '2026-10-08', 'status': 'open',
                              'entry': 120.0, 'stop': 90.0, 'target': 180.0, 'lot': 75, 'last': 130.0})
        paper_trade('BIG', entry=5000, stop=4990, at='2026-10-08T10:45:00+05:30')
        got = app.demo_sync()
        o = next(p for p in store.demo_positions() if p['mode'] == 'options')
        # Risk 500 / (30 x 75) = 0 lots: skipped, with the reason.
        self.assertEqual((o['status'], o['instrument']), ('skipped', 'NIFTY 25000 CE 14 Oct'))
        self.assertIn('one lot risks', o['note'])
        self.assertEqual(got['skipped'], 1)
        b = next(p for p in store.demo_positions() if p['symbol'] == 'BIG')
        self.assertEqual((b['status'], b['qty']), ('open', 2))                # 25 % of 50,000 / 5,000
        del iid

    def test_closed_between_syncs_releases_margin_in_order(self):
        self.start(100_000)
        paper_trade('ONE', entry=100, stop=99, at='2026-10-08T10:00:00+05:30', status='won', exit_=104, r=4)
        paper_trade('TWO', entry=100, stop=99, at='2026-10-08T15:00:00+05:30')
        app.demo_sync()
        one, two = (next(p for p in store.demo_positions() if p['symbol'] == s) for s in ('ONE', 'TWO'))
        self.assertEqual((one['status'], one['qty']), ('closed', 250))
        # ONE closed at 14:00, before TWO: its money was free again, and its profit grew the account.
        self.assertEqual(two['qty'], int(0.25 * (100_000 + one['net']) / 100))

    def test_withdraw_limits_and_reset(self):
        self.start(100_000)
        paper_trade('ONE', entry=100, stop=99)
        app.demo_sync()
        free = app.demo_account()['free']
        r = self.client.post('/api/demo/funds', json={'kind': 'withdraw', 'amount': free + 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.post('/api/demo/funds', json={'kind': 'withdraw', 'amount': 1000}).status_code, 200)
        self.assertEqual(app.demo_account()['balance'], 99_000)
        self.assertEqual(self.client.post('/api/demo/funds', json={'kind': 'gift', 'amount': 5}).status_code, 400)
        self.assertEqual(self.client.post('/api/demo/reset', json={}).status_code, 400)
        d = self.client.post('/api/demo/reset', json={'confirm': True, 'amount': 250_000}).get_json()
        self.assertEqual((d['account']['balance'], len(d['open']), len(d['ledger'])), (250_000, 0, 1))

    def test_trade_limits(self):
        self.start(1_000_000)
        self.client.post('/api/demo/settings', json={'max_open': 2, 'max_per_day': 3})
        paper_trade('ONE', entry=100, stop=99, at='2026-10-08T10:00:00+05:30', status='won', exit_=104, r=4)
        paper_trade('TWO', entry=100, stop=99, at='2026-10-08T10:10:00+05:30', status='won', exit_=104, r=4)
        paper_trade('THREE', entry=100, stop=99, at='2026-10-08T10:20:00+05:30')       # ONE and TWO still open then
        paper_trade('FOUR', entry=100, stop=99, at='2026-10-08T14:30:00+05:30')        # both closed at 14:00
        paper_trade('FIVE', entry=100, stop=99, at='2026-10-08T14:40:00+05:30')
        app.demo_sync()
        got = {p['symbol']: (p['status'], p.get('note') or '') for p in store.demo_positions()}
        self.assertEqual(got['THREE'], ('skipped', '2 positions already open: the most at once is 2'))
        self.assertEqual(got['FOUR'][0], 'open')                                       # a slot came free
        self.assertEqual(got['FIVE'], ('skipped', "3 trades already taken that day: the day's limit is 3"))
        st = demo.settings_from({'max_open': '7.6', 'max_per_day': -3})
        self.assertEqual((st['max_open'], st['max_per_day']), (8, 0))                  # whole numbers; 0 = no limit

    def test_start_over_from_the_first_trade_or_a_date(self):
        paper_trade('ONE', entry=100, stop=99, at='2026-10-06T10:00:00+05:30', status='won', exit_=104, r=4)
        paper_trade('TWO', entry=100, stop=99, at='2026-10-08T11:00:00+05:30')
        self.start(100_000)                                                 # started on the 8th: ONE is before it
        app.demo_sync()
        self.assertEqual({p['symbol'] for p in store.demo_positions()}, {'TWO'})
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T17:00:00+05:30')):
            d = self.client.post('/api/demo/reset', json={'confirm': True, 'amount': 500_000, 'since': 'first'}).get_json()
            self.assertEqual(d['since'], '2026-10-06T09:15:00+05:30')         # that day's open
            self.assertEqual({p['symbol'] for p in store.demo_positions()}, {'ONE', 'TWO'})   # replayed at once
            self.assertEqual([x['kind'] for x in d['ledger']][-1], 'opening')
            self.assertEqual(len(store.demo_ledger()), 1)                   # one clean opening line
            d = self.client.post('/api/demo/reset', json={'confirm': True, 'amount': 500_000, 'since': '2026-10-07'}).get_json()
            self.assertEqual(d['since'], '2026-10-07T09:15:00+05:30')
            self.assertEqual({p['symbol'] for p in store.demo_positions()}, {'TWO'})
            bad = self.client.post('/api/demo/reset', json={'confirm': True, 'amount': 1, 'since': '2026-12-01'})
            self.assertEqual(bad.status_code, 400)

    def test_mode_switched_off_and_api_shape(self):
        self.start()
        self.client.post('/api/demo/settings', json={'swing': False, 'risk_pct': 2})
        paper_trade()
        self.assertEqual(app.demo_sync()['opened'], 0)
        d = self.client.get('/api/demo').get_json()
        self.assertEqual((d['settings']['risk_pct'], d['settings']['swing']), (2.0, False))
        self.assertEqual(set(d['by_mode']), {'swing', 'intraday', 'options'})
        self.assertEqual(d['curve'][-1]['balance'], 1_000_000)


class LiveMarks(unittest.TestCase):
    """Open positions are marked from the live feed (charts.FEED) between the watchers' candles."""
    def setUp(self):
        _reset()
        store.clear_demo()
        store.set_settings({'demo': {}, 'demo_since': ''})
        self.client = app.app.test_client()
        app.charts.FEED._px.clear()

    def test_feed_price_marks_the_position_and_the_account(self):
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T09:00:00+05:30')):
            self.client.post('/api/demo/funds', json={'kind': 'deposit', 'amount': 1_000_000})
        paper_trade()                                            # 1,976 ABC from 126.5, candles say nothing yet
        app.demo_sync()
        self.assertEqual(store.demo_positions('open')[0]['unreal'], 0.0)
        app.charts.FEED.add({'ABC': 128.0})
        with mock.patch.object(app.data, 'market_open', return_value=True), \
             mock.patch.object(app.dhan, 'available', return_value=True):
            d = self.client.get('/api/demo/live').get_json()
        self.assertTrue(d['live']['on'])
        self.assertEqual((d['open'][0]['last'], d['open'][0]['unreal']), (128.0, round(1.5 * 1976, 2)))
        self.assertEqual(d['account']['unreal'], round(1.5 * 1976, 2))
        self.assertEqual(d['by_mode']['swing']['unreal'], round(1.5 * 1976, 2))
        self.assertEqual(self.client.get('/api/demo').get_json()['open'][0]['last'], 128.0)
        app.demo_sync()                                          # the minute's sync stores the live price too
        self.assertEqual(store.demo_positions('open')[0]['last'], 128.0)
        # The journal's open R follows the feed as well: (128 - 126.5) / 3.82.
        z = app.swing_trades_state()['journal'][0]
        self.assertEqual(z['trigger']['r_open'], round(1.5 / 3.82, 2))

    def test_stale_feed_price_is_not_used(self):
        app.charts.FEED.add({'ABC': 128.0}, at=1.0)              # long ago
        self.assertIsNone(app.live_px('ABC'))
        self.assertIsNone(app.live_px(''))

    def test_option_contract_ids(self):
        path = os.path.join(_tmp, 'opts.json')
        with open(path, 'w') as f:
            json.dump({'NIFTY|2026-10-13|22450|CE': '44608:N', 'SENSEX|2026-10-15|72200|CE': '1117378:B'}, f)
        with mock.patch.object(app.dhan, 'OPTS_PATH', app.dhan.Path(path)), \
             mock.patch.object(app.dhan, '_load_ids', return_value={}):
            app.dhan._opt_ids.clear()
            self.assertEqual(app.dhan.option_id('NIFTY', '2026-10-13', 22450.0, 'CE'), ('44608', 'NSE_FNO'))
            self.assertEqual(app.dhan.option_id('SENSEX', '2026-10-15T00:00:00', 72200, 'CE'), ('1117378', 'BSE_FNO'))
            self.assertIsNone(app.dhan.option_id('NIFTY', '2026-10-13', 22500, 'CE'))
            self.assertEqual(app._idea_key({'id': 7, 'symbol': 'NIFTY', 'expiry': '2026-10-13', 'strike': 22450, 'side': 'CE'}),
                             'OPT:NSE_FNO:44608')
        i = {'id': 7, 'status': 'open', 'entry': 100.0, 'stop': 75.0, 'last': 100.0, 'r_open': 0.0}
        app.charts.FEED.add({'OPT:NSE_FNO:44608': 110.0})
        self.assertEqual((app.live_idea(i)['last'], i['r_open']), (110.0, 0.4))
        app._live_keys.clear()
        app.dhan._opt_ids.clear()


class LivePrices(unittest.TestCase):
    """/api/live: the page's once-a-second prices, for symbols it is allowed to ask about."""
    def setUp(self):
        self.client = app.app.test_client()
        app.charts.FEED._px.clear()

    def test_answers_fresh_prices_and_marks_what_was_asked(self):
        app.charts.FEED.add({'ABC': 101.5, 'IDX:13': 22500.0, 'OPT:NSE_FNO:44608': 160.25})
        with mock.patch.object(app.data, 'market_open', return_value=True), \
             mock.patch.object(app.dhan, 'available', return_value=True), \
             mock.patch.object(app.dhan, 'security_id', side_effect=lambda s: '1' if s == 'ABC' else None), \
             mock.patch.object(app.charts.FEED, 'mark') as mark:
            d = self.client.get('/api/live?s=abc,IDX:13,OPT:NSE_FNO:44608,NOPE,OPT:X:1,../etc').get_json()
        self.assertTrue(d['on'])
        self.assertEqual({k: v[0] for k, v in d['px'].items()}, {'ABC': 101.5, 'IDX:13': 22500.0, 'OPT:NSE_FNO:44608': 160.25})
        mark.assert_called_once_with(['ABC', 'IDX:13', 'OPT:NSE_FNO:44608'])

    def test_market_closed_asks_nothing(self):
        with mock.patch.object(app.data, 'market_open', return_value=False), \
             mock.patch.object(app.charts.FEED, 'mark') as mark:
            d = self.client.get('/api/live?s=IDX:13').get_json()
        self.assertFalse(d['on'])
        mark.assert_not_called()

    def test_option_ids_read_the_map_once(self):
        path = os.path.join(_tmp, 'opts2.json')
        with open(path, 'w') as f:
            json.dump({'HDFCBANK|2026-10-27|700|CE': '1:N', 'HDFCBANK|2026-10-27|700|PE': '2:N'}, f)
        reads = []
        real = app.dhan.Path.read_text
        with mock.patch.object(app.dhan, 'OPTS_PATH', app.dhan.Path(path)), \
             mock.patch.object(app.dhan, '_load_ids', return_value={}), \
             mock.patch.object(app.dhan.Path, 'read_text', lambda self, *a, **k: reads.append(1) or real(self, *a, **k)):
            app.dhan._opt_ids.clear()
            got = app.dhan.option_ids('HDFCBANK', '2026-10-27', [(700.0, 'CE'), (700.0, 'PE'), (710.0, 'CE')])
            self.assertEqual(got, {(700.0, 'CE'): ('1', 'NSE_FNO'), (700.0, 'PE'): ('2', 'NSE_FNO')})
            self.assertEqual(len(reads), 1)
            app.dhan.option_ids('HDFCBANK', '2026-10-27', [(700.0, 'CE'), (710.0, 'CE')])   # cached, miss remembered
            self.assertEqual(len(reads), 1)
        app.dhan._opt_ids.clear()
        self.assertEqual(app.underlying_key('NIFTY'), 'IDX:13')
        self.assertEqual(app.underlying_key('HDFCBANK'), 'HDFCBANK')


if __name__ == '__main__':
    unittest.main()
