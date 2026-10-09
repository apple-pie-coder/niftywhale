"""
WebSockets: Dhan's live market feed (dhanws.py: packets, subscriptions, reconnects), the live feed
fed by it (livefeed.Feed with a stream), and the page's own socket (hub.py and /ws: the channels,
"this changed" from the database's triggers, sign-in and origin checks).

Run:  python -m unittest discover -s tests
"""
import json
import os
import queue
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

import app  # noqa: E402
from niftywhale import dhanws, hub, livefeed, store  # noqa: E402


def ticker_pkt(seg, sid, ltp, ltt=1791573399):
    return struct.pack('<BHBi', 2, 16, seg, sid) + struct.pack('<fi', ltp, ltt)


def quote_pkt(seg, sid, ltp, o, c, h, lo):
    return struct.pack('<BHBi', 4, 50, seg, sid) + struct.pack('<fhifiiiffff', ltp, 5, 1791573399, ltp, 1000, 0, 0, o, c, h, lo)


def full_pkt(seg, sid, ltp, oi, bid, ask):
    body = struct.pack('<fhifiiiiiiffff', ltp, 5, 1791573399, ltp, 777, 0, 0, oi, oi, oi, ltp, ltp, ltp, ltp)
    depth = struct.pack('<iihhff', 50, 75, 2, 3, bid, ask) + b'\0' * 80
    return struct.pack('<BHBi', 8, 162, seg, sid) + body + depth


class FakeDhan:
    """A stand-in for Dhan's feed: records what the stream sends, gives back what a test queues."""
    def __init__(self):
        self.sent, self.inbox, self.closed = [], queue.Queue(), False

    def send(self, msg):
        self.sent.append(json.loads(msg))

    def recv(self, timeout=None):
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Packets(unittest.TestCase):
    def test_every_packet_kind(self):
        prev = struct.pack('<BHBi', 6, 16, 0, 13) + struct.pack('<fi', 22231.8, 0)
        oi = struct.pack('<BHBi', 5, 12, 2, 4242) + struct.pack('<i', 123456)
        out = list(dhanws.parse(ticker_pkt(0, 13, 22520.45) + prev + quote_pkt(0, 25, 55256.65, 54746.25, 55256.65, 55419.3, 54617.2)
                                + full_pkt(2, 4242, 101.5, 9000, 101.4, 101.6) + oi))
        self.assertEqual([p['code'] for p in out], [2, 6, 4, 8, 5])
        t, pc, q, f, o = out
        self.assertEqual((t['seg'], t['sid']), ('IDX_I', 13))
        self.assertAlmostEqual(t['ltp'], 22520.45, places=2)
        self.assertEqual(t['ltt'], 1791573399 - 19800)                  # IST-shifted -> real epoch
        self.assertAlmostEqual(pc['prev_close'], 22231.8, places=1)
        self.assertAlmostEqual(q['high'], 55419.3, places=1)
        self.assertEqual((f['seg'], f['oi'], f['vol']), ('NSE_FNO', 9000, 777))
        self.assertAlmostEqual(f['bid'], 101.4, places=2)
        self.assertAlmostEqual(f['ask'], 101.6, places=2)
        self.assertEqual(o['oi'], 123456)

    def test_a_cut_packet_is_dropped_not_misread(self):
        out = list(dhanws.parse(ticker_pkt(1, 2885, 1170.3) + ticker_pkt(1, 1333, 707.25)[:10]))
        self.assertEqual(len(out), 1)

    def test_requests_are_batched_by_100(self):
        msgs = [json.loads(m) for m in dhanws._requests(15, [('NSE_EQ', i) for i in range(250)])]
        self.assertEqual([m['InstrumentCount'] for m in msgs], [100, 100, 50])
        self.assertEqual(msgs[0]['InstrumentList'][0], {'ExchangeSegment': 'NSE_EQ', 'SecurityId': '0'})

    def test_the_token_never_reaches_an_error(self):
        self.assertNotIn('SECRET', dhanws._clean(Exception('wss://x?version=2&token=SECRET&clientId=1')))


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.conns, self.got = [], []
        self.s = dhanws.Stream(lambda: 'wss://test', self.got.append, name='test-feed', connect=self._connect)

    def tearDown(self):
        self.s.close()

    def _connect(self, url):
        c = FakeDhan()
        self.conns.append(c)
        return c

    def test_subscribes_the_difference_and_hands_on_packets(self):
        self.s.want({('NSE_EQ', 2885): 'ticker', ('IDX_I', 13): 'quote'})
        self.assertTrue(wait_for(lambda: self.s.subscribed()))
        c = self.conns[0]
        codes = sorted((m['RequestCode'], m['InstrumentList'][0]['SecurityId']) for m in c.sent)
        self.assertEqual(codes, [(15, '2885'), (17, '13')])
        c.inbox.put(ticker_pkt(1, 2885, 1170.3))
        self.assertTrue(wait_for(lambda: self.got))
        self.assertEqual((self.got[0]['seg'], self.got[0]['sid']), ('NSE_EQ', 2885))
        # 2885 goes, 13 moves up to full: unsubscribe 2885 (16), 13 from quote (18), subscribe 13 full (21).
        c.sent.clear()
        self.s.want({('IDX_I', 13): 'full'})
        self.assertTrue(wait_for(lambda: len(c.sent) >= 3))
        self.assertEqual(sorted(m['RequestCode'] for m in c.sent), [16, 18, 21])

    def test_reconnects_after_a_drop_and_subscribes_again(self):
        with mock.patch.object(dhanws, 'BACKOFF', (0.05,)):
            self.s.want({('NSE_EQ', 2885): 'ticker'})
            self.assertTrue(wait_for(lambda: self.conns and self.conns[0].sent))
            self.conns[0].inbox.put(OSError('reset'))
            self.assertTrue(wait_for(lambda: len(self.conns) == 2 and self.conns[1].sent))
        self.assertEqual(self.conns[1].sent[0]['RequestCode'], 15)
        self.assertTrue(self.conns[0].closed)

    def test_dhan_closing_it_waits_and_says_why(self):
        self.s.want({('NSE_EQ', 2885): 'ticker'})
        self.assertTrue(wait_for(lambda: self.conns and self.conns[0].sent))
        self.conns[0].inbox.put(struct.pack('<BHBi', 50, 10, 0, 0) + struct.pack('<h', 805))
        self.assertTrue(wait_for(lambda: not self.s.connected() and self.s.error))
        self.assertIn('too many connections', self.s.error)
        time.sleep(0.3)
        self.assertEqual(len(self.conns), 1)                          # no hammering: it waits minutes


