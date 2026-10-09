"""
Smart money: what institutions did, from what NSE publishes after each session.

  Bulk deals      archives .../equities/bulk.csv. A named client trading 0.5 % or more of a
                  company's shares in a session. Latest session only: kept from the day the
                  app first saw it.
  Block deals     archives .../equities/block.csv. Trades of Rs 10 cr+ in the block window.
                  Latest session only, likewise.
  FII/DII cash    www.nseindia.com/api/fiidiiTradeReact. Buy, sell and net (Rs cr) of FIIs/FPIs
                  and DIIs in the cash market. Latest day only, likewise.
  Participant OI  archives .../nsccl/fao_participant_oi_DDMMYYYY.csv. Long and short contracts
                  in index and stock futures and options per client type: Client (retail),
                  DII, FII, Pro (brokers' own books). Dated: backfilled.
  Delivery        archives .../products/content/sec_bhavdata_full_DDMMYYYY.csv. Every stock's
                  traded and delivered quantity. Dated: backfilled.

Nothing here is real time and nothing names who is buying today: India publishes no
trader identities intraday. It is the record after the close, context for the next session.
Client types are read from the client's name (heuristics, see client_type), so a fund
whose name says nothing about it can be filed as a company.

Parsing and analysis are pure; fetching is paced like the news desk's.
"""
import csv
import io
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

UA = ('Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/124.0 Safari/537.36')
ARCHIVES = 'https://archives.nseindia.com'
BULK_URL = ARCHIVES + '/content/equities/bulk.csv'
BLOCK_URL = ARCHIVES + '/content/equities/block.csv'
POI_URL = ARCHIVES + '/content/nsccl/fao_participant_oi_{d}.csv'
BHAV_URL = ARCHIVES + '/products/content/sec_bhavdata_full_{d}.csv'
FLOWS_URL = 'https://www.nseindia.com/api/fiidiiTradeReact'
PACE_S = 1.5
TIMEOUT = 30
PARTICIPANTS = ('Client', 'DII', 'FII', 'Pro')

# Delivery reads: today's delivery % against the stock's own average of the 20 sessions before.
DELIV_SESSIONS = 20
HIGH_VOLUME = 1.5        # volume at least this many times its average...
HIGH_DELIVERY = 1.3      # ...with delivery % this many times its average: positions taken home
LOW_DELIVERY = 0.7       # ...or this far under it: traded and squared off the same day


# ---------------------------------------------------------------- helpers
def _num(x) -> Optional[float]:
    try:
        v = float(str(x).replace(',', '').strip())
        return v
    except (TypeError, ValueError):
        return None


def _day(text: str) -> Optional[str]:
    """'07-OCT-2026' / '07-Oct-2026' / '2026-10-07' -> '2026-10-07'."""
    t = (text or '').strip()
    for fmt in ('%d-%b-%Y', '%d-%B-%Y', '%Y-%m-%d', '%d/%m/%Y'):      # month names match in any case
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# Client types, from the name. Order matters: the first match wins.
CLIENT_TYPES = [
    ('mutual fund', re.compile(r'mutual fund|\bmf\b|asset management|\bamc\b|trustee|a/c .*scheme|\bscheme\b', re.I)),
    ('insurance', re.compile(r'insurance|assurance|\blic\b|life corporation', re.I)),
    # Foreign portfolio investors and other pooled money: offshore vehicles (PCC, SE, Pte, plc, LLC, LP),
    # the global houses by name, and anything calling itself a fund (FPIs, AIFs).
    ('fund', re.compile(
        r'\bfpi\b|\bfii\b|\bfund\b|\bfunds\b|\bpcc\b|\bicav\b|\bsicav\b|pte\.?\s*ltd|\bplc\b|\bse$|\bgmbh\b|\bllc\b|'
        r'\binc\.?$|\blp\b|\bl\.p\.|singapore|mauritius|cayman|ireland|luxembourg|europe|goldman sachs|morgan stanley|'
        r'societe generale|bnp paribas|citigroup|merrill|nomura|jpmorgan|j\.p\. morgan|\bubs\b|barclays|hsbc|vanguard|'
        r'blackrock|abu dhabi|government of singapore|norges|copthall|ishares|fidelity|invesco|franklin templeton|'
        r'polar capital|clsa|aberdeen|kuwait|qatar', re.I)),
    ('bank', re.compile(r'\bbank\b|banking corporation', re.I)),
    ('prop / quant', re.compile(
        r'quant|graviton|hrti|jump trading|tower research|nk securities|irage|mathisys|alphagrep|silverleaf|'
        r'qe securities|dolat|microcurves|plutus|sunrise investment|securities (pvt|private)|broking|'
        r'\bcapital markets?\b|share broking|trading (pvt|private|llp)', re.I)),
    ('company', re.compile(r'\b(limited|ltd|pvt|private|llp|corporation|corp|enterprises|holdings?|trust|ventures?|'
                           r'investments?|industries|finance|advisors?|partners|brokrage|brokerage)\b', re.I)),
]
# Who counts as an institution in the deal sums: not prop desks, companies or individuals.
INSTITUTIONAL = ('mutual fund', 'insurance', 'fund', 'bank')


