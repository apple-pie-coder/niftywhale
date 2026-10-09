"""
Option chain analysis: what the open interest, volume and implied volatility
of one expiry say, and the simple option-buying ideas that follow from it.

Everything here is a pure function of chains already fetched (dhan.option_chain),
so it is tested on hand-built chains. The scheduler in app.py does the fetching,
storing and alerting.

Reading a chain
  Open interest (OI) is the number of contracts open at a strike. Option
  writers (sellers) hold most of it, so a strike with the most call OI is where
  writers bet price will *not* go above (resistance), and the most put OI is where
  they bet it will not go below (support).

  Price and OI moving together say who is acting:
      price up,   OI up    long buildup     buyers opening
      price down, OI up    short buildup    writers opening
      price up,   OI down  short covering   writers closing
      price down, OI down  long unwinding   buyers closing

  Fresh put writing near the money is bullish (writers expect support to
  hold); fresh call writing is bearish. That balance is the `bias` here.
"""
import math
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

INDICES = {
    # symbol: (Dhan security id of the index, exchange segment, display name)
    'NIFTY': (13, 'IDX_I', 'Nifty 50'),
    'BANKNIFTY': (25, 'IDX_I', 'Nifty Bank'),
    'FINNIFTY': (27, 'IDX_I', 'Nifty Financial Services'),
    'MIDCPNIFTY': (442, 'IDX_I', 'Nifty Midcap Select'),
    'SENSEX': (51, 'IDX_I', 'BSE Sensex'),
}
DEFAULT_STOCKS = ('RELIANCE HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK INFY TCS HCLTECH LT ITC BHARTIARTL '
                  'BAJFINANCE MARUTI M&M TATASTEEL HINDALCO JSWSTEEL ADANIENT ADANIPORTS SUNPHARMA TITAN '
                  'ASIANPAINT BAJAJ-AUTO NTPC POWERGRID ONGC COALINDIA ULTRACEMCO DLF').split()
MAX_STOCKS = 60

SIDES = ('ce', 'pe')
FIELDS = ('oi', 'poi', 'ltp', 'pc', 'vol', 'pvol', 'iv', 'delta', 'bid', 'ask')
BUILDUPS = {
    'long_buildup': 'Long buildup', 'short_buildup': 'Short buildup',
    'short_covering': 'Short covering', 'long_unwinding': 'Long unwinding', 'neutral': '—',
}


def is_index(symbol: str) -> bool:
    return symbol in INDICES


