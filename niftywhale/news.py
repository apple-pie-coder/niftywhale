"""
The news desk: what has been said about the stocks on the board.

Two sources, both free and reachable from the Pi:

  NSE filings   the exchange's corporate announcements for one symbol
                (www.nseindia.com/api/corporate-announcements): results and
                board meetings, orders, ratings, fund raising, pledges,
                management changes, the exchange's own volume queries. What
                the company itself has told the market, timestamped.
  Media         Google News RSS, searched by company name over the last
                week (Indian edition): Economic Times, Moneycontrol, Business
                Standard and the rest. Google's search is loose, so only
                headlines that actually name the company are kept, and the
                same story syndicated across sites is kept once.

BSE's API refuses requests from here and yfinance's news is thin for NSE
names, so neither is used. Headlines are tagged by keyword (results, order,
rating, ...); there is no sentiment score: keyword sentiment is unreliable.

Parsing is pure (tests feed it saved responses); fetching is paced so neither
site is hammered (PACE_S between requests to the same host).
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, time as dtime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

IST = __import__('zoneinfo').ZoneInfo('Asia/Kolkata')
UA = ('Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/124.0 Safari/537.36')
NSE_HOME = 'https://www.nseindia.com/'
NSE_URL = 'https://www.nseindia.com/api/corporate-announcements'
GNEWS_URL = 'https://news.google.com/rss/search'
PACE_S = 2.0                    # seconds between two requests to the same host
FILING_DAYS = 30                # filings fetched this far back
MEDIA_DAYS = 7                  # media searched this far back
TIMEOUT = 15

# Filings that are housekeeping, not news: shown dimmed and never alerted.
ROUTINE = (
    'trading window', 'certificate under sebi (depositories', 'copy of newspaper publication',
    'loss of share certificate', 'duplicate share certificate', 'compliance certificate',
    'investor complaints', 'esop/esos/esps', 'reg. 74 (5)', 'regulation 74(5)', 'statement of deviation',
    'shareholders meeting', 'notice of shareholders', 'transcript', 'audio recording', 'video recording',
    'updates - change in registrar', 'disclosure of related party', 'certificate of interest payment',
)

# (tag, pattern), first match per tag. Applied to a filing's category and text, or a headline.
TAGS: List[Tuple[str, re.Pattern]] = [(t, re.compile(p, re.I)) for t, p in (
    ('results', r'financial results?|quarterly results?|\bq[1-4]\b|\bearnings\b|net profit|\bprofit\b|\brevenue\b|net loss|\bebitda\b'),
    ('board meeting', r'board meeting'),
    ('order', r'\border(s|book)?\b|\bcontract\b|bagging|\bLoA\b|letter of award|wins? .{0,30}(deal|project)'),
    ('dividend', r'dividend|record date'),
    ('split / bonus', r'stock split|sub-?division|\bsplit\b|\bbonus\b'),
    ('buyback', r'buy-?back'),
    ('fundraise', r'\bqip\b|preferential|rights issue|fund ?rais|\bncds?\b|debentures?|issue of securities|allotment'),
    ('rating', r'credit rating|\bupgrade|\bdowngrade|\brating\b'),
    ('broker call', r'target price|price target|\b(buy|sell|hold|accumulate|neutral|outperform|underperform)\b (call|rating)|brokerage|initiates coverage'),
    ('stake change', r'block deal|bulk deal|stake (sale|buy|hike)|offloads?|acquires? .{0,20}stake|\bofs\b|promoter (buy|sell)'),
    ('pledge', r'pledg'),
    ('regulatory', r'\bsebi\b|probe|penalt|show.?cause|\braids?\b|investigation|\bfraud\b|lawsuit|\bcourt\b|tribunal|\bnclt\b|tax demand|\bgst\b notice'),
    ('management', r'resign|appoint|\bceo\b|\bcfo\b|managing director|retire|cessation'),
    ('m&a', r'acqui|merger|amalgamat|takeover|demerger|scheme of arrangement'),
    ('volume query', r'spurt in volume|price movement|clarification|news verification'),
    ('business update', r'business update|guidance|outlook|monthly (sales|business)|production|\bvolumes?\b'),
)]

# Group names that open many companies' names: never enough on their own to say a
# headline is about one particular company.
GROUP_WORDS = {'tata', 'adani', 'bajaj', 'mahindra', 'birla', 'aditya', 'jsw', 'hdfc', 'icici',
               'kotak', 'bharat', 'india', 'indian', 'hindustan', 'national', 'state', 'bank', 'the', 'godrej',
               'larsen', 'jindal', 'shriram', 'murugappa', 'torrent', 'piramal', 'gujarat'}
# A headline quoting the price in another currency is about an overseas listing (ADR, Frankfurt), not the NSE stock.
# A deal size in dollars ("raises $2 billion") is Indian news and stays.
FOREIGN_PRICE = re.compile(r'\b(ADRs?|GDRs?|NYSE|Nasdaq|Frankfurt|Xetra)\b|'
                           r'\b(to|at)\s(EUR|USD|GBP|US\$|\$|€|£)\s?\d+(\.\d+)?(?!\s?(bn|billion|mn|million|crore|cr|lakh|k)\b)', re.I)
NAME_SUFFIXES = re.compile(r'[\s,]+(limited|ltd\.?|ltd|incorporated|inc\.?|company|co\.?|corporation|corp\.?)$', re.I)


# ---------------------------------------------------------------- helpers
def core_name(name: str) -> str:
    """'Tata Chemicals Ltd.' -> 'Tata Chemicals'; 'Larsen & Toubro Limited' -> 'Larsen & Toubro'."""
    n = (name or '').strip()
    for _ in range(2):
        n = NAME_SUFFIXES.sub('', n).strip()
    return re.sub(r'\s+', ' ', n)


def mentions(title: str, symbol: str, name: str) -> bool:
    """Does a headline name this company? Its name (without Ltd.), or its NSE symbol as
    a capitalised word (PCBL, VEDL): 'Tata' alone is not 'Tata Chemicals'."""
    t = title or ''
    core = core_name(name)
    if core and len(core) >= 4 and core.lower() not in GROUP_WORDS and core.lower() in t.lower():
        return True
    # The symbol as a word of its own: not glued to another capitalised word, so ELECON
    # does not claim 'EIMCO ELECON' (a different company).
    if len(symbol) >= 3 and re.search(r'(?<![A-Za-z])(?<![A-Z]{2} )' + re.escape(symbol) + r'(?![A-Za-z])(?! [A-Z]{2})', t):
        return True
    return False


def tags_for(text: str) -> List[str]:
    return [tag for tag, rx in TAGS if rx.search(text or '')]


def is_routine(category: str) -> bool:
    c = (category or '').lower()
    return any(c.startswith(r) or r in c for r in ROUTINE)


def _norm_title(title: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).strip()[:90]


def _uid(*parts: str) -> str:
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:16]


def _iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec='seconds')


# ---------------------------------------------------------------- parsing
def parse_filings(rows: Any, symbol: str) -> List[Dict[str, Any]]:
    """NSE corporate-announcements JSON (a list) -> news items."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            when = datetime.strptime(r['an_dt'], '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
        except (KeyError, TypeError, ValueError):
            continue
        category = (r.get('desc') or '').strip()
        text = re.sub(r'\s+', ' ', (r.get('attchmntText') or '').strip())
        out.append({
            'symbol': symbol, 'kind': 'filing', 'source': 'nse', 'publisher': 'NSE filing',
            'uid': 'nse:' + str(r.get('seq_id') or _uid(category, r['an_dt'])),
            'title': category or text[:120], 'detail': text[:400],
            'url': r.get('attchmntFile') or None, 'published': _iso(when),
            'routine': is_routine(category), 'tags': tags_for(category + ' ' + text),
        })
    return out