def client_type(name: str) -> str:
    for label, rx in CLIENT_TYPES:
        if rx.search(name or ''):
            return label
    return 'individual'


# ---------------------------------------------------------------- parsing
def parse_deals(text: str, kind: str) -> List[Dict[str, Any]]:
    """bulk.csv / block.csv -> deals. 'NO RECORDS' and broken rows are skipped."""
    out = []
    reader = csv.reader(io.StringIO(text or ''))
    header = next(reader, None)
    if not header:
        return out
    cols = {h.strip().lower(): i for i, h in enumerate(header)}

    def col(row, *names):
        for n in names:
            for h, i in cols.items():
                if h.startswith(n) and i < len(row):
                    return row[i].strip()
        return ''
    for row in reader:
        day = _day(col(row, 'date'))
        sym = col(row, 'symbol').upper()
        qty, px = _num(col(row, 'quantity')), _num(col(row, 'trade price', 'price'))
        side = col(row, 'buy/sell', 'buy').lower()
        if not day or not sym or qty is None or px is None or side not in ('buy', 'sell'):
            continue
        client = re.sub(r'\s+', ' ', col(row, 'client'))
        out.append({'day': day, 'symbol': sym, 'name': col(row, 'security'), 'client': client, 'side': side,
                    'qty': int(qty), 'price': px, 'value_cr': round(qty * px / 1e7, 2), 'kind': kind,
                    'remarks': col(row, 'remarks') if col(row, 'remarks') not in ('-', '') else '',
                    'ctype': client_type(client)})
    return out


