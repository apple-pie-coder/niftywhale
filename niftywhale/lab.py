"""
The lab: backtests, the autopilot and the weekly report, in their own process
(`python -m niftywhale.lab`, the `niftywhale-lab` container) so heavy work
never slows the live scanner. It shares the database with the app: the app
queues jobs in `lab_jobs` and reads results from `bt_runs`; the lab never
renews the Dhan token (the app owns it) and does its downloads and number
crunching outside market hours.

Schedule (IST):
  first start      bootstrap: download history, build records, baseline backtests
  weekdays 20:30   nightly: top up history and records, baseline backtests,
                   shadow / rollback / pause checks, options candidate outcomes
  Saturday 06:00   tune: walk-forward per mode; a proposal starts its shadow run
  Saturday 09:00   weekly Telegram report
"""
import json
import logging
from html import escape as _esc
import multiprocessing as mp
import os
import re
import signal
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from niftywhale import autopilot, backtest, data, dhan, history, intraday, notify, options, perf, smc, store, universe

logger = logging.getLogger('niftywhale.lab')

WORKERS = int(os.getenv('LAB_WORKERS', '3'))
SETTING_KEYS = {'swing': 'rules', 'intraday': 'intraday_rules', 'options': 'options_rules'}
STOP = {'flag': False}


# ---------------------------------------------------------------- configuration
def current_rules(mode: str):
    st = store.settings()
    try:
        saved = json.loads(st.get(SETTING_KEYS[mode]) or '{}')
    except ValueError:
        saved = {}
    if mode == 'swing':
        return smc.Rules.from_env().with_overrides(saved)
    if mode == 'intraday':
        return intraday.IntradayRules.from_env().with_overrides(saved)
    return options.rules_from(saved)


def specs(mode: str) -> Dict[str, Dict[str, Any]]:
    """The thresholds the autopilot may move, with the tuner's step and range."""
    if mode == 'swing':
        return {k: smc.TUNABLE[k] for k in backtest.FILTERS + backtest.GATES['swing']}
    if mode == 'intraday':
        return {k: intraday.TUNABLE[k] for k in backtest.FILTERS}
    out = {k: {'step': options.TUNABLE[k][3], 'min': options.TUNABLE[k][1], 'max': options.TUNABLE[k][2]}
           for k in options.LEARN_FILTERS}
    out.update({k: {'values': list(v), 'step': 5, 'min': min(v), 'max': max(v)} for k, v in options.EXIT_GRID.items()})
    return out


def current_thresholds(mode: str) -> Dict[str, float]:
    r = current_rules(mode)
    if mode == 'options':
        return {k: float(r[k]) for k in specs('options')} | {'max_per_day': r['max_per_day']}
    return backtest.thresholds(mode, r)


def policy() -> Dict[str, Any]:
    try:
        return autopilot.policy(json.loads(store.settings().get('autopilot_policy') or '{}'))
    except ValueError:
        return autopilot.policy({})


def universe_key(mode: str) -> str:
    """The universe a mode is backtested (and tuned) on: by default the one it trades live,
    so the autopilot never tunes intraday rules on large caps and applies them to smallcaps."""
    st = store.settings()
    if mode == 'swing':
        return st.get('lab_swing_universe') or st['universe']
    return st.get('lab_intraday_universe') or st['intraday_universe']


def universe_for(mode: str) -> List[Dict[str, Any]]:
    which = universe_key(mode)
    try:
        return universe.members(which)
    except (OSError, ValueError):
        return []


def years(mode: str) -> float:
    st = store.settings()
    try:
        return max(0.5, min(5.0, float(st['lab_years_swing' if mode == 'swing' else 'lab_years_intraday'])))
    except ValueError:
        return 3.0 if mode == 'swing' else 2.0


def busy_market(now: Optional[datetime] = None) -> bool:
    """Heavy work waits for the session to end: 09:00-15:45 on weekdays."""
    now = now or data.now_ist()
    return now.weekday() < 5 and dtime(9, 0) <= now.time() < dtime(15, 45)


def wait_for_close(job: Optional[Dict[str, Any]] = None) -> None:
    while busy_market() and not STOP['flag']:
        if job:
            store.update_job(job['id'], progress='waiting for the market to close')
        time.sleep(60)


def tg(text: str) -> bool:
    return store.settings()['telegram'] == '1' and notify.send(text)


def esc(x) -> str:
    """Telegram's HTML mode refuses a whole message over one stray '<' (a '< 12' bucket)."""
    return _esc(str(x), quote=False)


