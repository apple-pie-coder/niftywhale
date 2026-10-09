"""
The backtester: years of history replayed through the live rule code.

Two stages, so the autopilot can try hundreds of rule sets on a Raspberry Pi:

  1. RECORDS (slow, once per stock, then a day at a time): the screen run on
     every historical day -- swing on daily candles each evening, intraday on
     15m candles at every scan mark -- with the *loosest* values of the
     thresholds that only accept or reject a setup (volume, ATR %, discount
     depth, provisional R:R). Each passing setup is stored with those numbers.
     These thresholds never change the zone, the stop or the target (see
     smc.evaluate), so filtering the records later is exact.

  2. SIMULATION (fast, per rule set): the live zone lifecycle replayed over the
     records, keeping only setups the rule set passes -- sync_zones' rules for
     swing (one trade per stock, setups that played out not re-armed, a 10-day
     zone lifetime, a structure flip), sync_intraday_zones' for intraday (zones
     refreshed each 15m scan, tapped zones frozen, rejected or failed setups not
     re-armed that day, no entries after 14:30, square-off). Triggers and trade
     outcomes come from smc.choch_trigger / intraday.trigger, smc.follow_trade /
     intraday.outcome -- the live functions -- and are memoised, since most rule
     sets share most zones. `min_rr` is applied to the trigger's R:R the same way.

What a backtest cannot see: the live data's delays and gaps, today's universe
applied to the past (stocks that left the index are missing: survivorship
bias), costs and slippage (fills are at the alert's prices, as in the paper
journal). Results are a guide to how rules compare, not a forecast.
"""
import hashlib
import json
import logging
import math
import pickle
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from niftywhale import features, history, intraday, smc

logger = logging.getLogger(__name__)

TZ = 'Asia/Kolkata'
ZONE_LIFETIME_DAYS = 10
DAILY_BARS = 250                    # the live scan reads one year of daily candles
FILTERS = ('min_avg_volume', 'min_atr_pct', 'max_discount', 'pre_min_rr', 'min_rr')
# Context gates (smc.context_gate), checked at the trigger: like the filters, they never change the records.
GATES = {'swing': smc.GATES, 'intraday': ()}
LOOSE = {
    'swing': {'min_avg_volume': 0, 'min_atr_pct': 0.0, 'max_discount': 0.8, 'pre_min_rr': 0.5, 'min_rr': 1.0},
    'intraday': {'min_avg_volume': 0, 'min_atr_pct': 0.0, 'max_discount': 0.8, 'pre_min_rr': 0.3, 'min_rr': 1.0},
}
MARKS = [dtime(9, 30)] + [(datetime(2000, 1, 1, 9, 30) + timedelta(minutes=15 * k)).time() for k in range(1, 20)]
RECORDS_VERSION = 1


def ts(day: date, t: dtime) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, t)).tz_localize(TZ)


def structural_key(mode: str, rules) -> str:
    """Records depend on every rule except the filter thresholds and the gates; a change
    to those (swing length, displacement, ...) makes the stored records stale."""
    d = {k: v for k, v in rules.as_dict().items() if k not in FILTERS and k not in smc.GATES}
    return hashlib.sha1(json.dumps([RECORDS_VERSION, mode, d], sort_keys=True, default=str).encode()).hexdigest()[:12]


def loose_rules(mode: str, rules):
    return replace(rules, **{k: v for k, v in LOOSE[mode].items() if hasattr(rules, k)})


def records_path(mode: str, symbol: str) -> Path:
    return history.LAB_DIR / 'records' / mode / f'{symbol.replace("/", "_")}.pkl'


