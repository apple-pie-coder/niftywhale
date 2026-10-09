"""
Step 1: the universe a scan screens.

The protocol itself says Nifty 100 or F&O stocks only; those two (and their
union) stay the first options. Every other NSE index is here too, for slicing
the market differently -- by size, by sector, by theme. Step 2's liquidity
filter still applies to all of them, so a small-cap universe screens down to
its liquid names rather than flooding the setups with illiquid ones.

All lists come straight from the exchange (niftyindices.com and NSE's F&O
lot-size file), not approximated by market cap. One build downloads them all
and records, per stock, which indices it belongs to.
"""
import csv
import io
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

UNIVERSE_PATH = Path(os.getenv('UNIVERSE_PATH', 'data/universe.json'))
INDEX_URL = 'https://niftyindices.com/IndexConstituent/ind_{slug}list.csv'
FO_URL = 'https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv'
# NSE rejects requests without a browser-like agent.
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/120 Safari/537.36'}

# key -> (label, group). The key is also the niftyindices.com file name.
# Order here is the order in the dashboard's dropdown.
INDICES: Dict[str, tuple] = {
    'nifty50':               ('Nifty 50', 'Broad market'),
    'niftynext50':           ('Nifty Next 50', 'Broad market'),
    'nifty100':              ('Nifty 100', 'Broad market'),
    'nifty200':              ('Nifty 200', 'Broad market'),
    'nifty500':              ('Nifty 500', 'Broad market'),
    'niftymidcap100':        ('Nifty Midcap 100', 'Broad market'),
    'niftymidcap150':        ('Nifty Midcap 150', 'Broad market'),
    'niftysmallcap250':      ('Nifty Smallcap 250', 'Broad market'),
    'niftymidsmallcap400':   ('Nifty MidSmallcap 400', 'Broad market'),
    'niftybank':             ('Nifty Bank', 'Sectors'),
    'niftypsubank':          ('Nifty PSU Bank', 'Sectors'),
    'niftyfinance':          ('Nifty Financial Services', 'Sectors'),
    'niftyit':               ('Nifty IT', 'Sectors'),
    'niftypharma':           ('Nifty Pharma', 'Sectors'),
    'niftyhealthcare':       ('Nifty Healthcare', 'Sectors'),
    'niftyauto':             ('Nifty Auto', 'Sectors'),
    'niftyfmcg':             ('Nifty FMCG', 'Sectors'),
    'niftymetal':            ('Nifty Metal', 'Sectors'),
    'niftyenergy':           ('Nifty Energy', 'Sectors'),
    'niftyoilgas':           ('Nifty Oil & Gas', 'Sectors'),
    'niftyrealty':           ('Nifty Realty', 'Sectors'),
    'niftymedia':            ('Nifty Media', 'Sectors'),
    'niftyconsumerdurables': ('Nifty Consumer Durables', 'Sectors'),
    'niftycpse':             ('Nifty CPSE', 'Themes'),
    'niftypse':              ('Nifty PSE', 'Themes'),
    'niftyinfra':            ('Nifty Infrastructure', 'Themes'),
    'niftyconsumption':      ('Nifty India Consumption', 'Themes'),
    'niftymnc':              ('Nifty MNC', 'Themes'),
}
# The protocol's own choices (Step 1). 'nifty100' is both an index and one of these.
PROTOCOL = {'nifty100': 'Nifty 100', 'fo': 'F&O stocks', 'both': 'Nifty 100 + F&O'}
UNIVERSES = tuple(PROTOCOL) + tuple(k for k in INDICES if k not in PROTOCOL)


def valid(which: str) -> bool:
    return which in UNIVERSES


def label(which: str) -> str:
    return PROTOCOL.get(which) or INDICES.get(which, (which,))[0]


def _get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def _index_rows(slug: str) -> List[Dict[str, str]]:
    """One index's constituents. Empty if NSE answered with anything but the CSV."""
    text = _get(INDEX_URL.format(slug=slug))
    rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get('Symbol') or '').strip()]
    return rows