# ---------------------------------------------------------------- history and records
def update_history(job: Dict[str, Any], progress: Callable[[str], None]) -> Dict[str, Any]:
    if not dhan.available():
        raise RuntimeError('Dhan is not connected: 15-minute and 5-minute history come from Dhan')
    swing = [m['symbol'] for m in universe_for('swing')]
    intra = [m['symbol'] for m in universe_for('intraday')]
    progress('market context (Nifty, VIX)')
    history.update_market()
    allsym = sorted(set(swing) | set(intra))
    progress(f'daily candles for {len(allsym)} stocks')
    history.update_daily(allsym, max(years('swing'), years('intraday')) + 1.2)
    new = 0
    for i, s in enumerate(swing):
        wait_for_close(job)
        progress(f'15m candles {i + 1}/{len(swing)} ({s})')
        new += history.update_intraday(s, 15, years('swing') + 0.1, lambda: STOP['flag'])
    for s in intra:
        if s not in swing:
            new += history.update_intraday(s, 15, years('intraday') + 0.1, lambda: STOP['flag'])
    for i, s in enumerate(intra):
        wait_for_close(job)
        progress(f'5m candles {i + 1}/{len(intra)} ({s})')
        new += history.update_intraday(s, 5, years('intraday'), lambda: STOP['flag'])
    return {'new_bars': new}


def _build(args) -> int:
    """One stock's records (runs in a worker process)."""
    mode, symbol, rules_dict, start_iso, block = args
    logging.basicConfig(level=logging.WARNING)
    try:
        return _build_one(mode, symbol, rules_dict, start_iso, block)
    except Exception as e:                      # one bad file must not sink the whole job
        logging.getLogger('niftywhale.lab').warning(f'{mode} records for {symbol} failed: {e}')
        return -1


def _build_one(mode, symbol, rules_dict, start_iso, block) -> int:
    start = date.fromisoformat(start_iso)
    old = backtest.load_records(mode, symbol)
    if mode == 'swing':
        daily = history.load('1d', symbol)
        if daily is None:
            return 0
        rec = backtest.swing_records(symbol, daily, smc.Rules(**rules_dict), start, block, old)
        n = len(rec['days'])
    else:
        f15, daily = history.load('15m', symbol), history.load('1d', symbol)
        if f15 is None or daily is None:
            return 0
        rec = backtest.intraday_records(symbol, f15, daily, intraday.IntradayRules(**rules_dict), start, old)
        n = len(rec['days'])
    backtest.save_records(mode, symbol, rec)
    return n


def build_records(mode: str, job: Dict[str, Any], progress: Callable[[str], None]) -> int:
    members = universe_for(mode)
    rules = current_rules(mode)
    start = (data.now_ist() - timedelta(days=int(365 * years(mode)))).date().isoformat()
    tasks = [(mode, m['symbol'], rules.as_dict(), start,
              (None if m.get('fo') else 'not an F&O stock') if mode == 'swing' else None) for m in members]
    done = 0
    ctx = mp.get_context('spawn')                 # never fork a process holding threads and sockets
    batch = WORKERS * 3
    with ProcessPoolExecutor(max_workers=WORKERS, mp_context=ctx) as pool:
        # In small batches, so a long build stops for the session instead of competing with it.
        for i in range(0, len(tasks), batch):
            wait_for_close(job)
            if STOP['flag']:
                break
            for _ in pool.map(_build, tasks[i:i + batch], chunksize=1):
                done += 1
            progress(f'{mode} records {done}/{len(tasks)}')
    return done


# ---------------------------------------------------------------- simulation
class Bench:
    """Everything needed to simulate one mode, loaded once, with a memo shared by all
    the rule sets tried (most of them see the same zones)."""

    def __init__(self, mode: str):
        self.mode = mode
        self.rules = current_rules(mode)
        self.memo: Dict = {}
        self.cache: Dict = {}
        if mode == 'options':
            self.cands = [c for c in store.option_candidates() if c.get('outcomes')]
            return
        self.symbols = [m['symbol'] for m in universe_for(mode)]
        self.records = {s: backtest.load_records(mode, s) for s in self.symbols}
        key = backtest.structural_key(mode, self.rules)
        self.records = {s: r for s, r in self.records.items() if r and r.get('key') == key}
        self.market = history.load('market', 'NIFTY_VIX')
        self.dailies = {s: history.load('1d', s) for s in self.records}
        self.d15 = backtest.Data('15m')
        self.d5 = backtest.Data('5m') if mode == 'intraday' else None

    def ready(self) -> bool:
        return bool(self.cands) if self.mode == 'options' else bool(self.records)

    def run(self, thr: Dict[str, float]) -> List[Dict[str, Any]]:
        if self.mode == 'options':
            # Outcomes exist only for the exit grid: a hand-set 27 % stop is read as the nearest, 25 %.
            thr = {**thr, **{k: min(v, key=lambda g: abs(g - thr[k])) for k, v in options.EXIT_GRID.items()}}
        fp = tuple(sorted((k, round(v, 6)) for k, v in thr.items()))
        if fp not in self.cache:
            if self.mode == 'options':
                self.cache[fp] = options.simulate_candidates(self.cands, thr)
            else:
                self.cache[fp] = backtest.simulate(self.mode, list(self.records), thr, self.rules, self.memo, self.d15,
                                                   self.d5, self.records, self.market, self.dailies)
        return self.cache[fp]

    def span(self):
        if self.mode == 'options':
            days = sorted({c['session'] for c in self.cands})
        else:
            days = sorted({d for r in self.records.values() for d in (r['days'] if self.mode == 'swing' else r['days'].keys())})
        return (date.fromisoformat(days[0]), date.fromisoformat(days[-1])) if days else (None, None)


