"""
The intraday mode: the same SMC protocol, one timeframe down, inside one session.

    swing                               intraday
    daily structure, OB, discount       15m structure, OB, discount
    15m sweep + CHoCH                   5m sweep + CHoCH
    target: the leg's daily high        target: the nearest liquidity pool above --
                                        the leg high, today's high, the previous
                                        day's high, the opening range high, or an
                                        untaken 15m swing high
    R:R >= 1:3                          R:R >= 1:2
    held until stop or target           no entries after 14:30, out by 15:20

Shorts work exactly as in swing mode (smc.py mirrors the chart): a bearish 15m
structure, a zone in premium, the nearest pool BELOW as the target (previous
day's low, today's low, the opening range low, the leg low, a 15m swing low),
and a 5m sweep of a high with a CHoCH below a minor low.

The 15m screen reuses smc.evaluate() unchanged (with its daily-only filters
switched off, since liquidity and range are judged on daily candles here), and
the 5m trigger is smc.choch_trigger() on 5m bars. So a rule means the same thing
in both modes; only the timeframes and the numbers differ.

Pure functions, like smc.py: frames in, dicts out. The clock is always passed in.
"""
from dataclasses import dataclass, fields
import os
from datetime import datetime, time as dtime
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from niftywhale import smc


@dataclass(frozen=True)
class IntradayRules:
    # Steps 2-3, on daily candles: worth trading inside a day at all?
    min_avg_volume: float = 1_000_000     # shares/day over ~3 months
    min_atr_pct: float = 1.0              # daily ATR % of price: room to move in a session

    # Steps 4-8, on 15m candles
    sessions: int = 5                     # sessions of 15m candles analysed
    swing_len: int = 2                    # bars each side of a 15m pivot
    atr_len: int = 14                     # 15m ATR
    bos_lookback: int = 50                # 15m bars (~2 sessions) searched for the break
    displacement_atr: float = 2.0         # "massive" = origin-to-break >= this many 15m ATRs
    ob_search: int = 5
    max_discount: float = 0.5
    pre_min_rr: float = 1.0               # provisional, stop under the 15m leg origin
    min_target_pct: float = 0.3           # a pool closer than this % is not worth the costs

    # Steps 10-12, on 5m candles
    trigger_swing_len: int = 2            # bars each side of a 5m pivot
    stop_buffer_pct: float = 0.05         # stop this % under the 5m sweep low
    min_rr: float = 2.0

    # The clock (IST)
    start: str = '09:30'                  # first scan, once the first 15m candle has closed
    no_entry_after: str = '14:30'
    square_off: str = '15:20'

    @classmethod
    def from_env(cls) -> 'IntradayRules':
        """Every field can be overridden as INTRADAY_<FIELD_NAME_UPPER>."""
        values = {}
        for f in fields(cls):
            raw = os.getenv('INTRADAY_' + f.name.upper())
            if isinstance(f.default, str):
                try:
                    datetime.strptime((raw or '').strip(), '%H:%M')
                    values[f.name] = raw.strip()
                except ValueError:
                    pass                  # unset or a typo: the default
                continue
            # Numbers: a typo, inf, a negative or a zero-length window keeps the default.
            v = smc.env_number(raw, f.default, smc.MIN_INT.get(f.name, 1))
            if v is not None:
                values[f.name] = v
        return cls(**values)

    def as_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def with_overrides(self, overrides: Dict[str, Any]) -> 'IntradayRules':
        """A copy with tuner values applied, cast and clamped like smc.Rules.
        Unknown keys and bad values are ignored."""
        return IntradayRules(**smc.apply_overrides(self.as_dict(), overrides, TUNABLE))

    def clock(self, name: str) -> dtime:
        return datetime.strptime(getattr(self, name), '%H:%M').time()

    def screen_rules(self) -> smc.Rules:
        """The smc.Rules that make evaluate() read 15m candles. Liquidity and
        volatility are switched off there: they are judged on daily candles."""
        return smc.Rules(min_avg_volume=0, avg_volume_days=20, atr_len=self.atr_len, min_atr_pct=0,
                         swing_len=self.swing_len, bos_lookback=self.bos_lookback,
                         displacement_atr=self.displacement_atr, ob_search=self.ob_search,
                         max_discount=self.max_discount, pre_min_rr=0)

    def trigger_rules(self) -> smc.Rules:
        return smc.Rules(intraday_swing_len=self.trigger_swing_len,
                         stop_buffer_pct=self.stop_buffer_pct, min_rr=self.min_rr)