def build(previous: Optional[Dict] = None) -> Dict[str, object]:
    """
    Download every list and merge them into one record per stock.

    A list that fails to download keeps the members it had in `previous`
    rather than emptying that index; only the Nifty 100 is mandatory, because
    it is the protocol's own universe.
    """
    old_members: Dict[str, List[str]] = {}
    for s in (previous or {}).get('stocks', []):
        for k in s.get('indices', []):
            old_members.setdefault(k, []).append(s['symbol'])
    old_by_symbol = {s['symbol']: s for s in (previous or {}).get('stocks', [])}

    stocks: Dict[str, Dict] = {}
    failed: List[str] = []

    def entry(sym: str, name: str, industry: str = '') -> Dict:
        e = stocks.get(sym)
        if e is None:
            e = stocks[sym] = {'symbol': sym, 'name': name or sym, 'industry': industry,
                               'nifty100': False, 'fo': False, 'indices': []}
        if industry and not e['industry']:
            e['industry'] = industry
        return e

    for slug in INDICES:
        try:
            rows = _index_rows(slug)
            if not rows:
                raise ValueError('no constituents in the file')
            for r in rows:
                if is_placeholder(r['Symbol']):
                    continue
                e = entry(r['Symbol'].strip().upper(), (r.get('Company Name') or '').strip(),
                          (r.get('Industry') or '').strip())
                e['indices'].append(slug)
        except (requests.RequestException, ValueError) as ex:
            failed.append(slug)
            logger.warning(f'Universe: {slug} not refreshed ({ex}); keeping its previous members')
            for sym in old_members.get(slug, []):
                o = old_by_symbol.get(sym, {})
                entry(sym, o.get('name', sym), o.get('industry', ''))['indices'].append(slug)
        time.sleep(0.3)                      # niftyindices.com rate-limits bursts

    for e in stocks.values():
        e['nifty100'] = 'nifty100' in e['indices']
    if sum(e['nifty100'] for e in stocks.values()) < 90:
        raise ValueError('Nifty 100 list looks incomplete; refusing to overwrite the universe')

    reader = csv.reader(io.StringIO(_get(FO_URL)))
    next(reader, None)                                   # header
    for row in reader:
        if len(row) < 2:
            continue
        name, sym = row[0].strip(), row[1].strip().upper()
        # Index derivatives share this file with stocks, and every one is
        # named "NIFTY ..." (NIFTY 50, NIFTY BANK, NIFTY FPI 150, ...).
        if not sym or sym == 'SYMBOL' or name.upper().startswith('NIFTY'):
            continue
        entry(sym, name.title())['fo'] = True

    return {'built_at': datetime.now().isoformat(timespec='seconds'),
            'failed': failed,
            'stocks': sorted(stocks.values(), key=lambda s: s['symbol'])}


def save(universe: Dict[str, object], path: Path = None) -> None:
    path = Path(path or UNIVERSE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(universe, indent=1))
    tmp.replace(path)                                    # never a half-written file


def is_placeholder(symbol: str) -> bool:
    """NSE pads some index files with dummy rows (DUMMYHEG in the Smallcap 250 list): no
    such stock trades, so every scan would only fail to download it."""
    return symbol.strip().upper().startswith('DUMMY')


def load(path: Path = None) -> Dict[str, object]:
    with open(Path(path or UNIVERSE_PATH), encoding='utf-8') as f:
        u = json.load(f)
    # Files built before placeholders were skipped still list them.
    if isinstance(u, dict) and isinstance(u.get('stocks'), list):
        u['stocks'] = [x for x in u['stocks'] if not is_placeholder(x.get('symbol', ''))]
    return u


def _in(stock: Dict, which: str) -> bool:
    if which == 'nifty100':
        return stock.get('nifty100', False)
    if which == 'fo':
        return stock.get('fo', False)
    if which == 'both':
        return stock.get('nifty100', False) or stock.get('fo', False)
    return which in stock.get('indices', [])


def members(which: str = 'nifty100', path: Path = None) -> List[Dict]:
    """The stocks in one universe, each with its yfinance ticker attached."""
    return [{**s, 'ticker': s['symbol'] + '.NS'} for s in load(path).get('stocks', []) if _in(s, which)]


def options(u: Optional[Dict] = None) -> List[Dict]:
    """Every universe with its group and size, for the dashboard's picker."""
    stocks = (u or load()).get('stocks', [])
    out = [{'key': k, 'label': v, 'group': 'Protocol (Step 1)'} for k, v in PROTOCOL.items()]
    out += [{'key': k, 'label': lbl, 'group': grp} for k, (lbl, grp) in INDICES.items() if k not in PROTOCOL]
    for o in out:
        o['count'] = sum(1 for s in stocks if _in(s, o['key']))
    return out


def tag(stock: Dict) -> str:
    """A short label for search results: the protocol lists first, else the
    smallest broad index the stock is in."""
    if stock.get('nifty100'):
        return 'Nifty 100'
    if stock.get('fo'):
        return 'F&O'
    for k in ('niftymidcap100', 'niftymidcap150', 'nifty200', 'niftysmallcap250', 'nifty500'):
        if k in stock.get('indices', []):
            return INDICES[k][0].replace('Nifty ', '')
    return 'Index'


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        prev = load()
    except (OSError, ValueError):
        prev = None
    u = build(prev)
    save(u)
    s = u['stocks']
    print(f"{len(s)} stocks: {sum(x['nifty100'] for x in s)} Nifty 100, {sum(x['fo'] for x in s)} F&O, "
          f"{len(INDICES)} indices" + (f" (not refreshed: {', '.join(u['failed'])})" if u['failed'] else ''))