def baseline(mode: str, bench: Optional['Bench'] = None) -> Optional[Dict[str, Any]]:
    """The current rules over all the history: what the Performance view shows."""
    bench = bench or Bench(mode)
    if not bench.ready():
        return None
    thr = current_thresholds(mode)
    trades = bench.run(thr)
    first, last = bench.span()
    s = perf.summary(trades)
    run_id = store.add_run(mode, 'baseline', thr, (str(first), str(last)),
                           universe_key(mode) if mode != 'options' else 'options candidates',
                           s, perf.breakdown(trades), perf.equity(trades),
                           {'highlights': perf.highlights(perf.breakdown(trades)),
                            'symbols': len(bench.records) if mode != 'options' else None,
                            'by_symbol': _by_symbol(trades)})
    store.prune_runs()
    return {'run_id': run_id, **s}


def _by_symbol(trades, limit: int = 12):
    rows = {}
    for t in perf.closed(trades):
        rows.setdefault(t['symbol'], []).append(float(t['r']))
    out = [{'symbol': k, 'n': len(v), 'total_r': round(sum(v), 2)} for k, v in rows.items()]
    out.sort(key=lambda r: -r['total_r'])
    return {'best': out[:limit], 'worst': out[-limit:][::-1]}


# ---------------------------------------------------------------- autopilot
def state(mode: str) -> Dict[str, Any]:
    return store.get_json(f'autopilot:{mode}', {}) or {}


def save_state(mode: str, st: Dict[str, Any]) -> None:
    store.set_json(f'autopilot:{mode}', st)


def apply_thresholds(mode: str, thr: Dict[str, float], why: str, by: str = 'autopilot',
                     base: Optional[Dict[str, float]] = None) -> Dict[str, list]:
    """Write new thresholds into the rule overrides the live app reads at its next scan.
    Only the thresholds that differ from `base` (the rules the proposal was tuned against)
    are written, so a threshold you changed by hand since is left alone. Returns the
    changes as {threshold: [before, after]}."""
    key = SETTING_KEYS[mode]
    try:
        saved = json.loads(store.settings().get(key) or '{}')
    except ValueError:
        saved = {}
    sp = specs(mode)
    base = base or current_thresholds(mode)
    cast = lambda k, v: int(v) if k in ('min_avg_volume', 'max_per_day', 'min_zone_age') else v
    changed = {k: [saved.get(k), cast(k, v)] for k, v in thr.items()
               if k in sp and (base.get(k) is None or abs(float(v) - float(base[k])) > 1e-9)}
    new = {**saved, **{k: after for k, (_, after) in changed.items()}}
    store.set_settings({key: new})
    store.log_autopilot(mode, 'promote', {k: b for k, (b, _) in changed.items()}, {k: a for k, (_, a) in changed.items()}, why, by)
    st = state(mode)
    st['last_change'] = {'at': data.now_ist().isoformat(timespec='seconds'), 'changed': changed, 'why': why,
                         'session': data.now_ist().date().isoformat()}
    st.pop('challenger', None)
    st.pop('awaiting', None)
    st['watch_from'] = st['last_change']['at']           # the pause rule starts counting afresh
    save_state(mode, st)
    if store.settings().get(f'paused_{mode}') == '1':
        store.set_settings({f'paused_{mode}': '0'})
        store.log_autopilot(mode, 'resume', None, None, 'new rules promoted', by)
    return changed


def revert_change(mode: str, lc: Dict[str, Any]) -> Dict[str, Any]:
    """Put back the thresholds a change set, where they still hold its value (a later
    edit by hand wins). Returns the overrides now saved."""
    key = SETTING_KEYS[mode]
    try:
        saved = json.loads(store.settings().get(key) or '{}')
    except ValueError:
        saved = {}
    for k, (before, after) in (lc.get('changed') or {}).items():
        if saved.get(k) == after:
            if before is None:
                saved.pop(k, None)
            else:
                saved[k] = before
    store.set_settings({key: saved})
    return saved


