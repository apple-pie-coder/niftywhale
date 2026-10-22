"""
The news, smart-money and orderflow context of an entry, and the day's context of an option look (a candidate): what
was known at its time, recorded with it so results can be broken down by it and the autopilot can judge rules
on it. The app records them live; the lab fills them in for candidates recorded before they existed, from
the same tables (news kept 45 days, the option snapshots 30).
"""
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from niftywhale import features, history, options, orderflow, smart, store, universe


def news_context(symbol: str, at) -> dict:
    """What the desk had on a stock in the 24 hours before an entry, for the trade's
    context (features.py): the Performance tab can then say whether news mattered."""
    if store.settings().get('news') != '1':
        return {}
    at = pd.Timestamp(at)
    c = store.news_counts([symbol], (at - pd.Timedelta(hours=24)).isoformat(), at.isoformat()).get(symbol, {})
    out = {'news_24h': int(c.get('n') or 0), 'filing_24h': int(c.get('filings') or 0),
           'news_pos_24h': int(c.get('pos') or 0), 'news_neg_24h': int(c.get('neg') or 0)}
    if c.get('tone') is not None:
        out['news_tone_24h'] = c['tone']          # FinBERT's average read, -1 to +1
    return out


def smart_context(symbol: str, at) -> dict:
    """What NSE had published before an entry's day, for the trade's context: the stock's
    delivery read and its institutional deals of the week before, FII index-futures
    positioning (and its change over smart.FII_SESSIONS sessions) and FII cash flow of the session before."""
    try:
        day = pd.Timestamp(at).date().isoformat()
    except (TypeError, ValueError):
        return {}
    out = {'sm': 1}
    hist = store.delivery_history([symbol], smart.DELIV_SESSIONS + 1, until=day).get(symbol, [])
    rd = smart.delivery_read(hist)
    if rd:
        out.update(deliv_read=rd['read'], deliv_ratio=rd['ratio'], deliv_vol_ratio=rd['vol_ratio'])
    week = store.deals([symbol], since=(pd.Timestamp(day) - pd.Timedelta(days=8)).date().isoformat(), until=day)
    sm = smart.deal_summary(week).get(symbol)
    out['inst_deals_cr'] = sm['inst_net_cr'] if sm else 0.0
    pos = smart.positioning(store.poi(smart.FII_SESSIONS + 1, until=day))
    fii = ((pos.get('latest') or {}).get('FII') or {})
    if fii.get('long_pct') is not None:
        out['fii_long_pct'] = fii['long_pct']
    if pos.get('change5') is not None:
        out['fii_long_chg5'] = pos['change5']
    fl = [r for r in store.flows(10) if r['day'] < day and r['category'] == 'FII']
    if fl:
        out['fii_cash_cr'] = fl[-1]['net']
    # Futures open interest and results timing (backfill.py keeps them nightly), as the backtester records them.
    fut = store.series('sm_futoi', [symbol], 'oi, oi_chg, close', until=day, sessions=features.FUT_DAYS + 1).get(symbol, [])
    out.update(features.futures_context(fut))
    out.update(features.results_context(store.results_dates([symbol]).get(symbol, []), pd.Timestamp(day).date()))
    return out


def _epoch(at) -> float:
    ts = pd.Timestamp(at)
    return (ts.tz_localize('Asia/Kolkata') if ts.tzinfo is None else ts).timestamp()


def orderflow_context(bars: List[Dict[str, Any]], t: Dict[str, Any], side: str, trigger_s: int = 300) -> Dict[str, Any]:
    """Orderflow around an intraday entry, from the day's 1-minute bars (orderflow.reading): from the sweep to the
    CHoCH candle's close, absorption, delta divergence, stacked imbalances and exhaustion with the trade (+1), against
    it (-1) or neither (0), and the window's delta as a share of its volume; and the CHoCH candle's own delta share.
    `of_cov`: the share of the window's minutes read (low: the stock was not on the feed, or it was down). Recorded for
    the Performance tab, never a gate."""
    try:
        end = _epoch(t['choch_time']) + trigger_s
        start = _epoch(t.get('sweep_time') or t['choch_time'])
    except (KeyError, TypeError, ValueError):
        return {}
    w = orderflow.reading(bars, start, end, side)
    out = {'of_cov': w['coverage']}
    if w['bars']:
        out.update(of_delta=w['delta_share'], of_absorb=w['absorption'], of_div=w['divergence'],
                   of_stack=w['stacked'], of_exhaust=w['exhaustion'])
        trig = orderflow.reading(bars, end - trigger_s, end, side)
        if trig['bars']:
            out['of_trig_delta'] = trig['delta_share']
    return out


def market_day(market: Optional[pd.DataFrame], day: date, nifty: List[Tuple[str, float]], at: datetime) -> Dict[str, Any]:
    """The market's day for an option look at `at`: as of the close before `day` in the lab's daily file
    (features.market_context: India VIX, Nifty's trend and its distance from its 20 and 50-day averages, breadth)
    and Nifty's gap at the open (`nifty_gap_pct`) against the previous session's last read (store.prev_close), else
    the file's close. None when its open was not read."""
    out = features.market_context(market, day)
    prev = store.prev_close(options.MARKET, day.isoformat())
    if prev is None and market is not None and not market.empty and 'nifty' in market:
        m = market[market.index.date < day] if hasattr(market.index, 'date') else market
        prev = float(m['nifty'].iloc[-1]) if len(m) else None
    out['nifty_gap_pct'] = options.market_gap(nifty, at, prev)
    return out