# The thresholds the intraday tuner exposes. Times stay in .env.
TUNABLE: Dict[str, Dict[str, Any]] = {
    'min_avg_volume':   {'label': 'Min avg volume (daily)', 'step': 100_000, 'min': 0, 'max': 5_000_000,
                         'unit': 'shares', 'help': 'Step 2, on daily candles. Intraday needs liquidity even more.'},
    'min_atr_pct':      {'label': 'Min daily ATR', 'step': 0.1, 'min': 0, 'max': 4, 'unit': '%',
                         'help': 'Step 3. Room to move within one session.'},
    'swing_len':        {'label': '15m swing size', 'step': 1, 'min': 1, 'max': 5, 'unit': 'bars each side',
                         'help': 'Step 4. Bigger = only major 15m swings count.'},
    'displacement_atr': {'label': '"Massive" move', 'step': 0.1, 'min': 0.5, 'max': 5, 'unit': '× 15m ATR',
                         'help': 'Step 6. How far the move must run from its origin to the break.'},
    'bos_lookback':     {'label': 'Look back for the break', 'step': 5, 'min': 10, 'max': 125, 'unit': '15m bars',
                         'help': 'Step 6. 25 bars ≈ one session.'},
    'max_discount':     {'label': 'Discount / premium threshold', 'step': 0.05, 'min': 0.2, 'max': 0.8, 'unit': 'of leg',
                         'help': 'Step 8. 0.5 = the equilibrium; lower = deeper pullbacks only.'},
    'min_target_pct':   {'label': 'Nearest pool at least', 'step': 0.05, 'min': 0, 'max': 2, 'unit': '%',
                         'help': 'Pools closer than this to the entry are not worth the costs.'},
    'pre_min_rr':       {'label': 'Provisional R:R', 'step': 0.1, 'min': 0.3, 'max': 4, 'unit': ': 1',
                         'help': 'Stop beyond the 15m leg origin. Filters hopeless setups early.'},
    'min_rr':           {'label': 'Final R:R (5m)', 'step': 0.5, 'min': 1, 'max': 5, 'unit': ': 1',
                         'help': 'Step 12, on the tight stop beyond the 5m sweep.'},
}


SESSION_END = dtime(15, 30)      # NSE's close: after it, no more candles are coming today

STEPS = (
    ('history',     'Enough 15m candles'),
    ('liquidity',   'Avg volume ≥ 10 lakh'),
    ('volatility',  'Daily ATR% ≥ 1'),
    ('structure',   '15m trending structure'),
    ('order_block', '15m OB after a displacement'),
    ('intact',      'Order block intact'),
    ('discount',    'In discount / premium'),
    ('rr',          'Target pool + R:R'),
)

# Where a target came from, per side, in order of preference at the same price.
POOLS = {
    'long': {'pdh': 'Prev day high', 'day_high': "Today's high", 'or_high': 'Opening range high',
             'leg_high': '15m leg high', 'bsl': '15m swing high'},
    'short': {'pdl': 'Prev day low', 'day_low': "Today's low", 'or_low': 'Opening range low',
              'leg_low': '15m leg low', 'ssl': '15m swing low'},
}


# ---------------------------------------------------------------------------
def completed(frame: pd.DataFrame, now, minutes: int) -> pd.DataFrame:
    """Only candles that have closed by `now`."""
    df = smc.clean(frame)
    if now is None or df.empty:
        return df
    now_ts = pd.Timestamp(now)
    if df.index.tz is not None and now_ts.tz is None:
        now_ts = now_ts.tz_localize(df.index.tz)
    return df[df.index + pd.Timedelta(minutes=minutes) <= now_ts]


def last_sessions(df: pd.DataFrame, n: int) -> pd.DataFrame:
    days = sorted(set(df.index.date))[-n:]
    return df[np.isin(df.index.date, days)]


def day_levels(df15: pd.DataFrame) -> Dict[str, Any]:
    """
    The session's reference levels from completed 15m candles: the previous
    day's high/low, the opening range (the first 15m candle) and today's
    range so far. `session` is the date of the latest candle.
    """
    if df15.empty:
        return {}
    days = sorted(set(df15.index.date))
    today = df15[df15.index.date == days[-1]]
    out = {'session': str(days[-1]),
           'day_high': float(today['High'].max()), 'day_low': float(today['Low'].min()),
           'or_high': float(today['High'].iloc[0]), 'or_low': float(today['Low'].iloc[0]),
           'open': float(today['Open'].iloc[0]), 'bars_today': int(len(today))}
    if len(days) > 1:
        prev = df15[df15.index.date == days[-2]]
        out.update(pdh=float(prev['High'].max()), pdl=float(prev['Low'].min()),
                   prev_close=float(prev['Close'].iloc[-1]))
    return out