def describe(thr: Dict[str, float], base: Dict[str, float]) -> str:
    def show(k, v):                     # a context gate at its 'off' value reads as off
        return 'off' if k in smc.GATES and v == smc.TUNABLE[k]['off'] else f'{v:g}'
    parts = [f'{k} {show(k, base[k])} → {show(k, v)}' for k, v in thr.items()
             if base.get(k) is not None and abs(v - base[k]) > 1e-9]
    return ', '.join(parts) or 'no change'


def tune(mode: str, progress: Callable[[str], None]) -> Dict[str, Any]:
    pol = policy()
    st = state(mode)
    if pol['mode'] == 'off':
        return {'skipped': 'autopilot is off'}
    last = st.get('last_change', {}).get('at')
    if last and data.now_ist() - datetime.fromisoformat(last) < timedelta(days=pol['cooldown_days']):
        return {'skipped': f'a change was made on {last[:10]}; next tuning after {pol["cooldown_days"]} days'}
    if st.get('challenger') or st.get('awaiting'):
        return {'skipped': 'a proposal is already in shadow or awaiting approval'}
    bench = Bench(mode)
    if not bench.ready():
        return {'skipped': 'no backtest data yet'}
    first, lastday = bench.span()
    current = current_thresholds(mode)
    progress(f'{mode}: walk-forward tuning over {first} – {lastday}')
    sp = specs(mode)
    wf = autopilot.walk_forward(lambda thr: bench.run({**current, **thr}), {k: current[k] for k in sp}, sp, pol, mode,
                                first, lastday)
    st['last_tune'] = {'at': data.now_ist().isoformat(timespec='seconds'), 'oos_tuned': wf['oos_tuned'],
                       'oos_current': wf['oos_current'], 'gain_r': wf['gain_r'], 'reasons': wf['reasons'],
                       'folds': [{'test': f['test'], 'tuned': f['tuned'], 'current': f['current']} for f in wf['folds']]}
    if wf['proposal']:
        proposal = {**current, **wf['proposal']}
        st['challenger'] = {'params': proposal, 'base': current, 'since': data.now_ist().date().isoformat(),
                            'proposed_at': data.now_ist().isoformat(timespec='seconds'),
                            'change': describe(proposal, current),
                            'evidence': f"out of sample {wf['oos_tuned'].get('avg_r')}R/trade over {wf['oos_tuned'].get('trades')} trades "
                                        f"against {wf['oos_current'].get('avg_r')}R for the current rules"}
        store.log_autopilot(mode, 'propose', current, proposal, st['challenger']['evidence'])
        tg(f"🐋 <b>Autopilot · {mode}</b>: proposing {esc(st['challenger']['change'])}.\n{esc(st['challenger']['evidence'])}.\n"
           f"It now runs in shadow for {pol['shadow_days']} sessions before anything changes.")
    save_state(mode, st)
    return {'proposal': bool(wf['proposal']), 'reasons': wf['reasons'], 'gain_r': wf['gain_r']}