def parse_flows(rows: Any) -> List[Dict[str, Any]]:
    """fiidiiTradeReact JSON -> [{day, category 'FII' | 'DII', buy, sell, net}] (Rs cr)."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        day = _day(r.get('date', ''))
        cat = 'FII' if 'FII' in str(r.get('category', '')).upper() else 'DII' if 'DII' in str(r.get('category', '')).upper() else None
        if day and cat:
            out.append({'day': day, 'category': cat, 'buy': _num(r.get('buyValue')), 'sell': _num(r.get('sellValue')),
                        'net': _num(r.get('netValue'))})
    return out


POI_COLUMNS = {
    'future index long': 'fut_idx_long', 'future index short': 'fut_idx_short',
    'future stock long': 'fut_stk_long', 'future stock short': 'fut_stk_short',
    'option index call long': 'opt_idx_call_long', 'option index put long': 'opt_idx_put_long',
    'option index call short': 'opt_idx_call_short', 'option index put short': 'opt_idx_put_short',
    'option stock call long': 'opt_stk_call_long', 'option stock put long': 'opt_stk_put_long',
    'option stock call short': 'opt_stk_call_short', 'option stock put short': 'opt_stk_put_short',
    'total long contracts': 'total_long', 'total short contracts': 'total_short',
}


def parse_participant_oi(text: str) -> Dict[str, Dict[str, int]]:
    """fao_participant_oi CSV -> {participant: {column: contracts}} for Client, DII, FII, Pro."""
    rows = list(csv.reader(io.StringIO(text or '')))
    head = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == 'client type'), None)
    if head is None:
        return {}
    names = [POI_COLUMNS.get(h.strip().lower()) for h in rows[head]]
    out = {}
    for r in rows[head + 1:]:
        if not r or r[0].strip() not in PARTICIPANTS:
            continue
        out[r[0].strip()] = {n: int(_num(v) or 0) for n, v in zip(names, r) if n}
    return out


def parse_bhav(text: str) -> Dict[str, Dict[str, Any]]:
    """sec_bhavdata_full CSV -> {symbol: close, prev_close, qty, deliv_qty, deliv_pct, turnover_cr, trades}
    for the EQ series (the BE/BZ trade-for-trade series have 100 % delivery by rule)."""
    reader = csv.reader(io.StringIO(text or ''))
    header = [h.strip().upper() for h in next(reader, [])]
    idx = {h: i for i, h in enumerate(header)}
    need = ('SYMBOL', 'SERIES', 'CLOSE_PRICE', 'PREV_CLOSE', 'TTL_TRD_QNTY', 'DELIV_QTY', 'DELIV_PER')
    if not all(k in idx for k in need):
        return {}
    out = {}
    for r in reader:
        if len(r) < len(header) or r[idx['SERIES']].strip() != 'EQ':
            continue
        qty, dq, dp = _num(r[idx['TTL_TRD_QNTY']]), _num(r[idx['DELIV_QTY']]), _num(r[idx['DELIV_PER']])
        if not qty:
            continue
        out[r[idx['SYMBOL']].strip()] = {
            'close': _num(r[idx['CLOSE_PRICE']]), 'prev_close': _num(r[idx['PREV_CLOSE']]), 'qty': int(qty),
            'deliv_qty': int(dq) if dq is not None else None, 'deliv_pct': dp,
            'turnover_cr': round((_num(r[idx['TURNOVER_LACS']]) or 0) / 100, 2) if 'TURNOVER_LACS' in idx else None,
            'trades': int(_num(r[idx['NO_OF_TRADES']]) or 0) if 'NO_OF_TRADES' in idx else None}
    return out


# ---------------------------------------------------------------- analysis
def delivery_read(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The latest session's delivery against the stock's own previous DELIV_SESSIONS:
      accumulation  heavy volume, delivery well above normal, the stock up: positions taken home
      distribution  the same on a down day: holders delivering out
      churn         heavy volume, delivery well below normal: traded and squared off the same day
      normal        none of these
    `history` is the stock's rows (day, close, prev_close, qty, deliv_pct), oldest first."""
    rows = [r for r in history if r.get('deliv_pct') is not None and r.get('qty')]
    if len(rows) < 6:
        return None
    last, prior = rows[-1], rows[-1 - DELIV_SESSIONS:-1]
    avg_pct = sum(r['deliv_pct'] for r in prior) / len(prior)
    avg_qty = sum(r['qty'] for r in prior) / len(prior)
    ratio = last['deliv_pct'] / avg_pct if avg_pct else None
    vol = last['qty'] / avg_qty if avg_qty else None
    up = last.get('close') is not None and last.get('prev_close') and last['close'] >= last['prev_close']
    read = 'normal'
    if vol is not None and ratio is not None and vol >= HIGH_VOLUME:
        if ratio >= HIGH_DELIVERY:
            read = 'accumulation' if up else 'distribution'
        elif ratio <= LOW_DELIVERY:
            read = 'churn'
    return {'day': last['day'], 'deliv_pct': round(last['deliv_pct'], 2), 'avg_pct': round(avg_pct, 2),
            'ratio': round(ratio, 2) if ratio is not None else None, 'vol_ratio': round(vol, 2) if vol is not None else None,
            'change_pct': round((last['close'] / last['prev_close'] - 1) * 100, 2) if last.get('prev_close') and last.get('close') else None,
            'deliv_value_cr': round(last['deliv_qty'] * last['close'] / 1e7, 2) if last.get('deliv_qty') and last.get('close') else None,
            'read': read, 'sessions': len(prior)}


def positioning(days: List[Dict[str, Any]]) -> Dict[str, Any]:
    """FII (and every participant's) index futures positioning over time. `days` are
    {day, data: {participant: columns}}, oldest first. long_pct = longs / (longs + shorts)."""
    def summary(p):
        if not p:
            return None
        lo, sh = p.get('fut_idx_long', 0), p.get('fut_idx_short', 0)
        cl, cs = p.get('opt_idx_call_long', 0), p.get('opt_idx_call_short', 0)
        pl, ps = p.get('opt_idx_put_long', 0), p.get('opt_idx_put_short', 0)
        return {'fut_long': lo, 'fut_short': sh, 'fut_net': lo - sh,
                'long_pct': round(100 * lo / (lo + sh), 1) if lo + sh else None,
                'calls_net': cl - cs, 'puts_net': pl - ps,
                'stk_fut_net': p.get('fut_stk_long', 0) - p.get('fut_stk_short', 0)}
    series = [{'day': d['day'], **{k: summary(d['data'].get(k)) for k in PARTICIPANTS}} for d in days if d.get('data')]
    if not series:
        return {'latest': None, 'series': []}
    latest, prev = series[-1], series[-2] if len(series) > 1 else None
    fii = latest.get('FII') or {}
    change = None
    if prev and prev.get('FII') and fii:
        change = {'fut_net': fii['fut_net'] - prev['FII']['fut_net'],
                  'long_pct': round((fii['long_pct'] or 0) - (prev['FII']['long_pct'] or 0), 1)}
    return {'latest': latest, 'change': change,
            'series': [{'day': s['day'], 'long_pct': (s.get('FII') or {}).get('long_pct'),
                        'fut_net': (s.get('FII') or {}).get('fut_net'),
                        # every participant, for the chart's other lines and its hover
                        'parts': {k: {f: s[k][f] for f in ('long_pct', 'fut_net', 'calls_net', 'puts_net')}
                                  for k in PARTICIPANTS if s.get(k)}} for s in series]}


