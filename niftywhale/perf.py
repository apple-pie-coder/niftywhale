"""
Performance numbers for a list of trades (live paper trades or backtested):
headline statistics, the equity curve in R, and breakdowns by the context
recorded with each trade (niftywhale/features.py). Pure functions.

A breakdown bucket is only called out as notably good or bad when it has
enough trades and its average R is clearly away from the rest (a rough
t-test); small buckets are shown but greyed in the dashboard, because a
handful of trades says almost nothing.
"""
import math
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

MIN_BUCKET = 15


def closed(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in trades if t.get('status') in ('won', 'lost', 'closed') and t.get('r') is not None]


def summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    c = closed(trades)
    rs = [float(t['r']) for t in c]
    n = len(rs)
    if not n:
        return {'trades': 0, 'open': sum(t.get('status') == 'open' for t in trades)}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    curve, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        curve += r
        peak = max(peak, curve)
        dd = min(dd, curve - peak)
    return {
        'trades': n, 'open': sum(t.get('status') == 'open' for t in trades),
        'wins': len(wins), 'win_rate': round(100 * len(wins) / n, 1),
        'total_r': round(sum(rs), 2), 'avg_r': round(mean, 3),
        'avg_win': round(sum(wins) / len(wins), 2) if wins else None,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else None,
        'profit_factor': round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        'max_drawdown': round(dd, 2), 'sd': round(sd, 3),
        # Mean minus one standard error: what the autopilot maximises (rewards consistency and sample size).
        'lcb': round(mean - (sd / math.sqrt(n) if n > 1 else abs(mean)), 3),
        'first': c[0].get('entry_time'), 'last': c[-1].get('entry_time'),
    }


def equity(trades: List[Dict[str, Any]], max_points: int = 400) -> List[Tuple[str, float]]:
    """Cumulative R after each closed trade, thinned to at most `max_points`."""
    c = sorted(closed(trades), key=lambda t: t.get('exit_time') or t.get('entry_time') or '')
    pts, total = [], 0.0
    for t in c:
        total += float(t['r'])
        pts.append(((t.get('exit_time') or t.get('entry_time') or '')[:16], round(total, 2)))
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points - 1)] + [pts[-1]]
    return pts


def _minute_bucket(m):
    if m is None:
        return None
    return '09:15–10:30' if m < 630 else '10:30–12:00' if m < 720 else '12:00–13:30' if m < 810 else '13:30 on'