def shadow_and_guards(mode: str, bench: 'Bench') -> List[str]:
    """Nightly: move a shadow proposal along, check for a rollback, pause or resume."""
    pol = policy()
    notes = []
    st = state(mode)
    today = data.now_ist().date()
    ch = st.get('challenger')
    if ch and pol['mode'] != 'off' and bench.ready():
        since = date.fromisoformat(ch['since'])
        current = current_thresholds(mode)
        delta = {k: v for k, v in ch['params'].items() if abs(v - (ch.get('base') or current).get(k, v)) > 1e-9}
        new = [t for t in bench.run({**current, **delta}) if t['entry_time'][:10] >= ch['since']]
        old = [t for t in bench.run(current) if t['entry_time'][:10] >= ch['since']]
        days = len(pd.bdate_range(since, today)) - 1
        verdict, why = autopilot.shadow_decision(new, old, days, pol, mode)
        ch['shadow'] = {'days': days, 'challenger': perf.summary(new), 'current': perf.summary(old), 'verdict': verdict, 'why': why}
        if verdict == 'promote':
            if pol['mode'] == 'approve':
                st['awaiting'] = ch
                st.pop('challenger', None)
                store.log_autopilot(mode, 'awaiting', current, ch['params'], why)
                tg(f"🐋 <b>Autopilot · {mode}</b>: {esc(ch['change'])} passed its shadow run ({esc(why)}). "
                   'Approve it on the Performance tab.')
            else:
                save_state(mode, st)
                apply_thresholds(mode, ch['params'], f"{ch['evidence']}; shadow: {why}", base=ch.get('base'))
                tg(f"🐋 <b>Autopilot · {mode}</b>: changed {esc(ch['change'])}.\nWhy: {esc(ch['evidence'])}; shadow run: {esc(why)}.\n"
                   'Undo it on the Performance tab if you disagree.')
                notes.append(f'promoted: {ch["change"]}')
                st = state(mode)
        elif verdict == 'drop':
            store.log_autopilot(mode, 'drop', current, ch['params'], why)
            st.pop('challenger', None)
            notes.append(f'dropped proposal: {why}')
        save_state(mode, st)

    # Rollback: live trades since the last change lost, and the old rules would have done better.
    lc = st.get('last_change')
    if lc and pol['mode'] != 'off' and not lc.get('reverted') and bench.ready():
        live = [t for t in store.live_trades(mode) if (t['entry_time'] or '')[:10] >= lc['session']]
        prev = {**current_thresholds(mode), **{k: float(b) for k, (b, _) in (lc.get('changed') or {}).items() if b is not None}}
        newr = [t for t in bench.run(current_thresholds(mode)) if t['entry_time'][:10] >= lc['session']]
        oldr = [t for t in bench.run(prev) if t['entry_time'][:10] >= lc['session']]
        why = autopilot.rollback_due(live, newr, oldr, pol, mode)
        if why:
            revert_change(mode, lc)
            store.log_autopilot(mode, 'rollback', {k: a for k, (_, a) in (lc.get('changed') or {}).items()},
                                {k: b for k, (b, _) in (lc.get('changed') or {}).items()}, why)
            lc['reverted'] = data.now_ist().isoformat(timespec='seconds')
            save_state(mode, st)
            tg(f'🐋 <b>Autopilot · {mode}</b>: rolled back the last change. {esc(why)}.')
            notes.append('rolled back')

    # Pause / resume the Telegram entry alerts.
    trades = store.live_trades(mode)
    paused = store.settings().get(f'paused_{mode}') == '1'
    if not paused and pol['mode'] != 'off':
        # Only trades since alerts last came back (or rules last changed): the streak that
        # caused an earlier pause must not pause them again the next night.
        fresh = [t for t in trades if (t['entry_time'] or '') >= st.get('watch_from', '')]
        why = autopilot.pause_due(fresh, pol, mode)
        if why:
            store.set_settings({f'paused_{mode}': '1'})
            st['paused_at'] = data.now_ist().isoformat(timespec='seconds')
            save_state(mode, st)
            store.log_autopilot(mode, 'pause', None, None, why)
            tg(f'🐋 <b>Autopilot · {mode}</b>: entry alerts paused — {esc(why)}. Trades are still recorded on the dashboard.')
            notes.append('paused')
    elif paused and st.get('paused_at'):
        during = [t for t in trades if (t['entry_time'] or '') >= st['paused_at']]
        why = autopilot.resume_due(during)
        if why:
            store.set_settings({f'paused_{mode}': '0'})
            st.pop('paused_at', None)
            st['watch_from'] = data.now_ist().isoformat(timespec='seconds')
            save_state(mode, st)
            store.log_autopilot(mode, 'resume', None, None, why)
            tg(f'🐋 <b>Autopilot · {mode}</b>: entry alerts back on — {esc(why)}.')
            notes.append('resumed')
    return notes


# ---------------------------------------------------------------- options candidates
def candidate_outcomes(progress: Callable[[str], None]) -> int:
    """For each finished session's candidates, the option's later prices from that day's
    snapshots, and what every stop / target pair would have done."""
    today = data.now_ist().date().isoformat()
    after_close = not busy_market() and data.now_ist().time() >= dtime(15, 35)
    pending = [c for c in store.option_candidates(pending=True) if c['session'] < today or after_close]
    done = 0
    snaps: Dict = {}
    for c in pending:
        key = (c['symbol'], c['expiry'], c['session'])
        if key not in snaps:
            snaps[key] = store.snapshots_for(*key)
        path = []
        for sn in snaps[key]:
            if sn['ts'] <= c['ts']:
                continue
            ch = options.expand(sn['chain'])
            row = next((r for r in ch['strikes'] if r['k'] == c['strike']), None)
            o = (row or {}).get(c['side'].lower())
            path.append((sn['ts'], (o or {}).get('ltp'), ch.get('spot')))
        if not path:                       # no later read of this chain: nothing to learn from it
            store.set_candidate_outcomes(c['id'], {})
            continue
        outs = {f'{sp:g}/{tp:g}': options.path_outcome(path, c['entry'], sp, tp, c['direction'], c.get('level'))
                for sp in options.EXIT_GRID['stop_pct'] for tp in options.EXIT_GRID['target_pct']}
        store.set_candidate_outcomes(c['id'], outs)
        done += 1
    if done:
        progress(f'options: outcomes for {done} candidates')
    return done