def parse_media(xml_text: str, symbol: str, name: str) -> List[Dict[str, Any]]:
    """Google News RSS -> news items that name the company, one per story."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out, seen = [], set()
    for item in root.iter('item'):
        raw = (item.findtext('title') or '').strip()
        src = item.find('source')
        publisher = (src.text or '').strip() if src is not None and src.text else ''
        title = raw[: -len(' - ' + publisher)] if publisher and raw.endswith(' - ' + publisher) else raw
        if not title or not mentions(title, symbol, name) or FOREIGN_PRICE.search(title):
            continue
        key = _norm_title(title)
        if key in seen:                       # the same story from another site
            continue
        seen.add(key)
        try:
            when = parsedate_to_datetime(item.findtext('pubDate') or '')
            if when.tzinfo is None:
                when = when.replace(tzinfo=IST)
        except (TypeError, ValueError):
            continue
        out.append({
            'symbol': symbol, 'kind': 'media', 'source': 'gnews', 'publisher': publisher or 'news',
            'uid': 'gn:' + _uid(key), 'title': title, 'detail': '', 'url': item.findtext('link') or None,
            'published': _iso(when), 'routine': False, 'tags': tags_for(title),
        })
    return out


# ---------------------------------------------------------------- fetching
class Fetcher:
    """One HTTP session per process, paced per host. NSE wants its own cookies: when it
    refuses, the home page is fetched once for them and the call retried."""

    def __init__(self, pace: float = PACE_S):
        self.pace = pace
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept-Language': 'en-IN,en;q=0.9'})
        self.last: Dict[str, float] = {}
        self.lock = threading.Lock()

    def _wait(self, host: str) -> None:
        with self.lock:
            gap = self.pace - (time.time() - self.last.get(host, 0))
            if gap > 0:
                time.sleep(gap)
            self.last[host] = time.time()

    def filings(self, symbol: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or datetime.now(IST)
        params = {'index': 'equities', 'symbol': symbol,
                  'from_date': (now - timedelta(days=FILING_DAYS)).strftime('%d-%m-%Y'),
                  'to_date': now.strftime('%d-%m-%Y')}
        headers = {'Referer': NSE_HOME, 'Accept': 'application/json'}
        for attempt in range(2):
            self._wait('nse')
            r = self.session.get(NSE_URL, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code in (401, 403) and attempt == 0:
                self._wait('nse')
                self.session.get(NSE_HOME, timeout=TIMEOUT)      # cookies, whatever the status
                continue
            r.raise_for_status()
            return parse_filings(r.json(), symbol)
        return []

    def media(self, symbol: str, name: str) -> List[Dict[str, Any]]:
        core = core_name(name) or symbol
        q = f'"{core}"' + (f' OR "{symbol}"' if len(symbol) >= 4 and symbol.upper() != core.upper() else '')
        url = f'{GNEWS_URL}?q={quote_plus(q + f" when:{MEDIA_DAYS}d")}&hl=en-IN&gl=IN&ceid=IN:en'
        self._wait('gnews')
        r = self.session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return parse_media(r.text, symbol, name)


_FETCHER: Optional[Fetcher] = None


def fetcher() -> Fetcher:
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = Fetcher()
    return _FETCHER


def fetch(symbol: str, name: str, f: Optional[Fetcher] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Both sources for one stock: (items, errors). One failing never costs the other."""
    f = f or fetcher()
    items, errors = [], []
    for label, call in (('NSE', lambda: f.filings(symbol)), ('news', lambda: f.media(symbol, name))):
        try:
            items += call()
        except (requests.RequestException, ValueError) as e:
            errors.append(f'{label}: {str(e)[:120]}')
    return items, errors