def _band(edges: List[float], fmt: str = '{:g}') -> Callable[[Any], Optional[str]]:
    """Value -> label of the band it falls in, for numeric features."""
    def f(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        for lo, hi in zip([-math.inf] + edges, edges + [math.inf]):
            if lo <= v < hi:
                if lo == -math.inf:
                    return f'< {fmt.format(hi)}'
                if hi == math.inf:
                    return f'≥ {fmt.format(lo)}'
                return f'{fmt.format(lo)}–{fmt.format(hi)}'
        return None
    return f


WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

# (key, label, how to bucket a trade) -- in display order.
DIMENSIONS: List[Tuple[str, str, Callable[[Dict[str, Any]], Optional[str]]]] = [
    ('side', 'Direction', lambda t: {'long': 'Long', 'short': 'Short'}.get(t.get('side'))),
    ('entry_time', 'Entry time', lambda t: _minute_bucket(t['f'].get('entry_minute'))),
    ('weekday', 'Weekday', lambda t: WEEKDAYS[t['f']['weekday']] if t['f'].get('weekday') is not None else None),
    ('market_trend', 'Nifty trend (50-day)', lambda t: t['f'].get('market_trend')),
    ('vix', 'India VIX', lambda t: _band([12, 15, 18, 22])(t['f'].get('vix'))),
    ('gap', 'Stock gap that day', lambda t: _band([-1, -0.3, 0.3, 1], '{:+g}%')(t['f'].get('gap_pct'))),
    ('rr', 'R:R at entry', lambda t: _band([2, 3, 4, 6], '{:g}')(t['f'].get('rr'))),
    ('position', 'Pullback depth (of leg)', lambda t: _band([0.2, 0.35, 0.5, 0.65], '{:.2f}')(t['f'].get('position'))),
    ('atr', 'Daily ATR %', lambda t: _band([1.5, 2, 3], '{:g}%')(t['f'].get('atr_pct'))),
    ('score', 'Setup score', lambda t: _band([40, 55, 70])(t['f'].get('score'))),
    ('risk', 'Stop distance', lambda t: _band([0.5, 1, 2, 4], '{:g}%')(t['f'].get('risk_pct'))),
    ('zone_age', 'Days zone waited (swing)', lambda t: _band([1, 3, 6])(t['f'].get('zone_age_days'))),
    ('target', 'Target pool (intraday)', lambda t: t['f'].get('target_kind')),
    ('stock_trend', 'Stock vs its 50-day', lambda t: _band([-5, 0, 5], '{:+g}%')(t['f'].get('stock_vs_ma50'))),
    # Live trades only: the backtester has no news history, so its trades fall outside these two.
    ('news', 'News in the 24 h before entry', lambda t: None if 'news_24h' not in t['f'] else
        'NSE filing' if t['f'].get('filing_24h') else 'media only' if t['f']['news_24h'] else 'none'),
    ('news_tone', 'News tone vs the trade (FinBERT, 24 h)', lambda t: news_tone_bucket(t)),
    # Smart money (smart.py), from what NSE published before the entry's day. Live trades only too.
    ('inst_deals', 'Institutional deals in the week before', lambda t: inst_deals_bucket(t)),
    ('delivery', 'Delivery the session before', lambda t: (t.get('f') or {}).get('deliv_read') if (t.get('f') or {}).get('sm') else None),
    ('fii_futures', 'FII index futures positioning', lambda t: fii_stance(t)),
    ('fii_cash', 'FII cash flow the session before', lambda t: None if (t.get('f') or {}).get('fii_cash_cr') is None
        else 'FII net buyers' if t['f']['fii_cash_cr'] > 0 else 'FII net sellers'),
]


def _side(t: Dict[str, Any]) -> int:
    return -1 if t.get('side') in ('short', 'bearish') else 1


def inst_deals_bucket(t: Dict[str, Any]) -> Optional[str]:
    """Funds, MFs, insurers or banks net buying (or selling) the stock in bulk / block deals the week
    before entry, turned to the trade's side."""
    f = t.get('f') or {}
    if not f.get('sm'):
        return None
    net = f.get('inst_deals_cr') or 0
    if not net:
        return 'none'
    return 'with the trade' if net * _side(t) > 0 else 'against the trade'


def fii_stance(t: Dict[str, Any]) -> Optional[str]:
    f = t.get('f') or {}
    lp = f.get('fii_long_pct')
    if lp is None:
        return None
    return 'FII long-heavy (60 %+ long)' if lp >= 60 else 'FII short-heavy (40 % long or less)' if lp <= 40 else 'FII balanced'

TONE_EDGE = 0.15     # an average FinBERT read inside ±this is neutral news


def news_tone_bucket(t: Dict[str, Any]) -> Optional[str]:
    """Did the news before the entry point the trade's way? FinBERT's average read of the 24 h
    before entry, turned to the trade's side: good news on a long (or bad news on a short) is
    'with the trade'."""
    f = t.get('f') or {}
    if 'news_24h' not in f:
        return None
    if not f['news_24h']:
        return 'no news'
    tone = f.get('news_tone_24h')
    if tone is None:
        return None                          # news, but not read by FinBERT (scorer down)
    aligned = tone * (-1 if t.get('side') in ('short', 'bearish') else 1)
    return 'with the trade' if aligned >= TONE_EDGE else 'against the trade' if aligned <= -TONE_EDGE else 'neutral news'


def breakdown(trades: List[Dict[str, Any]], dims=None) -> List[Dict[str, Any]]:
    """For each dimension, the buckets with n, win rate, average and total R, and
    a `signal` ('good' / 'bad') when a bucket of MIN_BUCKET+ trades differs from the
    rest by more than two standard errors."""
    c = closed(trades)
    for t in c:
        t.setdefault('f', {})
    out = []
    for key, label, fn in (dims or DIMENSIONS):
        groups: Dict[str, List[float]] = OrderedDict()
        for t in c:
            try:
                b = fn(t)
            except (KeyError, TypeError, IndexError):
                b = None
            if b is not None:
                groups.setdefault(b, []).append(float(t['r']))
        if len(groups) < 2:
            continue
        rows = []
        for b, rs in groups.items():
            n = len(rs)
            mean = sum(rs) / n
            others = [r for bb, rr in groups.items() if bb != b for r in rr]
            signal = None
            if n >= MIN_BUCKET and len(others) >= MIN_BUCKET:
                m2 = sum(others) / len(others)
                v1 = sum((r - mean) ** 2 for r in rs) / (n - 1)
                v2 = sum((r - m2) ** 2 for r in others) / (len(others) - 1)
                se = math.sqrt(v1 / n + v2 / len(others))
                if se > 0 and abs(mean - m2) / se >= 2:
                    signal = 'good' if mean > m2 else 'bad'
            rows.append({'bucket': b, 'n': n, 'win_rate': round(100 * sum(r > 0 for r in rs) / n, 1),
                         'avg_r': round(mean, 3), 'total_r': round(sum(rs), 2), 'signal': signal,
                         'small': n < MIN_BUCKET})
        out.append({'key': key, 'label': label, 'rows': sorted(rows, key=_order)})
    return out


def _order(row):
    b = row['bucket']
    if b.startswith('<'):
        return (0, b)
    if b.startswith('≥'):
        return (2, b)
    return (1, b)


def highlights(bd: List[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    """The buckets that stand out, strongest first: what the weekly report mentions."""
    flagged = [{'dimension': d['label'], **r} for d in bd for r in d['rows'] if r['signal']]
    flagged.sort(key=lambda r: -abs(r['avg_r']) * math.sqrt(r['n']))
    return flagged[:limit]


def by_period(trades: List[Dict[str, Any]], length: int = 7) -> Dict[str, Any]:
    """Totals for the last `length` days of closed trades (by exit)."""
    from datetime import datetime, timedelta
    cut = (datetime.now() - timedelta(days=length)).isoformat()
    recent = [t for t in closed(trades) if (t.get('exit_time') or t.get('entry_time') or '') >= cut]
    return summary(recent)