# ---------------------------------------------------------------- weekly report
def weekly_report() -> str:
    lines = ['🐋 <b>NiftyWhale weekly report</b>']
    for mode in ('swing', 'intraday', 'options'):
        live = store.live_trades(mode)
        wk, allt = perf.by_period(live, 7), perf.summary(live)
        run = store.latest_run(mode, 'baseline')
        line = f"\n<b>{mode.capitalize()}</b>\nThis week: "
        line += (f"{wk['trades']} closed, {wk['wins']} won, {wk['total_r']:+.1f}R" if wk.get('trades') else 'no closed trades')
        if allt.get('trades'):
            line += f"\nAll time: {allt['trades']} trades, {allt['win_rate']:.0f}% won, {allt['total_r']:+.1f}R ({allt['avg_r']:+.2f}R/trade)"
        if run and run.get('summary', {}).get('trades'):
            s = run['summary']
            line += (f"\nBacktest ({run['period_from']} – {run['period_to']}): {s['trades']} trades, "
                     f"{s['win_rate']:.0f}% won, {s['avg_r']:+.2f}R/trade, max drawdown {s['max_drawdown']:.1f}R")
            for h in (run.get('extra') or {}).get('highlights', [])[:3]:
                line += f"\n  {'✅' if h['signal'] == 'good' else '⚠️'} {_esc(h['dimension'])} {_esc(h['bucket'])}: {h['avg_r']:+.2f}R over {h['n']} trades"
        st = state(mode)
        if st.get('challenger'):
            sh = st['challenger'].get('shadow') or {}
            line += f"\nAutopilot: testing {_esc(st['challenger']['change'])} in shadow ({_esc(sh.get('why', 'just started'))})"
        elif st.get('awaiting'):
            line += f"\nAutopilot: {_esc(st['awaiting']['change'])} is waiting for your approval"
        elif st.get('last_tune'):
            reasons = st['last_tune'].get('reasons') or []
            line += "\nAutopilot: no change proposed" + (f" — {_esc(reasons[0])}" if reasons else '')
        if store.settings().get(f'paused_{mode}') == '1':
            line += '\n⏸ Entry alerts are paused by the autopilot'
        lines.append(line)
    week = [r for r in store.autopilot_log(20) if r['ts'] >= (datetime.now() - timedelta(days=7)).isoformat()]
    if week:
        lines.append('\n<b>Autopilot this week</b>\n' + '\n'.join(f"{r['ts'][:10]} {_esc(r['mode'])}: {_esc(r['action'])}" for r in week[:8]))
    lines.append('\nPaper trades and backtests, not your fills. Not investment advice.')
    return '\n'.join(lines)


# ---------------------------------------------------------------- job steps
KIND_LABEL = {'bootstrap': 'First-time setup', 'nightly': 'Nightly update', 'backtest': 'Backtest',
              'tune': 'Tuning', 'report': 'Weekly report'}
STEP_LABEL = {
    'market': 'Market context (Nifty, VIX)', 'daily': 'Daily candles', 'm15': '15-minute candles',
    'm5': '5-minute candles', 'swing_records': 'Swing records', 'swing_sim': 'Swing: simulate the current rules',
    'intraday_records': 'Intraday records', 'intraday_sim': 'Intraday: simulate the current rules',
    'options': 'Options: candidate outcomes', 'tune_swing': 'Tune swing', 'tune_intraday': 'Tune intraday',
    'tune_options': 'Tune options', 'report': 'Write and send the report',
}
# The progress line names the step it belongs to, so even a job started by older
# code (no step record) shows where it is.
_STEP_RE = [
    (re.compile(r'^market context'), lambda m: 'market'),
    (re.compile(r'^daily candles'), lambda m: 'daily'),
    (re.compile(r'^15m candles (\d+)/(\d+)'), lambda m: 'm15'),
    (re.compile(r'^5m candles (\d+)/(\d+)'), lambda m: 'm5'),
    (re.compile(r'^(swing|intraday) records (\d+)/(\d+)'), lambda m: m.group(1) + '_records'),
    (re.compile(r'^(swing|intraday): simulating'), lambda m: m.group(1) + '_sim'),
    (re.compile(r'^(swing|intraday|options): walk-forward'), lambda m: 'tune_' + m.group(1)),
    (re.compile(r'^options:'), lambda m: 'options'),
    (re.compile(r'^writing the weekly report'), lambda m: 'report'),
]
_COUNT_RE = re.compile(r'(\d+)/(\d+)')


def step_of(text: str):
    """(step key, n, total) for a progress line; (None, None, None) for 'starting', 'waiting…'."""
    for rx, key in _STEP_RE:
        m = rx.match(text or '')
        if m:
            c = _COUNT_RE.search(text)
            return key(m), int(c.group(1)) if c else None, int(c.group(2)) if c else None
    return None, None, None