# ---------------------------------------------------------------- parsing
def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _side(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    if not raw:
        return None
    g = raw.get('greeks') or {}
    return {
        'oi': _num(raw.get('oi')), 'poi': _num(raw.get('previous_oi')),
        'ltp': _num(raw.get('last_price')), 'pc': _num(raw.get('previous_close_price')),
        'vol': _num(raw.get('volume')), 'pvol': _num(raw.get('previous_volume')),
        'iv': round(_num(raw.get('implied_volatility')), 2), 'delta': round(_num(g.get('delta')), 4),
        'bid': _num(raw.get('top_bid_price')), 'ask': _num(raw.get('top_ask_price')),
    }


def parse_chain(data: Dict[str, Any], window_pct: float = 8.0, max_strikes: int = 60) -> Dict[str, Any]:
    """Dhan's {last_price, oc: {"23250.000000": {ce, pe}}} -> {spot, strikes: [...]}, sorted by
    strike and trimmed to `max_strikes` strikes within `window_pct` of spot: far strikes add
    noise (and storage), not information."""
    spot = _num(data.get('last_price'))
    rows = []
    for k, v in (data.get('oc') or {}).items():
        strike = _num(k, None)
        if strike is None or not isinstance(v, dict):
            continue
        ce, pe = _side(v.get('ce')), _side(v.get('pe'))
        if not ce and not pe:
            continue
        rows.append({'k': strike, 'ce': ce, 'pe': pe})
    # Chain-wide totals and max pain come from every strike: a window that moves with spot
    # would make the PCR drift whenever price does, with no change in positioning.
    full = totals(rows)
    full['max_pain'] = max_pain(rows)
    if spot > 0:
        rows = [r for r in rows if abs(r['k'] - spot) / spot * 100 <= window_pct]
        rows.sort(key=lambda r: abs(r['k'] - spot))
        rows = rows[:max_strikes]
    rows.sort(key=lambda r: r['k'])
    return {'spot': spot, 'strikes': rows, 'all': full}


def compact(chain: Dict[str, Any]) -> Dict[str, Any]:
    """Columnar form for storage: a third of the size of the row form."""
    out = {'spot': chain['spot'], 'k': [r['k'] for r in chain['strikes']], 'all': chain.get('all')}
    for s in SIDES:
        out[s] = {f: [(r[s] or {}).get(f) if r[s] else None for r in chain['strikes']] for f in FIELDS}
    return out


def expand(c: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for i, k in enumerate(c.get('k') or []):
        row = {'k': k}
        for s in SIDES:
            cols = c.get(s) or {}
            row[s] = None if cols.get('oi', [None])[i] is None else {f: cols[f][i] for f in FIELDS if f in cols}
        rows.append(row)
    out = {'spot': c.get('spot'), 'strikes': rows}
    if c.get('all'):
        out['all'] = c['all']
    return out


# ---------------------------------------------------------------- metrics
def strike_step(strikes: List[Dict[str, Any]]) -> float:
    ks = [r['k'] for r in strikes]
    gaps = sorted(b - a for a, b in zip(ks, ks[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else 0.0


def atm_strike(chain: Dict[str, Any]) -> Optional[float]:
    rows = chain['strikes']
    return min(rows, key=lambda r: abs(r['k'] - chain['spot']))['k'] if rows else None


def _oi(r, s) -> float:
    return (r[s] or {}).get('oi') or 0.0


def totals(strikes) -> Dict[str, float]:
    t = {f'{s}_{f}': sum((r[s] or {}).get(f) or 0 for r in strikes) for s in SIDES for f in ('oi', 'poi', 'vol')}
    return t


def pcr(strikes, t: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Put OI / call OI, from `t` (chain-wide totals) when given."""
    t = t or totals(strikes)
    return round(t['pe_oi'] / t['ce_oi'], 3) if t['ce_oi'] else None


def pcr_volume(strikes, t: Optional[Dict[str, float]] = None) -> Optional[float]:
    t = t or totals(strikes)
    return round(t['pe_vol'] / t['ce_vol'], 3) if t['ce_vol'] else None


def max_pain(strikes) -> Optional[float]:
    """The expiry price at which option buyers, together, would collect the least:
    for each candidate strike K, what calls below K and puts above K would pay out."""
    if not strikes:
        return None
    best, best_pay = None, None
    for cand in strikes:
        K = cand['k']
        pay = sum(_oi(r, 'ce') * max(0.0, K - r['k']) + _oi(r, 'pe') * max(0.0, r['k'] - K) for r in strikes)
        if best_pay is None or pay < best_pay:
            best, best_pay = K, pay
    return best


def walls(strikes, spot: float, side: str, n: int = 2) -> List[Tuple[float, float]]:
    """The `n` strikes with the most OI on `side` (ce above spot = resistance, pe below
    spot = support), as (strike, oi). Falls back to the whole chain when spot sits
    beyond every strike on that side."""
    near = [r for r in strikes if (r['k'] >= spot if side == 'ce' else r['k'] <= spot) and _oi(r, side) > 0]
    pool = near or [r for r in strikes if _oi(r, side) > 0]
    return [(r['k'], _oi(r, side)) for r in sorted(pool, key=lambda r: -_oi(r, side))[:n]]


def buildup(o: Optional[Dict[str, float]], ref: Optional[Dict[str, float]] = None,
            min_oi_pct: float = 2.0, min_price_pct: float = 1.0) -> str:
    """Classify one option's price and OI change: against the previous session
    (Dhan's previous close and OI) or, given `ref`, against an earlier snapshot."""
    if not o:
        return 'neutral'
    p0 = (ref or {}).get('ltp') if ref else o.get('pc')
    oi0 = (ref or {}).get('oi') if ref else o.get('poi')
    p1, oi1 = o.get('ltp') or 0, o.get('oi') or 0
    if not p0 or not oi0:
        return 'neutral'
    dp, doi = (p1 - p0) / p0 * 100, (oi1 - oi0) / oi0 * 100
    if abs(doi) < min_oi_pct or abs(dp) < min_price_pct:
        return 'neutral'
    if doi > 0:
        return 'long_buildup' if dp > 0 else 'short_buildup'
    return 'short_covering' if dp > 0 else 'long_unwinding'


def _near(chain, n: int):
    """The `n` strikes either side of the money."""
    rows = chain['strikes']
    if not rows:
        return []
    atm = atm_strike(chain)
    i = next(j for j, r in enumerate(rows) if r['k'] == atm)
    return rows[max(0, i - n): i + n + 1]


def oi_change(chain, ref: Optional[Dict[str, Any]] = None, n: int = 5) -> Dict[str, float]:
    """OI added near the money per side: since the previous session, or since `ref`."""
    near = _near(chain, n)
    if ref:
        before = {r['k']: r for r in ref['strikes']}
        d = {s: sum(_oi(r, s) - _oi(before[r['k']], s) for r in near if r['k'] in before) for s in SIDES}
    else:
        d = {s: sum(_oi(r, s) - ((r[s] or {}).get('poi') or 0) for r in near) for s in SIDES}
    return {'ce': d['ce'], 'pe': d['pe']}


def bias(chain, ref: Optional[Dict[str, Any]] = None, n: int = 5) -> float:
    """-1 .. +1: net put writing (+) or call writing (-) near the money. Unwinding
    counts the other way: puts closed is a support leaving, calls closed one less cap."""
    d = oi_change(chain, ref, n)
    span = abs(d['ce']) + abs(d['pe'])
    return round((d['pe'] - d['ce']) / span, 3) if span else 0.0


def bias_label(b: Optional[float], threshold: float = 0.25) -> str:
    if b is None:
        return 'neutral'
    return 'bullish' if b >= threshold else 'bearish' if b <= -threshold else 'neutral'


def atm_iv(chain) -> Optional[float]:
    """Implied volatility at the money: the mean of call and put IV at the strike nearest
    spot, or the nearest strike where both sides have one."""
    for r in sorted(chain['strikes'], key=lambda r: abs(r['k'] - chain['spot'])):
        ivs = [(r[s] or {}).get('iv') or 0 for s in SIDES]
        ivs = [v for v in ivs if v > 0]
        if len(ivs) == 2:
            return round(sum(ivs) / 2, 2)
    return None


def skew(chain, target: float = 0.25) -> Optional[float]:
    """Put IV minus call IV at about 25 delta: how much more the market pays to insure
    against a fall than to bet on a rise. Positive (puts dearer) is the normal state."""
    def pick(side, want):
        best = None
        for r in chain['strikes']:
            o = r[side]
            if not o or not o.get('iv') or not o.get('delta'):
                continue
            gap = abs(o['delta'] - want)
            if best is None or gap < best[0]:
                best = (gap, o['iv'])
        return best[1] if best and best[0] <= 0.15 else None
    p, c = pick('pe', -target), pick('ce', target)
    return round(p - c, 2) if p is not None and c is not None else None


def iv_percentile(today: Optional[float], history: List[float], min_days: int = 20) -> Optional[float]:
    """Share of past sessions whose ATM IV was below today's. Needs `min_days` of history."""
    past = [v for v in history if v is not None and v > 0]
    if today is None or len(past) < min_days:
        return None
    return round(100 * sum(1 for v in past if v < today) / len(past), 1)


def unusual(chain, ref: Optional[Dict[str, Any]] = None, limit: int = 6,
            min_oi_jump: float = 0.5, min_vol_mult: float = 3.0) -> List[Dict[str, Any]]:
    """Strikes where OI jumped by `min_oi_jump` (50 %) or more, or volume is `min_vol_mult`
    times the previous session's, and the change is big next to the chain (at least 10 %
    of its largest OI / volume), so tiny far strikes going 0 -> 10 contracts are ignored."""
    rows = chain['strikes']
    before = {r['k']: r for r in ref['strikes']} if ref else {}
    max_oi = max([_oi(r, s) for r in rows for s in SIDES] or [0])
    max_vol = max([(r[s] or {}).get('vol') or 0 for r in rows for s in SIDES] or [0])
    out = []
    for r in rows:
        for s in SIDES:
            o = r[s]
            if not o:
                continue
            base = _oi(before[r['k']], s) if ref and r['k'] in before else (o.get('poi') or 0)
            doi = (o.get('oi') or 0) - base
            jump = doi / base if base else (math.inf if doi > 0 else 0)
            vol, pvol = o.get('vol') or 0, o.get('pvol') or 0
            vmult = vol / pvol if pvol else (math.inf if vol else 0)
            oi_flag = jump >= min_oi_jump and doi >= 0.1 * max_oi
            vol_flag = vmult >= min_vol_mult and vol >= 0.1 * max_vol
            if oi_flag or vol_flag:
                out.append({'strike': r['k'], 'side': s.upper(), 'oi': o.get('oi'), 'doi': doi,
                            'oi_jump_pct': None if math.isinf(jump) else round(jump * 100, 1),
                            'vol': vol, 'vol_mult': None if math.isinf(vmult) else round(vmult, 1),
                            'ltp': o.get('ltp'), 'buildup': buildup(o, before.get(r['k'], {}).get(s) if ref else None),
                            'why': 'OI' if oi_flag and not vol_flag else 'volume' if vol_flag and not oi_flag else 'OI + volume'})
    out.sort(key=lambda u: -abs(u['doi']))
    return out[:limit]


def summarize(chain, ref: Optional[Dict[str, Any]] = None, iv_history: Optional[List[float]] = None) -> Dict[str, Any]:
    """Every headline number for one snapshot. `ref` (the session's first snapshot) adds
    intraday changes; `iv_history` (past sessions' ATM IV) the IV percentile."""
    rows, spot = chain['strikes'], chain['spot']
    full = chain.get('all')
    t = full or totals(rows)
    res, sup = walls(rows, spot, 'ce'), walls(rows, spot, 'pe')
    iv = atm_iv(chain)
    d_day = oi_change(chain, None)
    out = {
        'spot': spot, 'atm': atm_strike(chain), 'step': strike_step(rows),
        'pcr': pcr(rows, full), 'pcr_vol': pcr_volume(rows, full),
        'max_pain': full['max_pain'] if full and full.get('max_pain') is not None else max_pain(rows),
        'resistance': res[0][0] if res else None, 'resistance2': res[1][0] if len(res) > 1 else None,
        'support': sup[0][0] if sup else None, 'support2': sup[1][0] if len(sup) > 1 else None,
        'ce_oi': t['ce_oi'], 'pe_oi': t['pe_oi'], 'ce_doi': t['ce_oi'] - t['ce_poi'], 'pe_doi': t['pe_oi'] - t['pe_poi'],
        'atm_iv': iv, 'skew': skew(chain),
        'iv_pct': iv_percentile(iv, iv_history or []), 'iv_days': len([v for v in (iv_history or []) if v]),
        'near_ce_doi': d_day['ce'], 'near_pe_doi': d_day['pe'],
        'bias': bias(chain), 'bias_intraday': bias(chain, ref) if ref else None,
    }
    out['bias_label'] = bias_label(out['bias_intraday'] if out['bias_intraday'] is not None else out['bias'])
    if ref:
        out['pcr_open'] = pcr(ref['strikes'], ref.get('all'))
        out['iv_open'] = atm_iv(ref)
        out['spot_open'] = ref['spot']
    return out


def annotate(chain, ref: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The chain table: each strike with its day (and intraday) OI change and buildup."""
    before = {r['k']: r for r in ref['strikes']} if ref else {}
    out = []
    for r in chain['strikes']:
        row = {'k': r['k']}
        for s in SIDES:
            o = r[s]
            if not o:
                row[s] = None
                continue
            b = (before.get(r['k']) or {}).get(s) if ref else None
            row[s] = {**o, 'doi': (o.get('oi') or 0) - (o.get('poi') or 0),
                      'doi_intraday': ((o.get('oi') or 0) - (b.get('oi') or 0)) if b else None,
                      'chg_pct': round((o['ltp'] - o['pc']) / o['pc'] * 100, 1) if o.get('pc') else None,
                      'buildup': buildup(o), 'buildup_intraday': buildup(o, b) if b else None}
        out.append(row)
    return out


# ---------------------------------------------------------------- ideas
TUNABLE = {
    # key: (default, min, max, step, label)
    'min_bias': (0.35, 0.1, 0.9, 0.05, 'Minimum OI bias (near-the-money writing balance)'),
    'min_move_pct': (0.15, 0.0, 2.0, 0.05, 'Underlying move in the bias direction over the last 30 min (%)'),
    'max_iv_pct': (80.0, 10.0, 100.0, 5.0, 'Skip when ATM IV percentile is above (only once 20 days are recorded)'),
    'max_spread_pct': (3.0, 0.5, 15.0, 0.5, 'Widest bid-ask spread on the option (% of price)'),
    'stop_pct': (25.0, 5.0, 60.0, 1.0, 'Stop: premium falls by (%)'),
    'target_pct': (50.0, 10.0, 300.0, 5.0, 'Target: premium rises by (%)'),
    'max_per_day': (2, 1, 10, 1, 'Ideas per underlying per day'),
}
TIMES = {'start': '09:45', 'no_entry_after': '14:30', 'square_off': '15:15'}


def rules_from(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Defaults with saved overrides, clamped. Junk (NaN, bools, strings) keeps the default."""
    out = {}
    for k, (default, lo, hi, step, _label) in TUNABLE.items():
        v = (overrides or {}).get(k, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            v = default
        v = min(hi, max(lo, v))
        out[k] = int(round(v)) if isinstance(default, int) else round(v, 4)
    return {**out, **TIMES}


def _hhmm(s: str) -> dtime:
    return datetime.strptime(s, '%H:%M').time()


def pick_expiry(expiries: List[str], today: str, for_trade: bool) -> Optional[str]:
    """The nearest expiry; for a trade, the nearest that is not today (an option on its
    expiry day loses most of its time value within hours)."""
    future = sorted(e for e in expiries if e >= today)
    if for_trade:
        future = [e for e in future if e > today] or future
    return future[0] if future else None


def idea(symbol: str, chain: Dict[str, Any], summary: Dict[str, Any], history: List[Dict[str, Any]],
         rules: Dict[str, Any], now: datetime, taken_today: int, open_now: bool) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    An option-buying idea, or (None, why not). Bullish: net put writing near the money
    (bias >= min_bias), the underlying up at least min_move_pct over the last 30 minutes,
    and the PCR not falling since the open -> buy the at-the-money call. Bearish mirrors it
    with the put. Skipped when IV is high next to its own history, the option's spread is
    wide, outside the entry window, or the underlying already has an open idea / its quota.
    `history` is today's earlier summaries for this symbol and expiry, oldest first, each
    with its 'ts'.
    """
    t = now.time()
    if t < _hhmm(rules['start']) or t >= _hhmm(rules['no_entry_after']):
        return None, f"outside the entry window ({rules['start']}–{rules['no_entry_after']})"
    if open_now:
        return None, 'an idea is already open'
    if taken_today >= rules['max_per_day']:
        return None, f"{taken_today} idea(s) today already"
    b = summary.get('bias_intraday')
    if b is None:
        return None, 'no opening snapshot yet'
    direction = 'long' if b >= rules['min_bias'] else 'short' if b <= -rules['min_bias'] else None
    if not direction:
        return None, f'OI bias {b:+.2f} is inside ±{rules["min_bias"]}'
    past = [h for h in history if h.get('ts') and (now - datetime.fromisoformat(h['ts'])).total_seconds() >= 25 * 60]
    if not past:
        return None, 'not 30 minutes of snapshots yet'
    ref = past[-1]
    move = (summary['spot'] - ref['spot']) / ref['spot'] * 100 if ref.get('spot') else 0
    if (direction == 'long' and move < rules['min_move_pct']) or (direction == 'short' and move > -rules['min_move_pct']):
        return None, f'{"bullish" if direction == "long" else "bearish"} OI, but the underlying moved {move:+.2f}% in 30 min'
    if summary.get('pcr') is not None and summary.get('pcr_open') is not None:
        if (direction == 'long' and summary['pcr'] < summary['pcr_open']) or (direction == 'short' and summary['pcr'] > summary['pcr_open']):
            return None, f"PCR {summary['pcr_open']:.2f} → {summary['pcr']:.2f} disagrees"
    if summary.get('iv_pct') is not None and summary['iv_pct'] > rules['max_iv_pct']:
        return None, f"ATM IV is in its {summary['iv_pct']:.0f}th percentile: options are dear"
    side = 'ce' if direction == 'long' else 'pe'
    atm = summary.get('atm')
    row = next((r for r in chain['strikes'] if r['k'] == atm), None)
    o = (row or {}).get(side)
    if not o or not o.get('ltp'):
        return None, 'no price at the money'
    entry = o['ask'] if o.get('ask') else o['ltp']
    if o.get('bid') and o.get('ask'):
        spread = (o['ask'] - o['bid']) / o['ask'] * 100
        if spread > rules['max_spread_pct']:
            return None, f'bid-ask spread {spread:.1f}% is too wide'
    stop = round(entry * (1 - rules['stop_pct'] / 100), 2)
    target = round(entry * (1 + rules['target_pct'] / 100), 2)
    level = summary.get('support') if direction == 'long' else summary.get('resistance')
    why = (f"{'put' if direction == 'long' else 'call'} writing near the money (bias {b:+.2f}), "
           f"underlying {move:+.2f}% in 30 min"
           + (f", PCR {summary['pcr_open']:.2f}→{summary['pcr']:.2f}" if summary.get('pcr_open') is not None and summary.get('pcr') is not None else '')
           + (f", ATM IV {summary['atm_iv']:.1f}%" if summary.get('atm_iv') else ''))
    return {'symbol': symbol, 'side': side.upper(), 'direction': direction, 'strike': atm,
            'entry': round(entry, 2), 'stop': stop, 'target': target, 'spot': summary['spot'],
            'level': level, 'bias': b, 'move_pct': round(move, 2), 'reason': why}, ''


def follow(i: Dict[str, Any], chain: Dict[str, Any], now: datetime, square_off: str = TIMES['square_off'],
           final: bool = False) -> Optional[Dict[str, Any]]:
    """
    Move an open idea along with a new snapshot. Returns the closing fields
    (status, exit, r, note) or None while it stays open (with i['last'] and
    i['r_open'] updated). Target fills at the target (a resting limit order);
    a stop fills at the price seen (a stop can slip); the underlying closing
    through the OI level the idea leaned on (support for a call, resistance
    for a put) exits at market; at the square-off time, or `final`, it exits
    at the last price.
    """
    row = next((r for r in chain['strikes'] if r['k'] == i['strike']), None)
    o = (row or {}).get(i['side'].lower())
    last = o.get('ltp') if o else None
    risk = i['entry'] - i['stop']
    r_of = lambda px: round((px - i['entry']) / risk, 2) if risk > 0 else 0.0
    if last:
        i['last'], i['r_open'] = last, r_of(last)
    spot = chain.get('spot')
    if last and last >= i['target']:
        return {'status': 'won', 'exit': i['target'], 'r': r_of(i['target']), 'note': 'target'}
    if last and last <= i['stop']:
        return {'status': 'lost', 'exit': last, 'r': r_of(last), 'note': 'stop'}
    lvl = i.get('level')
    if last and lvl and spot and ((i['direction'] == 'long' and spot < lvl) or (i['direction'] == 'short' and spot > lvl)):
        r = r_of(last)
        return {'status': 'won' if r > 0 else 'lost', 'exit': last, 'r': r,
                'note': f"{'support' if i['direction'] == 'long' else 'resistance'} {lvl:g} broke"}
    if final or now.time() >= _hhmm(square_off):
        px = last or i.get('last') or i['entry']
        return {'status': 'closed', 'exit': px, 'r': r_of(px), 'note': 'squared off'}
    return None


# ---------------------------------------------------------------- learning (autopilot)
# The idea filters the autopilot may move, and the exits it may try. Ideas are only
# what the CURRENT rules let through; to learn whether looser or tighter rules would
# do better, every look at a chain that comes near an idea is kept as a candidate,
# and the lab works out afterwards what each would have done for every exit pair.
LEARN_FILTERS = ('min_bias', 'min_move_pct', 'max_iv_pct', 'max_spread_pct')
EXIT_GRID = {'stop_pct': (15.0, 20.0, 25.0, 30.0, 40.0), 'target_pct': (30.0, 40.0, 50.0, 75.0, 100.0)}
CANDIDATE_MIN_BIAS = 0.15


def candidate(symbol: str, chain: Dict[str, Any], summary: Dict[str, Any], history: List[Dict[str, Any]],
              now: datetime, rules: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The numbers an idea is judged on, measured whatever the thresholds: inside the
    entry window, with a clear bias (|bias| >= 0.15), 30 minutes of history, the
    underlying not moving against the bias, and a price at the money. None otherwise."""
    t = now.time()
    if t < _hhmm(rules['start']) or t >= _hhmm(rules['no_entry_after']):
        return None
    b = summary.get('bias_intraday')
    if b is None or abs(b) < CANDIDATE_MIN_BIAS:
        return None
    direction = 'long' if b > 0 else 'short'
    past = [h for h in history if h.get('ts') and (now - datetime.fromisoformat(h['ts'])).total_seconds() >= 25 * 60]
    if not past or not past[-1].get('spot'):
        return None
    move = (summary['spot'] - past[-1]['spot']) / past[-1]['spot'] * 100
    if (move < 0 and direction == 'long') or (move > 0 and direction == 'short'):
        return None
    side = 'ce' if direction == 'long' else 'pe'
    row = next((r for r in chain['strikes'] if r['k'] == summary.get('atm')), None)
    o = (row or {}).get(side)
    if not o or not o.get('ltp'):
        return None
    spread = (o['ask'] - o['bid']) / o['ask'] * 100 if o.get('bid') and o.get('ask') else None
    pcr_ok = True
    if summary.get('pcr') is not None and summary.get('pcr_open') is not None:
        pcr_ok = summary['pcr'] >= summary['pcr_open'] if direction == 'long' else summary['pcr'] <= summary['pcr_open']
    return {'symbol': symbol, 'direction': direction, 'side': side.upper(), 'strike': summary.get('atm'),
            'entry': round(o['ask'] if o.get('ask') else o['ltp'], 2),
            'level': summary.get('support') if direction == 'long' else summary.get('resistance'),
            'f': {'bias': round(abs(b), 3), 'move_pct': round(abs(move), 3), 'pcr_ok': pcr_ok,
                  'iv_pct': summary.get('iv_pct'), 'atm_iv': summary.get('atm_iv'),
                  'spread_pct': round(spread, 2) if spread is not None else None,
                  'entry_minute': t.hour * 60 + t.minute, 'dte': summary.get('dte'),
                  'index': is_index(symbol)}}


def candidate_passes(c: Dict[str, Any], thr: Dict[str, float]) -> bool:
    f = c['f']
    if not f.get('pcr_ok', True):
        return False
    if f['bias'] < thr['min_bias'] - 1e-9 or f['move_pct'] < thr['min_move_pct'] - 1e-9:
        return False
    if f.get('iv_pct') is not None and f['iv_pct'] > thr['max_iv_pct']:
        return False
    if f.get('spread_pct') is not None and f['spread_pct'] > thr['max_spread_pct']:
        return False
    return True


def path_outcome(path: List[Tuple[str, float, Optional[float]]], entry: float, stop_pct: float, target_pct: float,
                 direction: str, level: Optional[float], square_off: str = TIMES['square_off']) -> Dict[str, Any]:
    """What an idea bought at `entry` would have done along the option's later prices
    `path` [(iso time, option price, spot)], with the same exits as follow()."""
    stop, target = entry * (1 - stop_pct / 100), entry * (1 + target_pct / 100)
    risk = entry - stop
    r_of = lambda px: round((px - entry) / risk, 3) if risk > 0 else 0.0
    sq = _hhmm(square_off)
    last = None
    for at, px, spot in path:
        if not px:
            continue
        last = (at, px)
        if px >= target:
            return {'status': 'won', 'exit': round(target, 2), 'exit_time': at, 'r': r_of(target)}
        if px <= stop:
            return {'status': 'lost', 'exit': px, 'exit_time': at, 'r': r_of(px)}
        if level and spot and ((direction == 'long' and spot < level) or (direction == 'short' and spot > level)):
            r = r_of(px)
            return {'status': 'won' if r > 0 else 'lost', 'exit': px, 'exit_time': at, 'r': r}
        if datetime.fromisoformat(at).time() >= sq:
            return {'status': 'closed', 'exit': px, 'exit_time': at, 'r': r_of(px)}
    if last:
        return {'status': 'closed', 'exit': last[1], 'exit_time': last[0], 'r': r_of(last[1])}
    return {'status': 'closed', 'exit': entry, 'exit_time': None, 'r': 0.0}


def simulate_candidates(cands: List[Dict[str, Any]], thr: Dict[str, float]) -> List[Dict[str, Any]]:
    """The ideas a rule set would have taken from the candidates, with the live
    limits (one open idea per underlying, `max_per_day` a day) and its exit pair."""
    key = f"{thr['stop_pct']:g}/{thr['target_pct']:g}"
    trades = []
    busy: Dict[str, str] = {}             # symbol -> exit time of its open idea
    count: Dict[Tuple[str, str], int] = {}
    for c in sorted(cands, key=lambda c: c['ts']):
        o = (c.get('outcomes') or {}).get(key)
        if o is None or not candidate_passes(c, thr):
            continue
        day = c['ts'][:10]
        if busy.get(c['symbol'], '') > c['ts'] or count.get((c['symbol'], day), 0) >= thr.get('max_per_day', 2):
            continue
        busy[c['symbol']] = o.get('exit_time') or c['ts']
        count[(c['symbol'], day)] = count.get((c['symbol'], day), 0) + 1
        trades.append({'mode': 'options', 'symbol': c['symbol'], 'side': c['direction'], 'entry_time': c['ts'],
                       'entry': c['entry'], 'status': o['status'], 'exit': o.get('exit'), 'exit_time': o.get('exit_time'),
                       'r': o['r'], 'f': {**c['f'], 'rr': thr['target_pct'] / thr['stop_pct']}})
    return trades