# ---------------------------------------------------------------- sentiment (FinBERT)
NLP_URL = os.getenv('NIFTYWHALE_NLP_URL', 'http://niftywhale-nlp:8000')
NLP_BATCH = 32


def text_for(item: Dict[str, Any]) -> str:
    """What FinBERT reads: a headline as written; a filing's category with the exchange's summary of it."""
    if item.get('kind') == 'filing' and item.get('detail'):
        return f"{item['title']}: {item['detail']}"[:600]
    return item.get('title') or ''


def score(texts: List[str], url: Optional[str] = None, timeout: float = 120) -> Optional[Tuple[List[Dict[str, Any]], str]]:
    """FinBERT's read of each text ({label, score -1..+1, probs}) and the model's name, or None
    when the scorer can't be reached (the items are simply read on a later pass)."""
    out: List[Dict[str, Any]] = []
    model = ''
    for k in range(0, len(texts), NLP_BATCH):
        try:
            r = requests.post((url or NLP_URL) + '/score', json={'texts': texts[k:k + NLP_BATCH]}, timeout=timeout)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f'Sentiment scorer unavailable: {str(e)[:120]}')
            return None
        out += body['results']
        model = body.get('model') or model
    return out, model


# ---------------------------------------------------------------- price reaction
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
CANDLE = timedelta(minutes=15)


def _closed_by(closes, t) -> Optional[float]:
    """Close of the last 15m candle that had finished by `t`."""
    done = closes[closes.index + CANDLE <= t]
    return float(done.iloc[-1]) if len(done) else None