def load_records(mode: str, symbol: str) -> Optional[Dict[str, Any]]:
    p = records_path(mode, symbol)
    if not p.exists():
        return None
    try:
        with open(p, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return None


def save_records(mode: str, symbol: str, rec: Dict[str, Any]) -> None:
    p = records_path(mode, symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    with open(tmp, 'wb') as f:
        pickle.dump(rec, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(p)


def _dates(index) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    return np.array(idx.tz_convert(TZ).date if idx.tz is not None else idx.date)


# ---------------------------------------------------------------- stage 1: records
def _setup_numbers(r: Dict[str, Any]) -> Dict[str, Any]:
    ob = r.get('order_block') or {}
    return {'side': r.get('side', 'long'), 'zl': r['zone']['low'], 'zh': r['zone']['high'],
            'tg': r['plan']['target'], 'stop': r['plan'].get('stop'), 'ob': ob.get('date'), 'obe': ob.get('low'),
            'pos': round(float(r['position']), 4), 'prr': round(float(r['plan']['rr']), 3),
            'atr': round(float(r.get('atr_pct') or 0), 3), 'vol': float(r.get('avg_volume') or 0),
            'sc': r.get('score'), 'iz': bool(r.get('in_zone')), 'tk': r.get('target_kind')}


def swing_records(symbol: str, daily: pd.DataFrame, rules: smc.Rules, start: date,
                  short_block: Optional[str], old: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The swing screen on every day from `start` (loose filters). Incremental: days
    already in `old` (with the same structural key) are kept."""
    key = structural_key('swing', rules)
    rec = old if old and old.get('key') == key else {'key': key, 'mode': 'swing', 'symbol': symbol, 'days': [], 'pass': {}}
    loose = loose_rules('swing', rules)
    df = smc.clean(daily)
    dates = _dates(df.index)
    done = set(rec['days'])
    for i, d in enumerate(dates):
        if d < start or d.isoformat() in done or i < 60:
            continue
        frame = df.iloc[max(0, i - DAILY_BARS + 1): i + 1]
        try:
            r = smc.evaluate_both(frame, loose, shorts=True, short_block=short_block)
        except Exception as e:                   # one odd day never sinks a backtest
            logger.debug(f'{symbol} {d}: {e}')
            continue
        rec['days'].append(d.isoformat())
        if r['passed']:
            rec['pass'][d.isoformat()] = _setup_numbers(r)
    rec['days'].sort()
    return rec


def intraday_records(symbol: str, f15: pd.DataFrame, daily: pd.DataFrame, rules: intraday.IntradayRules,
                     start: date, old: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The intraday screen at every 15m scan mark (09:30-14:15) of every session from
    `start`, with the day's daily-filter numbers. Incremental like swing_records."""
    key = structural_key('intraday', rules)
    rec = old if old and old.get('key') == key else {'key': key, 'mode': 'intraday', 'symbol': symbol, 'days': {}}
    loose = loose_rules('intraday', rules)
    f = smc.clean(f15)
    fd = _dates(f.index)
    sessions = sorted(set(fd))
    ddates = _dates(daily.index) if daily is not None and len(daily) else np.array([])
    ends = f.index + pd.Timedelta(minutes=15)
    for si, d in enumerate(sessions):
        if d < start or d.isoformat() in rec['days'] or si < rules.sessions:
            continue
        dly = daily[ddates < d] if len(ddates) else None
        p = intraday.daily_filter(dly, loose)
        if p['failed_at'] == 'history':
            continue
        day = {'vol': float(p['avg_volume']), 'atr': round(float(p['atr_pct']), 3), 'marks': {}}
        lo = np.searchsorted(fd, sessions[max(0, si - rules.sessions + 1)])
        for k, mark in enumerate(MARKS):
            now = ts(d, mark)
            hi = np.searchsorted(ends, now, side='right')
            window = f.iloc[lo:hi]
            if not len(window) or fd[hi - 1] != d:
                continue
            try:
                r = intraday.evaluate(window, loose, None, shorts=True)
            except Exception as e:
                logger.debug(f'{symbol} {d} {mark}: {e}')
                continue
            if r['passed']:
                day['marks'][k] = _setup_numbers(r)
        rec['days'][d.isoformat()] = day
    return rec


# ---------------------------------------------------------------- stage 2: simulation
def passes(s: Dict[str, Any], thr: Dict[str, float]) -> bool:
    return (s['vol'] >= thr['min_avg_volume'] and s['atr'] > thr['min_atr_pct']
            and s['pos'] <= thr['max_discount'] + 1e-9 and s['prr'] >= thr['pre_min_rr'] - 1e-9)


class Data:
    """Frames for one mode, loaded on first use and indexed by session for fast windows."""

    def __init__(self, interval: str):
        self.interval = interval
        self.frames: Dict[str, Any] = {}

    def get(self, symbol: str):
        if symbol not in self.frames:
            f = history.load(self.interval, symbol)
            if f is not None and len(f):
                f = smc.clean(f)
                fd = _dates(f.index)
                self.frames[symbol] = (f, fd)
            else:
                self.frames[symbol] = None
        return self.frames[symbol]

    def sessions(self, symbol: str, first: date, last: date) -> pd.DataFrame:
        got = self.get(symbol)
        if got is None:
            return pd.DataFrame()
        f, fd = got
        return f.iloc[np.searchsorted(fd, first): np.searchsorted(fd, last, side='right')]

    def after(self, symbol: str, day: date) -> pd.DataFrame:
        got = self.get(symbol)
        if got is None:
            return pd.DataFrame()
        f, fd = got
        return f.iloc[np.searchsorted(fd, day):]


def _same(z: Dict[str, Any], side: str, s: Dict[str, Any]) -> bool:
    """store.same_setup on records: the same side and order block."""
    return z['side'] == side and ((z.get('ob') and z['ob'] == s.get('ob')) or
                                  (z.get('obe') is not None and s.get('obe') is not None and abs(z['obe'] - s['obe']) < 1e-6))


def simulate_swing(symbol: str, rec: Dict[str, Any], thr: Dict[str, float], f15: Data, rules: smc.Rules,
                   memo: Dict, market: Optional[pd.DataFrame] = None, daily: Optional[pd.DataFrame] = None,
                   end: Optional[pd.Timestamp] = None) -> List[Dict[str, Any]]:
    trig_rules = replace(rules, min_rr=LOOSE['swing']['min_rr'])
    days = [date.fromisoformat(d) for d in rec['days']]
    zone = None          # the open zone: side, levels, created (date), since (ts), setup numbers
    spent: List[Dict[str, Any]] = []
    trade_until: Optional[pd.Timestamp] = None        # the open paper trade blocks the stock until its exit
    trades = []
    end = end or pd.Timestamp.now(tz=TZ)
    for i, d in enumerate(days):
        # 1. The 15m watcher over session d (on the zone as of the previous evening).
        if zone is not None and d > zone['created']:
            window = f15.sessions(symbol, days[i - 1] if i else d, d)
            if len(window):
                key = ('s', symbol, zone['zl'], zone['zh'], zone['tg'], zone['side'], zone['since'], d)
                if key not in memo:
                    memo[key] = smc.choch_trigger(window, zone['zl'], zone['zh'], zone['tg'], trig_rules,
                                                  sessions=2, since=zone['since'], side=zone['side'])
                t = memo[key]
                if t.get('triggered'):
                    late = pd.Timestamp(t['choch_time']).date() < d      # live would have found it too late
                    ok = not late and t['rr'] >= thr['min_rr'] - 1e-9
                    if ok:
                        setup = {**zone['s'], 'zone_age_days': (d - zone['created']).days}
                        f = features.combine(features.trade_context(t, {
                            'position': setup['pos'], 'pre_rr': setup['prr'], 'atr_pct': setup['atr'],
                            'avg_volume': setup['vol'], 'score': setup['sc'], 'in_zone': setup['iz'],
                            'zone_age_days': setup['zone_age_days']}),
                            features.market_context(market, d), features.stock_day_context(daily, d))
                        ok = smc.context_gate(f, zone['side'], thr) is None    # gated: spent, as live
                    if ok:
                        okey = ('so', symbol, t['choch_time'], t['entry'], t['stop'], t['target'])
                        o = memo.get(okey)
                        if o is None:
                            got = smc.follow_trade(t, f15.after(symbol, d), None, end)
                            o = {k: got.get(k) for k in ('status', 'exit', 'exit_time', 'r')}
                            if o['status'] != 'open':        # an open trade is re-followed as data arrives
                                memo[okey] = o
                        trades.append({'mode': 'swing', 'symbol': symbol, 'side': zone['side'], 'entry_time': t['choch_time'],
                                       'entry': t['entry'], 'stop': t['stop'], 'target': t['target'],
                                       'status': o['status'], 'exit': o.get('exit'), 'exit_time': o.get('exit_time'),
                                       'r': round(float(o['r']), 3) if o.get('r') is not None else None, 'f': f})
                        trade_until = pd.Timestamp(o['exit_time']) if o.get('exit_time') else end
                    spent.append(zone)
                    zone = None
        # 2. The evening scan of day d, as store.sync_zones.
        s = rec['pass'].get(d.isoformat())
        ok = s is not None and passes(s, thr)
        evening = ts(d, dtime(16, 15))
        trading = trade_until is not None and trade_until > evening
        if ok:
            side = s['side']
            if zone is not None and zone['side'] != side:
                zone = None                                        # structure flipped
            if any(_same(z, side, s) for z in spent):
                zone = None                                        # played out: wait for the next setup
            elif zone is not None:
                zone.update(zl=s['zl'], zh=s['zh'], tg=s['tg'], s=s)
            elif not trading:
                zone = {'side': side, 'zl': s['zl'], 'zh': s['zh'], 'tg': s['tg'], 'ob': s['ob'], 'obe': s['obe'],
                        'created': d, 'since': evening.isoformat(), 's': s}
        elif zone is not None:
            zone = None                                            # no longer passes the screen
        if zone is not None and (d - zone['created']).days >= ZONE_LIFETIME_DAYS:
            spent.append(zone)
            zone = None
    return trades


def simulate_intraday(symbol: str, rec: Dict[str, Any], thr: Dict[str, float], f5: Data, rules: intraday.IntradayRules,
                      memo: Dict, market: Optional[pd.DataFrame] = None, daily: Optional[pd.DataFrame] = None,
                      ) -> List[Dict[str, Any]]:
    loose = replace(rules, min_rr=LOOSE['intraday']['min_rr'])
    cutoff = rules.clock('no_entry_after')
    final_now = (datetime.combine(date(2000, 1, 1), cutoff) + timedelta(minutes=5)).time()
    trades = []
    for dstr, day in sorted(rec['days'].items()):
        if day['vol'] < thr['min_avg_volume'] or day['atr'] < thr['min_atr_pct']:
            continue
        d = date.fromisoformat(dstr)
        frame5 = None
        zones: List[Dict[str, Any]] = []

        def check(z, now_t):
            nonlocal frame5
            if frame5 is None:
                frame5 = f5.sessions(symbol, d, d)
            if not len(frame5):
                return
            key = ('i', symbol, dstr, z['zl'], z['zh'], z['tg'], z['side'], z['since'], now_t, z['stop'])
            if key not in memo:
                memo[key] = intraday.trigger(frame5, z['zl'], z['zh'], z['tg'], loose, ts(d, now_t),
                                             since=z['since'], invalid=z['stop'], side=z['side'])
            t = memo[key]
            if t.get('tapped') and z['status'] == 'watching':
                z['status'] = 'tapped'
            if t.get('invalid'):
                z['status'] = 'failed'
                return
            if t.get('triggered'):
                ok = not t.get('late') and t['rr'] >= thr['min_rr'] - 1e-9
                z['status'] = 'trade' if ok else 'rejected'
                if ok:
                    okey = ('io', symbol, t['choch_time'], t['entry'], t['stop'], t['target'])
                    if okey not in memo:
                        o = intraday.outcome(frame5, t, rules, ts(d, dtime(15, 30)))
                        memo[okey] = {k: o.get(k) for k in ('status', 'exit', 'exit_time', 'r')}
                    o = memo[okey]
                    s = z['s']
                    f = features.combine(features.trade_context(t, {
                        'position': s['pos'], 'pre_rr': s['prr'], 'atr_pct': day['atr'], 'avg_volume': day['vol'],
                        'score': s['sc'], 'in_zone': s['iz'], 'target_kind': s.get('tk')}),
                        features.market_context(market, d), features.stock_day_context(daily, d))
                    trades.append({'mode': 'intraday', 'symbol': symbol, 'side': z['side'], 'entry_time': t['choch_time'],
                                   'entry': t['entry'], 'stop': t['stop'], 'target': t['target'],
                                   'status': o['status'], 'exit': o.get('exit'), 'exit_time': o.get('exit_time'),
                                   'r': round(float(o['r']), 3) if o.get('r') is not None else None, 'f': f})

        for k, mark in enumerate(MARKS):
            open_z = next((z for z in zones if z['status'] in ('watching', 'tapped')), None)
            if open_z is not None:
                check(open_z, mark)                       # the 5m checks up to this scan
            if any(z['status'] in ('trade', 'tapped') for z in zones):
                continue                                  # a trade today, or price in the zone: leave it
            s = day['marks'].get(k)
            ok = s is not None and s['pos'] <= thr['max_discount'] + 1e-9 and s['prr'] >= thr['pre_min_rr'] - 1e-9
            watching = next((z for z in zones if z['status'] == 'watching'), None)
            if not ok:
                if watching is not None:
                    watching['status'] = 'expired'
                continue
            side = s['side']
            if any(_same(z, side, s) and z['status'] in ('rejected', 'failed') for z in zones):
                continue
            since = ts(d, mark).isoformat()
            if watching is not None:
                if (watching['zl'], watching['zh'], watching['tg']) != (s['zl'], s['zh'], s['tg']):
                    watching['since'] = since
                watching.update(side=side, zl=s['zl'], zh=s['zh'], tg=s['tg'], stop=s['stop'], ob=s['ob'], obe=s['obe'], s=s)
            else:
                zones.append({'status': 'watching', 'side': side, 'zl': s['zl'], 'zh': s['zh'], 'tg': s['tg'],
                              'stop': s['stop'], 'ob': s['ob'], 'obe': s['obe'], 'since': since, 's': s})
        open_z = next((z for z in zones if z['status'] in ('watching', 'tapped')), None)
        if open_z is not None:
            check(open_z, final_now)                      # the last checks before the entry cutoff
    return trades


def simulate(mode: str, symbols: List[str], thr: Dict[str, float], rules, memo: Dict, data15: Data, data5: Optional[Data],
             records: Dict[str, Dict[str, Any]], market: Optional[pd.DataFrame], dailies: Dict[str, pd.DataFrame],
             since: Optional[date] = None, until: Optional[date] = None) -> List[Dict[str, Any]]:
    """Every trade the rule set `thr` would have made, oldest first; optionally only
    those entered between `since` and `until` (the lifecycle still runs from the start)."""
    out = []
    for sym in symbols:
        rec = records.get(sym)
        if not rec:
            continue
        if mode == 'swing':
            out += simulate_swing(sym, rec, thr, data15, rules, memo, market, dailies.get(sym))
        else:
            out += simulate_intraday(sym, rec, thr, data5, rules, memo, market, dailies.get(sym))
    out.sort(key=lambda t: t['entry_time'])
    if since or until:
        out = [t for t in out if (not since or t['entry_time'][:10] >= since.isoformat())
               and (not until or t['entry_time'][:10] <= until.isoformat())]
    return out


def thresholds(mode: str, rules) -> Dict[str, float]:
    """The filter thresholds and gates of a rule set, as simulate() takes them."""
    return {k: float(getattr(rules, k)) for k in FILTERS + GATES[mode]}


def is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