def plan(kind: str, args: Dict[str, Any]) -> List[str]:
    """The steps a job goes through, in order."""
    if kind in ('bootstrap', 'nightly', 'backtest'):
        hist = ['market', 'daily', 'm15', 'm5'] if kind != 'backtest' or args.get('download', True) else []
        return hist + ['swing_records', 'swing_sim', 'intraday_records', 'intraday_sim', 'options']
    if kind == 'tune':
        return ['tune_' + m for m in args.get('modes', ['swing', 'intraday', 'options'])]
    if kind == 'report':
        return ['report']
    return []


class JobLog:
    """A job's progress(text): the latest line, plus each step's start, end, counts and notes."""

    def __init__(self, job: Dict[str, Any]):
        self.job, self.steps, self.cur = job, {}, None

    def _save(self, **fields) -> None:
        store.update_job(self.job['id'], steps=json.dumps(self.steps), **fields)

    def __call__(self, text: str) -> None:
        key, n, total = step_of(text)
        now = datetime.now().isoformat(timespec='seconds')
        if key and key != self.cur:
            if self.cur:
                self.steps[self.cur]['finished'] = now
            self.steps.setdefault(key, {'started': now}).pop('finished', None)
            self.cur = key
        if key and n is not None:
            self.steps[key].update(n=n, total=total)
        self._save(progress=text[:200])
        logger.info(f"[{self.job['kind']}] {text}")

    def note(self, text: str) -> None:
        """A result line, kept with the step it came out of."""
        if self.cur:
            self.steps[self.cur].setdefault('notes', []).append(text[:300])
            self._save()

    def skip(self, key: str, why: str) -> None:
        """A step the job passed over, and why."""
        self.steps[key] = {'skipped': True, 'notes': [why[:300]]}
        self._save()

    def close(self, error: bool = False) -> None:
        if self.cur:
            self.steps[self.cur]['finished'] = datetime.now().isoformat(timespec='seconds')
            if error:
                self.steps[self.cur]['error'] = True
            self._save()


def _secs(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return max(0, int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()))
    except (TypeError, ValueError):
        return None