def _session_close(day) -> datetime:
    return datetime.combine(day, SESSION_CLOSE, IST)


def reaction(published: str, stock15, nifty15, stock_daily=None, nifty_daily=None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    How the stock moved after a news item, against Nifty over the same span, in %:
    `react_1h` an hour after it could first be traded on, `react_close` by that session's
    close, `react_next` by the next session's close. Each from the last price before the
    news (an item out of hours is first tradable at the next open, so its base is the
    previous close). 15m candles when they reach back that far (`basis` '15m'); else
    daily closes for the close / next-day figures (`basis` 'daily': the base is then the
    previous close even for news that came mid-session). `done` once the next-day figure
    is in, or when nothing can ever be measured.
    """
    now = now or datetime.now(IST)
    pub = datetime.fromisoformat(published).astimezone(IST)
    out: Dict[str, Any] = {'react_1h': None, 'react_close': None, 'react_next': None, 'basis': None, 'done': False}

    def excess(p, b, pn, bn):
        if None in (p, b, pn, bn) or not b or not bn:
            return None
        return round((p / b - 1) * 100 - (pn / bn - 1) * 100, 3)

    s = stock15['Close'].dropna() if stock15 is not None and len(stock15) else None
    n = nifty15['Close'].dropna() if nifty15 is not None and len(nifty15) else None
    if s is not None and n is not None and len(s) and len(n):
        days = sorted(set(s.index.date))
        eff_day = next((d for d in days if d > pub.date() or (d == pub.date() and pub.time() < SESSION_CLOSE)), None)
        if eff_day is not None:
            eff = max(pub, datetime.combine(eff_day, SESSION_OPEN, IST))
            b, bn = _closed_by(s, eff), _closed_by(n, eff)
            if b is not None and bn is not None:           # the candles reach back to before the news
                out['basis'] = '15m'
                t1 = min(eff + timedelta(hours=1), _session_close(eff_day))
                if now >= t1:
                    out['react_1h'] = excess(_closed_by(s, t1), b, _closed_by(n, t1), bn)
                if now >= _session_close(eff_day):
                    t = _session_close(eff_day)
                    out['react_close'] = excess(_closed_by(s, t), b, _closed_by(n, t), bn)
                nxt = next((d for d in days if d > eff_day), None)
                if nxt is not None and now >= _session_close(nxt):
                    t = _session_close(nxt)
                    out['react_next'] = excess(_closed_by(s, t), b, _closed_by(n, t), bn)
                    out['done'] = True
                return out

    # Daily closes: the close and next-day reaction of older items.
    if stock_daily is not None and nifty_daily is not None and len(stock_daily) and len(nifty_daily):
        sd = {(i.date() if hasattr(i, 'date') else i): float(v) for i, v in stock_daily.dropna().items()}
        nd = {(i.date() if hasattr(i, 'date') else i): float(v) for i, v in nifty_daily.dropna().items()}
        days = sorted(d for d in nd if d in sd)
        eff_day = next((d for d in days if d > pub.date() or (d == pub.date() and pub.time() < SESSION_CLOSE)), None)
        before = [d for d in days if d < (eff_day or pub.date() + timedelta(days=1))]
        if eff_day is not None and before:
            base = before[-1]
            out['basis'] = 'daily'
            out['react_close'] = excess(sd[eff_day], sd[base], nd[eff_day], nd[base])
            nxt = next((d for d in days if d > eff_day), None)
            if nxt is not None:
                out['react_next'] = excess(sd[nxt], sd[base], nd[nxt], nd[base])
                out['done'] = True
            return out
    if now - pub > timedelta(days=12):
        out["done"] = True            # too old for the candles held, and no daily history: never measurable
    return out


# ---------------------------------------------------------------- alerts
def alert_worthy(item: Dict[str, Any], now: datetime, fresh_hours: float = 3) -> bool:
    """A Telegram alert is for a filing that is news: not housekeeping, and published in the
    last few hours (a stock seen for the first time must not dump its month of filings)."""
    if item['kind'] != 'filing' or item.get('routine'):
        return False
    try:
        when = datetime.fromisoformat(item['published'])
    except (KeyError, TypeError, ValueError):
        return False
    return now - when <= timedelta(hours=fresh_hours)


def counts(items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    items = list(items)
    return {'n': len(items), 'filings': sum(1 for i in items if i['kind'] == 'filing' and not i.get('routine'))}