class FeedWithStream(unittest.TestCase):
    def test_polls_only_what_the_stream_does_not_carry(self):
        fetched = []

        class Cover:
            calls = []

            def cover(self, want):
                self.calls.append(dict(want))
                return {s for s in want if s != 'TCS'}

        feed = livefeed.Feed(lambda syms: fetched.append(list(syms)) or {s: 1.0 for s in syms}, lambda: 0.2,
                             name='t', stream=Cover())
        feed.mark(['IDX:13', 'IDX:25'], mode='quote')
        feed.watch(['RELIANCE', 'TCS'])
        self.assertTrue(wait_for(lambda: fetched))
        self.assertEqual(fetched[0], ['TCS'])
        self.assertEqual(Cover.calls[-1], {'IDX:13': 'quote', 'IDX:25': 'quote', 'RELIANCE': 'ticker', 'TCS': 'ticker'})

    def test_a_streamed_repeat_within_a_second_is_one_tick(self):
        feed = livefeed.Feed(lambda s: {}, lambda: 1, name='t')
        feed._watch['X'] = time.time()
        for i in range(5):
            feed.add({'X': 10.0}, 1000.0 + i * 0.1)
        feed.add({'X': 10.5}, 1000.6)
        self.assertEqual(len(feed.ticks('X')), 2)
        self.assertEqual(feed.last('X'), (1000.6, 10.5))

    def test_since(self):
        feed = livefeed.Feed(lambda s: {}, lambda: 1, name='t')
        feed.add({'A': 1.0}, 100.0)
        feed.add({'B': 2.0}, 200.0)
        self.assertEqual(feed.since(['A', 'B', 'C'], 150.0), {'B': (200.0, 2.0)})


class SourceTest(unittest.TestCase):
    def test_packets_become_feed_prices_and_quotes(self):
        feed = livefeed.Feed(lambda s: {}, lambda: 1, name='t')
        src = app.charts.DhanSource(lambda: feed)
        with mock.patch.object(app.dhan, 'available', return_value=True), \
             mock.patch.object(src.stream, 'want'), \
             mock.patch.object(src.stream, 'subscribed', return_value={('IDX_I', 13): 'quote', ('NSE_FNO', 7): 'full'}):
            carried = src.cover({'IDX:13': 'quote', 'OPT:NSE_FNO:7': 'full'})
            self.assertEqual(carried, {'IDX:13', 'OPT:NSE_FNO:7'})
            src.stream.since = time.time() - 1
            for p in dhanws.parse(quote_pkt(0, 13, 22500.0, 22300.0, 0, 22550.0, 22280.0) + full_pkt(2, 7, 101.5, 9000, 101.4, 101.6)):
                src._packet(p)
            got = src.carried(['IDX:13', 'OPT:NSE_FNO:7', 'IDX:25'])
        self.assertEqual(set(got), {'IDX:13', 'OPT:NSE_FNO:7'})
        self.assertEqual(got['OPT:NSE_FNO:7']['oi'], 9000)
        self.assertAlmostEqual(feed.last('IDX:13')[1], 22500.0)
        self.assertAlmostEqual(feed.last('OPT:NSE_FNO:7')[1], 101.5, places=2)