def job_view(job: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """A lab_jobs row for the dashboard: its steps in order with state, timing and counts."""
    now_s = (now or datetime.now()).isoformat(timespec='seconds')
    args = job.get('args') or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    try:
        steps = json.loads(job.get('steps') or '{}')
    except ValueError:
        steps = {}
    recorded = bool(steps)
    status, prog = job.get('status'), job.get('progress') or ''
    keys = plan(job.get('kind'), args)
    cur, n, total = step_of(prog) if status == 'running' else (None, None, None)
    if status == 'running':
        if cur:
            st = steps.setdefault(cur, {})
            if n is not None:
                st.update(n=n, total=total)
        else:                                          # waiting or starting: the step left open, if any
            cur = next((k for k in reversed(keys) if k in steps and not steps[k].get('finished')), None)
    out = []
    ci = keys.index(cur) if cur in keys else None
    for i, k in enumerate(keys):
        st = steps.get(k, {})
        if status == 'queued':
            state = 'pending'
        elif status == 'running':
            state = 'skipped' if st.get('skipped') else \
                ('running' if i == ci else 'done' if i < ci else 'pending') if ci is not None \
                else 'done' if st.get('finished') else 'pending'
        elif st.get('skipped'):
            state = 'skipped'
        elif st.get('error'):
            state = 'error'
        elif recorded:
            state = 'done' if k in steps else 'skipped'
        else:                                          # finished before steps were recorded
            state = 'done' if status == 'done' else 'unknown'
        v = {'key': k, 'label': STEP_LABEL.get(k, k), 'state': state, 'n': st.get('n'), 'total': st.get('total'),
             'notes': st.get('notes') or [], 'started': st.get('started'), 'finished': st.get('finished')}
        v['secs'] = _secs(st.get('started'), st.get('finished') or (now_s if state == 'running' else None))
        if state == 'running' and v['secs'] and v['n'] and v['total'] and v['n'] < v['total']:
            v['eta_secs'] = int(v['secs'] / v['n'] * (v['total'] - v['n']))
        out.append(v)
    return {
        **{k: job.get(k) for k in ('id', 'kind', 'status', 'created', 'started', 'finished', 'progress', 'message')},
        'title': KIND_LABEL.get(job.get('kind'), job.get('kind')), 'args': args, 'steps': out,
        'step_index': ci, 'waiting': status == 'running' and prog.startswith('waiting'),
        'secs': _secs(job.get('started'), job.get('finished') or (now_s if status == 'running' else None)),
        'queued_secs': _secs(job.get('created'), job.get('started') or (now_s if status == 'queued' else None)),
    }


# ---------------------------------------------------------------- jobs
def run_job(job: Dict[str, Any]) -> str:
    kind = job['kind']
    msgs: List[str] = []
    progress = JobLog(job)

    def say(text: str) -> None:
        msgs.append(text)
        progress.note(text)

    try:
        if kind in ('bootstrap', 'nightly', 'backtest'):
            wait_for_close(job)
            if kind != 'backtest' or job['args'].get('download', True):
                try:
                    h = update_history(job, progress)
                    say(f"history: {h['new_bars']:,} new candles")
                except Exception as e:             # Dhan down: carry on with the history already on disk
                    logger.warning(f'history update failed: {dhan._redact(e)}')
                    say(f'history not updated ({dhan._redact(e)[:120]})')
            for mode in ('swing', 'intraday'):
                wait_for_close(job)
                build_records(mode, job, progress)
                progress(f'{mode}: simulating the current rules')
                bench = Bench(mode)
                b = baseline(mode, bench)
                say(f"{mode}: {b.get('trades', 0) if b else 0} trades, {b.get('total_r') if b else '—'}R")
                if kind == 'nightly':
                    for n in shadow_and_guards(mode, bench):
                        say(f'{mode}: {n}')
            progress('options: candidate outcomes')
            candidate_outcomes(progress)
            bench = Bench('options')
            if bench.ready():
                baseline('options', bench)
                if kind == 'nightly':
                    for n in shadow_and_guards('options', bench):
                        say(f'options: {n}')
        elif kind == 'tune':
            for mode in job['args'].get('modes', ['swing', 'intraday', 'options']):
                wait_for_close(job)
                r = tune(mode, progress)
                if r.get('skipped'):
                    msgs.append(f"{mode}: {r['skipped']}")
                    progress.skip('tune_' + mode, r['skipped'])
                else:
                    say(f'{mode}: ' + ('proposal in shadow' if r.get('proposal') else '; '.join(r.get('reasons') or [])))
        elif kind == 'report':
            progress('writing the weekly report')
            text = weekly_report()
            sent = tg(text)
            say('report sent' if sent else 'report written (Telegram off)')
            store.set_json('lab:last_report', {'at': data.now_ist().isoformat(timespec='seconds'), 'text': text})
        else:
            raise ValueError(f'unknown job {kind}')
    except BaseException:
        progress.close(error=True)
        raise
    progress.close()
    return ' · '.join(msgs)[:1000]


def schedule(now: datetime) -> None:
    """Queue the regular jobs when their time comes (once each)."""
    marks = store.get_json('lab:schedule', {}) or {}
    today = now.date().isoformat()

    def due(name: str, cond: bool) -> None:
        if cond and marks.get(name) != today:
            store.add_job(name)
            marks[name] = today
            store.set_json('lab:schedule', marks)
    if not store.get_json('lab:bootstrapped'):
        if store.add_job('bootstrap'):
            store.set_json('lab:bootstrapped', True)
    due('nightly', now.weekday() < 5 and now.time() >= dtime(20, 30))
    due('tune', now.weekday() == 5 and now.time() >= dtime(6, 0))
    due('report', now.weekday() == 5 and now.time() >= dtime(9, 0) and store.settings().get('weekly_report') == '1')


def main() -> None:
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    try:
        from niftywhale import config
        config.apply_log_level()
    except Exception:
        pass
    store.init()
    store.reset_running_jobs()
    signal.signal(signal.SIGTERM, lambda *a: STOP.update(flag=True))
    beat = history.LAB_DIR / 'heartbeat'
    beat.parent.mkdir(parents=True, exist_ok=True)

    def heartbeat():                              # for the container healthcheck, even mid-job
        while not STOP['flag']:
            beat.touch()
            time.sleep(30)
    import threading
    threading.Thread(target=heartbeat, daemon=True, name='heartbeat').start()
    logger.info('Lab started')
    while not STOP['flag']:
        try:
            schedule(data.now_ist())
            job = store.next_job()
            if job:
                store.update_job(job['id'], progress='starting')
                try:
                    msg = run_job(job)
                    store.update_job(job['id'], status='done', finished=datetime.now().isoformat(timespec='seconds'),
                                     message=msg, progress='')
                except Exception as e:
                    logger.error(f"job {job['kind']} failed: {traceback.format_exc()}")
                    store.update_job(job['id'], status='error', finished=datetime.now().isoformat(timespec='seconds'),
                                     message=dhan._redact(e)[:500], progress='')
                continue
        except Exception:
            logger.exception('lab tick failed')
        time.sleep(30)


if __name__ == '__main__':
    main()
