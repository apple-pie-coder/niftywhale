"""
The index ticker: live prices of the market's indices, their change on the
previous session's close, and where each sits in its day's range.

Dhan's quote API gives the last price and the day's open/high/low, but its
`close` field is not reliably the previous close (after the session it is
today's), so the previous close comes from daily candles, read once a session.
"""
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

# (Dhan IDX_I security id, short label, full name, group). Dhan lists ~190 indices;
# these are the ones a trader watches: headline, volatility, broad market, sectors, themes.
INDEXES = [
    (13, 'NIFTY 50', 'Nifty 50', 'headline'),
    (51, 'SENSEX', 'BSE Sensex', 'headline'),
    (25, 'BANK NIFTY', 'Nifty Bank', 'headline'),
    (27, 'FIN NIFTY', 'Nifty Financial Services', 'headline'),
    (442, 'MIDCAP SELECT', 'Nifty Midcap Select', 'headline'),
    (21, 'INDIA VIX', 'India VIX (expected 30-day volatility)', 'volatility'),
    (5024, 'GIFT NIFTY', 'GIFT Nifty (NSE IX, trades almost round the clock)', 'headline'),
    (38, 'NIFTY NEXT 50', 'Nifty Next 50', 'broad'),
    (17, 'NIFTY 100', 'Nifty 100', 'broad'),
    (19, 'NIFTY 500', 'Nifty 500', 'broad'),
    (37, 'MIDCAP 100', 'Nifty Midcap 100', 'broad'),
    (1, 'MIDCAP 150', 'Nifty Midcap 150', 'broad'),
    (5, 'SMALLCAP 100', 'Nifty Smallcap 100', 'broad'),
    (3, 'SMALLCAP 250', 'Nifty Smallcap 250', 'broad'),
    (29, 'IT', 'Nifty IT', 'sector'),
    (14, 'AUTO', 'Nifty Auto', 'sector'),
    (28, 'FMCG', 'Nifty FMCG', 'sector'),
    (32, 'PHARMA', 'Nifty Pharma', 'sector'),
    (447, 'HEALTHCARE', 'Nifty Healthcare', 'sector'),
    (31, 'METAL', 'Nifty Metal', 'sector'),
    (42, 'ENERGY', 'Nifty Energy', 'sector'),
    (470, 'OIL & GAS', 'Nifty Oil and Gas', 'sector'),
    (34, 'REALTY', 'Nifty Realty', 'sector'),
    (30, 'MEDIA', 'Nifty Media', 'sector'),
    (33, 'PSU BANK', 'Nifty PSU Bank', 'sector'),
    (15, 'PVT BANK', 'Nifty Private Bank', 'sector'),
    (466, 'CONS DURABLES', 'Nifty Consumer Durables', 'sector'),
    (803, 'CAPITAL MKT', 'Nifty Capital Markets', 'theme'),
    (493, 'DEFENCE', 'Nifty India Defence', 'theme'),
    (45, 'CPSE', 'Nifty CPSE', 'theme'),
    (41, 'PSE', 'Nifty PSE', 'theme'),
    (43, 'INFRA', 'Nifty Infrastructure', 'theme'),
    (40, 'CONSUMPTION', 'Nifty India Consumption', 'theme'),
    (44, 'MNC', 'Nifty MNC', 'theme'),
    (69, 'BANKEX', 'BSE Bankex', 'sector'),
]
IDS = [i[0] for i in INDEXES]
NIFTY, GIFT, VIX = 13, 5024, 21
# Tickers with an option chain in options mode: clicking one opens it.
OPTION_SYMBOL = {13: 'NIFTY', 25: 'BANKNIFTY', 27: 'FINNIFTY', 442: 'MIDCPNIFTY', 51: 'SENSEX'}


def session_date(now: datetime) -> date:
    """The session the latest quotes belong to: today from 09:00 on a weekday, else the
    weekday before (exchange holidays are not modelled, as elsewhere)."""
    d = now.date()
    if now.weekday() < 5 and (now.hour, now.minute) >= (9, 0):
        return d
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def prev_close(daily: List[tuple], session: date) -> Optional[float]:
    """The close of the last daily candle before `session`. `daily` is [(date, close)]."""
    before = [c for d, c in sorted(daily) if d < session and c]
    return before[-1] if before else None


def day_position(last: Optional[float], low: Optional[float], high: Optional[float]) -> Optional[float]:
    """Where `last` sits in the day's range: 0 at the low, 100 at the high."""
    if not last or not low or not high or high <= low:
        return None
    return round(max(0.0, min(100.0, (last - low) / (high - low) * 100)), 1)


def build(quotes: Dict[int, Dict[str, float]], prev: Dict[int, Optional[float]]) -> List[Dict[str, Any]]:
    """One row per index in INDEXES order, skipping any without a price."""
    rows = []
    nifty = (quotes.get(NIFTY) or {}).get('last')
    for sid, label, name, group in INDEXES:
        q = quotes.get(sid)
        if not q or not q.get('last'):
            continue
        last = q['last']
        pc = prev.get(sid)
        ok = lambda v: v if v and v > 0 else None      # GIFT Nifty's quote has no day OHLC (zeros)
        low, high, opn = ok(q.get('low')), ok(q.get('high')), ok(q.get('open'))
        row = {'id': sid, 'label': label, 'name': name, 'group': group, 'last': last,
               'open': opn, 'high': high, 'low': low, 'prev_close': pc,
               'change': round(last - pc, 2) if pc else None,
               'change_pct': round((last - pc) / pc * 100, 2) if pc else None,
               'position': day_position(last, low, high),
               'option': OPTION_SYMBOL.get(sid)}
        if sid == GIFT and nifty:
            row['vs_nifty'] = round(last - nifty, 2)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- the market's mood
# The page's background tint (index.html, applyMood) follows it. Three things, from the strip's own rows:
#   Nifty 50's move              half the weight: ±0.8 % is already a strong day
#   breadth                      how many indices are up rather than down (sectors, themes, broad market)
#   India VIX's move             fear rising pulls the mood down, and with VIX's level makes `fear`
MOOD_WORDS = ((0.6, 'Euphoric'), (0.25, 'Upbeat'), (-0.25, 'Calm'), (-0.6, 'Nervous'))


def mood(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """{score -1 (fear) .. +1 (euphoria), fear 0..1, label, and what it was made from}, or None
    without Nifty's change (no previous close yet)."""
    by = {r['id']: r for r in rows}
    nifty = (by.get(NIFTY) or {}).get('change_pct')
    if nifty is None:
        return None
    pool = [r['change_pct'] for r in rows if r['id'] not in (GIFT, VIX) and r.get('change_pct') is not None]
    up, down = sum(1 for c in pool if c > 0), sum(1 for c in pool if c < 0)
    breadth = (up - down) / len(pool) if pool else 0.0
    v = by.get(VIX) or {}
    vix, vix_chg = v.get('last'), v.get('change_pct') or 0.0
    score = 0.5 * math.tanh(nifty / 0.8) + 0.35 * breadth - 0.15 * math.tanh(vix_chg / 6)
    score = max(-1.0, min(1.0, score))
    fear = max(0.0, min(1.0, max((vix - 14) / 10 if vix else 0.0, vix_chg / 15)))
    label = next((w for cut, w in MOOD_WORDS if score >= cut), 'Fearful')
    if label == 'Calm' and fear >= 0.5:
        label = 'Uneasy'
    return {'score': round(score, 3), 'fear': round(fear, 3), 'label': label, 'nifty': nifty, 'up': up, 'down': down,
            'count': len(pool), 'vix': vix, 'vix_chg': round(vix_chg, 2)}
