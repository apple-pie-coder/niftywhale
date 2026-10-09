"""
The news desk (niftywhale/news.py): parsing NSE filings and Google News RSS,
which headlines count as being about a company, housekeeping filings, tags,
the filing alerts on open trades and tapped zones, storage, the API, and the
news context recorded with a trade.

Run:  python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import news, perf, store  # noqa: E402

from tests.test_regressions import _reset, _zone  # noqa: E402

IST = news.IST
NOW = datetime(2026, 10, 8, 11, 0, tzinfo=IST)

# Shaped like NSE's corporate-announcements answer (trimmed).
FILINGS = [
    {'an_dt': '08-Oct-2026 10:12:40', 'desc': 'Outcome of Board Meeting', 'seq_id': '1001', 'symbol': 'VEDL',
     'attchmntText': 'Vedanta Limited has informed the Exchange regarding the outcome of the board meeting: '
                     'approved the unaudited financial results for the quarter ended September 30, 2026.',
     'attchmntFile': 'https://nsearchives.nseindia.com/corporate/VEDL_results.pdf'},
    {'an_dt': '07-Oct-2026 18:01:00', 'desc': 'Trading Window', 'seq_id': '1000', 'symbol': 'VEDL',
     'attchmntText': 'Closure of trading window', 'attchmntFile': ''},
    {'an_dt': '01-Oct-2026 16:30:00', 'desc': 'Credit Rating', 'seq_id': '990', 'symbol': 'VEDL',
     'attchmntText': 'CRISIL has upgraded the long-term rating', 'attchmntFile': None},
    {'an_dt': 'garbage', 'desc': 'Broken row'},
]


def rss(*items):
    body = ''.join(f'<item><title>{t} - {p}</title><link>https://news.google.com/rss/articles/{k}</link>'
                   f'<pubDate>{d}</pubDate><source url="https://x">{p}</source></item>'
                   for k, (t, p, d) in enumerate(items))
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'


MEDIA = rss(
    ('Tata Chemicals shares jump 6% after Q2 profit beats estimates', 'The Economic Times', 'Wed, 08 Oct 2026 04:30:00 GMT'),
    ('Tata Chemicals shares jump 6% after Q2 profit beats estimates', 'Moneycontrol', 'Wed, 08 Oct 2026 04:40:00 GMT'),
    ('Tata Motors unveils new EV', 'Business Standard', 'Tue, 07 Oct 2026 10:00:00 GMT'),
    ('TATACHEM: brokerages raise target price', 'Mint', 'Tue, 07 Oct 2026 09:00:00 GMT'),
)


class Parsing(unittest.TestCase):
    def test_filings(self):
        items = news.parse_filings(FILINGS, 'VEDL')
        self.assertEqual(len(items), 3)                          # the broken row is skipped
        board = items[0]
        self.assertEqual((board['kind'], board['uid'], board['published']),
                         ('filing', 'nse:1001', '2026-10-08T10:12:40+05:30'))
        self.assertIn('results', board['tags'])
        self.assertIn('board meeting', board['tags'])
        self.assertFalse(board['routine'])
        self.assertTrue(items[1]['routine'])                     # trading window: housekeeping
        self.assertIsNone(items[1]['url'])
        self.assertIn('rating', items[2]['tags'])
        self.assertEqual(news.parse_filings({'error': 'x'}, 'VEDL'), [])

    def test_media_keeps_the_company_once(self):
        items = news.parse_media(MEDIA, 'TATACHEM', 'Tata Chemicals Ltd.')
        self.assertEqual([i['title'] for i in items], ['Tata Chemicals shares jump 6% after Q2 profit beats estimates',
                                                       'TATACHEM: brokerages raise target price'])
        self.assertEqual(items[0]['publisher'], 'The Economic Times')      # the publisher suffix comes off
        self.assertEqual(items[0]['published'], '2026-10-08T10:00:00+05:30')
        self.assertIn('results', items[0]['tags'])
        self.assertIn('broker call', items[1]['tags'])
        self.assertEqual(news.parse_media('<not xml', 'X', 'X'), [])

    def test_mentions(self):
        self.assertTrue(news.mentions('Vedanta Q2 results', 'VEDL', 'Vedanta Ltd.'))
        self.assertTrue(news.mentions('PCBL.NS Q2 earnings', 'PCBL', 'PCBL Chemical Ltd.'))
        self.assertTrue(news.mentions('Elecon Engineering bags order', 'ELECON', 'Elecon Engineering Company Ltd.'))
        self.assertFalse(news.mentions('EIMCO ELECON Share Price Today Down 5%', 'ELECON', 'Elecon Engineering Company Ltd.'))
        self.assertFalse(news.mentions('Tata group shares rally', 'TATACHEM', 'Tata Chemicals Ltd.'))
        self.assertFalse(news.mentions('Bank stocks fall', 'BANKBARODA', 'Bank of Baroda'))
        self.assertEqual(news.core_name('Larsen & Toubro Ltd.'), 'Larsen & Toubro')
        # An overseas listing's price is not the NSE stock; a deal size in dollars is still Indian news.
        foreign = rss(('ICICI Bank stock loses 0.40 percent pre-market to EUR 24.80', 'Boerse', 'Wed, 08 Oct 2026 04:30:00 GMT'),
                      ('ICICI Bank raises $2 billion via bonds', 'Mint', 'Wed, 08 Oct 2026 05:30:00 GMT'))
        self.assertEqual([i['title'] for i in news.parse_media(foreign, 'ICICIBANK', 'ICICI Bank Ltd.')],
                         ['ICICI Bank raises $2 billion via bonds'])
        self.assertEqual(news.core_name('Elecon Engineering Company Ltd.'), 'Elecon Engineering')

    def test_alert_worthy(self):
        f = news.parse_filings(FILINGS, 'VEDL')
        self.assertTrue(news.alert_worthy(f[0], NOW))                       # 48 minutes old
        self.assertFalse(news.alert_worthy(f[0], NOW + timedelta(hours=4)))  # stale
        self.assertFalse(news.alert_worthy({**f[1], 'published': NOW.isoformat()}, NOW))   # housekeeping
        media = news.parse_media(MEDIA, 'TATACHEM', 'Tata Chemicals Ltd.')[0]
        self.assertFalse(news.alert_worthy({**media, 'published': NOW.isoformat()}, NOW))  # media never


class Storage(unittest.TestCase):
    def setUp(self):
        store.init()
        with store.connect() as c:
            c.execute('DELETE FROM news')

    def test_each_item_once_and_counts(self):
        items = news.parse_filings(FILINGS, 'VEDL') + news.parse_media(MEDIA, 'TATACHEM', 'Tata Chemicals Ltd.')
        self.assertEqual(len(store.add_news(items)), 5)
        self.assertEqual(store.add_news(items), [])                         # nothing new the second time
        c = store.news_counts(['VEDL', 'TATACHEM', 'PCBL'], '2026-10-07T00:00:00+05:30')
        self.assertEqual(c['VEDL'], {'n': 2, 'filings': 1, 'pos': 0, 'neg': 0, 'tone': None})   # trading window: not a filing that counts; nothing read yet
        self.assertEqual(c['TATACHEM']['n'], 2)
        self.assertNotIn('PCBL', c)
        got = store.news(['VEDL'], kind='filing')
        self.assertEqual([i['uid'] for i in got], ['nse:1001', 'nse:1000', 'nse:990'])
        self.assertIsInstance(got[0]['tags'], list)
        self.assertEqual(len(store.news(['VEDL'], routine=False)), 2)
        self.assertEqual(store.news([]), [])


def fake_score(texts, url=None, timeout=120):
    """FinBERT stand-in: 'upgrade' / 'beats' read positive, 'probe' / 'falls' negative."""
    out = []
    for t in texts:
        t = t.lower()
        lab = 'positive' if ('upgrade' in t or 'beats' in t or 'results' in t) else 'negative' if ('probe' in t or 'falls' in t) else 'neutral'
        out.append({'label': lab, 'score': {'positive': 0.8, 'negative': -0.7, 'neutral': 0.05}[lab], 'probs': {}})
    return out, 'finbert-test'


class DeskInTheApp(unittest.TestCase):
    def setUp(self):
        _reset()
        with store.connect() as c:
            c.execute('DELETE FROM news')
        app.NEWS_STATE['fetched'].clear()
        store.set_settings({'telegram': '1', 'news': '1', 'news_alerts': '1'})
        self.client = app.app.test_client()

    def fake_fetch(self, symbol, name, f=None):
        if symbol == 'VEDL':
            return news.parse_filings(FILINGS, 'VEDL'), []
        return [], ['NSE: 403']

    def run_pass(self, **kw):
        with mock.patch.object(app.news, 'fetch', side_effect=self.fake_fetch), \
                mock.patch.object(app.news, 'score', side_effect=fake_score), \
                mock.patch.object(app, 'news_reactions', return_value=0), \
                mock.patch.object(app, 'option_stocks', return_value=[]), \
                mock.patch.object(app.data, 'now_ist', return_value=NOW), \
                mock.patch.object(app.notify, 'send', return_value=True) as send:
            return app.news_pass(**kw), send

    def test_watch_list_and_why(self):
        _zone('VEDL', status='tapped')
        _zone('PCBL', status='watching')
        with mock.patch.object(app.data, 'now_ist', return_value=NOW), \
                mock.patch.object(app, 'option_stocks', return_value=['PCBL', 'SBIN']):
            watch = {w['symbol']: w['why'] for w in app.news_watch()}
        self.assertEqual(watch, {'VEDL': 'tapped', 'PCBL': 'board', 'SBIN': 'options'})   # the board outranks options

    def test_a_fresh_filing_on_a_tapped_zone_is_sent_once(self):
        _zone('VEDL', status='tapped')
        _zone('PCBL', status='watching')
        r, send = self.run_pass()
        self.assertEqual((r['stocks'], r['new'], r['alerts'], r['errors']), (2, 3, 1, 1))
        msg = send.call_args[0][0]
        self.assertIn('VEDL · NSE filing', msg)
        self.assertIn('FinBERT reads it as <b>positive</b> (+0.80)', msg)
        self.assertIn('price in the zone', msg)
        self.assertIn('Outcome of Board Meeting', msg)
        r, send = self.run_pass()
        self.assertEqual((r['new'], r['alerts']), (0, 0))                    # already seen: never again
        send.assert_not_called()

    def test_no_alert_for_a_stock_only_on_the_board_or_when_switched_off(self):
        _zone('VEDL', status='watching')
        r, send = self.run_pass()
        self.assertEqual((r['new'], r['alerts']), (3, 0))
        send.assert_not_called()
        with store.connect() as c:
            c.execute('DELETE FROM news')
        _reset()
        store.set_settings({'telegram': '1', 'news': '1', 'news_alerts': '0'})
        _zone('VEDL', status='tapped')
        r, send = self.run_pass()
        self.assertEqual(r['alerts'], 0)

    def test_api(self):
        _zone('VEDL', status='tapped')
        self.run_pass()
        with mock.patch.object(app.data, 'now_ist', return_value=NOW), mock.patch.object(app, 'option_stocks', return_value=[]):
            d = self.client.get('/api/news').get_json()
            self.assertEqual(d['tones']['VEDL']['pos'], 1)        # the results; the 1 Oct upgrade is outside the 24 h
            self.assertIn('impact', d)
            self.assertEqual({i['why'] for i in d['items']}, {'tapped'})
            self.assertEqual(len(d['items']), 3)
            self.assertEqual(len(self.client.get('/api/news?kind=media').get_json()['items']), 0)
            self.assertEqual(self.client.get('/api/news?symbol=%3Cscript%3E').status_code, 400)
            with mock.patch.object(app.news, 'fetch', side_effect=self.fake_fetch), \
                    mock.patch.object(app.news, 'score', side_effect=fake_score), \
                    mock.patch.object(app, 'news_reactions', return_value=0):
                r = self.client.post('/api/news/refresh', json={'symbol': 'VEDL'})
                self.assertEqual(r.get_json()['status'], 'fresh')           # fetched by the pass a moment ago
                app.NEWS_STATE['fetched'].clear()
                r = self.client.post('/api/news/refresh', json={'symbol': 'VEDL'})
                self.assertEqual(r.get_json()['status'], 'done')

    def test_trade_context_and_breakdown(self):
        store.add_news(news.parse_filings(FILINGS, 'VEDL'))
        ctx = app.news_context('VEDL', '2026-10-08T10:45:00+05:30')
        self.assertEqual(ctx, {'news_24h': 2, 'filing_24h': 1, 'news_pos_24h': 0, 'news_neg_24h': 0})   # not read yet
        with mock.patch.object(app.news, 'score', side_effect=fake_score):
            self.assertEqual(app.news_score_backlog(), 2)                  # the trading window is never read
        ctx = app.news_context('VEDL', '2026-10-08T10:45:00+05:30')
        self.assertEqual((ctx['news_pos_24h'], ctx['news_tone_24h']), (1, 0.8))   # housekeeping kept out of the tone
        self.assertEqual(app.news_context('VEDL', '2026-10-08T10:00:00+05:30')['news_24h'], 1)
        store.set_settings({'news': '0'})
        self.assertEqual(app.news_context('VEDL', '2026-10-08T10:45:00+05:30'), {})
        dim = next(fn for key, _, fn in perf.DIMENSIONS if key == 'news')
        self.assertEqual(dim({'f': ctx}), 'NSE filing')
        self.assertEqual(dim({'f': {'news_24h': 3, 'filing_24h': 0}}), 'media only')
        self.assertIsNone(dim({'f': {}}))                                   # backtested trades: no news history
        tone = next(fn for key, _, fn in perf.DIMENSIONS if key == 'news_tone')
        self.assertEqual(tone({'side': 'long', 'f': {'news_24h': 2, 'news_tone_24h': 0.6}}), 'with the trade')
        self.assertEqual(tone({'side': 'short', 'f': {'news_24h': 2, 'news_tone_24h': 0.6}}), 'against the trade')
        self.assertEqual(tone({'side': 'short', 'f': {'news_24h': 1, 'news_tone_24h': -0.3}}), 'with the trade')
        self.assertEqual(tone({'side': 'long', 'f': {'news_24h': 1, 'news_tone_24h': 0.05}}), 'neutral news')
        self.assertEqual(tone({'side': 'long', 'f': {'news_24h': 0}}), 'no news')
        self.assertIsNone(tone({'side': 'long', 'f': {'news_24h': 3}}))       # news the scorer never read
        self.assertIsNone(tone({'side': 'long', 'f': {}}))


def candles(start, closes, minutes=15):
    """15m candles from `start` (IST), one close each, skipping to the next session at 15:30."""
    import pandas as pd
    idx, t = [], start
    for _ in closes:
        idx.append(t)
        t = t + timedelta(minutes=minutes)
        if t.time() >= news.SESSION_CLOSE:
            t = datetime.combine(t.date() + timedelta(days=1), news.SESSION_OPEN, IST)
    return pd.DataFrame({'Close': closes}, index=pd.DatetimeIndex(idx))


class Reaction(unittest.TestCase):
    """Two sessions of 15m candles (7 Oct and 8 Oct, 25 candles each); the stock steps up, Nifty is flat."""
    import pandas as _pd
    D1 = datetime(2026, 10, 7, 9, 15, tzinfo=IST)
    stock = candles(D1, [100.0] * 10 + [110.0] * 15 + [120.0] * 25)
    nifty = candles(D1, [1000.0] * 50)

    def test_mid_session(self):
        # Published 11:40 on 7 Oct: base = the 11:15 candle's close (100); an hour on the stock is 110.
        r = news.reaction('2026-10-07T11:40:00+05:30', self.stock, self.nifty, now=datetime(2026, 10, 9, 10, 0, tzinfo=IST))
        self.assertEqual((r['basis'], r['react_1h'], r['react_close'], r['react_next'], r['done']),
                         ('15m', 10.0, 10.0, 20.0, True))

    def test_out_of_hours_counts_from_the_next_open(self):
        r = news.reaction('2026-10-07T19:00:00+05:30', self.stock, self.nifty, now=datetime(2026, 10, 9, 10, 0, tzinfo=IST))
        self.assertEqual(r['react_1h'], 9.091)              # base 110 (7 Oct close) -> 120 an hour after 8 Oct's open
        self.assertIsNone(r['react_next'])                  # no session after 8 Oct in the candles yet
        self.assertFalse(r['done'])

    def test_pending_until_the_time_has_passed(self):
        r = news.reaction('2026-10-07T11:40:00+05:30', self.stock, self.nifty, now=datetime(2026, 10, 7, 12, 0, tzinfo=IST))
        self.assertEqual((r['react_1h'], r['react_close'], r['done']), (None, None, False))

    def test_against_nifty(self):
        up = candles(self.D1, [1000.0] * 10 + [1050.0] * 40)      # Nifty +5% at the same time
        r = news.reaction('2026-10-07T11:40:00+05:30', self.stock, up, now=datetime(2026, 10, 9, 10, 0, tzinfo=IST))
        self.assertEqual(r['react_1h'], 5.0)

    def test_daily_fallback_and_too_old(self):
        import pandas as pd
        days = pd.DatetimeIndex(['2026-09-28', '2026-09-29', '2026-09-30'])
        sd, nd = pd.Series([100.0, 103.0, 99.0], index=days), pd.Series([1000.0, 1000.0, 1010.0], index=days)
        r = news.reaction('2026-09-28T18:00:00+05:30', self.stock, self.nifty, sd, nd, now=NOW)
        self.assertEqual((r['basis'], r['react_close'], r['react_next'], r['done']), ('daily', 3.0, -2.0, True))
        r = news.reaction('2026-09-01T10:00:00+05:30', None, None, None, None, now=NOW)
        self.assertTrue(r['done'] and r['basis'] is None)

    def test_scorer_client(self):
        ok = mock.Mock(**{'json.return_value': {'model': 'finbert-int8', 'results': [{'label': 'neutral', 'score': 0.0}] * 32}})
        ok.raise_for_status.return_value = None
        with mock.patch.object(news.requests, 'post', return_value=ok) as post:
            results, model = news.score(['x'] * 40)
        self.assertEqual((post.call_count, model), (2, 'finbert-int8'))      # batches of 32
        with mock.patch.object(news.requests, 'post', side_effect=news.requests.ConnectionError('down')):
            self.assertIsNone(news.score(['x']))
        self.assertEqual(news.text_for({'kind': 'filing', 'title': 'Credit Rating', 'detail': 'upgraded'}), 'Credit Rating: upgraded')


if __name__ == '__main__':
    unittest.main()