def daily_filter(daily: Optional[pd.DataFrame], rules: IntradayRules) -> Dict[str, Any]:
    """Steps 2-3 on daily candles. {'failed_at', 'reason', 'avg_volume', 'atr_pct'}."""
    if daily is None or daily.empty:
        return {'failed_at': 'history', 'reason': 'no daily candles'}
    df = smc.clean(daily)
    if len(df) < 30:
        return {'failed_at': 'history', 'reason': f'{len(df)} daily sessions, need 30'}
    avg_vol = smc.avg_volume(df['Volume'].to_numpy(dtype=float)[-63:])
    a = float(smc.atr(df, 14).iloc[-1])
    atr_pct = a / float(df['Close'].iloc[-1]) * 100
    out = {'failed_at': None, 'reason': '', 'avg_volume': avg_vol, 'atr_pct': atr_pct}
    if avg_vol < rules.min_avg_volume:
        out.update(failed_at='liquidity', reason=f'avg volume {avg_vol:,.0f} < {rules.min_avg_volume:,.0f}')
    elif atr_pct < rules.min_atr_pct:
        out.update(failed_at='volatility', reason=f'daily ATR {atr_pct:.2f}% < {rules.min_atr_pct}%')
    return out


def evaluate(frame15: pd.DataFrame, rules: IntradayRules, now=None, side: str = 'long',
             shorts: bool = False) -> Dict[str, Any]:
    """
    Steps 4-8 on completed 15m candles, then the target: the nearest
    liquidity pool beyond the entry (above for a long, below for a short)
    that is at least `min_target_pct` away. With `shorts`, a stock whose
    long fails on a bearish structure is evaluated as a short instead (one
    verdict per stock, as smc.evaluate_both). Returns smc.evaluate()'s dict
    with the intraday target, plan and the session's levels added.
    """
    df = last_sessions(completed(frame15, now, 15), rules.sessions)
    levels = day_levels(df)
    if shorts:
        r = smc.evaluate_both(df, rules.screen_rules(), shorts=True)
    else:
        r = smc.evaluate(df, rules.screen_rules(), side)
    side = r.get('side', side)
    r['levels'] = levels
    if r['failed_at'] not in (None, 'rr'):
        return r

    short = side == 'short'
    price = r['close']
    entry = max(price, r['zone']['low']) if short else min(price, r['zone']['high'])
    stop = r['plan']['stop']
    if short:
        edge = entry * (1 - rules.min_target_pct / 100)
        pools = {'leg_low': r['leg']['low'], 'day_low': levels.get('day_low'),
                 'pdl': levels.get('pdl'), 'or_low': levels.get('or_low')}
        candidates = [(v, k) for k, v in pools.items() if v is not None and v < edge]
        candidates += [(v, 'ssl') for v in r['pools']['ssl'] if v < edge]
        nearest = lambda x: -x[0]                                      # noqa: E731  highest below
    else:
        edge = entry * (1 + rules.min_target_pct / 100)
        pools = {'leg_high': r['leg']['high'], 'day_high': levels.get('day_high'),
                 'pdh': levels.get('pdh'), 'or_high': levels.get('or_high')}
        candidates = [(v, k) for k, v in pools.items() if v is not None and v > edge]
        candidates += [(v, 'bsl') for v in r['pools']['bsl'] if v > edge]
        nearest = lambda x: x[0]                                       # noqa: E731  lowest above
    names = POOLS[side]
    r['passed'], r['failed_at'], r['reason'] = False, None, ''
    if not candidates:
        r['plan'] = {'entry': entry, 'stop': stop, 'target': None, 'rr': 0.0}
        r['failed_at'], r['reason'] = 'rr', (f'no liquidity pool ≥ {rules.min_target_pct}% '
                                             f'{"below" if short else "above"} the zone')
        return r
    # Nearest first; at the same price, the more telling name wins.
    rank = {k: i for i, k in enumerate(names)}
    target, kind = min(candidates, key=lambda x: (nearest(x), rank[x[1]]))
    risk = (stop - entry) if short else (entry - stop)
    rr = abs(target - entry) / risk if risk > 0 else 0.0
    r['plan'] = {'entry': entry, 'stop': stop, 'target': float(target), 'rr': rr}
    r['target_kind'] = kind
    r['target_label'] = names[kind]
    r['pools_beyond'] = sorted({(round(v, 2), names[k]) for v, k in candidates}, reverse=short)[:5]
    if rr < rules.pre_min_rr:
        r['failed_at'], r['reason'] = 'rr', (f'provisional R:R 1:{rr:.1f} to the {names[kind].lower()} '
                                             f'< 1:{rules.pre_min_rr:g}')
        return r
    r['passed'] = True
    r['score'] = smc.score(r, smc.Rules(min_rr=rules.min_rr, max_discount=rules.max_discount,
                                        min_atr_pct=0))
    return r


