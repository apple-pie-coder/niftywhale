"""
The autopilot: rules that learn from results, inside limits you set.

It only ever moves the filter thresholds (the same ones the rule tuners expose:
volume, ATR %, discount depth, provisional and final R:R; for swing also the
context gates -- Nifty's 20-day run, the zone's age, an R:R cap; for options the
bias, move, IV, spread and the exits). It never changes the strategy, the code, or
places an order. Every step is logged and announced on Telegram, and can be
undone from the dashboard.

  1. TUNE (weekly), walk-forward: history is cut into 3-month folds. For each
     fold, rules are tuned on everything before it and then judged on the fold
     itself -- data the tuning never saw. Only if that out-of-sample record beats
     the current rules (more R per trade, more R in total, no much deeper
     drawdown, enough trades) is a change proposed: the rules tuned on all the
     history, each threshold moved at most `max_steps` steps.
  2. SHADOW: the proposal runs alongside the live rules on new data for
     `shadow_days` sessions (replayed nightly). It is promoted if it does at
     least as well there; dropped if clearly worse.
  3. PROMOTE: the thresholds are saved (they take effect at the next scan),
     unless the policy says to wait for your approval.
  4. ROLLBACK: if the live paper trades since a promotion lose more than
     `rollback_r` and the previous rules would have done better over the same
     days, the previous rules come back.
  5. PAUSE: if a mode's last `pause_window` closed paper trades lose more than
     `pause_r`, its Telegram entry alerts stop (they are still recorded); they
     resume when trades during the pause add up to +2R, new rules are promoted,
     or you resume them.

Pure decision logic here; the lab (niftywhale/lab.py) runs it and stores state.
"""
import math
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from niftywhale import perf

MODES = ('swing', 'intraday', 'options')

DEFAULT_POLICY: Dict[str, Any] = {
    'mode': 'auto',                 # auto: promote by itself | approve: wait for a click | off
    'max_steps': 2,                 # how far one promotion may move each threshold (tuner steps)
    'search_steps': 4,              # how far the tuner looks either side of the current value
    'fold_months': 3,
    'min_train_folds': 2,
    # Swing's context gates can carve a handful of lucky trades out of the history, so a swing
    # rule set must keep a real sample, in training and out of sample, to count.
    'min_oos_trades': {'swing': 40, 'intraday': 60, 'options': 40},
    'min_train_trades': {'swing': 60, 'intraday': 60, 'options': 40},
    'min_gain_r': 0.05,             # out-of-sample R per trade the proposal must add
    'shadow_days': 20,
    'shadow_max_days': 60,
    'shadow_min_trades': {'swing': 4, 'intraday': 15, 'options': 10},
    'cooldown_days': 14,
    'rollback_r': {'swing': 4.0, 'intraday': 6.0, 'options': 6.0},
    'pause_r': {'swing': 6.0, 'intraday': 10.0, 'options': 8.0},
    'pause_window': 30,
    'limits': {},                   # {mode: {param: [min, max]}}: your bounds inside the tuner ranges
}