class Triggers(unittest.TestCase):
    def test_every_write_counts_from_any_connection(self):
        store.init()
        h = hub.Hub(lambda: store.DB_PATH)
        before = h.topics().get('db:zones')
        self.assertIsNotNone(before)
        with store.connect() as c:
            c.execute("INSERT INTO settings (key, value) VALUES ('ws_test', '1') "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
            c.execute("UPDATE zones SET status = status WHERE 0")      # touches nothing: no count
        h._topics_at = 0
        after = h.topics()
        self.assertEqual(after['db:zones'], before)
        self.assertGreater(after['db:settings'], 0)
        self.assertFalse(any(k.startswith('db:auth') for k in after))


class ConnectionClosed(Exception):
    """Named like simple-websocket's: the hub ends a closed connection quietly."""


class FakePage:
    """The browser's end of /ws for Hub.serve: what it sends, what the server sent it."""
    def __init__(self, msgs=()):
        self.inbox, self.out, self.closed = queue.Queue(), [], None
        for m in msgs:
            self.inbox.put(json.dumps(m))

    def receive(self, timeout=None):
        if self.closed:
            raise ConnectionClosed()
        try:
            m = self.inbox.get(timeout=timeout) if timeout else self.inbox.get_nowait()
        except queue.Empty:
            return None
        if m is StopIteration:
            raise ConnectionClosed()
        return m

    def send(self, text):
        self.out.append(json.loads(text))

    def close(self, reason=None, message=None):
        self.closed = reason

    def of(self, t):
        return [m for m in self.out if m['t'] == t]


class HubTest(unittest.TestCase):
    def serve(self, h, page, **kw):
        th = threading.Thread(target=h.serve, args=(page,), kwargs=kw, daemon=True)
        th.start()
        return th

    def test_channels_dedupe_and_topics(self):
        store.init()
        h = hub.Hub(lambda: store.DB_PATH)
        state = {'v': 1}
        h.channel('thing', lambda p, mem: {'v': state['v'], 'p': p.get('x')}, every=0.05)
        h.probe('run', lambda: dict(state))
        page = FakePage([{'t': 'sub', 'ch': 'thing', 'p': {'x': 7}}])
        th = self.serve(h, page)
        self.assertTrue(wait_for(lambda: page.of('thing')))
        time.sleep(0.3)
        self.assertEqual(len(page.of('thing')), 1)                     # unchanged: sent once
        self.assertEqual(page.of('hello')[0]['channels'], ['thing'])
        state['v'] = 2
        self.assertTrue(wait_for(lambda: len(page.of('thing')) == 2))
        self.assertEqual(page.of('thing')[-1]['d'], {'v': 2, 'p': 7})
        self.assertTrue(wait_for(lambda: any('run' in m['d'] for m in page.of('topics')), 3))
        page.inbox.put(StopIteration)
        th.join(3)
        self.assertEqual(h.clients(), 0)

    def test_hidden_pauses_all_but_background_channels(self):
        h = hub.Hub(lambda: store.DB_PATH)
        n = {'fg': 0, 'bg': 0}
        h.channel('fg', lambda p, mem: n.__setitem__('fg', n['fg'] + 1) or n['fg'], every=0.05)
        h.channel('bg', lambda p, mem: n.__setitem__('bg', n['bg'] + 1) or n['bg'], every=0.05, background=True)
        page = FakePage([{'t': 'vis', 'hidden': True}, {'t': 'sub', 'ch': 'fg'}, {'t': 'sub', 'ch': 'bg'}])
        th = self.serve(h, page)
        self.assertTrue(wait_for(lambda: n['bg'] > 3))
        self.assertEqual(n['fg'], 0)
        page.inbox.put(StopIteration)
        th.join(3)

    def test_too_many_pages_are_turned_away(self):
        h = hub.Hub(lambda: store.DB_PATH)
        h._clients = hub.MAX_CLIENTS
        page = FakePage()
        h.serve(page)
        self.assertEqual(page.closed, hub.CLOSE_BUSY)

    def test_a_signed_out_page_is_closed(self):
        h = hub.Hub(lambda: store.DB_PATH)
        page = FakePage()
        with mock.patch.object(hub, 'RECHECK_S', 0.1):
            th = self.serve(h, page, still_signed_in=lambda: False)
            th.join(3)
        self.assertEqual(page.closed, hub.CLOSE_SIGNED_OUT)

    def test_bad_channel_parameters_answer_an_error(self):
        page = FakePage([{'t': 'sub', 'ch': 'chain', 'p': {'symbol': '../x'}}, {'t': 'ping'}])
        th = self.serve(app.HUB, page)
        self.assertTrue(wait_for(lambda: page.of('chain') and page.of('pong')))
        self.assertIn('error', page.of('chain')[0])
        page.inbox.put(StopIteration)
        th.join(3)


class PricePush(unittest.TestCase):
    def test_px_sends_each_price_once_as_it_changes(self):
        mem, p = {}, {'s': ['IDX:13', 'IDX:25', 'bad symbol']}
        feed = livefeed.Feed(lambda s: {}, lambda: 1, name='t')
        with mock.patch.object(app.charts, 'FEED', feed), mock.patch.object(app, 'live_on', return_value=True), \
             mock.patch.object(feed, 'mark') as mark:
            first = app.ws_px(p, mem)
            self.assertEqual(first['px'], {})                          # nothing priced yet, but "on" is news
            self.assertEqual(mark.call_args[0][0], ['IDX:13', 'IDX:25'])
            app.charts.FEED.add({'IDX:13': 22500.0})
            got = app.ws_px(p, mem)
            self.assertEqual(list(got['px']), ['IDX:13'])
            self.assertIsNone(app.ws_px(p, mem))                       # nothing new
            app.charts.FEED.add({'IDX:13': 22501.0, 'IDX:25': 55000.0})
            self.assertEqual(sorted(app.ws_px(p, mem)['px']), ['IDX:13', 'IDX:25'])

    def test_ticker_from_the_stream(self):
        saved = dict(app.TICKER)
        self.addCleanup(lambda: (app.TICKER.clear(), app.TICKER.update(saved)))
        app.TICKER.update(rows=[], session=None, prev={}, prev_session=None)
        keys = app.TICKER_KEYS
        carried = {k: {'ltp': 100.0 + i, 'open': 99.0, 'high': 101.0 + i, 'low': 98.0} for i, k in enumerate(keys)}
        with mock.patch.object(app.charts, 'streaming', return_value=True), \
             mock.patch.object(app.data, 'market_open', return_value=True), \
             mock.patch.object(app.dhan, 'available', return_value=True), \
             mock.patch.object(app.charts.SOURCE, 'carried', return_value=carried), \
             mock.patch.object(app.charts.FEED, 'mark') as mark, \
             mock.patch.object(app.dhan, 'index_quotes') as rest, \
             mock.patch.object(app.threading, 'Thread'):
            d = app.ticker_state()
        self.assertTrue(d['stream'])
        self.assertEqual(len(d['rows']), len(keys))
        self.assertEqual(mark.call_args[1], {'mode': 'quote'})
        rest.assert_not_called()


class Endpoint(unittest.TestCase):
    """/ws through Flask: sign-in like any page, and another site's page refused."""
    def test_origin_and_sign_in(self):
        from niftywhale import auth
        with mock.patch.object(auth, 'ENABLED', True), mock.patch.object(auth, '_token', return_value=None), \
             mock.patch.object(auth, '_session', return_value={'id': 1}), mock.patch.object(auth, 'owner', return_value={'id': 1}):
            c = app.app.test_client()
            h = {'Upgrade': 'websocket', 'Connection': 'Upgrade', 'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
                 'Sec-WebSocket-Version': '13'}
            r = c.get('/ws', headers={**h, 'Origin': 'https://evil.example'})
            self.assertEqual(r.status_code, 403)
        with mock.patch.object(auth, 'ENABLED', True), mock.patch.object(auth, '_token', return_value=None), \
             mock.patch.object(auth, '_session', return_value=None), mock.patch.object(auth, 'owner', return_value={'id': 1}):
            r = app.app.test_client().get('/ws', headers={'Accept': 'application/json'})
            self.assertEqual(r.status_code, 401)

    def test_status(self):
        d = app.app.test_client().get('/api/live/status').get_json()
        self.assertIn('stream', d)
        self.assertIn('pages', d)


if __name__ == '__main__':
    unittest.main()