def trigger(frame5: pd.DataFrame, zone_low: float, zone_high: float, target: float,
            rules: IntradayRules, now, since=None, invalid: Optional[float] = None,
            side: str = 'long') -> Dict[str, Any]:
    """
    Steps 10-12 on today's 5m candles, with the entry cutoff applied. `since`
    is when the zone was set: price there earlier in the day was not a tap.
    `invalid` is the 15m leg's origin: a sweep beyond it (below for a long,
    above for a short) is not a sweep of the zone but the structure failing,
    and its CHoCH is no entry.
    """
    if since is not None:
        since = pd.Timestamp(since)
        if since.tz is None:
            since = since.tz_localize('Asia/Kolkata')
        since = since.floor('5min')
    t = smc.choch_trigger(frame5, zone_low, zone_high, target, rules.trigger_rules(),
                          now=now, bar_minutes=5, sessions=1, since=since, side=side)
    short = side == 'short'
    # Up to the CHoCH if there was one (a later move is the trade's stop, not
    # the setup failing); otherwise everything since the tap. For a short
    # these keys hold highs (see smc.choch_trigger).
    far = t['sweep_low'] if t.get('triggered') else t.get('low')
    beyond = far is not None and invalid is not None and (far > invalid if short else far < invalid)
    if t.get('tapped') and beyond:
        t['invalid'] = True
        if t.get('triggered'):
            t['valid'] = False
        t['reason'] = (f'broke {"above" if short else "below"} the 15m leg origin ({invalid:.2f}) '
                       f'— structure failed')
    if t.get('triggered') and t['valid']:
        at = pd.Timestamp(t['choch_time'])
        # The CHoCH candle closes 5 minutes after it opens; that close is the entry.
        closes = (at + pd.Timedelta(minutes=5)).time()
        if closes > rules.clock('no_entry_after'):
            t['valid'] = False
            t['late'] = True
            t['reason'] = f"CHoCH at {closes.strftime('%H:%M')}, after the {rules.no_entry_after} entry cutoff"
    return t


def outcome(frame5: pd.DataFrame, t: Dict[str, Any], rules: IntradayRules, now) -> Dict[str, Any]:
    """
    What happened to a triggered trade, from the 5m candles after the entry:
      won     a candle reached the target
      lost    a candle reached the stop (a candle that touches both counts as
              lost: 5m candles cannot say which came first, so assume the worse)
      closed  neither by the square-off time; out at that candle's close
      open    still running
    Returns {'status', 'exit', 'exit_time', 'r'} (r = result in multiples of risk).
    Works for either side: a short's stop is above and its target below.
    """
    df = completed(frame5, now, 5)
    entry_at = pd.Timestamp(t['choch_time'])
    day = entry_at.date()
    df = df[(df.index > entry_at) & (df.index.date == day)]
    entry, stop, target = t['entry'], t['stop'], t['target']
    short = t.get('side') == 'short' or stop > entry
    sign = -1 if short else 1
    risk = (entry - stop) * sign
    r_of = (lambda px: (px - entry) * sign / risk) if risk > 0 else (lambda px: 0.0)
    cutoff = rules.clock('square_off')
    for ts, bar in df.iterrows():
        bar_close_time = (ts + pd.Timedelta(minutes=5)).time()
        stopped = bar['High'] >= stop if short else bar['Low'] <= stop
        reached = bar['Low'] <= target if short else bar['High'] >= target
        if stopped:
            return {'status': 'lost', 'exit': stop, 'exit_time': ts.isoformat(), 'r': r_of(stop)}
        if reached:
            return {'status': 'won', 'exit': target, 'exit_time': ts.isoformat(), 'r': r_of(target)}
        if bar_close_time >= cutoff:
            px = float(bar['Close'])
            return {'status': 'closed', 'exit': px, 'exit_time': ts.isoformat(), 'r': r_of(px)}
    last = float(df['Close'].iloc[-1]) if len(df) else entry
    now_ts = pd.Timestamp(now)
    if now_ts.date() > day or now_ts.time() >= SESSION_END:
        # The session is over and no candle covered the square-off (data gap):
        # out at the last price we have. Before then, the square-off candle may
        # just not have arrived yet (delayed data): an earlier candle's close
        # is not the square-off price, so the trade stays open until it does.
        return {'status': 'closed', 'exit': last,
                'exit_time': df.index[-1].isoformat() if len(df) else t['choch_time'], 'r': r_of(last)}
    return {'status': 'open', 'last': last, 'r': r_of(last)}