def underlying_day(spots: List[Tuple[str, float]], at: datetime, market_move: Optional[float]) -> Dict[str, Any]:
    """The underlying's own day at `at`, from its reads (`spots`): its move since its open (`und_move`), how far it
    is back off its low and high (`und_off_low`, `und_off_high`) and its move less Nifty's (`und_vs_nifty`), in %."""
    move, swing = options.market_move(spots, at), options.market_swing(spots, at)
    return {'und_move': move, 'und_off_low': swing['off_low'], 'und_off_high': swing['off_high'],
            'und_vs_nifty': round(move - market_move, 3) if move is not None and market_move is not None else None}


def candidate_context(symbol: str, at: datetime, market: Optional[pd.DataFrame], market_move: Optional[float]) -> Dict[str, Any]:
    """All of an option look's context beyond its own numbers (options.candidate): the market's day, the
    underlying's day, and for a stock the news and smart money, as an idea records them. `ctx` marks it done."""
    session = at.date().isoformat()
    out = {**market_day(market, at.date(), store.day_spots(options.MARKET, session), at),
           **underlying_day(store.day_spots(symbol, session), at, market_move), 'ctx': 1}
    if symbol not in options.INDICES:            # a stock's news; an index has none of its own
        out.update(news_context(symbol, at.isoformat(timespec='seconds')))
        out.update(smart_context(symbol, at.isoformat(timespec='seconds')))
    return out


def crowding(cands: List[Dict[str, Any]]) -> Dict[int, int]:
    """For each candidate (by id), how many looks of its pass (options.pass_of) leaned its way, itself included: a
    dozen underlyings saying the same thing at once is one bet on the market. Looks are kept only for an
    underlying with no idea open, so those count too few."""
    count: Dict[Tuple[str, str], int] = {}
    for c in cands:
        k = (options.pass_of(c['ts']), c['direction'])
        count[k] = count.get(k, 0) + 1
    return {c['id']: count[(options.pass_of(c['ts']), c['direction'])] for c in cands}


# What the market's daily closes decide in a recorded context (features.market_context, relative_context).
MARKET_KEYS = ('nifty_vs_ma50', 'nifty_vs_ma20', 'nifty_ret20', 'market_trend', 'vix', 'breadth50', 'rs_nifty20', 'rs_sector20')


def _market_part(market: Optional[pd.DataFrame], symbol: str, day: date) -> Dict[str, Any]:
    out = features.market_context(market, day)
    if symbol not in options.INDICES:
        out.update(features.relative_context(history.load('1d', symbol), market, universe.sectors().get(symbol), day))
    return {k: v for k, v in out.items() if k in MARKET_KEYS}


def refresh_market_context(market: Optional[pd.DataFrame], days: int = 10) -> Dict[str, int]:
    """The market's part of the context recorded with each entry and option look of the last `days`, worked out
    again from `market` (history.load_market) as of the close before its day. The live app reads the lab's daily
    file, which can be a session behind when it records (a nightly that missed its close); once the close is in,
    this puts the record right, so the breakdowns and the autopilot read what was known at the entry. Only those
    keys change, and only where the market gives them. Returns how many rows changed in each table."""
    if market is None or market.empty:
        return {}
    since = (datetime.now(store.IST).date() - timedelta(days=days)).isoformat()
    done = {'zones': 0, 'ideas': 0, 'candidates': 0}

    def fixed(f: Dict[str, Any], symbol: str, day: date) -> Optional[Dict[str, Any]]:
        new = _market_part(market, symbol, day)
        merged = {**f, **new}
        return merged if merged != f else None

    with store.connect() as c:
        zones = [dict(r) for r in c.execute("SELECT id, symbol, trigger, ctx FROM zones WHERE ctx IS NOT NULL AND ctx != '{}'")]
        ideas = [dict(r) for r in c.execute('SELECT id, symbol, created_at, features FROM oc_ideas WHERE session >= ? '
                                            'AND features IS NOT NULL', (since,))]
    for z in zones:
        t = json.loads(z['trigger']) if z.get('trigger') else {}
        at = t.get('choch_time') or ''
        if at[:10] < since:
            continue
        f = fixed(json.loads(z['ctx']), z['symbol'], pd.Timestamp(at).date())
        if f is not None:
            store.update_zone(z['id'], ctx=json.dumps(f))
            done['zones'] += 1
    for i in ideas:
        f = fixed(json.loads(i['features']), i['symbol'], pd.Timestamp(i['created_at']).date())
        if f is not None:
            store.update_idea(i['id'], features=json.dumps(f))
            done['ideas'] += 1
    for cnd in store.option_candidates(since=since):
        if not (cnd.get('f') or {}).get('ctx'):
            continue                                   # not filled in yet: candidate_context does it whole
        f = fixed(cnd['f'], cnd['symbol'], pd.Timestamp(cnd['ts']).date())
        if f is not None:
            store.set_candidate_features(cnd['id'], f)
            done['candidates'] += 1
    return done
