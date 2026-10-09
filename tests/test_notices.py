"""
In-app notices (the bell): stored once per key, a later step adding detail without raising it again,
read state, only fresh events (a demo replay raises nothing), the demo account's part joined to its
trade's notice, live near-stop / near-target and square-off warnings, the feed watch, autopilot.

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

import app  # noqa: E402
from niftywhale import store  # noqa: E402

from tests.test_demo import paper_trade  # noqa: E402
from tests.test_regressions import _reset  # noqa: E402

AT = datetime.fromisoformat('2026-10-08T10:05:00+05:30')       # five minutes after paper_trade's entry


def clear():
    with store.connect() as c:
        c.execute('DELETE FROM notices')


class Store(unittest.TestCase):
    def setUp(self):
        clear()

    def test_once_per_key_and_updates_keep_read_state(self):
        a = store.add_notice('k1', 'entry', 'info', 'T', 'body')
        self.assertIsNotNone(a)
        self.assertIsNone(store.add_notice('k1', 'entry', 'info', 'T2', 'other'))          # same key: kept as it was
        self.assertEqual(store.notice('k1')['title'], 'T')
        seq = store.notice_counts()['seq']
        store.read_notices([a])
        store.add_notice('k1', 'entry', 'good', 'T3', 'more', update=True)
        n = store.notice('k1')
        self.assertEqual((n['title'], n['level'], n['read']), ('T3', 'good', 1))
        self.assertGreater(n['seq'], seq)
        self.assertEqual([x['id'] for x in store.notices(seq)], [a])                   # the page sees the change

    def test_counts_and_read_all(self):
        store.add_notice('a', 'news', 'info', 'A')
        store.add_notice('b', 'news', 'warn', 'B')
        self.assertEqual(store.notice_counts()['unread'], 2)
        store.read_notices()
        self.assertEqual(store.notice_counts()['unread'], 0)


class Events(unittest.TestCase):
    def setUp(self):
        _reset()
        store.clear_demo()
        clear()
        store.set_settings({'demo': {}, 'demo_since': ''})
        self.client = app.app.test_client()

    def test_entry_result_and_demo_detail(self):
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T09:00:00+05:30')):
            self.client.post('/api/demo/funds', json={'kind': 'deposit', 'amount': 1_000_000})
        zid = paper_trade()
        t = {'side': 'long', 'entry': 126.5, 'stop': 122.68, 'target': 151.0}
        with mock.patch.object(app.data, 'now_ist', return_value=AT):
            app.notice_entry('swing', f'swing:{zid}', 'ABC', t, '2026-10-08T10:00:00+05:30')
            app.demo_sync()
        n = store.notice(f'open:swing:{zid}')
        self.assertEqual(n['title'], 'Swing long · ABC · new entry')
        self.assertIn('R:R 1:6.4', n['body'])
        self.assertIn('Demo: 1,976 shares', n['body'])
        self.assertEqual(json.loads(n['link']), {'stock': 'ABC', 'ref': f'swing:{zid}'})
        # Target: the watcher's result, then the demo's net P&L joins it.
        with store.connect() as c:
            tt = json.loads(c.execute('SELECT trigger FROM zones WHERE id = ?', (zid,)).fetchone()[0])
            tt.update(exit=151.0, exit_time='2026-10-08T11:00:00+05:30', r=6.4)
            c.execute("UPDATE zones SET trigger = ?, status = 'won' WHERE id = ?", (json.dumps(tt), zid))
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T11:02:00+05:30')):
            app.notice_result('swing', f'swing:{zid}', 'ABC', t, 'won', 151.0, 6.4, '2026-10-08T11:00:00+05:30')
            app.demo_sync()
        n = store.notice(f'close:swing:{zid}')
        self.assertEqual((n['title'], n['level']), ('Swing long · ABC · Target hit', 'good'))
        self.assertIn('+6.40R', n['body'])
        self.assertRegex(n['body'], r'Demo: net \+₹[\d,]+ after ₹[\d,]+ charges')
        self.assertEqual(store.notice_counts()['unread'], 2)                   # one per step, not per writer

    def test_old_events_raise_nothing(self):
        # A demo account started after the fact replays the day: no notices for any of it.
        paper_trade()
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T15:00:00+05:30')):
            self.client.post('/api/demo/reset', json={'amount': 1_000_000, 'since': '2026-10-08'})
            app.notice_entry('swing', 'swing:1', 'ABC', {'entry': 1, 'stop': 0.5, 'target': 2}, '2026-10-08T10:00:00+05:30')
        self.assertEqual(store.notice_counts()['unread'], 0)

    def test_demo_skip_is_a_warning(self):
        with mock.patch.object(app.data, 'now_ist', return_value=datetime.fromisoformat('2026-10-08T09:00:00+05:30')):
            self.client.post('/api/demo/funds', json={'kind': 'deposit', 'amount': 1_000_000})
        self.client.post('/api/demo/settings', json={'max_open': 1})
        paper_trade('AAA', at='2026-10-08T10:00:00+05:30')
        paper_trade('BBB', at='2026-10-08T10:01:00+05:30')
        with mock.patch.object(app.data, 'now_ist', return_value=AT):
            app.demo_sync()
        skips = [n for n in store.notices() if n['kind'] == 'skip']
        self.assertEqual(len(skips), 1)
        self.assertEqual((skips[0]['symbol'], skips[0]['level']), ('BBB', 'warn'))
        self.assertIn('most at once is 1', skips[0]['body'])


class Live(unittest.TestCase):
    def setUp(self):
        clear()
        app.charts.FEED._px.clear()
        app._feed.update(down_since=None, said=None)

    def trade(self, **kw):
        return {'ref': 'swing:9', 'mode': 'swing', 'symbol': 'ABC', 'side': 'long', 'entry': 100.0, 'stop': 90.0,
                'target': 130.0, 'key': 'ABC', 't': {}, **kw}

    def test_near_stop_and_target_once(self):
        now = datetime.fromisoformat('2026-10-08T11:00:00+05:30')
        app.charts.FEED.add({'ABC': 91.5})                                   # -0.85R
        app.live_warnings([self.trade()], now)
        app.live_warnings([self.trade()], now)
        ns = store.notice('nearstop:swing:9')
        self.assertEqual((ns['level'], ns['title']), ('warn', 'Swing long · ABC · near its stop'))
        self.assertIn('the 15m candle decides', ns['body'])
        self.assertEqual(store.notice_counts()['unread'], 1)
        app.charts.FEED.add({'ABC': 128.0})                                  # 93 % of the way to 130
        app.live_warnings([self.trade()], now)
        self.assertEqual(store.notice('neartarget:swing:9')['level'], 'good')
        # A short: the stop is above.
        app.charts.FEED.add({'XYZ': 109.0})
        app.live_warnings([self.trade(ref='intraday:3', mode='intraday', symbol='XYZ', key='XYZ', side='short',
                                      stop=110.0, target=80.0)], now)
        self.assertIn('the 5m candle decides', store.notice('nearstop:intraday:3')['body'])

    def test_square_off_warning(self):
        tr = self.trade(ref='intraday:3', mode='intraday', symbol='XYZ', key='NOPE')
        app.live_warnings([tr], datetime.fromisoformat('2026-10-08T14:50:00+05:30'))
        self.assertEqual(store.notice_counts()['unread'], 0)                 # too early
        cut = app.irules().clock('square_off')
        at = datetime.combine(datetime(2026, 10, 8).date(), cut, app.data.IST)
        app.live_warnings([tr], at.replace(minute=cut.minute - 5) if cut.minute >= 5 else at.replace(hour=cut.hour - 1, minute=55))
        n = store.notice('squareoff:intraday:2026-10-08')
        self.assertEqual(n['title'], f"1 intraday trade square off at {cut.strftime('%H:%M')}")

    def test_feed_down_then_back(self):
        now = datetime.fromisoformat('2026-10-08T11:00:00+05:30')
        with mock.patch.object(app.dhan, 'status', return_value={'message': 'token rejected'}), \
             mock.patch.object(app.time, 'time', side_effect=[1000.0, 1000.0, 1070.0, 1070.0]):
            app.feed_watch(False, now)
            self.assertEqual(store.notice_counts()['unread'], 0)              # not yet a minute
            app.feed_watch(False, now)
        down = [n for n in store.notices() if n['kind'] == 'system']
        self.assertEqual(down[0]['title'], 'Live prices stopped')
        app.feed_watch(True, now)
        self.assertEqual(store.notices()[0]['title'], 'Live prices are back')


class Routes(unittest.TestCase):
    def setUp(self):
        clear()
        self.client = app.app.test_client()

    def test_list_read_and_live_carries_counts(self):
        a = store.add_notice('x', 'scan', 'info', 'Swing scan: 3 setups')
        store.add_notice('y', 'system', 'warn', 'Live prices stopped')
        d = self.client.get('/api/notices').get_json()
        self.assertEqual(([n['key'] for n in d['items']], d['unread']), (['y', 'x'], 2))
        seq = d['seq']
        self.assertEqual(self.client.get(f'/api/notices?after={seq}').get_json()['items'], [])
        self.assertEqual(self.client.post('/api/notices/read', json={'ids': [a]}).get_json()['unread'], 1)
        self.assertEqual(self.client.post('/api/notices/read', json={'ids': ['x']}).status_code, 400)
        self.assertEqual(self.client.get('/api/live').get_json()['notices']['unread'], 1)
        self.client.post('/api/notices/read', json={})
        self.assertEqual(self.client.get('/api/live').get_json()['notices']['unread'], 0)

    def test_autopilot_changes_are_notices(self):
        store.log_autopilot('swing', 'awaiting', {}, {}, 'challenger beat the rules out of sample')
        store.log_autopilot('swing', 'propose', {}, {}, 'internal')
        store.log_autopilot('all', 'policy', {}, {}, 'changed on the Performance tab', 'you')
        items = store.notices()
        self.assertEqual([(n['level'], n['title']) for n in items], [('warn', 'Autopilot wants your OK for new swing rules')])


if __name__ == '__main__':
    unittest.main()


class ClearAll(unittest.TestCase):
    """The bell's Clear all: hidden for good on every device, never raised again by the same event."""
    def setUp(self):
        clear()

    def test_clear_hides_and_the_same_event_stays_quiet(self):
        store.add_notice('risk:1', 'risk', 'warn', 'Near the stop')
        store.add_notice('entry:2', 'entry', 'info', 'Entry')
        seq_before = store.notice_counts()['seq']
        r = app.app.test_client().post('/api/notices/clear', json={})
        self.assertEqual(r.get_json()['cleared'], 2)
        self.assertEqual(r.get_json()['unread'], 0)
        self.assertEqual(store.notices(0), [])                          # a fresh page shows nothing
        changed = store.notices(seq_before)                             # an open page learns they went
        self.assertEqual(sorted(n['cleared'] for n in changed), [1, 1])
        self.assertIsNone(store.add_notice('risk:1', 'risk', 'warn', 'Near the stop'))   # not raised again
        self.assertEqual(store.notices(0), [])
        store.add_notice('entry:3', 'entry', 'info', 'A new one')                       # new events still come
        self.assertEqual([n['key'] for n in store.notices(0)], ['entry:3'])
        self.assertEqual(store.notice_counts()['unread'], 1)

    def test_nothing_to_clear(self):
        self.assertEqual(app.app.test_client().post('/api/notices/clear', json={}).get_json()['cleared'], 0)