def policy(saved: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_POLICY.items()}
    for k, v in (saved or {}).items():
        if k not in out:
            continue
        if isinstance(out[k], dict) and isinstance(v, dict):
            out[k].update({kk: vv for kk, vv in v.items()})
        elif k == 'mode' and v in ('auto', 'approve', 'off'):
            out[k] = v
        elif isinstance(out[k], (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[k] = type(out[k])(v)
    return out


# ---------------------------------------------------------------- search space
def grid(param: str, current: float, spec: Dict[str, Any], pol: Dict[str, Any], mode: str,
         span: Optional[int] = None) -> List[float]:
    """Values the tuner may try for one threshold: the tuner's steps around the current
    value, inside the tuner range and your limits. A spec with `values` (the options
    exits, which are only known on a grid) is searched over those."""
    span = pol['search_steps'] if span is None else span
    lim = (pol.get('limits') or {}).get(mode, {}).get(param)
    if spec.get('values'):
        vals = [v for v in spec['values'] if not (lim and len(lim) == 2) or float(lim[0]) <= v <= float(lim[1])]
        if not vals:
            return [current]
        i = min(range(len(vals)), key=lambda j: abs(vals[j] - current))
        return vals[max(0, i - span): i + span + 1]
    step = float(spec['step'])
    lo, hi = float(spec['min']), float(spec['max'])
    if lim and len(lim) == 2:
        lo, hi = max(lo, float(lim[0])), min(hi, float(lim[1]))
    vals = sorted({round(min(hi, max(lo, current + k * step)), 6) for k in range(-span, span + 1)})
    return vals or [current]


def clamp_steps(proposal: Dict[str, float], current: Dict[str, float], specs: Dict[str, Dict[str, Any]],
                max_steps: int) -> Dict[str, float]:
    out = dict(current)
    for k, v in proposal.items():
        if k not in specs:
            continue
        if specs[k].get('values'):
            vals = list(specs[k]['values'])
            i = min(range(len(vals)), key=lambda j: abs(vals[j] - current[k]))
            j = min(range(len(vals)), key=lambda j: abs(vals[j] - v))
            out[k] = vals[max(i - max_steps, min(i + max_steps, j))]
            continue
        step = float(specs[k]['step'])
        limit = max_steps * step
        out[k] = round(min(current[k] + limit, max(current[k] - limit, v)), 6)
    return out


def objective(trades: List[Dict[str, Any]], min_n: int) -> float:
    s = perf.summary(trades)
    return s['lcb'] if s.get('trades', 0) >= min_n else -math.inf


def tune(evaluate: Callable[[Dict[str, float]], List[Dict[str, Any]]], current: Dict[str, float],
         specs: Dict[str, Dict[str, Any]], pol: Dict[str, Any], mode: str, window: Tuple[Optional[str], Optional[str]],
         rounds: int = 2) -> Tuple[Dict[str, float], float]:
    """Coordinate ascent on the objective over trades entered inside `window`
    (ISO dates, end exclusive). Returns the best thresholds and their score."""
    since, until = window
    pick = lambda ts: [t for t in ts if (not since or t['entry_time'][:10] >= since) and (not until or t['entry_time'][:10] < until)]
    min_n = pol['min_train_trades'][mode]
    best = dict(current)
    best_score = objective(pick(evaluate(best)), min_n)
    for _ in range(rounds):
        improved = False
        for p in specs:
            for v in grid(p, best[p], specs[p], pol, mode):
                if v == best[p]:
                    continue
                cand = {**best, p: v}
                sc = objective(pick(evaluate(cand)), min_n)
                if sc > best_score + 1e-9:
                    best, best_score, improved = cand, sc, True
        if not improved:
            break
    return best, best_score


def folds(first: date, last: date, months: int) -> List[Tuple[date, date]]:
    out, cur = [], first
    while cur <= last:
        m = cur.month - 1 + months
        nxt = date(cur.year + m // 12, m % 12 + 1, 1)
        out.append((cur, min(nxt, last + timedelta(days=1))))
        cur = nxt
    return out


def walk_forward(evaluate: Callable[[Dict[str, float]], List[Dict[str, Any]]], current: Dict[str, float],
                 specs: Dict[str, Dict[str, Any]], pol: Dict[str, Any], mode: str, first: date, last: date) -> Dict[str, Any]:
    """Out-of-sample comparison of 'tune on the past, trade the next fold' against
    the current rules, and the proposal if the tuning earns it."""
    months = 1 if mode == 'options' else pol['fold_months']     # options history is months, not years
    fs = folds(date(first.year, first.month, 1), last, months)
    oos_tuned, oos_current, rows = [], [], []
    for k in range(pol['min_train_folds'], len(fs)):
        train = (fs[0][0].isoformat(), fs[k][0].isoformat())
        test = (fs[k][0].isoformat(), fs[k][1].isoformat())
        params, score = tune(evaluate, current, specs, pol, mode, train)
        inside = lambda ts: [t for t in ts if test[0] <= t['entry_time'][:10] < test[1]]
        a, b = inside(evaluate(params)), inside(evaluate(current))
        oos_tuned += a
        oos_current += b
        rows.append({'train': train, 'test': test, 'params': params, 'train_score': None if math.isinf(score) else round(score, 3),
                     'tuned': perf.summary(a), 'current': perf.summary(b)})
    st, sc = perf.summary(oos_tuned), perf.summary(oos_current)
    reasons = []
    if not rows:
        reasons.append(f'not enough history for walk-forward ({len(fs)} folds of {months} month(s))')
    if st.get('trades', 0) < pol['min_oos_trades'][mode]:
        reasons.append(f"only {st.get('trades', 0)} out-of-sample trades (need {pol['min_oos_trades'][mode]})")
    gain = (st.get('avg_r') or 0) - (sc.get('avg_r') or 0)
    if st.get('trades') and gain < pol['min_gain_r']:
        reasons.append(f'tuning added {gain:+.3f}R per trade out of sample (need +{pol["min_gain_r"]})')
    if st.get('trades') and (st.get('total_r') or 0) <= (sc.get('total_r') or 0):
        reasons.append(f"total {st.get('total_r')}R out of sample, not more than the current rules' {sc.get('total_r')}R")
    if st.get('trades') and (st.get('max_drawdown') or 0) < 1.25 * (sc.get('max_drawdown') or 0) - 2:
        reasons.append('deeper drawdown than the current rules')
    proposal = None
    if not reasons:
        whole, _ = tune(evaluate, current, specs, pol, mode, (fs[0][0].isoformat(), None))
        proposal = clamp_steps(whole, current, specs, pol['max_steps'])
        if all(abs(proposal[k] - current[k]) < 1e-9 for k in current):
            proposal, reasons = None, ['tuning on all history lands on the current rules']
    return {'folds': rows, 'oos_tuned': st, 'oos_current': sc, 'gain_r': round(gain, 3),
            'proposal': proposal, 'reasons': reasons}


# ---------------------------------------------------------------- shadow, rollback, pause
def shadow_decision(challenger: List[Dict[str, Any]], champion: List[Dict[str, Any]], days: int,
                    pol: Dict[str, Any], mode: str) -> Tuple[str, str]:
    """('promote' | 'drop' | 'wait', why) for a challenger that has run `days` sessions."""
    a, b = perf.summary(challenger), perf.summary(champion)
    na, nb = a.get('trades', 0), b.get('trades', 0)
    ta, tb = a.get('total_r', 0) or 0, b.get('total_r', 0) or 0
    need = pol['shadow_min_trades'][mode]
    if na + nb >= need and ta < tb - 3:
        return 'drop', f'{ta:+.1f}R in shadow against {tb:+.1f}R for the current rules'
    if days < pol['shadow_days'] or (na + nb < need and days < pol['shadow_max_days']):
        return 'wait', f'{days}/{pol["shadow_days"]} sessions · {na} trades vs {nb}'
    if ta >= tb - 0.5:
        return 'promote', f'{ta:+.1f}R in {days} shadow sessions against {tb:+.1f}R for the current rules'
    return 'drop', f'{ta:+.1f}R in shadow against {tb:+.1f}R for the current rules'


def rollback_due(live_since: List[Dict[str, Any]], replay_new: List[Dict[str, Any]], replay_old: List[Dict[str, Any]],
                 pol: Dict[str, Any], mode: str) -> Optional[str]:
    live = perf.summary(live_since).get('total_r', 0) or 0
    new, old = (perf.summary(x).get('total_r', 0) or 0 for x in (replay_new, replay_old))
    if live <= -pol['rollback_r'][mode] and old > new + 1:
        return (f'live paper trades since the change: {live:+.1f}R; the previous rules would have made '
                f'{old:+.1f}R over the same days against {new:+.1f}R')
    return None


def pause_due(recent: List[Dict[str, Any]], pol: Dict[str, Any], mode: str) -> Optional[str]:
    c = perf.closed(recent)[-pol['pause_window']:]
    total = sum(float(t['r']) for t in c)
    if len(c) >= min(10, pol['pause_window']) and total <= -pol['pause_r'][mode]:
        return f'the last {len(c)} paper trades add up to {total:+.1f}R (limit −{pol["pause_r"][mode]:g}R)'
    return None


def resume_due(during_pause: List[Dict[str, Any]]) -> Optional[str]:
    c = perf.closed(during_pause)
    total = sum(float(t['r']) for t in c)
    if len(c) >= 5 and total >= 2:
        return f'{len(c)} trades during the pause made {total:+.1f}R'
    return None