def stance(long_pct: Optional[float]) -> Optional[str]:
    """FII index futures: long-heavy over 60 % long, short-heavy under 40 %."""
    if long_pct is None:
        return None
    return 'long-heavy' if long_pct >= 60 else 'short-heavy' if long_pct <= 40 else 'balanced'


def deal_summary(deals: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Per stock: institutional buys and sells (client types in INSTITUTIONAL) and everyone's."""
    out: Dict[str, Dict[str, Any]] = {}
    for d in deals:
        s = out.setdefault(d['symbol'], {'n': 0, 'inst_buy_cr': 0.0, 'inst_sell_cr': 0.0, 'buy_cr': 0.0, 'sell_cr': 0.0,
                                         'last': None, 'clients': {}})
        s['n'] += 1
        key = 'buy_cr' if d['side'] == 'buy' else 'sell_cr'
        s[key] = round(s[key] + d['value_cr'], 2)
        if d['ctype'] in INSTITUTIONAL:
            ik = 'inst_buy_cr' if d['side'] == 'buy' else 'inst_sell_cr'
            s[ik] = round(s[ik] + d['value_cr'], 2)
        s['last'] = max(s['last'] or '', d['day'])
        c = s['clients'].setdefault(d['client'], {'ctype': d['ctype'], 'buy_cr': 0.0, 'sell_cr': 0.0})
        c['buy_cr' if d['side'] == 'buy' else 'sell_cr'] = round(c['buy_cr' if d['side'] == 'buy' else 'sell_cr'] + d['value_cr'], 2)
    for s in out.values():
        s['inst_net_cr'] = round(s['inst_buy_cr'] - s['inst_sell_cr'], 2)
        s['net_cr'] = round(s['buy_cr'] - s['sell_cr'], 2)
        top = sorted(s['clients'].items(), key=lambda kv: -(kv[1]['buy_cr'] + kv[1]['sell_cr']))[:3]
        s['top'] = [{'client': k, **v} for k, v in top]
        del s['clients']
    return out


# ---------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self, pace: float = PACE_S):
        self.pace = pace
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept-Language': 'en-IN,en;q=0.9'})
        self.last = 0.0
        self.lock = threading.Lock()

    def get(self, url: str, **kw) -> Optional[requests.Response]:
        """The response, or None for a file that isn't there (a holiday, not published yet)."""
        with self.lock:
            gap = self.pace - (time.time() - self.last)
            if gap > 0:
                time.sleep(gap)
            self.last = time.time()
        r = self.session.get(url, timeout=TIMEOUT, **kw)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r

    def deals(self) -> List[Dict[str, Any]]:
        out = []
        for kind, url in (('bulk', BULK_URL), ('block', BLOCK_URL)):
            r = self.get(url)
            out += parse_deals(r.text, kind) if r is not None else []
        return out

    def flows(self) -> List[Dict[str, Any]]:
        r = self.get(FLOWS_URL, headers={'Referer': 'https://www.nseindia.com/', 'Accept': 'application/json'})
        return parse_flows(r.json()) if r is not None else []

    def participant_oi(self, day: date) -> Optional[Dict[str, Dict[str, int]]]:
        r = self.get(POI_URL.format(d=day.strftime('%d%m%Y')))
        return parse_participant_oi(r.text) if r is not None else None

    def bhav(self, day: date) -> Optional[Dict[str, Dict[str, Any]]]:
        r = self.get(BHAV_URL.format(d=day.strftime('%d%m%Y')))
        return parse_bhav(r.text) if r is not None else None


_FETCHER: Optional[Fetcher] = None


def fetcher() -> Fetcher:
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = Fetcher()
    return _FETCHER


def weekdays_back(last: date, n: int) -> List[date]:
    """The last `n` weekdays up to and including `last`, oldest first (holidays are found by a 404)."""
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]
