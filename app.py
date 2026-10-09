"""
NiftyWhale -- a mechanical SMC setup scanner for NSE stocks.

Swing mode, two jobs:

  SCAN     Phases 1-3 (+ a provisional Phase 5) over the Nifty 100 / F&O
           universe on daily candles. Runs on demand or every weekday evening.
           Every passing stock becomes a watched zone.
  WATCH    Phase 4-5 during market hours: every 15 minutes, for the watched
           zones only, look for a tap into the zone, a 15m liquidity sweep and
           a 15m change of character; if the resulting R:R clears 1:3, alert.

Intraday mode (niftywhale/intraday.py), the same protocol one timeframe down:
  every 15 minutes in the session a 15m scan sets the day's zones, and every
  5 minutes a 5m sweep + CHoCH inside one is an entry at R:R >= 1:2. No
  entries after 14:30; open trades are tracked to target, stop or the 15:20
  square-off, which makes a journal of how the alerts actually played out.

Analysis only. Nothing here places an order.
"""
import json
import logging
import re
from html import escape as _esc
import math
import os
import threading
import requests
import time
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, g, jsonify, redirect, render_template, request  # noqa: E402
from flask_sock import Sock  # noqa: E402

from niftywhale import (auth, autopilot, charts, config, data, demo, dhan, features, glossary, history, hub, indicators, intraday, news, notify,  # noqa: E402
                        options, patterns, perf, smart, smc, store, ticker, universe)

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('niftywhale')
import mimetypes  # noqa: E402
mimetypes.add_type('application/manifest+json', '.webmanifest')   # the Home Screen app's manifest
try:
    config.apply_log_level()                 # a level chosen in App settings beats LOG_LEVEL
except Exception:
    pass

ENV_RULES = smc.Rules.from_env()
ENV_INTRA = intraday.IntradayRules.from_env()


def saved_overrides() -> dict:
    try:
        return json.loads(store.settings().get('rules') or '{}')
    except ValueError:
        return {}


def current_rules() -> smc.Rules:
    """The .env rules with anything saved from the rule tuner on top."""
    return ENV_RULES.with_overrides(saved_overrides())


def saved_intra_overrides() -> dict:
    try:
        return json.loads(store.settings().get('intraday_rules') or '{}')
    except ValueError:
        return {}


def irules() -> intraday.IntradayRules:
    """The intraday .env rules with anything saved from the intraday tuner on top."""
    return ENV_INTRA.with_overrides(saved_intra_overrides())


def short_block(stock: dict, mode: str = 'swing'):
    """Why this stock may not be shorted, or None. A cash-market short must be
    squared off the same day, so a swing short is only possible through F&O."""
    if mode == 'swing' and not stock.get('fo'):
        return 'shorting overnight needs F&O, and this is not an F&O stock'
    return None
# After each candle closes: let it reach yfinance (watch_delay), or Dhan, which has it within seconds
# (live_watch_delay). App settings, .env as the fallback (config.py).
def candle_delay() -> int:
    return config.get_int('live_watch_delay') if dhan.available() else config.get_int('watch_delay')

app = Flask(__name__)
# Behind Caddy (HTTPS on pandorasbox.local:5443): trust its X-Forwarded-For / -Proto / -Host, so the app
# sees the real client address and knows the request came over HTTPS (secure cookies, passkey origin).
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
# Signing in (niftywhale/auth.py): its routes, and the gate every other request passes first.
app.register_blueprint(auth.bp)
app.before_request(auth.gate)
if not auth.ENABLED:
    logger.warning('SIGN-IN IS OFF (NIFTYWHALE_AUTH=0): anyone who can reach the app can use it. This is for tests only.')
else:
    try:
        auth.setup_code()                       # logs the first-time setup code while there is no owner yet
    except Exception:
        logger.exception('Could not prepare the first-time setup code')

SCAN_LOCK = threading.Lock()
WATCH_LOCK = threading.Lock()
CANCEL = threading.Event()
STATE = {
    'scan': {'running': False, 'phase': 'idle', 'done': 0, 'total': 0,
             'started_at': None, 'scan_id': None, 'message': ''},
    'watch': {'running': False, 'last_run': None, 'next_run': None, 'message': ''},
    'intraday': {'scanning': False, 'checking': False, 'done': 0, 'total': 0, 'last_scan': None,
                 'last_check': None, 'next_scan': None, 'next_check': None, 'message': '', 'phase': ''},
    'options': {'running': False, 'done': 0, 'total': 0, 'last_run': None, 'next_run': None, 'message': '',
                'errors': {}, 'origin': None},
}
INTRA_SCAN_LOCK = threading.Lock()
INTRA_CHECK_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Scan (Phases 1-3)
# ---------------------------------------------------------------------------
def run_scan(origin: str = 'manual', which: str = None) -> None:
    """Screen the universe. Holds SCAN_LOCK for its whole run, and always
    releases it: a database hiccup must not leave every later scan refused."""
    s = STATE['scan']
    scan_id, status, error = None, 'done', None
    funnel, passed_rows, scanned = {}, [], 0
    try:
        which = which or store.settings()['universe']
        scan_id = store.start_scan(which, origin)
        s.update(running=True, phase='download', done=0, total=0, scan_id=scan_id,
                 started_at=datetime.now().isoformat(timespec='seconds'), message='')
        stocks = universe.members(which)
        tickers = [m['ticker'] for m in stocks]
        s['total'] = len(tickers)
        logger.info(f'Scan #{scan_id} ({origin}): {len(tickers)} stocks in {which}')

        # Fresh candles only: a frame cached at 14:35 is not the close.
        frames = data.daily(tickers, should_stop=CANCEL.is_set,
                            progress=lambda d, t: s.update(done=d, total=t), stale_ok=False)
        if CANCEL.is_set():
            status = 'stopped'

        s['phase'] = 'analyse'
        funnel, passed_rows, results = screen(stocks, frames, current_rules(),
                                              store.settings()['shorts'] == '1')
        scanned = funnel['data']
        store.save_results(scan_id, results)

        store.save_candidates(scan_id, passed_rows)
        if status == 'done' and scanned < max(1, len(stocks) / 2):
            # The data source was down (no network, yfinance refusing): judging
            # the universe on a fraction of it would expire every other zone.
            status = 'error'
            error = (f'only {scanned} of {len(stocks)} stocks had daily candles '
                     f'(data source down?); zones left as they were')
            logger.warning(f'Scan #{scan_id}: {error}')
        # A stopped scan saw only part of the universe; syncing would expire
        # every zone it did not get to. Stocks it could not judge keep theirs.
        no_data = {r['symbol'] for r in results if r.get('unjudged')}
        z = store.sync_zones(scan_id, passed_rows, no_data) if status == 'done' else \
            {'added': 0, 'refreshed': 0, 'expired': 0, 'spent': 0}
        logger.info(f"Scan #{scan_id}: {len(passed_rows)} setups "
                    f"({sum(r['analysis']['in_zone'] for r in passed_rows)} in zone); "
                    f"zones +{z['added']} ~{z['refreshed']} -{z['expired']}"
                    + (f" ({z['spent']} not re-armed: played out or trade open)" if z.get('spent') else '')
                    + f"; funnel {funnel}")

        if status == 'done' and origin == 'schedule' and passed_rows and store.settings()['telegram'] == '1':
            notify.send(scan_summary(passed_rows, which))
        if status == 'done' and origin == 'schedule':
            inz = sum(r['analysis']['in_zone'] for r in passed_rows)
            store.add_notice(f'scan:{scan_id}', 'scan', 'info', f"Swing scan: {len(passed_rows)} setup{'s' if len(passed_rows) != 1 else ''}",
                             f"{inz} already in their zone · zones +{z['added']} refreshed {z['refreshed']} expired {z['expired']}",
                             link={'tab': 'swing'})
    except Exception as e:
        logger.exception(f'Scan #{scan_id} failed')
        status, error = 'error', str(e)
        store.add_notice(f'scan-error:{scan_id}', 'system', 'warn', 'Swing scan failed', dhan._redact(e)[:200],
                         link={'tab': 'swing'})
    finally:
        try:
            if scan_id is not None:
                store.finish_scan(scan_id, status, scanned, len(passed_rows), funnel, error)
        except Exception:
            logger.exception(f'Scan #{scan_id}: could not record its result')
        s.update(running=False, phase='idle',
                 message=error or f'{len(passed_rows)} setups from {scanned} stocks')
        CANCEL.clear()
        SCAN_LOCK.release()


def screen(stocks, frames, rules, shorts=True):
    """
    Evaluate every stock, long -- or short, when its structure is bearish,
    shorts are on and it is an F&O stock. Returns (funnel, passing rows, one
    verdict per stock). Shared by the real scan and the rule tuner's what-if,
    so the two can never disagree about what a rule means.
    """
    order = [k for k, _ in smc.STEPS]
    reached = {k: 0 for k in order}          # how many stocks passed each step
    reached['universe'] = len(stocks)
    reached['data'] = 0
    passed, results = [], []
    for m in stocks:
        frame = frames.get(m['ticker'])
        # `unjudged`: no verdict on the stock itself, so the scan leaves its zone alone.
        if frame is None or frame.empty:
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': 'data',
                            'reason': 'no daily candles from yfinance', 'unjudged': True})
            continue
        try:
            r = smc.evaluate_both(frame, rules, shorts=shorts, short_block=short_block(m))
        except Exception as e:                       # one odd frame never sinks a scan
            logger.warning(f"{m['symbol']}: analysis failed: {e}")
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': 'history',
                            'reason': f'analysis error: {e}', 'unjudged': True})
            continue
        # The funnel's "Daily data" card means usable data: too little history
        # drops there, which is also where its "who stopped here" list shows it.
        if r['failed_at'] != 'history':
            reached['data'] += 1
        last = order.index(r['failed_at']) if r['failed_at'] else len(order)
        for k in order[:last]:
            reached[k] += 1
        results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': r['failed_at'],
                        'reason': r['reason'], 'passed': r['passed']})
        if r['passed']:
            # Price action on the last three daily candles, as confirmation.
            r['patterns'] = patterns.detect(frame, last_n=3, zone=r['zone'], side=r['side'])
            passed.append({'symbol': m['symbol'], 'name': m['name'], 'analysis': r})
    passed.sort(key=lambda x: (not x['analysis']['in_zone'], -x['analysis']['score']))
    return reached, passed, results


def scan_summary(rows, which) -> str:
    in_zone = [r for r in rows if r['analysis']['in_zone']]
    shorts = sum(r['analysis'].get('side') == 'short' for r in rows)
    lines = [f'🐋 <b>NiftyWhale</b> · {len(rows)} SMC setup(s) in {h(universe.label(which))}'
             + (f' ({len(rows) - shorts} long, {shorts} short)' if shorts else '')]

    def arrow(r):
        return '▼' if r['analysis'].get('side') == 'short' else '▲'
    if in_zone:
        lines.append(f'\n<b>In the zone now</b> ({len(in_zone)}):')
        lines += [f"  {arrow(r)} {h(r['symbol'])} · zone {r['analysis']['zone']['low']:.2f}–"
                  f"{r['analysis']['zone']['high']:.2f}" for r in in_zone[:8]]
    armed = [r for r in rows if not r['analysis']['in_zone']]
    if armed:
        lines.append(f'\n<b>Waiting for the pullback</b> ({len(armed)}):')
        lines += [f"  {arrow(r)} {h(r['symbol'])} · {r['analysis']['distance_pct']:.1f}% "
                  f"{'below' if r['analysis'].get('side') == 'short' else 'above'} zone" for r in armed[:8]]
    lines.append('\nWatching these on 15m candles tomorrow. Analysis only, not advice.')
    return '\n'.join(lines)


def start_scan_thread(origin: str, which: str = None) -> bool:
    if not SCAN_LOCK.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=run_scan, args=(origin, which), daemon=True, name='scan').start()
    except Exception:
        SCAN_LOCK.release()
        raise
    return True


# ---------------------------------------------------------------------------
# Watch (Phases 4-5)
# ---------------------------------------------------------------------------
def check_zones(origin: str = 'schedule') -> dict:
    """One pass over the open zones on 15m candles, and every open swing paper
    trade followed to its stop or target."""
    if not WATCH_LOCK.acquire(blocking=False):
        return {'skipped': 'a check is already running'}
    w = STATE['watch']
    w['running'] = True
    summary = {'checked': 0, 'tapped': 0, 'triggered': 0, 'rejected': 0, 'patterns': 0, 'closed': 0}
    try:
        zones = store.open_zones()
        trades = store.swing_trades(('triggered',))
        if not zones and not trades:
            w['message'] = 'no zones to watch'
            return summary
        tickers = list(dict.fromkeys(z['symbol'] + '.NS' for z in zones + trades))
        frames = data.intraday(tickers)
        now = data.now_ist()
        st = store.settings()
        telegram_on = st['telegram'] == '1'
        pattern_alerts = st['pattern_alerts'] == '1'
        rules = current_rules()
        for z in zones:
            frame = frames.get(z['symbol'] + '.NS')
            stamp = now.isoformat(timespec='seconds')
            if frame is None or frame.empty:
                store.update_zone(z['id'], only_if=OPEN, last_checked=stamp, note='no 15m data')
                continue
            summary['checked'] += 1
            # Only price action from the candle the zone was set in counts: a CHoCH
            # from before the zone existed is old news (a scan at 13:48 must not
            # turn the 09:30 CHoCH into a fresh entry alert).
            t = smc.choch_trigger(frame, z['zone_low'], z['zone_high'], z['target'], rules, now=now,
                                  since=zone_since(z, 15), side=z.get('side') or 'long')
            late = too_late(t, 15, now, SWING_LATE_MIN)
            if late:
                t.update(valid=False, late=True, reason=late)
            ctx = live_context(z, t, frame, 'swing') if t.get('triggered') and t['valid'] else None
            if ctx and z.get('source') != 'manual':
                # The context gates the autopilot learns (off by default). A zone you set
                # yourself is your call, and the backtester never saw those.
                gate = smc.context_gate(json.loads(ctx), z.get('side') or 'long', rules.as_dict())
                if gate:
                    t.update(valid=False, gated=True, reason=f'CHoCH, but skipped: {gate}')
            fields = {'last_checked': stamp, 'note': t.get('reason', '')}
            if t['tapped'] and z['status'] == 'watching':
                fields['status'] = 'tapped'
            if t.get('triggered'):
                if ctx:
                    fields['ctx'] = ctx
                if t['valid']:
                    # The entry alert is also a paper trade, followed from the CHoCH
                    # candle's close to its stop or target.
                    t.update(paper=True, followed_to=(pd.Timestamp(t['choch_time'])
                                                      + pd.Timedelta(minutes=15)).isoformat())
                fields.update(status='triggered' if t['valid'] else 'rejected', trigger=t)
            # Claim the change before alerting: a dismissal or a scan's expiry may
            # have landed since the zone was read, and then there is nothing to say.
            if not store.update_zone(z['id'], only_if=OPEN, **fields):
                continue
            if fields.get('status') == 'tapped':
                summary['tapped'] += 1
            if t.get('triggered'):
                ok = t['valid']
                msg = entry_message(z, t, now) if ok else None
                sent = bool(ok and telegram_on and not paused('swing') and notify.send(msg))
                store.add_alert(z, 'entry' if ok else 'rejected', t, sent, msg or t['reason'])
                if ok:
                    notice_entry('swing', f"swing:{z['id']}", z['symbol'], t, t.get('choch_time'))
                summary['triggered' if ok else 'rejected'] += 1
                logger.info(f"{z['symbol']}: {t['reason']}" + (' -> Telegram' if sent else ''))
                if ok:                                  # it may already have run to its stop or target
                    summary['closed'] += _follow_swing({**z, 'status': 'triggered', 'trigger': t},
                                                       frame, now, telegram_on)
            summary['patterns'] += record_patterns(z, frame, now, telegram_on and pattern_alerts)
        for z in trades:
            summary['checked'] += 1
            summary['closed'] += _follow_swing(z, frames.get(z['symbol'] + '.NS'), now, telegram_on)
        w['message'] = (f"{summary['checked']} checked · {summary['tapped']} newly tapped · "
                        f"{summary['triggered']} triggered · {summary['rejected']} rejected · "
                        f"{summary['closed']} trade(s) closed · {summary['patterns']} new pattern(s)")
        return summary
    except Exception as e:
        logger.exception('Zone check failed')
        w['message'] = f'check failed: {e}'
        return {'error': str(e)}
    finally:
        w['running'] = False
        w['last_run'] = datetime.now().isoformat(timespec='seconds')
        WATCH_LOCK.release()


OPEN = ('watching', 'tapped')


def _follow_swing(z, frame, now, tg) -> int:
    """
    Move a swing paper trade on from where it was last followed: smc.follow_trade
    on the 15m candles (daily ones fill a gap after downtime). On target or stop
    the zone becomes won / lost, the alert log gets the result and Telegram a
    short message. Returns 1 if the trade closed.
    """
    t = dict(z['trigger'] or {}) if isinstance(z['trigger'], dict) else json.loads(z['trigger'] or '{}')
    if not t.get('paper') or not t.get('entry'):
        return 0
    o = smc.follow_trade(t, frame, None, now)
    if o.get('need_daily'):
        ticker = z['symbol'] + '.NS'
        o = smc.follow_trade(t, frame, data.daily([ticker]).get(ticker), now)
    stamp = now.isoformat(timespec='seconds')
    t.update(followed_to=o['followed_to'], approx=o.get('approx', False))
    if o['status'] == 'open':
        t.update(last=o['last'], r_open=o['r'])
        note = f"open trade · {o['r']:+.2f}R at {o['last']:.2f}"
        if o.get('need_daily'):
            note += ' · waiting for daily candles to cover a data gap'
        store.update_zone(z['id'], only_if=('triggered',), last_checked=stamp, trigger=t, note=note)
        return 0
    t.update(exit=o['exit'], exit_time=o['exit_time'], r=o['r'], result=o['status'])
    word = {'won': 'Target hit', 'lost': 'Stopped out'}[o['status']]
    when = (pd.Timestamp(o['exit_time']).strftime('%d %b') + ', daily candle'
            if o.get('source') == 'daily' else _at_time(o['exit_time'], now))
    note = f"{word} at {o['exit']:.2f} ({when}) · {o['r']:+.2f}R"
    if not store.update_zone(z['id'], only_if=('triggered',), status=o['status'], last_checked=stamp,
                             trigger=t, note=note):
        return 0                                    # already settled: report it once
    msg = (f"🐋 <b>Swing {_words(t)['side'].lower()} · {h(z['symbol'])} · {word}</b>\n"
           f"Entry {t['entry']:.2f} ({_at_time(t['choch_time'], now)}) → exit {o['exit']:.2f} ({when}) · "
           f"<b>{o['r']:+.2f}R</b>\n"
           + ("Price gapped through the stop: out at the open.\n"
              if o['status'] == 'lost' and abs(o['exit'] - t['stop']) > 1e-9 else '')
           + 'Paper trade: analysis only, no order was placed.')
    sent = bool(tg and not paused('swing') and notify.send(msg))
    store.add_alert(z, o['status'], {**t, 'rr': o['r'], 'choch_time': o['exit_time']}, sent, note)
    notice_result('swing', f"swing:{z['id']}", z['symbol'], t, o['status'], o['exit'], o['r'], o['exit_time'])
    logger.info(f"Swing {z['symbol']}: {note}" + (' -> Telegram' if sent else ''))
    return 1


def zone_since(z, minutes: int):
    """When a zone's watch began, floored to the start of the `minutes` candle
    it began in: from that candle on, price action counts as action at the
    zone. An intraday zone whose levels moved restarts then (`meta.since`)."""
    meta = z.get('meta') if isinstance(z.get('meta'), dict) else {}
    raw = meta.get('since') or z.get('created_at')
    if not raw:
        return None
    ts = pd.Timestamp(raw)
    if ts.tz is None:
        ts = ts.tz_localize('Asia/Kolkata')
    return ts.floor(f'{minutes}min')


SWING_LATE_MIN = 45     # a 15m CHoCH found this long after its candle closed was missed live
INTRA_LATE_MIN = 20     # a 5m one, likewise


def too_late(t: dict, minutes: int, now: datetime, limit_min: int):
    """
    None while a triggered CHoCH is fresh enough to act on; otherwise why not.
    The watchers find a CHoCH within a candle or two of its close. One found
    later (the watcher was off, the app was down, the zone's levels changed)
    is history: its entry price is the close of a candle long gone, and price
    may already have run to the stop. It is recorded, never sent as an entry.
    """
    if not t.get('triggered') or not t.get('valid'):
        return None
    closed = pd.Timestamp(t['choch_time']) + pd.Timedelta(minutes=minutes)
    age = pd.Timestamp(now) - closed
    if age <= pd.Timedelta(minutes=limit_min):
        return None
    mins = int(age.total_seconds() // 60)
    ago = f'{mins} min' if mins < 120 else f'{mins // 60} h' if mins < 48 * 60 else f'{mins // 1440} days'
    return (f"CHoCH at {_at_time(t['choch_time'], now)} (R:R 1:{t['rr']:.1f}) found {ago} after its candle "
            f"closed: too late to enter")


def latest_sessions(frame, sessions=2):
    """The last few sessions of an intraday frame (prior candles give the
    patterns their 'after a decline' context)."""
    df = smc.clean(frame)
    days = sorted(set(df.index.date))[-sessions:]
    return df[[d in days for d in df.index.date]]


def pd_date(iso: str):
    return datetime.fromisoformat(iso).date()


def record_patterns(z, frame, now, alert) -> int:
    """
    Log the patterns for the zone's side (bullish for a long, bearish for a
    short) on the latest session's completed 15m candles.
    Returns how many were new. With `alert`, a new pattern inside the zone
    is sent to Telegram -- once, however many checks see it.
    """
    df = patterns.completed(latest_sessions(frame), now)
    if df.empty:
        return 0
    last_day = df.index[-1].date()
    new = 0
    for hit in patterns.detect(df, zone={'low': z['zone_low'], 'high': z['zone_high']},
                               side=z.get('side') or 'long'):
        if pd_date(hit['time']) != last_day:
            continue
        if not store.add_signal(z['symbol'], '15m', hit, z['id']):
            continue
        new += 1
        if alert and hit['at_zone']:
            msg = (f"🐋 <b>Price action · {h(z['symbol'])}</b>\n"
                   f"{h(hit['label'])} on the 15m candle at {hit['time'][11:16]} IST, inside the zone "
                   f"{z['zone_low']:.2f}–{z['zone_high']:.2f} (close {hit['close']:.2f}).\n"
                   f"A confirmation, not the entry: the entry is the 15m CHoCH.")
            if notify.send(msg):
                store.mark_signal_sent(z['symbol'], '15m', hit['key'], hit['time'])
    return new


def h(x) -> str:
    """Escape for Telegram's HTML mode. An unescaped & (M&M, "F&O") or < makes
    Telegram refuse the whole message, so every dynamic value goes through this."""
    return _esc(str(x), quote=False)


def _words(t) -> dict:
    """The direction words for a trigger's messages."""
    short = t.get('side') == 'short'
    return {'side': 'SHORT' if short else 'LONG', 'act': 'Sell' if short else 'Buy',
            'past': 'below' if short else 'above', 'extreme': 'high' if short else 'low',
            'pool': 'SSL' if short else 'BSL'}


def _at_time(iso: str, now: datetime = None) -> str:
    """'11:45 IST', or '11:45 IST on 05 Oct' for a candle from an earlier day:
    an alert must never pass yesterday's CHoCH off as today's."""
    try:
        ts = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return f'{str(iso)[11:16]} IST'
    now = now or data.now_ist()
    if ts.tzinfo is not None:
        ts = ts.astimezone(data.IST)
    return ts.strftime('%H:%M IST') + ('' if ts.date() == now.date() else ts.strftime(' on %d %b'))


def entry_message(z, t, now: datetime = None) -> str:
    w = _words(t)
    risk = abs(t['entry'] - t['stop'])
    return (f"🐋 <b>SMC ENTRY · {w['side']} · {h(z['symbol'])}</b>\n"
            f"15m CHoCH {w['past']} {t['choch_level']:.2f} at {_at_time(t['choch_time'], now)}, "
            f"after sweeping {t['swept_level']:.2f} ({w['extreme']} {t['sweep_low']:.2f}).\n\n"
            f"<b>{w['act']}</b> {t['entry']:.2f}\n"
            f"<b>Stop</b> {t['stop']:.2f}  (risk {risk:.2f}, {risk / t['entry'] * 100:.1f}%)\n"
            f"<b>Target</b> {t['target']:.2f}  (daily {w['pool']})\n"
            f"<b>R:R</b> 1:{t['rr']:.1f}\n\n"
            f"Daily zone {z['zone_low']:.2f}–{z['zone_high']:.2f}."
            + (" Short via F&amp;O (futures or options)." if t.get('side') == 'short' else '') + "\n"
            "Analysis only — no order was placed. Not investment advice.")


# ---------------------------------------------------------------------------
# Learning context and pauses (niftywhale/features.py, niftywhale/lab.py)
# ---------------------------------------------------------------------------
_MARKET = {'at': 0.0, 'frame': None}


def market_frame():
    """Nifty / VIX daily closes, kept by the lab; re-read hourly."""
    if time.time() - _MARKET['at'] > 3600:
        _MARKET.update(at=time.time(), frame=history.load('market', 'NIFTY_VIX'))
    return _MARKET['frame']


def paused(mode: str) -> bool:
    """Entry alerts muted for this mode (by the autopilot or by you): trades are still
    recorded and followed, just not sent to Telegram."""
    return store.settings().get(f'paused_{mode}') == '1'


def _setup_from_scan(symbol: str, mode: str) -> dict:
    scan = store.latest_scan(mode=mode)
    if not scan:
        return {}
    for c in store.candidates(scan['id']):
        if c['symbol'] == symbol:
            a = c['analysis'] or {}
            return {'position': a.get('position'), 'pre_rr': (a.get('plan') or {}).get('rr'), 'atr_pct': a.get('atr_pct'),
                    'avg_volume': a.get('avg_volume'), 'score': a.get('score'), 'in_zone': a.get('in_zone'),
                    'target_kind': a.get('target_kind')}
    return {}


def live_context(z: dict, t: dict, frame, mode: str) -> str:
    """The context of a live entry, as the backtester records it (JSON for zones.ctx).
    Never lets a missing piece hold up an alert."""
    try:
        day = pd.Timestamp(t['choch_time']).date()
        setup = _setup_from_scan(z['symbol'], mode)
        if mode == 'intraday':
            setup.setdefault('pre_rr', (z.get('meta') or {}).get('rr'))
            setup.setdefault('target_kind', (z.get('meta') or {}).get('target_kind'))
        else:
            try:
                setup['zone_age_days'] = (day - datetime.fromisoformat(z['created_at']).date()).days
            except (TypeError, ValueError):
                pass
        open_today = None
        if frame is not None and len(frame):
            today = frame[[ix.date() == day for ix in frame.index]]
            open_today = float(today['Open'].iloc[0]) if len(today) else None
        ctx = features.combine(features.trade_context(t, setup), features.market_context(market_frame(), day),
                               features.stock_day_context(history.load('1d', z['symbol']), day, open_today),
                               news_context(z['symbol'], t['choch_time']), smart_context(z['symbol'], t['choch_time']))
        return json.dumps(ctx)
    except Exception as e:
        logger.warning(f"context for {z.get('symbol')}: {e}")
        return json.dumps({})


# ---------------------------------------------------------------------------
# News desk (niftywhale/news.py)
# ---------------------------------------------------------------------------
NEWS_STATE = {'running': False, 'last_pass': None, 'last_full': None, 'message': '', 'errors': [], 'fetched': {}}
NEWS_LOCK = threading.Lock()
NEWS_EVERY_S = 15 * 60          # the board's stocks, during the session
NEWS_FULL_S = 60 * 60           # every followed stock (setups too), and the only pass outside the session
NEWS_ONE_GAP_S = 5 * 60         # an on-demand refresh of one stock, at most this often
NEWS_PER_STOCK = 15             # media headlines per stock in the every-stock view
NEWS_REACT_DAYS = 35            # reactions are measured for items this recent (filings go back 30)
NEWS_WHY = ('trade', 'tapped', 'board', 'intraday', 'options', 'setup')     # most important first
NEWS_HOURLY = ('options', 'setup')      # followed on the hourly pass only


def news_watch() -> list:
    """The stocks the desk follows, each with why (NEWS_WHY): open paper trades, zones price
    is in, the rest of the board, today's intraday zones, and the latest scans' setups."""
    now = data.now_ist()
    out = {}

    def add(sym, name, why):
        old = out.get(sym)
        if old is None or NEWS_WHY.index(why) < NEWS_WHY.index(old['why']):
            out[sym] = {'symbol': sym, 'name': (old or {}).get('name') or name or sym, 'why': why}
    for z in store.recent_zones(200):
        if _live_zone(z, now):
            add(z['symbol'], z.get('name'), 'trade' if z['status'] == 'triggered' and store.is_paper(z)
                else 'tapped' if z['status'] == 'tapped' else 'board')
    for z in store.intraday_zones(('watching', 'tapped', 'triggered'), session=now.date().isoformat()):
        add(z['symbol'], z.get('name'), 'trade' if z['status'] == 'triggered' else
            'tapped' if z['status'] == 'tapped' else 'intraday')
    for mode in ('swing', 'intraday'):
        scan = store.latest_scan(mode=mode)
        for c in store.candidates(scan['id']) if scan else []:
            add(c['symbol'], c.get('name'), 'setup')
    # Options mode: the stocks whose chains it reads (an index has no company news), and the
    # underlying of an open idea, which is a trade like any other.
    for i in store.ideas(limit=200):
        if i['status'] == 'open' and i['symbol'] not in options.INDICES:
            add(i['symbol'], None, 'trade')
    for sym in option_stocks():
        if sym not in options.INDICES:
            add(sym, None, 'options')
    return sorted(out.values(), key=lambda w: (NEWS_WHY.index(w['why']), w['symbol']))


def _names() -> dict:
    try:
        return {s['symbol']: s.get('name') or s['symbol'] for s in universe.load().get('stocks', [])}
    except (OSError, ValueError):
        return {}


def news_message(item: dict) -> str:
    where = {'trade': 'open trade', 'tapped': 'price in the zone'}.get(item.get('why'), 'on the board')
    detail = item.get('detail') or ''
    sent = item.get('sent')
    return (f"📰 <b>{h(item['symbol'])} · NSE filing</b> · {where}\n"
            f"<b>{h(item['title'])}</b>\n"
            + (h(detail[:300] + ('…' if len(detail) > 300 else '')) + '\n' if detail else '')
            + (f"FinBERT reads it as <b>{h(sent['label'])}</b> ({sent['score']:+.2f})\n" if sent else '')
            + (f"<a href=\"{h(item['url'])}\">The filing</a> · " if item.get('url') else '')
            + f"{datetime.fromisoformat(item['published']).strftime('%d %b %H:%M')}")


def news_pass(full: bool = True, only: list = None) -> dict:
    """Fetch the followed stocks' news (or `only` these), store what is new, and send a
    Telegram alert for a fresh NSE filing on an open trade or a zone price is in."""
    if not NEWS_LOCK.acquire(blocking=False):
        return {'skipped': 'a news pass is already running'}
    NEWS_STATE['running'] = True
    try:
        watch = only if only is not None else news_watch()
        if not full:
            watch = [w for w in watch if w['why'] not in NEWS_HOURLY]
        names = _names()
        fresh, errors = [], []
        for w in watch:
            items, errs = news.fetch(w['symbol'], names.get(w['symbol']) or w['name'])
            errors += [f"{w['symbol']} · {e}" for e in errs]
            fresh += [{**i, 'why': w['why']} for i in store.add_news(items)]
            NEWS_STATE['fetched'][w['symbol']] = time.time()
        st = store.settings()
        sent = 0
        for i in fresh:
            if i['why'] in ('trade', 'tapped') and news.alert_worthy(i, data.now_ist()):
                store.add_notice(f"news:{i['symbol']}:{i['uid']}", 'news', 'info',
                                 f"{'Filing' if i.get('kind') == 'filing' else 'News'} · {i['symbol']} "
                                 f"({'open trade' if i['why'] == 'trade' else 'in its zone'})",
                                 i.get('title') or '', i['symbol'], None, {'news': i['symbol']})
        if st['telegram'] == '1' and st.get('news_alerts') == '1':
            now = data.now_ist()
            due = [i for i in fresh if i['why'] in ('trade', 'tapped') and news.alert_worthy(i, now)]
            read = news.score([news.text_for(i) for i in due]) if due else None
            for k, i in enumerate(due):
                sent += bool(notify.send(news_message({**i, 'sent': read[0][k] if read else None})))
        scored = news_score_backlog()
        reacted = news_reactions()
        if full:
            store.prune_news()
            # Headlines kept before a filter existed (an overseas listing's price) go too.
            store.drop_news([i['id'] for i in store.news(None, 'media', limit=5000)
                             if news.FOREIGN_PRICE.search(i['title'] or '')])
            NEWS_STATE['last_full'] = datetime.now().isoformat(timespec='seconds')
        NEWS_STATE.update(last_pass=datetime.now().isoformat(timespec='seconds'), errors=errors[-10:],
                          message=f"{len(watch)} stock(s) · {len(fresh)} new item(s)"
                                  + (f" · {scored} read by FinBERT" if scored else '')
                                  + (f" · {reacted} reaction(s) measured" if reacted else '')
                                  + (f" · {sent} filing alert(s)" if sent else '')
                                  + (f" · {len(errors)} source error(s)" if errors else ''))
        if errors:
            logger.warning(f'News: {len(errors)} source error(s), e.g. {errors[0]}')
        return {'stocks': len(watch), 'new': len(fresh), 'alerts': sent, 'errors': len(errors)}
    finally:
        NEWS_STATE['running'] = False
        NEWS_LOCK.release()


def news_score_backlog(limit: int = 150) -> int:
    """Let FinBERT read the items it hasn't yet (new ones, and any a scorer outage left behind)."""
    rows = store.news_unscored(limit)
    if not rows:
        return 0
    got = news.score([news.text_for(r) for r in rows])
    if not got:
        return 0
    results, model = got
    store.set_sentiment({r['id']: res for r, res in zip(rows, results)}, model)
    return len(results)


def news_reactions() -> int:
    """Measure, or finish measuring, how each recent item's stock moved afterwards (news.reaction)."""
    now = data.now_ist()
    # Filings are fetched 30 days back, so look back a little further than that. At most 500 a pass:
    # a first pass over weeks of items would otherwise hold the app for minutes.
    rows = store.news_unreacted((now - timedelta(days=NEWS_REACT_DAYS)).isoformat(timespec='seconds'), limit=500)
    if not rows:
        return 0
    symbols = sorted({r['symbol'] for r in rows})
    frames = data.intraday_cached([s + '.NS' for s in symbols] + ['^NSEI'])
    # Daily closes for items older than the 15m candles reach: the app's own daily cache (a year,
    # any stock, one batched download for what it lacks), else the lab's history files.
    daily = data.frames_for([s + '.NS' for s in symbols] + ['^NSEI'])

    def closes(sym):
        f = daily.get(sym + '.NS')
        if f is None or f.empty:
            f = history.load('1d', sym)
        return f['Close'] if f is not None and 'Close' in f else None
    nd = daily.get('^NSEI')
    if nd is not None and not nd.empty:
        nifty_daily = nd['Close']
    else:
        market = market_frame()
        nifty_daily = market['nifty'] if market is not None and 'nifty' in market else None
    done = 0
    for r in rows:
        try:
            got = news.reaction(r['published'], frames.get(r['symbol'] + '.NS'), frames.get('^NSEI'),
                                closes(r['symbol']), nifty_daily, now)
        except Exception as e:
            logger.warning(f"News reaction {r['symbol']}: {e}")
            continue
        if got['basis'] or got['done']:
            store.set_reaction(r['id'], got)
            done += 1
    return done


def news_impact(days: int = 30) -> dict:
    """Does the tone move prices? For each FinBERT label: how the stocks did after their news
    (vs Nifty) one hour on, by the close and the next day; per kind of item too."""
    rows = store.news_graded((data.now_ist() - timedelta(days=days)).isoformat(timespec='seconds'))

    def stats(vals):
        v = [float(x) for x in vals if x is not None]
        if not v:
            return {'n': 0}
        return {'n': len(v), 'mean': round(sum(v) / len(v), 2), 'up_pct': round(100 * sum(x > 0 for x in v) / len(v))}
    out = {'days': days, 'items': len(rows), 'by_label': {}, 'by_kind': {}}
    for lab in ('positive', 'neutral', 'negative'):
        sub = [r for r in rows if r['sent_label'] == lab]
        out['by_label'][lab] = {h: stats([r[f'react_{h}'] for r in sub]) for h in ('1h', 'close', 'next')}
        for kind in ('filing', 'media'):
            k = [r for r in sub if r['kind'] == kind]
            out['by_kind'].setdefault(kind, {})[lab] = {h: stats([r[f'react_{h}'] for r in k]) for h in ('1h', 'close', 'next')}
    return out


def news_loop() -> None:
    """Every 15 minutes in the session for the board's stocks; every hour for all of them
    (setups too), which is also the only pass outside the session. Quiet 00:00-06:00."""
    time.sleep(90)                       # let a fresh start settle first
    last_full = 0.0
    while True:
        try:
            now = data.now_ist()
            if store.settings().get('news') == '1' and now.hour >= 6:
                full = time.time() - last_full >= NEWS_FULL_S
                if full or data.market_open():
                    news_pass(full=full)
                    if full:
                        last_full = time.time()
        except Exception:
            logger.exception('News pass failed')
        time.sleep(NEWS_EVERY_S)


# ---------------------------------------------------------------------------
# Smart money (niftywhale/smart.py)
# ---------------------------------------------------------------------------
SMART_STATE = {'running': False, 'last_pass': None, 'message': '', 'errors': []}
SMART_LOCK = threading.Lock()
SMART_BACKFILL = 25             # sessions of delivery and participant OI held (fetched on the first pass)
SMART_DEAL_DAYS = 30            # deals shown per stock


def last_session(now: datetime = None) -> date:
    """The latest session whose end-of-day files NSE may have out: today from 18:00, else the weekday before."""
    now = now or data.now_ist()
    d = now.date() if now.time() >= dtime(18, 0) else now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def smart_pass() -> dict:
    """Fetch what NSE has published: the latest bulk / block deals and FII/DII flows, and
    delivery and participant OI for each of the last SMART_BACKFILL sessions not held yet. A
    dated file that isn't there is a holiday (or not out yet: only files 3+ days old are
    written off, so today's is tried again on the next pass)."""
    if not SMART_LOCK.acquire(blocking=False):
        return {'skipped': 'a smart-money pass is already running'}
    SMART_STATE['running'] = True
    try:
        f, errors, got = smart.fetcher(), [], {'deals': 0, 'flows': 0, 'delivery': 0, 'poi': 0}
        try:
            got['deals'] = store.add_deals(f.deals())
        except (requests.RequestException, ValueError) as e:
            errors.append(f'deals: {str(e)[:120]}')
        try:
            got['flows'] = store.add_flows(f.flows())
        except (requests.RequestException, ValueError) as e:
            errors.append(f'FII/DII: {str(e)[:120]}')
        absent = set(store.get_json('smart:absent', []) or [])
        written_off = (data.now_ist().date() - timedelta(days=3)).isoformat()
        have = {'sm_delivery': set(store.smart_days('sm_delivery')), 'sm_poi': set(store.smart_days('sm_poi'))}
        for d in smart.weekdays_back(last_session(), SMART_BACKFILL):
            day = d.isoformat()
            for table, fetch, save in (('sm_delivery', f.bhav, lambda day, r: store.add_delivery(day, r)),
                                       ('sm_poi', f.participant_oi, lambda day, r: store.set_poi(day, r))):
                key = f'{table}:{day}'
                if day in have[table] or key in absent:
                    continue
                try:
                    rows = fetch(d)
                except (requests.RequestException, ValueError) as e:
                    errors.append(f'{table} {day}: {str(e)[:100]}')
                    continue
                if rows:
                    save(day, rows)
                    got['delivery' if table == 'sm_delivery' else 'poi'] += 1
                elif day < written_off:
                    absent.add(key)                  # a holiday
        store.set_json('smart:absent', sorted(absent)[-200:])
        store.prune_smart()
        SMART_STATE.update(last_pass=datetime.now().isoformat(timespec='seconds'), errors=errors[-10:],
                           message=f"{got['deals']} new deal(s) · FII/DII {'updated' if got['flows'] else 'unchanged'} · "
                                   f"{got['delivery']} delivery and {got['poi']} positioning session(s) fetched"
                                   + (f" · {len(errors)} error(s)" if errors else ''))
        if errors:
            logger.warning(f'Smart money: {len(errors)} error(s), e.g. {errors[0]}')
        return {**got, 'errors': len(errors)}
    finally:
        SMART_STATE['running'] = False
        SMART_LOCK.release()


def smart_loop() -> None:
    """Hourly from 17:30 to midnight on weekdays (NSE puts the day's files out between about
    17:30 and 20:30), once at 08:00 to catch anything late, and once shortly after a start."""
    time.sleep(120)
    first = True
    while True:
        try:
            now = data.now_ist()
            evening = now.weekday() < 5 and now.time() >= dtime(17, 30)
            if first or evening or now.hour == 8:
                smart_pass()
                first = False
        except Exception:
            logger.exception('Smart-money pass failed')
        time.sleep(3600)


def smart_stock(symbol: str, until: str = None) -> dict:
    """One stock: its delivery read for the latest session held (before `until`) and its deals."""
    hist = store.delivery_history([symbol], smart.DELIV_SESSIONS + 1, until=until).get(symbol, [])
    since = ((date.fromisoformat(until) if until else data.now_ist().date()) - timedelta(days=SMART_DEAL_DAYS)).isoformat()
    deals = store.deals([symbol], since=since, until=until, limit=100)
    return {'delivery': smart.delivery_read(hist), 'history': hist,
            'deals': smart.deal_summary(deals).get(symbol), 'deal_list': deals}


def smart_context(symbol: str, at) -> dict:
    """What NSE had published before an entry's day, for the trade's context: the stock's
    delivery read and its institutional deals of the week before, FII index-futures
    positioning and FII cash flow of the session before."""
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
    pos = smart.positioning(store.poi(2, until=day))
    fii = ((pos.get('latest') or {}).get('FII') or {})
    if fii.get('long_pct') is not None:
        out['fii_long_pct'] = fii['long_pct']
    fl = [r for r in store.flows(10) if r['day'] < day and r['category'] == 'FII']
    if fl:
        out['fii_cash_cr'] = fl[-1]['net']
    return out


# ---------------------------------------------------------------------------
# Demo funds (niftywhale/demo.py)
# ---------------------------------------------------------------------------
DEMO_LOCK = threading.Lock()
DEMO_MODES = ('swing', 'intraday', 'options')


def demo_settings() -> dict:
    try:
        return demo.settings_from(json.loads(store.settings().get('demo') or '{}'))
    except ValueError:
        return demo.settings_from({})


def _when(iso):
    """An ISO time as an aware IST datetime (stored times are IST, some without the offset)."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=data.IST)


def demo_account(at: datetime = None, positions: list = None, ledger: list = None) -> dict:
    """The account as of `at` (now by default): money in and out, the net P&L of positions closed
    by then, and the margin of positions open then. `balance` is what sizing works from."""
    positions = store.demo_positions() if positions is None else positions
    ledger = store.demo_ledger() if ledger is None else ledger
    now = at is None                     # the account as it stands, not as of a past moment
    at = at or data.now_ist()
    led = [x for x in ledger if now or (_when(x['ts']) or at) <= at]
    deposits = round(sum(x['amount'] for x in led if x['kind'] == 'deposit'), 2)
    withdrawals = round(sum(x['amount'] for x in led if x['kind'] == 'withdraw'), 2)
    taken = [p for p in positions if p['status'] in ('open', 'closed') and (now or (_when(p['entry_time']) or at) <= at)]
    closed = [p for p in taken if p['status'] == 'closed' and (now or (_when(p['exit_time']) or at) <= at)]
    open_ = [p for p in taken if p not in closed]
    realized = round(sum(p['net'] or 0 for p in closed), 2)
    balance = round(deposits - withdrawals + realized, 2)
    blocked = round(sum(p['margin'] or 0 for p in open_), 2)
    unreal = round(sum(p['unreal'] or 0 for p in open_ if p['status'] == 'open'), 2)
    return {'deposits': deposits, 'withdrawals': withdrawals, 'realized': realized,
            'charges': round(sum(p['charges'] or 0 for p in closed), 2), 'gross': round(sum(p['gross'] or 0 for p in closed), 2),
            'balance': balance, 'blocked': blocked, 'free': round(balance - blocked, 2), 'unreal': unreal,
            'equity': round(balance + unreal, 2), 'open': len(open_), 'closed': len(closed),
            'return_pct': round((balance + unreal - (deposits - withdrawals)) / (deposits - withdrawals) * 100, 2)
            if deposits - withdrawals > 0 else None}


# ---------------------------------------------------------------- live marks
# Open positions priced from the quote feed every second (charts.FEED, the Charts tab's poll, one
# request for every instrument), not only on the watchers' candle closes and chain passes: what the
# demo account and the open-trade journals show in between. Targets and stops are still judged on
# the candles and chains, so a live price never closes a trade or changes its result.
LIVE_FRESH_S = 15            # a feed price older than this is not live any more
LIVE_EVERY_S = 3             # how often marks_loop renews the feed's interest
_live_keys: dict = {}        # 'options:<idea id>' (also the demo position's ref) -> 'OPT:<segment>:<id>'


def _idea_key(i: dict) -> str:
    """The feed symbol of an option idea's contract, '' while its security ID is unknown."""
    ref = f"options:{i['id']}"
    if ref not in _live_keys:
        hit = dhan.option_id(i['symbol'], i['expiry'], i['strike'], i['side'])
        if not hit:
            return ''                    # option_id asks again later by itself
        _live_keys[ref] = f'OPT:{hit[1]}:{hit[0]}'
    return _live_keys[ref]


def _pos_key(p: dict) -> str:
    """A demo position's feed symbol. Shares and stock futures are both marked at the share price:
    the position was entered (and settles) at the trade's share price, so the futures basis
    must not show up as profit or loss."""
    return _live_keys.get(p['ref'], '') if p['mode'] == 'options' else p['symbol']


def live_px(key: str):
    """(price, epoch time) from the feed if it has a fresh one, else None."""
    hit = charts.FEED.last(key) if key else None
    return (hit[1], hit[0]) if hit and time.time() - hit[0] <= LIVE_FRESH_S else None


def _r_at(px: float, entry, stop, side) -> float:
    risk = abs(float(entry) - float(stop))
    return round((px - float(entry)) * (-1 if side == 'short' else 1) / risk, 2) if risk > 0 else 0.0


def live_demo(positions: list) -> list:
    """Open demo positions marked to the feed's price where it has a fresh one (in place)."""
    for p in positions:
        hit = p['status'] == 'open' and live_px(_pos_key(p))
        if hit:
            p.update(last=hit[0], unreal=demo.mark(p, hit[0]), live_at=hit[1])
    return positions


def live_zone(z: dict) -> dict:
    """An open swing / intraday paper trade's last price and R from the feed (in place)."""
    t = z.get('trigger')
    if z.get('status') == 'triggered' and isinstance(t, dict) and t.get('entry') and t.get('stop') is not None:
        hit = live_px(z['symbol'])
        if hit:
            t.update(last=hit[0], r_open=_r_at(hit[0], t['entry'], t['stop'], z.get('side')), live_at=hit[1])
    return z


def live_idea(i: dict) -> dict:
    """An open option idea's premium and R from the feed (in place), and its feed symbol (`lp`)."""
    if i.get('status') == 'open' and i.get('entry') and i.get('stop') is not None:
        i['lp'] = _live_keys.get(f"options:{i['id']}", '')
        hit = live_px(i['lp'])
        if hit:
            i.update(last=hit[0], r_open=_r_at(hit[0], i['entry'], i['stop'], 'long'), live_at=hit[1])
    return i


def underlying_key(sym: str) -> str:
    """The feed symbol of an option underlying: 'IDX:<id>' for an index, else the stock."""
    return f'IDX:{options.INDICES[sym][0]}' if options.is_index(sym) else sym


LIVE_MAX = 250               # feed symbols one /api/live call may ask for
LIVE_OPT_RE = re.compile(r'^OPT:(NSE|BSE)_FNO:\d{1,9}$')


def live_symbol_ok(s: str) -> bool:
    if s.startswith('IDX:'):
        return s[4:].isdigit()
    if s.startswith('OPT:'):
        return bool(LIVE_OPT_RE.match(s))
    return bool(SYMBOL_RE.match(s)) and dhan.security_id(s) is not None


# ---------------------------------------------------------------- one trade, in full
# What a row in any trade table opens: the trade itself rather than the stock's analysis. Its
# levels and result, why it triggered, the demo position and its charges, the alerts and notices
# it raised, the context and news at entry, and (separately, it costs Dhan calls) its candles.
TRADE_CANDLES: dict = {}             # ref -> (time, answer)
OPTION_WORD = {'target': 'Target hit', 'stop': 'Stopped out', 'squared off': 'Squared off'}


def _ref_parts(ref: str):
    mode, _, rid = (ref or '').partition(':')
    return (mode, int(rid)) if mode in DEMO_MODES and rid.isdigit() else (None, None)


def trade_detail(ref: str):
    mode, rid = _ref_parts(ref)
    if not mode:
        return None
    d = {'ref': ref, 'mode': mode}
    if mode == 'options':
        i = store.idea(rid)
        if not i:
            return None
        if i['status'] == 'open':
            _idea_key(i)
        live_idea(i)
        d.update(symbol=i['symbol'], name=options.INDICES[i['symbol']][2] if options.is_index(i['symbol']) else None,
                 side='long', direction=i['direction'], status=i['status'], entry=i['entry'], stop=i['stop'],
                 target=i['target'], exit=i['exit'], entry_time=i['created_at'], exit_time=i['exit_time'],
                 r=i['r'] if i['status'] != 'open' else None, r_open=i['r_open'], last=i['last'], why=i.get('reason'),
                 note=i.get('note'), lp=_live_keys.get(ref, ''), alerts=[],
                 word=OPTION_WORD.get(i.get('note') or '', RESULT_WORD.get(i['status'], i['status'])),
                 option={'strike': i['strike'], 'side': i['side'], 'expiry': i['expiry'], 'lot': i['lot'],
                         'spot': i['spot'], 'level': i['level'], 'underlying': underlying_key(i['symbol'])},
                 features=json.loads(i['features']) if i.get('features') else {})
    else:
        z = store.get_zone(rid)
        if not z or (z.get('mode') or 'swing') != mode:
            return None
        z = store._zone_rows([z])[0]
        t = z.get('trigger') or {}
        if not t.get('entry') or z['status'] not in (store.SWING_TRADES if mode == 'swing' else store.INTRADAY_TRADES):
            return None
        live_zone(z)
        status = 'open' if z['status'] == 'triggered' else z['status']
        d.update(symbol=z['symbol'], name=z.get('name'), side=z.get('side') or 'long', direction=z.get('side') or 'long',
                 status=status, entry=t['entry'], stop=t['stop'], target=t.get('target'), exit=t.get('exit'),
                 entry_time=t.get('choch_time'), exit_time=t.get('exit_time'),
                 r=t.get('r') if status != 'open' else None, r_open=t.get('r_open'), last=t.get('last'),
                 why=t.get('reason'), note=z.get('note'), lp=z['symbol'], word=RESULT_WORD.get(status, 'Open'),
                 setup={'zone_low': z['zone_low'], 'zone_high': z['zone_high'], 'zone_from': z.get('created_at'),
                        'tap_time': t.get('tap_time'), 'sweep_time': t.get('sweep_time'), 'swept_level': t.get('swept_level'),
                        'sweep_low': t.get('sweep_low'), 'choch_level': t.get('choch_level'),
                        'target_label': (z.get('meta') or {}).get('target_label'), 'approx': t.get('approx'),
                        'gap_stop': status == 'lost' and t.get('exit') is not None and abs(float(t['exit']) - float(t['stop'])) > 1e-9},
                 alerts=store.zone_alerts(rid),
                 features=json.loads(z['ctx']) if z.get('ctx') else {})
    if d['status'] == 'open':
        d['word'] = 'Open'
    risk = abs(float(d['entry']) - float(d['stop'])) if d.get('stop') is not None else None
    reward = abs(float(d['target']) - float(d['entry'])) if d.get('target') is not None else None
    d.update(risk_pts=risk, reward_pts=reward, rr=round(reward / risk, 2) if risk and reward else None)
    start, end = _when(d['entry_time']), _when(d.get('exit_time')) or data.now_ist()
    d['minutes'] = round((end - start).total_seconds() / 60) if start else None
    pos = store.demo_position(ref)
    if pos and pos['status'] == 'open':
        live_demo([pos])
    d['demo'] = pos
    d['notices'] = store.trade_notices(ref)
    d['news'] = [] if options.is_index(d['symbol']) or not start else [
        n for n in store.news([d['symbol']], since=(start - timedelta(days=1)).isoformat(timespec='seconds'), limit=40)
        if (_when(n['published']) or end) <= end + timedelta(hours=2)][:12]
    return d


def _candle_rows(f, daily: bool) -> list:
    out = []
    for ts, r in f.iterrows():
        out.append({'o': round(float(r.Open), 2), 'h': round(float(r.High), 2), 'l': round(float(r.Low), 2),
                    'c': round(float(r.Close), 2), 't': ts.isoformat(),
                    'd': ts.strftime('%Y-%m-%d') if daily else ts.strftime('%m-%d %H:%M'),
                    'day': None if daily else ts.strftime('%Y-%m-%d')})
    return out


def trade_candles(ref: str) -> dict:
    """The trade's window on its own instrument: the option contract's premium for an idea (its
    underlying if the contract is not found), the stock otherwise. Intraday and options: 5m from
    the entry's session; swing: 15m, or 60m / daily for a long trade. Bars carry `entry_k` / `exit_k`."""
    hit = TRADE_CANDLES.get(ref)
    if hit and time.time() - hit[0] < hit[1]['ttl']:
        return hit[1]
    d = trade_detail(ref)
    if not d:
        return {'error': 'no such trade'}
    if not dhan.available():
        return {'bars': [], 'note': 'The trade chart needs Dhan (real-time data).', 'ttl': 30}
    start, end = _when(d['entry_time']), _when(d.get('exit_time')) or data.now_ist()
    now = data.now_ist()
    span = (end.date() - start.date()).days
    if d['mode'] in ('intraday', 'options'):
        minutes = 5
    else:
        minutes = 15 if span <= 6 else 60 if span <= 30 else 1440
    back = (now.date() - start.date()).days + (4 if minutes < 1440 else 30)
    if minutes < 1440 and back > 89:
        minutes, back = 1440, back + 30
    what, label = None, d['symbol']
    if d['mode'] == 'options':
        o = d['option']
        hit_id = dhan.option_id(d['symbol'], o['expiry'], o['strike'], o['side'])
        if hit_id:
            what = (hit_id[0], hit_id[1], 'OPTIDX' if options.is_index(d['symbol']) else 'OPTSTK')
            label = f"{d['symbol']} {float(o['strike']):g} {o['side']} premium"
        else:
            u = o['underlying']
            what = (u[4:], 'IDX_I', 'INDEX') if u.startswith('IDX:') else (dhan.security_id(u), 'NSE_EQ', 'EQUITY')
            label = f"{d['symbol']} (the option's own candles are not available)"
    else:
        what = (dhan.security_id(d['symbol']), 'NSE_EQ', 'EQUITY')
    if not what or not what[0]:
        return {'bars': [], 'note': 'No Dhan instrument for this trade.', 'ttl': 300}
    try:
        f = dhan.chart_candles(*what, minutes, back)
    except Exception as e:
        return {'bars': [], 'note': f'Candles failed: {dhan._redact(e)[:120]}', 'ttl': 30}
    daily = minutes == 1440
    if len(f):
        # From the session before the entry (the entry's own session for 5m) to the exit's session end.
        first_day = start.date() if minutes == 5 else (start - timedelta(days=4 if minutes == 15 else 10)).date()
        f = f[[(ts.date() if hasattr(ts, 'date') else ts) >= first_day for ts in f.index]]
        if minutes == 15:
            days = sorted({ts.date() for ts in f.index})
            keep = [x for x in days if x < start.date()][-1:] + [x for x in days if x >= start.date()]
            f = f[[ts.date() in keep for ts in f.index]]
        if d['status'] != 'open':
            last_day = end.date()
            f = f[[(ts.date() if hasattr(ts, 'date') else ts) <= last_day for ts in f.index]]
    bars = _candle_rows(f, daily) if len(f) else []

    def k_of(when):
        if not when or not bars:
            return None
        at = _when(when)
        key = at.strftime('%Y-%m-%d') if daily else None
        for k, b in enumerate(bars):
            if daily and b['d'] == key:
                return k
            if not daily and _when(b['t']) <= at < _when(b['t']) + timedelta(minutes=minutes):
                return k
        return None
    if bars and d['status'] == 'open' and not daily:
        bars[-1]['live'] = _when(bars[-1]['t']) + timedelta(minutes=minutes) > now
    out = {'bars': bars, 'tf': minutes, 'label': label, 'entry_k': k_of(d['entry_time']), 'exit_k': k_of(d.get('exit_time')),
           'lp': d['lp'] if d['mode'] != 'options' or label.endswith('premium') else '',
           'ttl': 20 if d['status'] == 'open' else 3600}
    TRADE_CANDLES[ref] = (time.time(), out)
    if len(TRADE_CANDLES) > 200:
        for k in sorted(TRADE_CANDLES, key=lambda k: TRADE_CANDLES[k][0])[:50]:
            TRADE_CANDLES.pop(k, None)
    return out


@app.route('/api/trade/<path:ref>/candles')
def api_trade_candles(ref):
    out = trade_candles(ref)
    if out.get('error'):
        return jsonify(out), 404
    return jsonify(safe(out))


@app.route('/api/trade/<path:ref>')
def api_trade(ref):
    """One trade in full (a row in any trade table): ref is 'swing:<zone id>', 'intraday:<zone id>'
    or 'options:<idea id>'."""
    d = trade_detail(ref)
    if not d:
        return jsonify({'error': 'no such trade'}), 404
    return jsonify(safe(d))


# ---------------------------------------------------------------- App settings (config.py)
@app.route('/api/config')
def api_config():
    """The settings that used to be .env only: each value and where it comes from ('app', 'env',
    'default'). A secret is only ever reported as set or not; the Telegram token as its bot's name."""
    v = config.view()
    v['telegram_token']['bot'] = notify.bot_name() if v['telegram_token']['set'] else None
    return jsonify(safe({'fields': v, 'dhan': dhan.status(), 'telegram_configured': notify.configured()}))


@app.route('/api/config', methods=['POST'])
def api_config_save():
    """Save any of config.FIELDS; '' clears one back to .env or its default. New Dhan login
    details (or a data source change) take effect at once: the login backoff is cleared and,
    in automatic mode, a login starts."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body:
        return jsonify({'error': 'send the settings as a JSON object'}), 400
    errors = {k: e for k in body for e in [config.validate(k, body[k])] if e}
    if errors:
        return jsonify({'error': '; '.join(errors.values()), 'fields': errors}), 400
    config.save(body)
    logger.info('App settings changed: ' + ', '.join(sorted(body)))        # names only, never values
    if any(k.startswith('dhan_') for k in body) or 'data_source' in body:
        dhan._state.update(next_login=0.0, rejected=False, profile=None, profile_at=0.0, error=None)
        if dhan.mode() == 'auto':
            threading.Thread(target=dhan.keep_alive, daemon=True, name='dhan-login').start()
    return api_config()


@app.route('/api/notices')
def api_notices():
    """The bell: notices changed since `after` (a seq; 0 = the latest 100), with the unread count."""
    try:
        after = max(0, int(request.args.get('after') or 0))
    except ValueError:
        after = 0
    return jsonify(safe({'items': store.notices(after), **store.notice_counts()}))


@app.route('/api/notices/read', methods=['POST'])
def api_notices_read():
    """Mark notices read: {"ids": [...]}, or every one with no ids."""
    ids = (request.get_json(silent=True) or {}).get('ids')
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
        return jsonify({'error': 'ids must be a list of numbers'}), 400
    store.read_notices(ids or None)
    return jsonify(store.notice_counts())


@app.route('/api/notices/clear', methods=['POST'])
def api_notices_clear():
    """Clear every notification from the bell (on every device). They stay cleared: the same event is not
    raised again."""
    n = store.clear_notices()
    return jsonify({'cleared': n, **store.notice_counts()})


def live_symbols(raw) -> list:
    """The feed symbols a page asked for: valid ones, at most LIVE_MAX."""
    syms = []
    for s in raw:
        s = str(s).strip().upper()
        if s and s not in syms and len(syms) < LIVE_MAX and live_symbol_ok(s):
            syms.append(s)
    return syms


def live_on() -> bool:
    return data.market_open() and dhan.available()


@app.route('/api/live')
def api_live():
    """The latest price of each feed symbol asked for (a stock, 'IDX:<id>', 'OPT:<segment>:<id>'),
    for every live number on the page. The page's WebSocket (/ws, channel px) pushes the same
    prices as they trade; this is what a page polls each second when it has no socket. Asking puts
    a symbol on the feed for WATCH_S seconds; prices are answered only while fresh (LIVE_FRESH_S)."""
    on = live_on()
    syms = live_symbols((request.args.get('s') or '').split(','))
    if on and syms:
        charts.FEED.mark(syms)
    px_ = {}
    for s in syms:
        hit = live_px(s)
        if hit:
            px_[s] = [hit[0], round(hit[1], 3)]
    return jsonify({'now': time.time(), 'on': on, 'every_ms': 1000, 'px': px_, 'error': charts.FEED.error if on else None,
                    'notices': store.notice_counts()})


def _open_live_keys() -> list:
    keys = {_idea_key(i) for i in store.ideas(status='open')}
    keys |= {_pos_key(p) for p in store.demo_positions(status='open')}
    keys |= {z['symbol'] for z in store.swing_trades(statuses=('triggered',))}
    keys |= {z['symbol'] for z in store.intraday_zones(('triggered',))}
    keys.discard('')
    return sorted(keys)


def marks_loop() -> None:
    """Keep every open position's instrument on the feed while the market is open, and raise the
    live notices (near a stop or target, a square-off coming, the feed out)."""
    time.sleep(15)
    pruned = 0.0
    while True:
        try:
            if data.market_open():
                ok = dhan.available()
                if ok:
                    charts.FEED.mark(_open_live_keys())
                    live_warnings(_open_trades(), data.now_ist())
                    charts.SOURCE.prune()
                feed_watch(ok, data.now_ist())
            elif time.time() - pruned > 86400:
                store.prune_notices()
                pruned = time.time()
        except Exception:
            logger.exception('Live marks failed')
        time.sleep(LIVE_EVERY_S if data.market_open() else 60)


# ---------------------------------------------------------------- in-app notices (the bell)
# What deserves a look, written where it happens: an entry, its result (target, stop, square-off),
# the demo account taking, skipping or settling it, a live price near a stop or a target, a
# square-off coming with trades still open, the live feed dropping, a filing on an open trade,
# the evening scan, an autopilot change (store.log_autopilot). Telegram is separate and unchanged.
NOTICE_FRESH_S = 1800        # an event older than this (a demo replay, a long catch-up) raises none
NEAR_STOP_R = -0.8           # live R at or below this: near the stop
NEAR_TARGET = 0.9            # this share of the way to the target: near the target
SQUARE_OFF_WARN_MIN = 10     # minutes before a square-off with trades still open
FEED_DOWN_S = 60             # the feed out this long in session before it is a notice
RESULT_WORD = {'won': 'Target hit', 'lost': 'Stopped out', 'closed': 'Squared off'}
DEMO_WAKE = threading.Event()    # set by a new entry or result: the demo account follows at once
_feed = {'down_since': None, 'said': None}


def _fresh(when) -> bool:
    at = _when(when)
    return at is not None and (data.now_ist() - at).total_seconds() <= NOTICE_FRESH_S


def _f(v) -> str:
    return '{:,.2f}'.format(float(v)) if v is not None else '—'


def _trade_link(mode: str, symbol: str, ref: str = None) -> dict:
    out = {'intra': symbol} if mode == 'intraday' else {'chain': symbol} if mode == 'options' else {'stock': symbol}
    return {**out, 'ref': ref} if ref else out


def _trade_label(mode: str, symbol: str, t: dict) -> str:
    """'Swing long · PAYTM', 'Intraday short · SBIN', 'Option · NIFTY 22450 CE'."""
    if mode == 'options':
        return f"Option · {symbol} {float(t.get('strike') or 0):g} {t.get('opt_side') or t.get('side') or ''}".strip()
    return f"{DEMO_MODE_NAMES[mode]} {'short' if t.get('side') == 'short' else 'long'} · {symbol}"


def notice_entry(mode: str, ref: str, symbol: str, t: dict, at) -> None:
    if not _fresh(at):
        return
    body = f"Entry {_f(t.get('entry'))} · stop {_f(t.get('stop'))} · target {_f(t.get('target'))}"
    risk = abs(float(t['entry']) - float(t['stop'])) if t.get('entry') is not None and t.get('stop') is not None else 0
    if risk and t.get('target') is not None:
        body += f" · R:R 1:{abs(float(t['target']) - float(t['entry'])) / risk:.1f}"
    store.add_notice(f'open:{ref}', 'entry', 'info', f'{_trade_label(mode, symbol, t)} · new entry', body,
                     symbol, mode, _trade_link(mode, symbol, ref))
    DEMO_WAKE.set()


def notice_result(mode: str, ref: str, symbol: str, t: dict, status: str, exit_, r, at, word: str = None) -> None:
    if not _fresh(at):
        return
    r = float(r or 0)
    level = 'good' if status == 'won' or (status == 'closed' and r > 0) else \
        'bad' if status == 'lost' or r < 0 else 'info'
    store.add_notice(f'close:{ref}', 'result', level,
                     f"{_trade_label(mode, symbol, t)} · {word or RESULT_WORD.get(status, status)}",
                     f"{_f(t.get('entry'))} → {_f(exit_)} · {r:+.2f}R", symbol, mode, _trade_link(mode, symbol, ref))
    DEMO_WAKE.set()


def _demo_qty(p: dict) -> str:
    what = f"{p['lots']} lot{'s' if p['lots'] > 1 else ''} × {p['lot_size']}" if p.get('lots') else f"{p['qty']:,} shares"
    return f"{what} {demo.PRODUCTS.get(p.get('product'), p.get('product') or '')}".strip()


def _demo_notice(key: str, kind: str, level: str, title: str, base: str, extra: str, p: dict) -> None:
    """Add the demo account's part to the trade's notice (or raise one if the trade's is missing)."""
    n = store.notice(key)
    if n and ' · Demo: ' in (n['body'] or ''):
        return
    body = (n['body'] if n else base) + ' · Demo: ' + extra
    store.add_notice(key, n['kind'] if n else kind, n['level'] if n else level, n['title'] if n else title, body,
                     p['symbol'], p['mode'], _trade_link(p['mode'], p['symbol'], p['ref']), update=True)


def notice_demo_open(p: dict) -> None:
    if not _fresh(p.get('entry_time')):
        return
    label = _trade_label(p['mode'], p['symbol'], {'side': p.get('direction'), 'strike': None}) \
        if p['mode'] != 'options' else f"Option · {p['instrument']}"
    _demo_notice(f"open:{p['ref']}", 'entry', 'info', f'{label} · new entry',
                 f"Entry {_f(p['entry'])} · stop {_f(p.get('stop'))} · target {_f(p.get('target'))}",
                 f"{_demo_qty(p)}, ₹{p['margin']:,.0f} blocked, risks ₹{p['risk']:,.0f}", p)


def notice_demo_skip(p: dict) -> None:
    if _fresh(p.get('entry_time')):
        store.add_notice(f"skip:{p['ref']}", 'skip', 'warn', f"Demo account skipped {p['instrument']}",
                         p.get('note') or '', p['symbol'], p['mode'], {'tab': 'demo', 'ref': p['ref']})


def notice_demo_close(p: dict) -> None:
    if not _fresh(p.get('exit_time')):
        return
    net = p.get('net') or 0
    _demo_notice(f"close:{p['ref']}", 'result', 'good' if net > 0 else 'bad' if net < 0 else 'info',
                 f"{p['instrument']} · {p.get('note') or 'closed'}", f"{_f(p['entry'])} → {_f(p.get('exit'))}",
                 f"net {'+' if net >= 0 else '−'}₹{abs(net):,.0f} after ₹{p.get('charges') or 0:,.0f} charges", p)


def _open_trades() -> list:
    """Every open paper trade with its feed symbol, for the live warnings."""
    out = []
    for mode, rows in (('swing', store.swing_trades(statuses=('triggered',))),
                       ('intraday', store.intraday_zones(('triggered',)))):
        for z in rows:
            t = z.get('trigger') or {}
            if t.get('entry') is not None and t.get('stop') is not None:
                out.append({'ref': f"{mode}:{z['id']}", 'mode': mode, 'symbol': z['symbol'], 'side': z.get('side') or 'long',
                            'entry': t['entry'], 'stop': t['stop'], 'target': t.get('target'), 'key': z['symbol'], 't': t})
    for i in store.ideas(status='open'):
        if i.get('entry') and i.get('stop') is not None:
            out.append({'ref': f"options:{i['id']}", 'mode': 'options', 'symbol': i['symbol'], 'side': 'long',
                        'entry': i['entry'], 'stop': i['stop'], 'target': i.get('target'), 'key': _idea_key(i),
                        't': {'strike': i['strike'], 'opt_side': i['side']}})
    return out


def live_warnings(trades: list, now: datetime) -> None:
    """Near a stop or a target on the live price (the candles still decide), and a square-off coming."""
    for tr in trades:
        hit = live_px(tr['key'])
        if not hit:
            continue
        p, entry, stop, target = hit[0], float(tr['entry']), float(tr['stop']), tr['target']
        sign, risk = (-1 if tr['side'] == 'short' else 1), abs(float(tr['entry']) - float(tr['stop']))
        if not risk:
            continue
        r = (p - entry) * sign / risk
        label = _trade_label(tr['mode'], tr['symbol'], {**tr['t'], 'side': tr['side'] if tr['mode'] != 'options' else tr['t'].get('opt_side')})
        decides = {'swing': 'the 15m candle', 'intraday': 'the 5m candle', 'options': 'the next chain read'}[tr['mode']]
        if r <= NEAR_STOP_R:
            store.add_notice(f"nearstop:{tr['ref']}", 'risk', 'warn', f'{label} · near its stop',
                             f"{_f(p)} now · stop {_f(stop)} · {r:+.2f}R. Live price; {decides} decides.",
                             tr['symbol'], tr['mode'], _trade_link(tr['mode'], tr['symbol'], tr['ref']))
        reward = abs(float(target) - entry) if target is not None else 0
        if reward and (p - entry) * sign / reward >= NEAR_TARGET:
            store.add_notice(f"neartarget:{tr['ref']}", 'risk', 'good', f'{label} · near its target',
                             f"{_f(p)} now · target {_f(target)} · {r:+.2f}R. Live price; {decides} decides.",
                             tr['symbol'], tr['mode'], _trade_link(tr['mode'], tr['symbol'], tr['ref']))
    day = now.date().isoformat()
    for mode, at in (('intraday', irules().clock('square_off')),
                     ('options', datetime.strptime(orules()['square_off'], '%H:%M').time())):
        n = sum(1 for tr in trades if tr['mode'] == mode)
        until = (datetime.combine(now.date(), at, now.tzinfo) - now).total_seconds() / 60
        if n and 0 < until <= SQUARE_OFF_WARN_MIN:
            word = 'intraday trade' if mode == 'intraday' else 'option idea'
            store.add_notice(f'squareoff:{mode}:{day}', 'squareoff', 'warn',
                             f"{n} {word}{'s' if n > 1 else ''} square off at {at.strftime('%H:%M')}",
                             ', '.join(sorted({tr['symbol'] for tr in trades if tr['mode'] == mode})) +
                             '. Paper trades close by themselves; square off any real position you hold.',
                             None, mode, {'tab': mode})


def feed_watch(ok: bool, now: datetime) -> None:
    """In session: a notice when the live feed has been out FEED_DOWN_S, and one when it is back."""
    if ok:
        if _feed['said']:
            store.add_notice(f"feed-up:{_feed['said']}", 'system', 'good', 'Live prices are back',
                             'Dhan is answering again.', link={'tab': 'dhan'})
        _feed.update(down_since=None, said=None)
        return
    _feed['down_since'] = _feed['down_since'] or time.time()
    if not _feed['said'] and time.time() - _feed['down_since'] >= FEED_DOWN_S:
        _feed['said'] = now.isoformat(timespec='minutes')
        store.add_notice(f"feed-down:{_feed['said']}", 'system', 'warn', 'Live prices stopped',
                         f"Dhan: {dhan.status().get('message')}. The watchers fall back to delayed yfinance candles.",
                         link={'tab': 'dhan'})


def _instrument(t: dict, product: str = None) -> str:
    if t['mode'] == 'options':
        exp = t.get('expiry') or ''
        try:
            exp = datetime.fromisoformat(exp).strftime('%d %b')
        except ValueError:
            pass
        return f"{t['symbol']} {float(t.get('strike') or 0):g} {t.get('opt_side') or ''} {exp}".strip()
    return f"{t['symbol']} FUT" if product == 'FUT' else t['symbol']


def demo_sync() -> dict:
    """Mirror the app's trades into the demo account: every trade from `demo_since` on gets a
    position sized from the account as it stood at its entry (demo.size), in entry order; a
    trade that has closed settles its position (demo.settle); an open one is marked to its
    latest price. Idempotent: each trade is taken once (demo_positions.ref)."""
    since = _when(store.settings().get('demo_since'))
    if not since:
        return {'skipped': 'no demo account yet'}
    if not DEMO_LOCK.acquire(blocking=False):
        return {'skipped': 'already syncing'}
    try:
        st = demo_settings()
        positions, ledger = store.demo_positions(), store.demo_ledger()
        have = {p['ref'] for p in positions}
        trades = [t for m in DEMO_MODES for t in store.live_trades(m) if t.get('ref') and t.get('entry')]
        by_ref = {t['ref']: t for t in trades}
        new = sorted((t for t in trades if t['ref'] not in have and st.get(t['mode'])
                      and (_when(t['entry_time']) or since) >= since), key=lambda t: _when(t['entry_time']))
        got = {'opened': 0, 'skipped': 0, 'closed': 0, 'marked': 0}

        def settle(p, t):
            res = demo.settle(p, float(t['exit']), st['charges'], t.get('exit_time') or t['entry_time'])
            fields = dict(status='closed', exit=float(t['exit']), exit_time=t.get('exit_time') or t['entry_time'],
                          gross=res['gross'], charges=res['charges'], net=res['net'], charges_detail=res['charges_detail'],
                          last=float(t['exit']), unreal=None, note=DEMO_OUTCOME.get(t['status'], ''))
            store.update_demo_position(p['id'], **fields)
            p.update(fields)
            got['closed'] += 1
            notice_demo_close(p)

        for t in new:
            at = _when(t['entry_time'])
            acct = demo_account(at, positions, ledger)
            pos_side = 'long' if t['mode'] == 'options' else (t.get('side') or 'long')
            lot = (t.get('lot') or dhan.lot_size(t['symbol'])) if t['mode'] == 'options' else \
                dhan.lot_size(t['symbol']) if t['mode'] == 'swing' and t.get('side') == 'short' else None
            # The hard stops first: positions open at once, and trades entered that day.
            day = at.date()
            today = sum(1 for p in positions if p['status'] in ('open', 'closed') and (_when(p['entry_time']) or at).date() == day)
            if st['max_open'] and acct['open'] >= st['max_open']:
                sz = {'skip': f"{acct['open']} positions already open: the most at once is {st['max_open']}"}
            elif st['max_per_day'] and today >= st['max_per_day']:
                sz = {'skip': f"{today} trades already taken that day: the day's limit is {st['max_per_day']}"}
            else:
                sz = demo.size(t, acct['balance'], acct['free'], st, lot)
            base = {'ref': t['ref'], 'mode': t['mode'], 'symbol': t['symbol'], 'side': pos_side, 'direction': t.get('side'),
                    'entry': float(t['entry']), 'entry_time': t['entry_time'], 'stop': t.get('stop'), 'target': t.get('target')}
            if 'skip' in sz:
                p = {**base, 'instrument': _instrument(t), 'status': 'skipped', 'qty': 0, 'note': sz['skip']}
                p['id'] = store.add_demo_position(p)
                positions.append(p)
                got['skipped'] += 1
                notice_demo_skip(p)
                continue
            p = {**base, **{k: sz[k] for k in ('product', 'qty', 'lots', 'lot_size', 'margin', 'risk')},
                 'instrument': _instrument(t, sz['product']), 'status': 'open', 'last': float(t['entry']), 'unreal': 0.0,
                 'gross': None, 'net': None, 'charges': None}
            p['id'] = store.add_demo_position(p)
            positions.append(p)
            got['opened'] += 1
            notice_demo_open(p)
            if t['status'] != 'open' and t.get('exit') is not None:
                settle(p, t)                     # opened and closed between two syncs: in order, at once
        for p in positions:
            if p['status'] != 'open':
                continue
            t = by_ref.get(p['ref'])
            if not t:
                continue
            if t['status'] != 'open' and t.get('exit') is not None:
                settle(p, t)
                continue
            hit = live_px(_pos_key(p))
            last = hit[0] if hit else t.get('last')
            if last is None and t.get('r_open') is not None and t.get('stop') is not None:
                sign = -1 if p['side'] == 'short' else 1
                last = float(p['entry']) + float(t['r_open']) * abs(float(p['entry']) - float(t['stop'])) * sign
            if last is not None:
                store.update_demo_position(p['id'], last=float(last), unreal=demo.mark(p, last))
                got['marked'] += 1
        return got
    finally:
        DEMO_LOCK.release()


DEMO_OUTCOME = {'won': 'target hit', 'lost': 'stop hit', 'closed': 'squared off'}
DEMO_MODE_NAMES = {'swing': 'Swing', 'intraday': 'Intraday', 'options': 'Options'}


def demo_first_trade() -> str:
    """When the app's first trade (any mode) was entered: where a backdated account starts."""
    times = [_when(t['entry_time']) for m in DEMO_MODES for t in store.live_trades(m) if t.get('ref') and t.get('entry')]
    times = [x for x in times if x]
    return min(times).isoformat(timespec='seconds') if times else None


def _demo_what(p: dict) -> str:
    """A closed position in words: what was bought or sold, how much, at what, and how it ended."""
    if p['product'] == 'OPT':
        did = f"bought {p['lots']} lot{'s' if p['lots'] != 1 else ''} × {p['lot_size']} ({'bearish' if p.get('direction') == 'short' else 'bullish'})"
    elif p['product'] == 'FUT':
        did = f"{'sold' if p['side'] == 'short' else 'bought'} {p['lots']} lot{'s' if p['lots'] != 1 else ''} × {p['lot_size']}"
    else:
        did = f"{'sold short' if p['side'] == 'short' else 'bought'} {p['qty']:,} share{'s' if p['qty'] != 1 else ''}"
    out = f"{did} @ {p['entry']:,.2f} → {p['exit']:,.2f}"
    return f"{out} · {p['note']}" if p.get('note') else out


def demo_statement(positions: list = None, ledger: list = None) -> dict:
    """The account's statement, oldest first: the opening balance, money added and withdrawn,
    and for each closed trade its P&L and its charges as separate lines (as a broker books
    them), each with the balance after it; plus the summary that ties them together."""
    positions = store.demo_positions() if positions is None else positions
    ledger = store.demo_ledger() if ledger is None else ledger
    lines = []
    for i, x in enumerate(sorted(ledger, key=lambda x: (_when(x['ts']) or data.now_ist(), x['id']))):
        kind = 'opening' if i == 0 and x['kind'] == 'deposit' else x['kind']
        lines.append({'ts': x['ts'], 'kind': kind, 'amount': x['amount'] if x['kind'] == 'deposit' else -x['amount'],
                      'title': {'opening': 'Opening balance', 'deposit': 'Funds added', 'withdraw': 'Funds withdrawn'}[kind],
                      'detail': x.get('note') or ''})
    for p in positions:
        if p['status'] != 'closed':
            continue
        mode = DEMO_MODE_NAMES.get(p['mode'], p['mode'])
        lines.append({'ts': p['exit_time'], 'kind': 'pnl', 'amount': p['gross'] or 0, 'symbol': p['symbol'], 'ref': p['ref'],
                      'title': f"{mode} · {p['instrument']}", 'detail': _demo_what(p)})
        if p['charges']:
            lines.append({'ts': p['exit_time'], 'kind': 'charges', 'amount': -p['charges'], 'symbol': p['symbol'], 'ref': p['ref'],
                          'title': f"Charges · {p['instrument']}", 'detail': f"{demo.PRODUCTS.get(p['product'], p['product'])} round trip",
                          'charges': p['charges_detail']})
    order = {'opening': 0, 'deposit': 1, 'pnl': 2, 'charges': 3, 'withdraw': 4}
    lines.sort(key=lambda x: (_when(x['ts']) or data.now_ist(), x.get('ref') or '', order[x['kind']]))
    run = 0.0
    for x in lines:
        run += x['amount']
        x['balance'] = round(run, 2)
    total = lambda *kinds: round(sum(x['amount'] for x in lines if x['kind'] in kinds), 2)  # noqa: E731
    summary = {'from': lines[0]['ts'] if lines else None, 'to': lines[-1]['ts'] if lines else None,
               'opening': total('opening'), 'added': total('deposit'), 'withdrawn': -total('withdraw'),
               'pnl': total('pnl'), 'charges': -total('charges'), 'closing': round(run, 2),
               'trades': sum(1 for x in lines if x['kind'] == 'pnl')}
    return {'lines': lines, 'summary': summary}


def demo_loop() -> None:
    """Every minute during the session, every ten minutes otherwise."""
    time.sleep(60)
    while True:
        try:
            demo_sync()
        except Exception:
            logger.exception('Demo sync failed')
        DEMO_WAKE.wait(60 if data.market_open() else 600)
        DEMO_WAKE.clear()
        time.sleep(1)                    # let the watcher finish writing the rest of its pass


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


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def _next_quarter(now: datetime) -> datetime:
    """The next 15m candle close, plus the delay for it to reach the data source."""
    base = now.replace(second=0, microsecond=0)
    minutes = 15 - base.minute % 15
    delay = candle_delay()
    return base + timedelta(minutes=minutes, seconds=delay)


SCAN_RETRY_S = 15 * 60          # a failed evening scan is tried again after this long ...
SCAN_ATTEMPTS = 4               # ... up to this many times a day


def scheduled_scan_due(now: datetime, st: dict) -> bool:
    """Is it time for the evening scan? Once a weekday after `scan_time`. A
    scheduled scan that failed (the network was down, the process died
    mid-scan) does not count: it is retried every SCAN_RETRY_S, so one bad
    minute at 16:15 does not leave tomorrow without zones."""
    if st['auto_scan'] != '1' or now.weekday() >= 5 or STATE['scan']['running']:
        return False
    hh, mm = (int(x) for x in st['scan_time'].split(':'))
    if now.hour * 60 + now.minute < hh * 60 + mm:
        return False
    local = now.astimezone().replace(tzinfo=None) if now.tzinfo else now    # scans are stamped in local time
    runs = store.scheduled_scans(local.date().isoformat())
    if any(r['status'] in ('done', 'stopped') for r in runs) or len(runs) >= SCAN_ATTEMPTS:
        return False
    if runs:
        try:
            last = datetime.fromisoformat(runs[-1]['started_at'])
        except (TypeError, ValueError):
            last = datetime.min
        if local - last < timedelta(seconds=SCAN_RETRY_S):
            return False
        logger.warning(f'Scheduled scan failed earlier ({runs[-1]["status"]}); trying again')
    return True


def scheduler_loop() -> None:
    logger.info('Scheduler started (IST)')
    while True:
        try:
            dhan.keep_alive()            # renew / regenerate the Dhan token before it lapses
        except Exception:
            logger.exception('Dhan token upkeep failed')
        try:
            now = data.now_ist()
            st = store.settings()

            # Evening screen, once per weekday (retried if it failed).
            if scheduled_scan_due(now, st):
                logger.info('Scheduled scan starting')
                start_scan_thread('schedule')

            # Zone watcher: on every 15m close during the session, and once
            # just after the close so the 15:15 candle is not missed.
            w = STATE['watch']
            nxt = w.get('_next')
            in_window = data.market_open(now) or data.market_open(now - timedelta(minutes=20))
            if st['watcher'] == '1' and in_window:
                if nxt is None:
                    nxt = _next_quarter(now)
                if now >= nxt:
                    check_zones('schedule')
                    nxt = _next_quarter(data.now_ist())
                w['_next'] = nxt
                w['next_run'] = nxt.isoformat(timespec='seconds')
            else:
                w['_next'] = None
                w['next_run'] = None
        except Exception:
            logger.exception('Scheduler tick failed')
        time.sleep(20)


# ---------------------------------------------------------------------------
# Intraday (niftywhale/intraday.py)
# ---------------------------------------------------------------------------
def _at(now: datetime, hhmm) -> datetime:
    t = hhmm if not isinstance(hhmm, str) else datetime.strptime(hhmm, '%H:%M').time()
    return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def intraday_phase(now: datetime = None) -> dict:
    """Where the session is, for the scheduler and the dashboard."""
    now = now or data.now_ist()
    r = irules()
    if now.weekday() >= 5:
        return {'key': 'closed', 'label': 'Weekend'}
    if now < _at(now, '09:15'):
        return {'key': 'pre', 'label': f'Pre-open · first scan at {r.start}'}
    if now < _at(now, r.start):
        return {'key': 'opening', 'label': f'Opening range forming · first scan at {r.start}'}
    if now < _at(now, r.no_entry_after):
        return {'key': 'live', 'label': f'Scanning · entries until {r.no_entry_after}'}
    if now < _at(now, r.square_off):
        return {'key': 'manage', 'label': f'No new entries · square off at {r.square_off}'}
    if now < _at(now, '15:30'):
        return {'key': 'squared', 'label': 'Squared off'}
    return {'key': 'closed', 'label': 'Session over'}


LAST_INTRA: dict = {}          # the last intraday scan's candles, for the tuner's what-if


def intraday_screen(stocks, dailies, frames, rules, now, shorts=True):
    """
    Steps 2-8 for every stock: daily filters, then the 15m screen (long, or
    short on a bearish structure when shorts are on). Returns (funnel,
    passing rows, one verdict per stock). Shared by the scan and the
    intraday tuner's what-if.
    """
    order = [k for k, _ in intraday.STEPS]
    reached = {k: 0 for k in order}
    reached.update(universe=len(stocks), data=0)
    results, passed = [], []
    for m in stocks:
        p = intraday.daily_filter(dailies.get(m['ticker']), rules)
        if p['failed_at'] == 'history':
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': 'data',
                            'reason': p['reason']})
            continue
        # Daily filters first: a stock that fails them is never sent for 15m
        # candles, and must count at its own step, not as "no candles".
        if p['failed_at']:
            reached['data'] += 1
            reached['history'] += 1
            if p['failed_at'] == 'volatility':
                reached['liquidity'] += 1
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': p['failed_at'],
                            'reason': p['reason']})
            continue
        frame = frames.get(m['ticker'])
        if frame is None or frame.empty:
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': 'data',
                            'reason': 'no 15m candles'})
            continue
        try:
            r = intraday.evaluate(frame, rules, now, shorts=shorts)
        except Exception as e:
            logger.warning(f"Intraday {m['symbol']}: {e}")
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': 'data',
                            'reason': f'analysis error: {e}'})
            continue
        if r['failed_at'] == 'history':                # too few 15m candles: a data problem
            results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': 'data',
                            'reason': r['reason'] + ' (15m)'})
            continue
        reached['data'] += 1
        # smc.evaluate reports its liquidity/volatility on 15m bars; the
        # daily ones are what this mode filters on, so show those.
        r['avg_volume'], r['atr_pct_15m'], r['atr_pct'] = p['avg_volume'], r.get('atr_pct'), p['atr_pct']
        last = order.index(r['failed_at']) if r['failed_at'] else len(order)
        for k in order[:last]:
            reached[k] += 1
        results.append({'symbol': m['symbol'], 'name': m['name'], 'failed_at': r['failed_at'],
                        'reason': r['reason'], 'passed': r['passed']})
        if r['passed']:
            passed.append({'symbol': m['symbol'], 'name': m['name'], 'analysis': r})
    passed.sort(key=lambda x: (not x['analysis']['in_zone'], -x['analysis']['score']))
    return reached, passed, results


def run_intraday_scan(origin: str = 'schedule') -> dict:
    """
    Steps 1-8 on 15m candles for the intraday universe. During the session
    (before the entry cutoff) the passing stocks become today's zones; at any
    other time the scan is a preview of the last session and sets no zones.
    """
    global LAST_INTRA
    if not INTRA_SCAN_LOCK.acquire(blocking=False):
        return {'skipped': 'an intraday scan is already running'}
    st = STATE['intraday']
    st.update(scanning=True, done=0, total=0, message='')
    scan_id, status, error, funnel, passed, scanned = None, 'done', None, {}, [], 0
    try:
        which = store.settings()['intraday_universe']
        scan_id = store.start_scan(which, origin, mode='intraday')
        now = data.now_ist()
        rules = irules()
        shorts = store.settings()['intraday_shorts'] == '1'
        stocks = universe.members(which)
        tickers = [m['ticker'] for m in stocks]
        st['total'] = len(tickers)
        dailies = data.frames_for(tickers)                 # liquidity + range: cached daily candles
        # Only the stocks that pass the daily filters need 15m candles.
        need = [t for t in tickers if not intraday.daily_filter(dailies.get(t), rules)['failed_at']]
        frames = {}
        for i in range(0, len(need), 10):                  # in chunks, so progress moves
            frames.update(data.intraday_cached(need[i:i + 10], 15))
            st['done'] = min(len(tickers), len(tickers) - len(need) + i + 10)
        st['done'] = len(tickers)
        # Swapped whole, never cleared and refilled: a what-if reading it from
        # another thread must not catch it half-built.
        LAST_INTRA = {'universe': which, 'stocks': stocks, 'dailies': dailies, 'frames': frames, 'now': now}
        funnel, passed, results = intraday_screen(stocks, dailies, frames, rules, now, shorts)
        scanned = funnel['data']
        store.save_results(scan_id, results)
        store.save_candidates(scan_id, passed)

        session = now.date().isoformat()
        # Zones only from today's candles, and only while entries are still allowed
        # -- judged when the scan finishes, not when it started.
        done_at = data.now_ist()
        live = intraday_phase(done_at)['key'] == 'live' and data.market_open(done_at)
        n_short = sum(x['analysis'].get('side') == 'short' for x in passed)
        found = f"{len(passed)} setup(s)" + (f" ({n_short} short)" if n_short else '') + f" from {scanned}"
        if live:
            fresh = [x for x in passed if x['analysis'].get('levels', {}).get('session') == session]
            z = store.sync_intraday_zones(scan_id, fresh, session)
            st['message'] = f"{found} · zones +{z['added']} ~{z['refreshed']} -{z['expired']}"
        else:
            st['message'] = f'{found} · preview, no zones set outside the session'
        logger.info(f"Intraday scan #{scan_id} ({origin}, {which}): {st['message']}; funnel {funnel}")
        return {'scan_id': scan_id, 'setups': len(passed), 'scanned': scanned, 'live': live}
    except Exception as e:
        logger.exception('Intraday scan failed')
        status, error = 'error', str(e)
        st['message'] = f'scan failed: {e}'
        return {'error': str(e)}
    finally:
        try:
            if scan_id is not None:
                store.finish_scan(scan_id, status, scanned, len(passed), funnel, error)
        except Exception:
            logger.exception(f'Intraday scan #{scan_id}: could not record its result')
        st.update(scanning=False, last_scan=datetime.now().isoformat(timespec='seconds'))
        INTRA_SCAN_LOCK.release()


def start_intraday_scan_thread(origin: str) -> None:
    """A 15m scan in the background. The 5m check runs on the intraday loop
    and must not wait for it: a scan of a wide universe takes a minute or
    more, and an entry found a minute late is a worse entry."""
    threading.Thread(target=run_intraday_scan, args=(origin,), daemon=True, name='intraday-scan').start()


def check_intraday(origin: str = 'schedule') -> dict:
    """
    On 5m candles: move today's zones along (tap, trigger, reject, fail) and
    follow every triggered trade to its target, stop or the square-off.
    """
    if not INTRA_CHECK_LOCK.acquire(blocking=False):
        return {'skipped': 'a check is already running'}
    st = STATE['intraday']
    st['checking'] = True
    summary = {'checked': 0, 'tapped': 0, 'triggered': 0, 'rejected': 0, 'failed': 0, 'closed': 0}
    try:
        now = data.now_ist()
        session = now.date().isoformat()
        store.expire_intraday(before_session=session)      # anything left over from an earlier day
        # Today's open zones, plus every open trade -- including one from an earlier
        # session that was never settled (the app was off at its square-off).
        zones = store.intraday_zones(store.INTRADAY_OPEN, session=session) + \
            store.intraday_zones(('triggered',), limit=50)
        if not zones:
            st['message'] = 'no intraday zones today'
            return summary
        frames = data.intraday([z['symbol'] + '.NS' for z in zones], 5)
        tg = store.settings()['telegram'] == '1'
        over = now >= _at(now, '15:30') or not data.market_open(now) and now > _at(now, '09:15')
        for z in zones:
            frame = frames.get(z['symbol'] + '.NS')
            stamp = now.isoformat(timespec='seconds')
            if frame is None or frame.empty:
                store.update_zone(z['id'], only_if=store.INTRADAY_OPEN + ('triggered',),
                                  last_checked=stamp, note='no 5m data')
                continue
            summary['checked'] += 1
            if z['status'] == 'triggered':
                summary['closed'] += _follow_trade(z, frame, now, tg)
                continue
            invalid = z['meta'].get('stop')
            t = intraday.trigger(frame, z['zone_low'], z['zone_high'], z['target'], irules(), now,
                                 since=zone_since(z, 5), invalid=invalid, side=z.get('side') or 'long')
            late = None if t.get('invalid') else too_late(t, 5, now, INTRA_LATE_MIN)
            if late:
                t.update(valid=False, late=True, reason=late)
            fields = {'last_checked': stamp, 'note': t.get('reason', '')}
            if t['tapped'] and z['status'] == 'watching':
                fields['status'] = 'tapped'
            if t.get('triggered') and not t.get('invalid'):
                ok = t['valid']
                fields.update(status='triggered' if ok else 'rejected', trigger=t)
                if ok:
                    fields['ctx'] = live_context(z, t, frame, 'intraday')
                # Claim it first: a zone dismissed since it was read gets no alert.
                if not store.update_zone(z['id'], only_if=store.INTRADAY_OPEN, **fields):
                    continue
                msg = intraday_entry_message(z, t) if ok else None
                sent = bool(ok and tg and not paused('intraday') and notify.send(msg))
                store.add_alert(z, 'entry' if ok else 'rejected', t, sent, msg or t['reason'])
                if ok:
                    notice_entry('intraday', f"intraday:{z['id']}", z['symbol'], t, t.get('choch_time'))
                summary['triggered' if ok else 'rejected'] += 1
                logger.info(f"Intraday {z['symbol']}: {t['reason']}" + (' -> Telegram' if sent else ''))
                if ok:                                      # it may already have run to target or stop
                    z.update(status='triggered', trigger=t)
                    summary['closed'] += _follow_trade(z, frame, now, tg)
                continue
            if t.get('invalid'):
                fields.update(status='expired', note=t['reason'])
            elif over or now >= _at(now, irules().no_entry_after):
                fields.update(status='expired', note=f'no CHoCH before the {irules().no_entry_after} cutoff')
            if store.update_zone(z['id'], only_if=store.INTRADAY_OPEN, **fields):
                summary['tapped'] += fields.get('status') == 'tapped'
                summary['failed'] += bool(t.get('invalid'))
        st['message'] = (f"{summary['checked']} checked · {summary['tapped']} tapped · "
                         f"{summary['triggered']} triggered · {summary['rejected']} rejected · "
                         f"{summary['closed']} closed")
        return summary
    except Exception as e:
        logger.exception('Intraday check failed')
        st['message'] = f'check failed: {e}'
        return {'error': str(e)}
    finally:
        st.update(checking=False, last_check=datetime.now().isoformat(timespec='seconds'))
        INTRA_CHECK_LOCK.release()


def _follow_trade(z, frame, now, tg) -> int:
    """Update a triggered trade from the 5m candles after its entry. Returns 1 if it closed."""
    t = dict(z['trigger'] or {})
    if not t.get('entry'):
        return 0
    o = intraday.outcome(frame, t, irules(), now)
    stamp = now.isoformat(timespec='seconds')
    if o['status'] == 'open':
        t.update(last=o['last'], r_open=o['r'])
        store.update_zone(z['id'], only_if=('triggered',), last_checked=stamp, trigger=t,
                          note=f"open · {o['r']:+.2f}R at {o['last']:.2f}")
        return 0
    t.update(exit=o['exit'], exit_time=o['exit_time'], r=o['r'], result=o['status'])
    word = {'won': 'Target hit', 'lost': 'Stopped out', 'closed': 'Squared off'}[o['status']]
    note = f"{word} at {o['exit']:.2f} ({o['exit_time'][11:16]}) · {o['r']:+.2f}R"
    if not store.update_zone(z['id'], only_if=('triggered',), status=o['status'], last_checked=stamp,
                             trigger=t, note=note):
        return 0                                    # already settled: report it once
    msg = (f"🐋 <b>Intraday {_words(t)['side'].lower()} · {h(z['symbol'])} · {word}</b>\n"
           f"Entry {t['entry']:.2f} → exit {o['exit']:.2f} at {o['exit_time'][11:16]} IST · "
           f"<b>{o['r']:+.2f}R</b>\n"
           + ("Square off now if you are still in it.\n" if o['status'] == 'closed' else '')
           + 'Analysis only — no order was placed.')
    sent = bool(tg and not paused('intraday') and notify.send(msg))
    notice_result('intraday', f"intraday:{z['id']}", z['symbol'], t, o['status'], o['exit'], o['r'], o['exit_time'])
    store.add_alert(z, o['status'], {**t, 'entry': t['entry'], 'stop': t['stop'], 'target': t['target'],
                                      'rr': o['r'], 'choch_time': o['exit_time']}, sent, note)
    logger.info(f"Intraday {z['symbol']}: {note}")
    return 1


def intraday_entry_message(z, t) -> str:
    w = _words(t)
    risk = abs(t['entry'] - t['stop'])
    label = z['meta'].get('target_label') or 'liquidity'
    return (f"🐋 <b>INTRADAY {w['side']} · {h(z['symbol'])}</b>\n"
            f"5m CHoCH {w['past']} {t['choch_level']:.2f} at {t['choch_time'][11:16]} IST, "
            f"after sweeping {t['swept_level']:.2f} ({w['extreme']} {t['sweep_low']:.2f}).\n\n"
            f"<b>{w['act']}</b> {t['entry']:.2f}\n"
            f"<b>Stop</b> {t['stop']:.2f}  (risk {risk:.2f}, {risk / t['entry'] * 100:.2f}%)\n"
            f"<b>Target</b> {t['target']:.2f}  ({h(label.lower())})\n"
            f"<b>R:R</b> 1:{t['rr']:.1f}\n\n"
            f"15m zone {z['zone_low']:.2f}–{z['zone_high']:.2f}. Square off by {irules().square_off}.\n"
            f"Analysis only — no order was placed. Not investment advice.")


def _next_mark(now: datetime, minutes: int) -> datetime:
    """The next `minutes` candle close, plus the delay for it to reach the data source."""
    base = now.replace(second=0, microsecond=0)
    delay = candle_delay()
    return base + timedelta(minutes=minutes - base.minute % minutes, seconds=delay)


def intraday_loop() -> None:
    """Its own thread, so a 15m scan of the F&O list never delays a 5m check."""
    logger.info('Intraday scheduler started')
    st = STATE['intraday']
    while True:
        try:
            now = data.now_ist()
            on = store.settings()['intraday'] == '1'
            phase = intraday_phase(now)['key']
            st['phase'] = phase
            if not on or phase in ('closed', 'pre'):
                st.update(next_scan=None, next_check=None, _scan=None, _check=None)
                # Wrap up once a day after the close -- whether or not intraday mode is
                # still on (switching it off mid-session must not leave trades open
                # forever), and on any day something is left (e.g. after a restart).
                after_close = phase == 'closed' and (now.weekday() >= 5 or now >= _at(now, '15:30'))
                if after_close and st.get('_wrapped') != now.date():
                    if store.intraday_zones(store.INTRADAY_OPEN + ('triggered',), limit=1):
                        check_intraday('close')            # settle anything still open
                        store.expire_intraday(note='session over')
                    store.prune_intraday()
                    st['_wrapped'] = now.date()
                time.sleep(15)
                continue
            if st.get('_scan') is None:
                st['_scan'] = max(_next_mark(now, 15), _at(now, irules().start) + timedelta(seconds=10))
            if st.get('_check') is None:
                st['_check'] = _next_mark(now, 5)
            if phase == 'live' and now >= st['_scan']:
                start_intraday_scan_thread('schedule')          # never holds up the 5m check
                st['_scan'] = _next_mark(data.now_ist(), 15)
            if now >= st['_check']:
                check_intraday('schedule')
                st['_check'] = _next_mark(data.now_ist(), 5)
            st['next_scan'] = st['_scan'].isoformat(timespec='seconds') if phase == 'live' else None
            st['next_check'] = st['_check'].isoformat(timespec='seconds')
        except Exception:
            logger.exception('Intraday tick failed')
        time.sleep(5)


def intraday_state() -> dict:
    session = data.now_ist().date().isoformat()
    scan = store.latest_scan(mode='intraday')
    zones = [live_zone(z) for z in store.intraday_zones(session=session)]
    journal = [live_zone(z) for z in store.intraday_zones(store.INTRADAY_TRADES, limit=500)]   # the table pages
    closed = [z for z in journal if z['status'] in ('won', 'lost', 'closed') and z['trigger']]
    today = [z for z in closed if z['created_at'].startswith(session)]

    def stats(rows):
        rs = [z['trigger'].get('r') or 0 for z in rows]
        return {'trades': len(rs), 'wins': sum(1 for x in rs if x > 0), 'r': round(sum(rs), 2)}
    return {
        'session': session,
        'phase': intraday_phase(),
        'state': {k: v for k, v in STATE['intraday'].items() if not k.startswith('_')},
        'rules': irules().as_dict(),
        'env_rules': ENV_INTRA.as_dict(),
        'rule_overrides': saved_intra_overrides(),
        'tunable': intraday.TUNABLE,
        'tunable_order': list(intraday.TUNABLE),
        'whatif_ready': bool(LAST_INTRA),
        'steps': [{'key': k, 'label': v} for k, v in intraday.STEPS],
        'scan': scan,
        'candidates': store.candidates(scan['id']) if scan else [],
        'zones': zones,
        'journal': journal,
        'stats': {'today': stats(today), 'all': stats(closed)},
    }


# ---------------------------------------------------------------------------
# Options (niftywhale/options.py)
# ---------------------------------------------------------------------------
OPT_CYCLE_MIN = 3               # a pass every 3 minutes in the session
OPT_STOCKS_PER_CYCLE = 10       # stocks rotate: 30 of them are each seen every ~9 minutes
OPT_LOCK = threading.Lock()     # one pass at a time (the loop, the dashboard's refresh)
OPT_WHY: dict = {}              # symbol -> why the last look made no idea (for the dashboard)
OPT_LEVELS: dict = {}           # symbol -> (support, resistance) last announced
OPT_WINDOW = {'index': 8.0, 'stock': 15.0}   # strikes kept: within this % of spot


def saved_opt_overrides() -> dict:
    try:
        return json.loads(store.settings().get('options_rules') or '{}')
    except ValueError:
        return {}


def orules() -> dict:
    return options.rules_from(saved_opt_overrides())


def option_stocks(st: dict = None) -> list:
    """The stocks options mode scans: the saved list, else the default 30, F&O names only."""
    st = st or store.settings()
    raw = (st.get('options_stocks') or '').replace(',', ' ').split() or list(options.DEFAULT_STOCKS)
    out = []
    for s in raw:
        s = s.strip().upper()
        if SYMBOL_RE.match(s) and s not in options.INDICES and s not in out:
            out.append(s)
    return out[:options.MAX_STOCKS]


def _opt_alerts_on(st: dict = None) -> bool:
    st = st or store.settings()
    return st['telegram'] == '1' and st['options_alerts'] == '1' and notify.configured()


def _inr(x) -> str:
    return '₹{:,.2f}'.format(x) if x is not None else '—'


def _exp_label(expiry: str) -> str:
    try:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d %b')
    except (TypeError, ValueError):
        return str(expiry)


def idea_message(i: dict) -> str:
    risk = i['entry'] - i['stop']
    lot = i.get('lot')
    rr_ = (i['target'] - i['entry']) / risk if risk > 0 else 0
    level_word = 'below' if i['direction'] == 'long' else 'above'
    level_kind = 'put-OI support' if i['direction'] == 'long' else 'call-OI resistance'
    return (f"🐋 <b>OPTION IDEA · {h(i['symbol'])} {i['strike']:g} {i['side']}</b> · {_exp_label(i['expiry'])} expiry\n"
            f"{'Bullish' if i['direction'] == 'long' else 'Bearish'}: {h(i['reason'])}.\n\n"
            f"<b>Buy</b> ≈ {_inr(i['entry'])}\n"
            f"<b>Stop</b> {_inr(i['stop'])}  ({orules()['stop_pct']:g}% of premium)\n"
            f"<b>Target</b> {_inr(i['target'])}  (R:R 1:{rr_:.1f})\n"
            + (f"Lot {lot} · risk {_inr(risk * lot)} per lot\n" if lot else '')
            + (f"Exit early if {h(i['symbol'])} trades {level_word} {i['level']:g} ({level_kind}). " if i.get('level') else '')
            + f"Square off by {options.TIMES['square_off']}.\n"
            "Paper trade — no order was placed. Not investment advice.")


def idea_result_message(i: dict) -> str:
    icon = {'won': '✅', 'lost': '❌'}.get(i['status'], '⏹')
    what = {'target': 'Target hit', 'stop': 'Stopped out', 'squared off': 'Squared off'}.get(i['note'], i['note'].capitalize())
    lot = i.get('lot')
    pnl = (i['exit'] - i['entry']) * lot if lot else None
    return (f"{icon} <b>Option idea · {h(i['symbol'])} {i['strike']:g} {i['side']} · {h(what)}</b>\n"
            f"Entry {_inr(i['entry'])} → exit {_inr(i['exit'])} · <b>{i['r']:+.2f}R</b>"
            + (f" ({'+' if pnl >= 0 else '−'}{_inr(abs(pnl))} per lot)" if pnl is not None else '') + "\nPaper trade.")


def _follow_idea(i: dict, chain: dict, now: datetime, tg: bool, final: bool = False) -> bool:
    """Move one open idea along; True if it closed. The status change is claimed before
    any message goes out, so a closed idea is announced exactly once."""
    res = options.follow(i, chain, now, orules()['square_off'], final=final)
    if res is None:
        store.update_idea(i['id'], only_if='open', last=i.get('last'), r_open=i.get('r_open'))
        return False
    claimed = store.update_idea(i['id'], only_if='open', status=res['status'], exit=res['exit'],
                                exit_time=now.isoformat(timespec='seconds'), r=res['r'], note=res['note'],
                                last=i.get('last'), r_open=i.get('r_open'))
    if claimed:
        i.update(res)
        if tg and not paused('options'):
            notify.send(idea_result_message(i))
        notice_result('options', f"options:{i['id']}", i['symbol'], {**i, 'opt_side': i['side']}, res['status'], res['exit'],
                      res['r'], now.isoformat(timespec='seconds'),
                      {'target': 'Target hit', 'stop': 'Stopped out', 'squared off': 'Squared off'}.get(res['note'],
                                                                                                  str(res['note']).capitalize()))
        logger.info(f"Option idea {i['symbol']} {i['strike']:g} {i['side']}: {res['note']} at {res['exit']} ({res['r']:+}R)")
    return bool(claimed)


def _level_alert(sym: str, summary: dict, series: list, st: dict) -> None:
    """Indices only, when switched on: the OI support or resistance moved and held for
    two snapshots in a row (one snapshot can flicker between two near-equal strikes)."""
    if not options.is_index(sym) or st['options_level_alerts'] != '1' or not _opt_alerts_on(st):
        return
    cur = (summary.get('support'), summary.get('resistance'))
    prev = (series[-2]['summary'].get('support'), series[-2]['summary'].get('resistance')) if len(series) >= 2 else None
    said = OPT_LEVELS.get(sym)
    if said is None or said[2] != summary.get('_session'):
        OPT_LEVELS[sym] = (*cur, summary.get('_session'))      # first look of the session: just remember
        return
    if prev != cur or cur == said[:2]:
        return
    OPT_LEVELS[sym] = (*cur, summary.get('_session'))
    moves = []
    if cur[0] != said[0] and cur[0] is not None:
        moves.append(f"support {said[0]:g} → <b>{cur[0]:g}</b> ({'put writers stepping up' if said[0] and cur[0] > said[0] else 'put writers backing off'})")
    if cur[1] != said[1] and cur[1] is not None:
        moves.append(f"resistance {said[1]:g} → <b>{cur[1]:g}</b> ({'call writers backing off' if said[1] and cur[1] > said[1] else 'call writers pressing down'})")
    if moves:
        notify.send(f"🐋 <b>{h(sym)} OI levels moved</b> · spot {summary['spot']:,.2f}\n" + '\n'.join(moves)
                    + f"\nPCR {summary.get('pcr') or 0:.2f} · max pain {summary.get('max_pain') or 0:g}")


def fetch_options(sym: str, now: datetime = None, allow_ideas: bool = True) -> dict:
    """Fetch, analyse and store one underlying: its nearest expiry, plus the next one on
    an expiry day (ideas never use a same-day expiry). Then follow its open ideas and
    maybe start one. Returns {'expiries': [...], 'idea': id or None}."""
    now = now or data.now_ist()
    st = store.settings()
    today = now.date().isoformat()
    exps = dhan.expiries(sym, options.INDICES)
    # After the close an expiry dated today is settled: its chain is dead prices.
    closed = now.hour * 60 + now.minute >= 15 * 60 + 30
    near = options.pick_expiry(exps, today, for_trade=closed)
    trade = options.pick_expiry(exps, today, for_trade=True)
    window = OPT_WINDOW['index' if options.is_index(sym) else 'stock']
    got = {}
    for exp in dict.fromkeys(e for e in (near, trade) if e):
        raw = dhan.option_chain(sym, exp, options.INDICES)
        chain = options.parse_chain(raw or {}, window_pct=window)
        if not chain['strikes'] or not chain['spot']:
            continue
        first = store.first_snapshot(sym, exp, today)
        ref = options.expand(first['chain']) if first and first.get('chain') else None
        summary = options.summarize(chain, ref, store.iv_history(sym, today))
        summary.update(expiry=exp, dte=(datetime.strptime(exp, '%Y-%m-%d').date() - now.date()).days,
                       lot=dhan.lot_size(sym), unusual=options.unusual(chain), unusual_intraday=options.unusual(chain, ref) if ref else [],
                       is_near=exp == near, is_trade=exp == trade)
        store.add_snapshot(sym, exp, now.isoformat(timespec='seconds'), today, chain['spot'], summary, options.compact(chain))
        # The IV history is kept on the first expiry that isn't today's: a same-day expiry's
        # IV collapses toward zero and would wreck every later percentile.
        if exp == trade:
            store.save_daily(sym, today, chain['spot'], summary.get('atm_iv'), summary.get('pcr'), summary.get('max_pain'))
        if exp == near:
            series = store.snapshot_series(sym, exp, today)
            _level_alert(sym, {**summary, '_session': today}, series, st)
        got[exp] = (chain, summary)

    tg = _opt_alerts_on(st)
    for i in store.ideas(status='open', symbol=sym):
        if i['expiry'] in got:
            _follow_idea(i, got[i['expiry']][0], now, tg)

    made = None
    if allow_ideas and trade in got and data.market_open(now):
        chain, summary = got[trade]
        series = store.snapshot_series(sym, trade, today)
        past = [{'ts': s['ts'], **s['summary'], 'spot': s['spot']} for s in series[:-1]]
        open_now = bool(store.ideas(status='open', symbol=sym))
        taken = len(store.ideas(session=today, symbol=sym))
        i, why = options.idea(sym, chain, summary, past, orules(), now, taken, open_now)
        OPT_WHY[sym] = why
        # Every look that came near an idea is kept for the autopilot (options.candidate).
        cand = None
        try:
            cand = options.candidate(sym, chain, summary, past, now, orules())
            if cand and not open_now:
                store.add_candidate(cand, trade, today, now.isoformat(timespec='seconds'), taken=bool(i))
        except Exception as e:
            logger.warning(f'Options candidate {sym}: {e}')
        if i:
            i.update(expiry=trade, created_at=now.isoformat(timespec='seconds'), session=today, status='open',
                     lot=summary.get('lot'), last=i['entry'], r_open=0.0)
            i['id'] = made = store.add_idea(i)
            sent = bool(tg and not paused('options') and notify.send(idea_message(i)))
            notice_entry('options', f"options:{i['id']}", sym, {**i, 'opt_side': i['side']}, i['created_at'])
            f = dict((cand or {}).get('f') or {'bias': abs(i['bias']), 'move_pct': abs(i['move_pct'])})
            if sym not in options.INDICES:            # a stock's news; an index has none of its own
                try:
                    f.update(news_context(sym, now.isoformat(timespec='seconds')))
                    f.update(smart_context(sym, now.isoformat(timespec='seconds')))
                except Exception as e:
                    logger.warning(f'News / smart-money context {sym}: {e}')
            store.update_idea(i['id'], sent=int(sent), features=json.dumps(f))
            logger.info(f"Option idea {sym} {i['strike']:g} {i['side']} at {i['entry']} ({i['reason']})")
    return {'expiries': list(got), 'idea': made}


def run_options_cycle(origin: str = 'schedule', symbols: list = None, everything: bool = False) -> dict:
    """One pass: the indices, any stock with an open idea, and the next batch of stocks
    (all of them with `everything`, as in the after-close pass)."""
    if not OPT_LOCK.acquire(blocking=False):
        return {'skipped': 'a pass is already running'}
    st = STATE['options']
    try:
        if not dhan.available():
            st['message'] = 'Needs Dhan: option chains come only from Dhan'
            return {'skipped': st['message']}
        stocks = option_stocks()
        if symbols is None:
            ptr = st.get('_ptr', 0) % max(1, len(stocks))
            batch = stocks if everything else (stocks + stocks)[ptr:ptr + OPT_STOCKS_PER_CYCLE]
            st['_ptr'] = ptr + OPT_STOCKS_PER_CYCLE
            open_syms = [i['symbol'] for i in store.ideas(status='open')]
            symbols = list(dict.fromkeys(list(options.INDICES) + open_syms + batch))
        st.update(running=True, done=0, total=len(symbols), errors={})
        made = 0
        for sym in symbols:
            try:
                made += bool(fetch_options(sym, allow_ideas=origin != 'close')['idea'])
            except Exception as e:                     # one bad underlying never stops the pass
                st['errors'][sym] = dhan._redact(e)[:160]
                logger.warning(f'Options: {sym} failed: {st["errors"][sym]}')
                if dhan._state.get('rejected'):
                    break
            st['done'] += 1
        st.update(last_run=data.now_ist().isoformat(timespec='seconds'), origin=origin,
                  message=f"{st['done'] - len(st['errors'])}/{len(symbols)} chains" + (f' · {made} new idea(s)' if made else '')
                  + (f" · {len(st['errors'])} failed" if st['errors'] else ''))
        return {'fetched': st['done'] - len(st['errors']), 'failed': len(st['errors']), 'ideas': made}
    finally:
        st['running'] = False
        OPT_LOCK.release()


def settle_ideas(now: datetime) -> int:
    """After the session: close whatever is still open at its last seen price (say the
    app was down at the square-off)."""
    closed = 0
    for i in store.ideas(status='open'):
        snap = store.latest_snapshot(i['symbol'], i['expiry'])
        chain = options.expand(snap['chain']) if snap else {'spot': None, 'strikes': []}
        closed += _follow_idea(i, chain, now, _opt_alerts_on(), final=True)
    return closed


def _next_opt(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    return base + timedelta(minutes=OPT_CYCLE_MIN - base.minute % OPT_CYCLE_MIN, seconds=5)


def options_loop() -> None:
    """Its own thread: the option-chain limit (1 request / 3 s) is separate from the
    chart one, and a pass (~45 s) must not hold up the swing or intraday checks."""
    logger.info('Options scheduler started')
    st = STATE['options']
    while True:
        try:
            now = data.now_ist()
            s = store.settings()
            if s['options'] != '1':
                st.update(next_run=None, _next=None, message='Options mode is off')
            elif data.market_open(now):
                if st.get('_next') is None:
                    st['_next'] = now                          # first pass right away
                if now >= st['_next']:
                    run_options_cycle('schedule')
                    st['_next'] = _next_opt(data.now_ist())
                st['next_run'] = st['_next'].isoformat(timespec='seconds')
            else:
                st.update(next_run=None, _next=None)
                # Once after each session: every chain's closing numbers (the IV history),
                # settle anything still open, and prune.
                after = now.weekday() < 5 and now.hour * 60 + now.minute >= 15 * 60 + 32
                if after and st.get('_closed') != now.date() and dhan.available():
                    run_options_cycle('close', everything=True)
                    settle_ideas(now)
                    store.prune_options(now.date().isoformat())
                    st['_closed'] = now.date()
        except Exception:
            logger.exception('Options tick failed')
        time.sleep(5)


def options_state() -> dict:
    """The Options tab: the latest look at every underlying, today's ideas, the journal."""
    now = data.now_ist()
    today = now.date().isoformat()
    since = (now.date() - timedelta(days=7)).isoformat()
    stocks = option_stocks()
    latest = {}
    for snap in store.latest_summaries(since):
        sm = snap['summary']
        # One row per underlying: the nearest expiry's latest look.
        if sm.get('is_near') and snap['symbol'] not in latest:
            latest[snap['symbol']] = {'symbol': snap['symbol'], 'ts': snap['ts'], 'session': snap['session'],
                                      'expiry': snap['expiry'], **{k: v for k, v in sm.items() if not k.startswith('unusual')},
                                      'unusual': sm.get('unusual') or []}
    rows = []
    for sym in list(options.INDICES) + stocks:
        r = latest.get(sym) or {'symbol': sym}
        r.update(index=options.is_index(sym), name=options.INDICES[sym][2] if options.is_index(sym) else None,
                 why=OPT_WHY.get(sym), lp=underlying_key(sym))
        rows.append(r)
    journal = [live_idea(i) for i in store.ideas(limit=500)]
    closed = [i for i in journal if i['status'] in ('won', 'lost', 'closed')]
    today_closed = [i for i in closed if i['session'] == today]

    def stats(xs):
        rs = [i['r'] or 0 for i in xs]
        return {'trades': len(rs), 'wins': sum(1 for x in rs if x > 0), 'r': round(sum(rs), 2)}
    unusual_feed = []
    for r in rows:
        for u in (r.get('unusual') or [])[:3]:
            unusual_feed.append({**u, 'symbol': r['symbol'], 'ts': r.get('ts'), 'expiry': r.get('expiry')})
    # Biggest first, relative to the underlying's whole chain: 5 lakh contracts is a lot for a
    # stock and little for Nifty.
    chain_oi = {r['symbol']: (r.get('ce_oi') or 0) + (r.get('pe_oi') or 0) for r in rows}
    unusual_feed.sort(key=lambda u: -abs(u.get('doi') or 0) / max(1.0, chain_oi.get(u['symbol']) or 1.0))
    return {
        'now': now.isoformat(timespec='seconds'), 'session': today, 'market_open': data.market_open(now),
        'state': {k: v for k, v in STATE['options'].items() if not k.startswith('_')},
        'settings': {k: v for k, v in store.settings().items() if k.startswith('options')},
        'stocks': stocks, 'default_stocks': list(options.DEFAULT_STOCKS),
        'rules': orules(), 'rule_overrides': saved_opt_overrides(),
        'tunable': {k: {'default': d, 'min': lo, 'max': hi, 'step': step, 'label': label}
                    for k, (d, lo, hi, step, label) in options.TUNABLE.items()},
        'tunable_order': list(options.TUNABLE),
        'market_data': dhan.status(),
        'rows': rows,
        'unusual': unusual_feed[:12],
        'journal': journal,
        'stats': {'today': stats(today_closed), 'all': stats(closed),
                  'open': sum(i['status'] == 'open' for i in journal),
                  'open_r': round(sum(i['r_open'] or 0 for i in journal if i['status'] == 'open'), 2)},
    }


def _chain_lp(sym: str, expiry: str, table: list) -> list:
    """Each contract's feed symbol on the chain table, so its LTP moves live (in place)."""
    ids = dhan.option_ids(sym, expiry, [(r['k'], s.upper()) for r in table for s in options.SIDES if r[s]])
    for r in table:
        for s in options.SIDES:
            hit = r[s] and ids.get((r['k'], s.upper()))
            if hit:
                r[s]['lp'] = f'OPT:{hit[1]}:{hit[0]}'
    return table


# The chain drawer between two chain reads (every 3-9 minutes, Dhan's slow /optionchain): the
# latest snapshot's strikes with live OI, volume, LTP and bid / ask from one full-quote request
# for all of them, spot from the feed, and the summary computed again on that. Expiry-wide totals
# move by what the stored window moved (far strikes barely do); max pain and IV stay as read.
CHAIN_LIVE: dict = {}
CHAIN_LIVE_S = 3.0
CHAIN_STREAM_S = 1.0          # with the stream: worked out again from its packets this often
CHAIN_LIVE_LOCK = threading.Lock()


def chain_live(sym: str, expiry: str = None):
    snap = store.latest_snapshot(sym, expiry)
    now = data.now_ist()
    if not snap or snap['session'] != now.date().isoformat() or not data.market_open(now) or not dhan.available():
        return None
    key = (sym, snap['expiry'])
    # With Dhan's stream the chain's contracts are subscribed in full mode (OI, volume, depth) and
    # the chain is worked out again every second from what it pushed; without it, one REST quote
    # of every contract per CHAIN_LIVE_S.
    streamed = charts.streaming()
    ttl = CHAIN_STREAM_S if streamed else CHAIN_LIVE_S
    hit = CHAIN_LIVE.get(key)
    if hit and time.time() - hit[0] < ttl and hit[1]['snap'] == snap['id']:
        return hit[1]
    with CHAIN_LIVE_LOCK:                 # however many browsers watch, one request per CHAIN_LIVE_S
        hit = CHAIN_LIVE.get(key)
        if hit and time.time() - hit[0] < ttl and hit[1]['snap'] == snap['id']:
            return hit[1]
        chain = options.expand(snap['chain'])
        ids = dhan.option_ids(sym, snap['expiry'], [(r['k'], s.upper()) for r in chain['strikes'] for s in options.SIDES if r[s]])
        q = None
        if streamed:
            syms = {f'OPT:{seg}:{int(sid)}': (seg, int(sid)) for sid, seg in ids.values()}
            charts.FEED.mark(list(syms), mode='full')
            got = charts.SOURCE.carried(list(syms))
            if syms and len(got) >= 0.8 * len(syms):
                q = {syms[s]: {'ltp': x.get('ltp') or 0, 'oi': x.get('oi') or 0, 'vol': x.get('vol') or 0,
                               'bid': x.get('bid') or 0, 'ask': x.get('ask') or 0} for s, x in got.items()}
        if q is None:
            by_seg: dict = {}
            for sid, seg in ids.values():
                by_seg.setdefault(seg, []).append(int(sid))
            q = dhan.quotes_full(by_seg)
        before = options.totals(chain['strikes'])
        moved = 0
        for r in chain['strikes']:
            for s in options.SIDES:
                o, cid = r[s], ids.get((r['k'], s.upper()))
                live = o and cid and q.get((cid[1], int(cid[0])))
                if live and (live['oi'] or live['ltp']):
                    o.update(oi=live['oi'] or o['oi'], vol=live['vol'] or o.get('vol'), ltp=live['ltp'] or o['ltp'],
                             bid=live['bid'] or o.get('bid'), ask=live['ask'] or o.get('ask'))
                    moved += 1
        spot = live_px(underlying_key(sym))
        if spot:
            chain['spot'] = spot[0]
        if chain.get('all'):
            after = options.totals(chain['strikes'])
            chain['all'] = {**chain['all'], **{k: chain['all'].get(k, 0) + after[k] - before[k] for k in after}}
        first = store.first_snapshot(sym, snap['expiry'], snap['session'])
        ref = options.expand(first['chain']) if first and first['id'] != snap['id'] and first.get('chain') else None
        summary = options.summarize(chain, ref, None)
        for k in ('iv_pct', 'iv_days', 'dte'):           # need the IV history / calendar: as read
            if k in snap['summary']:
                summary[k] = snap['summary'][k]
        out = {'snap': snap['id'], 'ts': now.isoformat(timespec='seconds'), 'quoted': moved, 'expiry': snap['expiry'],
               'summary': {**snap['summary'], **summary}, 'chain': _chain_lp(sym, snap['expiry'], options.annotate(chain, ref))}
        CHAIN_LIVE[key] = (time.time(), out)
        return out


def options_chain_view(sym: str, expiry: str = None) -> dict:
    """One underlying for the drawer: the chain table, the day's PCR / IV / spot lines."""
    snap = store.latest_snapshot(sym, expiry)
    if not snap:
        return None
    session = snap['session']
    first = store.first_snapshot(sym, snap['expiry'], session)
    chain = options.expand(snap['chain'])
    ref = options.expand(first['chain']) if first and first['id'] != snap['id'] and first.get('chain') else None
    series = store.snapshot_series(sym, snap['expiry'], session)
    pick = lambda k: [s['summary'].get(k) for s in series]
    table = _chain_lp(sym, snap['expiry'], options.annotate(chain, ref))
    return {
        'symbol': sym, 'name': options.INDICES[sym][2] if options.is_index(sym) else None,
        'expiry': snap['expiry'], 'expiries': store.expiries_seen(sym), 'ts': snap['ts'], 'session': session,
        'summary': snap['summary'], 'chain': table, 'ref_ts': first['ts'] if ref else None,
        'series': {'ts': [s['ts'] for s in series], 'spot': [s['spot'] for s in series], 'pcr': pick('pcr'),
                   'atm_iv': pick('atm_iv'), 'max_pain': pick('max_pain'), 'support': pick('support'),
                   'resistance': pick('resistance'), 'bias': pick('bias_intraday')},
        'ideas': [live_idea(i) for i in store.ideas(symbol=sym, limit=20)], 'why': OPT_WHY.get(sym), 'lp': underlying_key(sym),
    }


# ---------------------------------------------------------------------------
# Index ticker (niftywhale/ticker.py)
# ---------------------------------------------------------------------------
TICKER = {'at': 0.0, 'rows': [], 'error': None, 'session': None, 'prev': {}, 'prev_session': None, 'loading_prev': False}
TICKER_LOCK = threading.Lock()
TICKER_TTL_LIVE = 5             # seconds between Dhan quote calls in the session, however many browsers poll
TICKER_TTL_CLOSED = 300


def _load_prev_closes(session) -> None:
    """Previous closes for the session, one daily-candle request per index. Runs in the
    background; the ticker shows no change until done. The lab may be downloading on the same
    Dhan account (after hours), so requests are paced and a refused one (429) is retried; an
    index still missing is tried again in TICKER_PREV_RETRY seconds rather than next session."""
    prev = dict(TICKER['prev']) if TICKER['prev_session'] == session else {}
    try:
        for sid in ticker.IDS:
            if prev.get(sid) is not None:
                continue
            for attempt in range(3):
                try:
                    time.sleep(0.4 + attempt * 2.0)
                    prev[sid] = ticker.prev_close(dhan.index_daily_closes(sid), session)
                    break
                except Exception as e:                # one index without candles must not stop the rest
                    if attempt == 2:
                        logger.info(f'Ticker: no daily candles for index {sid}: {dhan._redact(e)}')
        missing = [sid for sid in ticker.IDS if prev.get(sid) is None and sid != ticker.GIFT]
        TICKER.update(prev=prev, prev_session=session, prev_retry_at=time.time() + TICKER_PREV_RETRY if missing else None)
    finally:
        TICKER['loading_prev'] = False


TICKER_PREV_RETRY = 300
TICKER_KEYS = [f'IDX:{i}' for i in ticker.IDS]


def _ticker_stream(session) -> bool:
    """The strip from Dhan's stream (the indices in quote mode: price and the day's open / high /
    low), once it carries most of them; False sends the caller to the REST quote."""
    charts.FEED.mark(TICKER_KEYS, mode='quote')
    got = charts.SOURCE.carried(TICKER_KEYS)
    if len(got) < len(TICKER_KEYS) // 2:
        return False
    quotes = {int(k[4:]): {'last': q['ltp'], 'open': q.get('open'), 'high': q.get('high'), 'low': q.get('low')}
              for k, q in got.items() if q.get('ltp')}
    # Indices the stream has not priced yet keep their last REST quote.
    for r in TICKER['rows'] if TICKER['session'] == session else []:
        quotes.setdefault(r['id'], r)
    TICKER.update(rows=ticker.build(quotes, TICKER['prev'] if TICKER['prev_session'] == session else {}),
                  at=time.time(), error=None, session=session)
    return True


def ticker_state() -> dict:
    now = data.now_ist()
    live = data.market_open(now)
    session = ticker.session_date(now)
    if not dhan.available():
        return {'rows': [], 'live': False, 'source': None, 'message': 'The index ticker needs Dhan (real-time data)'}
    retry = TICKER.get('prev_retry_at') and time.time() >= TICKER['prev_retry_at']
    if (TICKER['prev_session'] != session or retry) and not TICKER['loading_prev']:
        TICKER['loading_prev'] = True
        threading.Thread(target=_load_prev_closes, args=(session,), daemon=True, name='ticker-prev').start()
    if live and charts.streaming() and _ticker_stream(session):
        return {'rows': TICKER['rows'], 'mood': ticker.mood(TICKER['rows']), 'live': True, 'source': 'dhan', 'stream': True,
                'session': session.isoformat(),
                'updated': datetime.fromtimestamp(TICKER['at'], data.IST).isoformat(timespec='seconds'),
                'refresh_s': 1, 'error': None}
    ttl = TICKER_TTL_LIVE if live else TICKER_TTL_CLOSED
    stale = lambda: time.time() - TICKER['at'] >= ttl or TICKER['session'] != session or not TICKER['rows']
    if stale():
        # One Dhan call per TTL, not one per browser. With prices to show, a request that finds
        # the call already in flight answers from the cache rather than holding a server thread
        # for up to the quote's 10 s timeout; only the very first request waits.
        if TICKER_LOCK.acquire(blocking=not TICKER['rows']):
            try:
                if stale():
                    try:
                        quotes = dhan.index_quotes(ticker.IDS)
                        TICKER.update(rows=ticker.build(quotes, TICKER['prev'] if TICKER['prev_session'] == session else {}),
                                      at=time.time(), error=None, session=session)
                    except Exception as e:
                        TICKER.update(error=dhan._redact(e)[:160], at=time.time())
                        logger.warning(f"Ticker: quotes failed: {TICKER['error']}")
            finally:
                TICKER_LOCK.release()
    elif TICKER['prev_session'] == session and TICKER['rows'] and any(
            r.get('prev_close') is None and TICKER['prev'].get(r['id']) is not None for r in TICKER['rows']):
        # The previous closes arrived after the last quote: fill them in without another call.
        TICKER['rows'] = ticker.build({r['id']: r for r in TICKER['rows']}, TICKER['prev'])
    return {'rows': TICKER['rows'], 'mood': ticker.mood(TICKER['rows']), 'live': live, 'source': 'dhan', 'session': session.isoformat(),
            'updated': datetime.fromtimestamp(TICKER['at'], data.IST).isoformat(timespec='seconds') if TICKER['at'] else None,
            'refresh_s': ttl, 'error': TICKER['error']}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def safe(obj):
    """NaN/inf -> None, recursively. Python's json writes NaN, which no
    browser will parse, and one gappy volume series would blank the page."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe(v) for v in obj]
    return obj


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/docs')
def docs():
    """The README, rendered. Read on each request, so the guide is always the
    README that shipped with this build."""
    import markdown
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'README.md')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return 'README.md is missing from this build.', 404
    md = markdown.Markdown(extensions=['tables', 'fenced_code', 'toc', 'sane_lists'])
    body = md.convert(text)
    return render_template('docs.html', body=body, toc=md.toc)


@app.route('/favicon.ico')
def favicon():
    """Browsers that ask for /favicon.ico directly get the SVG icon instead of a 404."""
    return redirect('/static/favicon.svg', code=301)


@app.route('/api/glossary')
def api_glossary():
    """Every abbreviation and symbol the dashboard shows, for the Legend."""
    return jsonify(glossary.as_json())


@app.route('/healthz')
def healthz():
    return 'ok'


@app.route('/trust')
def trust_page():
    """Public: how to trust the Pi's HTTPS certificate on each kind of device (the guide itself needs a sign-in)."""
    return render_template('trust.html')


@app.route('/login')
def login_page():
    """The split-view sign-in page: first-time setup, password + code, or a passkey."""
    if auth.ENABLED and auth.owner() and auth._session():
        return redirect(auth._safe_next(request.args.get('next')))
    return render_template('login.html')


@app.route('/api/state')
def api_state():
    scan = store.latest_scan()
    try:
        u = universe.load()
        uni = {'built_at': u.get('built_at'), 'failed': u.get('failed', []),
               'options': universe.options(u)}
    except (OSError, ValueError):
        uni = None
    return jsonify(safe({
        'now': data.now_ist().isoformat(timespec='seconds'),
        'market_open': data.market_open(),
        'mood': market_mood(),
        'scan_state': STATE['scan'],
        'watch_state': {k: v for k, v in STATE['watch'].items() if not k.startswith('_')},
        'settings': store.settings(),
        'indicator_specs': indicators.SETTINGS, 'indicator_settings': indicator_settings(),
        'telegram_configured': notify.configured(),
        'universe': uni,
        'steps': [{'key': k, 'label': v} for k, v in smc.STEPS],
        'rules': current_rules().as_dict(),
        'env_rules': ENV_RULES.as_dict(),
        'rule_overrides': saved_overrides(),
        'tunable': smc.TUNABLE,
        'tunable_order': list(smc.TUNABLE),        # jsonify sorts keys; the tuner wants protocol order
        'scan': scan,
        'candidates': store.candidates(scan['id']) if scan else [],
        # Tables page in the browser, so send their history rather than the newest few.
        'zones': store.recent_zones(300),
        'alerts': store.alerts(300),
        'signals': store.signals(300),
        'market_data': dhan.status(),
        'patterns': patterns.PATTERNS,
        'swing_trades': swing_trades_state(),
        'intraday': intraday_state(),
        'options': {'open': len(store.ideas(status='open')), 'state': {k: v for k, v in STATE['options'].items()
                                                                       if not k.startswith('_')}},
    }))


def swing_trades_state() -> dict:
    """The swing paper-trade journal and its totals, for the dashboard."""
    # Newest entry first: a zone set early can trigger late, so not in zone order.
    journal = sorted((live_zone(z) for z in store.swing_trades(limit=500)), key=lambda z: z['trigger'].get('choch_time') or '',
                     reverse=True)
    closed = [z for z in journal if z['status'] in ('won', 'lost')]
    rs = [z['trigger'].get('r') or 0 for z in closed]
    return {'journal': journal,
            'stats': {'open': sum(z['status'] == 'triggered' for z in journal),
                      'open_r': round(sum(z['trigger'].get('r_open') or 0
                                          for z in journal if z['status'] == 'triggered'), 2),
                      'trades': len(rs), 'wins': sum(1 for x in rs if x > 0), 'r': round(sum(rs), 2)}}


@app.route('/api/scan', methods=['POST'])
def api_scan():
    body = request.get_json(silent=True) or {}
    which = body.get('universe')
    if which is not None and not universe.valid(which):
        return jsonify({'error': f'universe must be one of {list(universe.UNIVERSES)}'}), 400
    if which:
        store.set_settings({'universe': which})
    if not start_scan_thread('manual', which):
        return jsonify({'error': 'a scan is already running'}), 409
    return jsonify({'status': 'started'})


@app.route('/api/scan/stop', methods=['POST'])
def api_scan_stop():
    if not STATE['scan']['running']:
        return jsonify({'status': 'idle'}), 409
    CANCEL.set()
    return jsonify({'status': 'stopping'})


@app.route('/api/watch/check', methods=['POST'])
def api_watch_check():
    return jsonify(check_zones('manual'))


@app.route('/api/settings', methods=['POST'])
def api_settings():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'send the settings as a JSON object'}), 400
    for k in ('universe', 'intraday_universe', 'lab_swing_universe', 'lab_intraday_universe'):
        if k in body and not (k.startswith('lab_') and body[k] == '') and not universe.valid(body[k]):
            return jsonify({'error': 'unknown universe'}), 400
    for k in ('lab_years_swing', 'lab_years_intraday'):
        if k in body:
            try:
                if not 0.5 <= float(body[k]) <= 5:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({'error': f'{k} must be 0.5 to 5 years'}), 400
    if 'scan_time' in body:
        try:
            datetime.strptime(str(body['scan_time']), '%H:%M')
        except ValueError:
            return jsonify({'error': 'scan_time must be HH:MM'}), 400
    if 'indicators' in body:
        if not isinstance(body['indicators'], dict):
            return jsonify({'error': 'indicators must be an object'}), 400
        # Stored as given back by settings_from: only known keys, every value clamped to its range.
        body = {**body, 'indicators': indicators.settings_from(body['indicators'])}
    return jsonify(store.set_settings(body))


def _dismiss(zone_id, mode):
    """Stop watching an open zone of this mode. 404 for an unknown zone, one
    of the other mode, or one that has already triggered or closed."""
    z = store.get_zone(zone_id)
    if not z or (z.get('mode') or 'swing') != mode:
        return jsonify({'error': 'no such zone'}), 404
    if z['status'] not in ('watching', 'tapped'):
        return jsonify({'error': f"zone is {z['status']}, not open"}), 409
    store.update_zone(zone_id, status='dismissed', note='dismissed by you')
    return jsonify({'status': 'dismissed'})


@app.route('/api/zones/<int:zone_id>/dismiss', methods=['POST'])
def api_dismiss(zone_id):
    return _dismiss(zone_id, 'swing')


def _rules_from_request() -> smc.Rules:
    """?rules=<json> lets the tuner open a stock under its what-if rules."""
    raw = request.args.get('rules')
    if not raw:
        return current_rules()
    try:
        return ENV_RULES.with_overrides(json.loads(raw))
    except ValueError:
        return current_rules()


@app.route('/api/stock/<symbol>')
def api_stock(symbol):
    """Daily candles plus a fresh analysis, for the chart view."""
    sym = symbol.upper().split('.')[0]
    ticker = sym + '.NS'
    # Fresh candles (10 minutes during the session; after it, only ones fetched
    # once the close settled), else whatever is cached if the download fails.
    frame = data.daily([ticker]).get(ticker)
    if frame is None or frame.empty:
        return jsonify({'error': f'no data for {sym} on NSE'}), 404
    member = next((m for m in universe.load().get('stocks', []) if m['symbol'] == sym), None)
    analysis = smc.evaluate_both(frame, _rules_from_request(), shorts=store.settings()['shorts'] == '1',
                                 short_block=short_block(member or {}))
    try:
        count = max(30, min(260, int(request.args.get('bars', 140))))
    except ValueError:
        count = 140
    full = smc.clean(frame)
    tail = full.tail(count)
    offset = len(full) - len(tail)
    bars = [{'d': str(i.date()), 'o': round(float(r.Open), 2), 'h': round(float(r.High), 2),
             'l': round(float(r.Low), 2), 'c': round(float(r.Close), 2)}
            for i, r in tail.iterrows()]
    zone = analysis.get('zone')
    daily_patterns = [p for p in patterns.detect(full, zone=zone, side=analysis['side'])
                      if p['index'] >= offset]
    return jsonify(safe({'symbol': sym, 'name': member['name'] if member else None,
                         'in_universe': member is not None, 'patterns': daily_patterns,
                         'bars': bars, 'offset': offset, 'analysis': analysis,
                         'ind': chart_indicators(full, len(tail))}))


def indicator_settings() -> dict:
    """The saved chart indicator settings, made safe (indicators.settings_from)."""
    try:
        return indicators.settings_from(json.loads(store.settings().get('indicators') or '{}'))
    except ValueError:
        return indicators.settings_from({})


def chart_indicators(frame, shown: int, minutes=None, fine=None) -> dict:
    """Delta, VWAP, ORB and Bollinger Bands for the last `shown` candles of
    `frame`. Computed over the whole frame first, so the bands are warmed up
    and VWAP / cumulative delta start at each session's open."""
    try:
        ind = indicators.for_bars(frame, minutes, fine, intraday=minutes is not None, settings=indicator_settings())
        return indicators.tail(ind, shown)
    except Exception as e:                       # indicators never break a chart
        logger.warning(f'indicators failed: {e}')
        return {}


def _fine(ticker):
    """1-minute candles for the delta estimate; None if unavailable."""
    try:
        return data.intraday_cached([ticker], 1).get(ticker)
    except Exception as e:
        logger.warning(f'1m candles for {ticker} failed: {e}')
        return None


TRIGGERED_SHOWN_DAYS = 14       # a swing trigger stays on the board and charts this long


def _live_zone(z, now: datetime) -> bool:
    """Open, an open paper trade, or a trigger from before paper trading within
    TRIGGERED_SHOWN_DAYS (those were never followed to an exit, so without a
    limit they would stay on the live board, fetched every minute, for good)."""
    if z['status'] in OPEN:
        return True
    if z['status'] != 'triggered':
        return False
    if store.is_paper(z):
        return True
    try:
        when = datetime.fromisoformat(z.get('last_checked') or z.get('created_at') or '')
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=data.IST)
    return now - when < timedelta(days=TRIGGERED_SHOWN_DAYS)


def _open_zone(sym):
    now = data.now_ist()
    live = [z for z in store.recent_zones(200) if z['symbol'] == sym and _live_zone(z, now)]
    # A zone being watched now beats an older one that already triggered.
    return next((z for z in live if z['status'] in OPEN), live[0] if live else None)


@app.route('/api/intraday/<symbol>')
def api_intraday(symbol):
    """The last few sessions of 15m candles with their patterns, for the chart."""
    sym = symbol.upper().split('.')[0]
    frame = data.intraday_cached([sym + '.NS']).get(sym + '.NS')
    if frame is None or frame.empty:
        return jsonify({'error': f'no 15m data for {sym}'}), 404
    df = latest_sessions(frame, 3)
    z = _open_zone(sym)
    zone = {'low': z['zone_low'], 'high': z['zone_high']} if z else None
    done = patterns.completed(df, data.now_ist())
    bars = [{'d': i.strftime('%m-%d %H:%M'), 'day': str(i.date()),
             'o': round(float(r.Open), 2), 'h': round(float(r.High), 2),
             'l': round(float(r.Low), 2), 'c': round(float(r.Close), 2),
             'live': i not in done.index}
            for i, r in df.iterrows()]
    full = smc.clean(frame)
    return jsonify(safe({'symbol': sym, 'bars': bars, 'zone': z,
                         'patterns': patterns.detect(done, zone=zone, side=(z or {}).get('side') or 'long'),
                         'ind': chart_indicators(full, len(df), 15, _fine(sym + '.NS'))}))


@app.route('/api/board')
def api_board():
    """
    The live board: every watched stock's price today against its zone.
    15m candles from yfinance, reused for a minute; the last candle may still
    be forming, which is what makes its close the current price.
    """
    now = data.now_ist()
    zones = [z for z in store.recent_zones(200) if _live_zone(z, now)]
    seen, picked = set(), []
    for z in zones:                                  # one card per stock
        if z['symbol'] not in seen:
            seen.add(z['symbol'])
            picked.append(z)
    tickers = [z['symbol'] + '.NS' for z in picked]
    frames = data.intraday_cached(tickers)
    live = data.live_quotes(tickers)               # {} unless Dhan is connected
    cards = []
    for z in picked:
        frame = frames.get(z['symbol'] + '.NS')
        if frame is None or frame.empty:
            cards.append({'symbol': z['symbol'], 'name': z['name'], 'status': z['status'], 'error': 'no data'})
            continue
        df = latest_sessions(frame, 2)
        day = df.index[-1].date()
        today = df[[d == day for d in df.index.date]]
        before = df[[d < day for d in df.index.date]]
        last = float(today['Close'].iloc[-1])
        prev = float(before['Close'].iloc[-1]) if len(before) else None
        day_high, day_low = float(today['High'].max()), float(today['Low'].min())
        q = live.get(z['symbol'] + '.NS')
        if q:                                        # real-time price beats the last candle's close
            last = q['last']
            prev = q.get('prev_close') or prev
            day_high = max(day_high, q.get('high') or day_high, last)
            day_low = min(day_low, q.get('low') or day_low, last)
        lo, hi = z['zone_low'], z['zone_high']
        where = 'in' if lo <= last <= hi else 'above' if last > hi else 'below'
        t = z.get('trigger') or {}
        trade = None
        if z['status'] == 'triggered' and t.get('paper') and t.get('entry'):
            sign = -1 if t.get('side') == 'short' else 1
            risk = (t['entry'] - t['stop']) * sign
            trade = {'entry': t['entry'], 'stop': t['stop'], 'target': t['target'],
                     'r': (last - t['entry']) * sign / risk if risk > 0 else None}
        done = patterns.completed(df, now)
        pats = [p for p in patterns.detect(done, zone={'low': lo, 'high': hi}, side=z.get('side') or 'long')
                if pd_date(p['time']) == day]
        cards.append({
            'symbol': z['symbol'], 'name': z['name'], 'status': z['status'], 'source': z.get('source'),
            'side': z.get('side') or 'long',
            'last': last, 'prev_close': prev,
            'change_pct': (last - prev) / prev * 100 if prev else None,
            'day_high': day_high, 'day_low': day_low, 'live_price': bool(q),
            'as_of': today.index[-1].isoformat(), 'session': str(day),
            'zone_low': lo, 'zone_high': hi, 'target': z['target'], 'where': where,
            'distance_pct': (last - hi) / last * 100 if where == 'above' else
                            (last - lo) / last * 100 if where == 'below' else 0.0,
            'bars': [[round(float(r.Open), 2), round(float(r.High), 2), round(float(r.Low), 2),
                      round(float(r.Close), 2)] for r in today.itertuples()],
            'patterns': pats, 'trade': trade,
        })
    order = {'triggered': 0, 'tapped': 1, 'watching': 2}
    cards.sort(key=lambda c: (order.get(c['status'], 3), abs(c.get('distance_pct') or 99)))
    # Today's news per card (the news desk), for its badge.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
    counts = store.news_counts([c['symbol'] for c in cards], midnight)
    for c in cards:
        c['news'] = counts.get(c['symbol'], {'n': 0, 'filings': 0})
    return jsonify(safe({'now': now.isoformat(timespec='seconds'), 'market_open': data.market_open(),
                         'source': 'dhan' if live else 'yfinance', 'cards': cards}))


@app.route('/api/news')
def api_news():
    """The news desk: NSE filings and media headlines, newest first. Every followed stock by
    default; ?symbol=X for one stock (any stock: the drawer asks); ?kind=filing|media;
    ?days=1-45 (default 7)."""
    sym = (request.args.get('symbol') or '').upper().split('.')[0] or None
    if sym and not SYMBOL_RE.match(sym):
        return jsonify({'error': 'bad symbol'}), 400
    kind = request.args.get('kind') if request.args.get('kind') in ('filing', 'media') else None
    days = max(1, min(request.args.get('days', 7, type=int) or 7, 45))
    since = (data.now_ist() - timedelta(days=days)).isoformat(timespec='seconds')
    watch = news_watch()
    why = {w['symbol']: w['why'] for w in watch}
    items = store.news([sym] if sym else [w['symbol'] for w in watch], kind, since, limit=800)
    hidden = {}
    if not sym:
        # Across every stock, one much-covered name (Vedanta: 100 stories a week) must not bury the
        # rest: its newest NEWS_PER_STOCK headlines stay; filings always do. Its own view has them all.
        kept, seen = [], {}
        for i in items:
            if i['kind'] == 'media':
                seen[i['symbol']] = seen.get(i['symbol'], 0) + 1
                if seen[i['symbol']] > NEWS_PER_STOCK:
                    hidden[i['symbol']] = hidden.get(i['symbol'], 0) + 1
                    continue
            kept.append(i)
        items = kept
    fetched = NEWS_STATE['fetched'].get(sym) if sym else None
    midnight = data.now_ist().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
    today = store.news_counts([w['symbol'] for w in watch], midnight).values()
    day_ago = (data.now_ist() - timedelta(hours=24)).isoformat(timespec='seconds')
    tones = store.news_counts([sym] if sym else [w['symbol'] for w in watch], day_ago)
    return jsonify(safe({
        'today': {'n': sum(c['n'] for c in today), 'filings': sum(c['filings'] for c in today)},
        'tones': tones, 'impact': news_impact(30),
        'items': [{**i, 'why': why.get(i['symbol'])} for i in items[:400]], 'watch': watch, 'days': days,
        'hidden': hidden,
        'enabled': store.settings().get('news') == '1',
        'fetched_at': datetime.fromtimestamp(fetched).isoformat(timespec='seconds') if fetched else None,
        'state': {k: NEWS_STATE[k] for k in ('running', 'last_pass', 'last_full', 'message', 'errors')},
    }))


def _demo_view() -> dict:
    positions = live_demo(store.demo_positions())
    acct = demo_account(positions=positions)
    by_mode = {}
    for m in DEMO_MODES:
        c = [p for p in positions if p['mode'] == m and p['status'] == 'closed']
        o = [p for p in positions if p['mode'] == m and p['status'] == 'open']
        by_mode[m] = {'closed': len(c), 'wins': sum(1 for p in c if (p['net'] or 0) > 0),
                      'net': round(sum(p['net'] or 0 for p in c), 2), 'charges': round(sum(p['charges'] or 0 for p in c), 2),
                      'open': len(o), 'unreal': round(sum(p['unreal'] or 0 for p in o), 2),
                      'skipped': sum(1 for p in positions if p['mode'] == m and p['status'] == 'skipped')}
    # The balance after each deposit, withdrawal and closed trade, oldest first.
    events = [{'ts': x['ts'], 'amount': x['amount'] if x['kind'] == 'deposit' else -x['amount'], 'kind': x['kind'],
               'label': x.get('note') or ''} for x in store.demo_ledger()]
    events += [{'ts': p['exit_time'], 'amount': p['net'] or 0, 'kind': 'trade', 'label': p['instrument'], 'mode': p['mode'],
                'symbol': p['symbol'], 'outcome': p.get('note') or ''} for p in positions if p['status'] == 'closed']
    curve, run = [], 0.0
    for e in sorted(events, key=lambda e: (_when(e['ts']) or data.now_ist())):
        run += e['amount']
        curve.append({**e, 'amount': round(e['amount'], 2), 'balance': round(run, 2)})
    statement = demo_statement(positions, store.demo_ledger())
    # What the charges were, line by line, over every closed trade.
    paid = {k: 0.0 for k in demo.CHARGE_KEYS}
    rolls = 0
    for p in positions:
        det = p['status'] == 'closed' and p.get('charges_detail') or {}
        for k in demo.CHARGE_KEYS:
            paid[k] += det.get(k) or 0
        rolls += det.get('rollovers') or 0
    paid = {k: round(v, 2) for k, v in paid.items()}
    return {'settings': demo_settings(), 'since': store.settings().get('demo_since') or None, 'account': acct,
            'by_mode': by_mode, 'curve': curve, 'ledger': statement['lines'][::-1],
            'statement': statement['summary'], 'first_trade': demo_first_trade(),
            'charges': {'lines': paid, 'total': round(sum(paid.values()), 2), 'rollovers': rolls,
                        'schedule': demo.SCHEDULE_AS_OF},
            'open': [{**p, 'lp': _pos_key(p)} for p in positions if p['status'] == 'open'],
            # Newest first; the tab pages them.
            'closed': sorted((p for p in positions if p['status'] == 'closed'), key=lambda p: _when(p['exit_time']) or data.now_ist(), reverse=True),
            'skipped': sorted((p for p in positions if p['status'] == 'skipped'), key=lambda p: _when(p['entry_time']) or data.now_ist(), reverse=True),
            'products': demo.PRODUCTS, 'live': _demo_live_state()}


def _demo_live_state() -> dict:
    on = data.market_open() and dhan.available()
    return {'on': on, 'every_ms': 1000 if on else 0, 'error': charts.FEED.error if on else None}


def demo_live() -> dict:
    """The open positions' latest prices and the account totals they move."""
    positions = live_demo(store.demo_positions())
    opn = [p for p in positions if p['status'] == 'open']
    by_mode = {m: {'open': sum(1 for p in opn if p['mode'] == m),
                   'unreal': round(sum(p['unreal'] or 0 for p in opn if p['mode'] == m), 2)} for m in DEMO_MODES}
    return {'live': _demo_live_state(), 'account': demo_account(positions=positions), 'by_mode': by_mode,
            'open': [{'id': p['id'], 'last': p['last'], 'unreal': p['unreal'], 'live_at': p.get('live_at')} for p in opn]}


@app.route('/api/demo/live')
def api_demo_live():
    """The Demo tab's quick refresh (every second in session, when the page has no WebSocket: the
    socket's demo channel pushes the same); /api/demo has everything else."""
    return jsonify(safe({'now': time.time(), **demo_live()}))


@app.route('/api/demo')
def api_demo():
    """The demo account: balance, equity, margin in use, positions open and closed (with charges),
    trades skipped and why, the ledger, P&L by mode, the balance curve, the settings."""
    return jsonify(safe(_demo_view()))


@app.route('/api/demo/funds', methods=['POST'])
def api_demo_funds():
    """Add or withdraw demo money: {"kind": "deposit" | "withdraw", "amount": 100000, "note": ""}.
    The first deposit starts the account: trades from then on are taken."""
    body = request.get_json(silent=True) or {}
    kind = body.get('kind')
    try:
        amount = round(float(body.get('amount')), 2)
    except (TypeError, ValueError):
        return jsonify({'error': 'amount must be a number'}), 400
    if kind not in ('deposit', 'withdraw') or not (0 < amount <= 1e10):
        return jsonify({'error': 'send kind deposit or withdraw and an amount above 0'}), 400
    if kind == 'withdraw':
        free = demo_account()['free']
        if amount > free + 1e-6:
            return jsonify({'error': f'only ₹{free:,.2f} is free to withdraw (the rest is in open positions)'}), 409
    if not store.settings().get('demo_since'):
        store.set_settings({'demo_since': data.now_ist().isoformat(timespec='seconds')})
    store.add_demo_ledger(kind, amount, str(body.get('note') or '')[:200], data.now_ist().isoformat(timespec='seconds'))
    return jsonify(safe(_demo_view()))


@app.route('/api/demo/settings', methods=['POST'])
def api_demo_settings():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'send the settings as an object'}), 400
    st = demo.settings_from({**demo_settings(), **body})
    store.set_settings({'demo': st})
    return jsonify(safe(_demo_view()))


@app.route('/api/demo/reset', methods=['POST'])
def api_demo_reset():
    """Start over: every position and ledger row goes. {"amount": N} opens the new account with N;
    {"since": "now" | "first" | "YYYY-MM-DD"} is where it starts counting trades: now (the default),
    the app's first trade, or that day's open; a backdated account is replayed at once."""
    body = request.get_json(silent=True) or {}
    if body.get('confirm') is not True:
        return jsonify({'error': 'send {"confirm": true} to wipe the demo account'}), 400
    try:
        amount = round(float(body.get('amount') or 0), 2)
    except (TypeError, ValueError):
        return jsonify({'error': 'amount must be a number'}), 400
    since = str(body.get('since') or 'now')
    if since == 'now':
        start = data.now_ist().isoformat(timespec='seconds')
    elif since == 'first':                    # the opening of the first trade's day
        first = _when(demo_first_trade()) or data.now_ist()
        start = min(first, first.replace(hour=9, minute=15, second=0, microsecond=0)).isoformat(timespec='seconds')
    else:
        try:
            day = date.fromisoformat(since)
        except ValueError:
            return jsonify({'error': 'since must be now, first or a date (YYYY-MM-DD)'}), 400
        if day > data.now_ist().date():
            return jsonify({'error': 'since cannot be in the future'}), 400
        start = datetime.combine(day, dtime(9, 15), tzinfo=data.now_ist().tzinfo).isoformat(timespec='seconds')
    with DEMO_LOCK:
        store.clear_demo()
        store.set_settings({'demo_since': start if amount > 0 else ''})
        if amount > 0:
            store.add_demo_ledger('deposit', amount, str(body.get('note') or '')[:200], start)
    if amount > 0:
        demo_sync()
    return jsonify(safe(_demo_view()))


@app.route('/api/demo/statement.csv')
def api_demo_statement_csv():
    """The whole statement as a spreadsheet: one row per line, oldest first, with each trade's
    charges broken down."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['date', 'time', 'type', 'description', 'detail', 'amount', 'balance', *demo.CHARGE_KEYS])
    names = {'opening': 'Opening balance', 'deposit': 'Funds added', 'withdraw': 'Funds withdrawn', 'pnl': 'Trade P&L', 'charges': 'Charges'}
    for x in demo_statement()['lines']:
        at = _when(x['ts'])
        ch = x.get('charges') or {}
        w.writerow([at.strftime('%Y-%m-%d') if at else '', at.strftime('%H:%M') if at else '', names[x['kind']], x['title'],
                    x['detail'], f"{x['amount']:.2f}", f"{x['balance']:.2f}",
                    *[f"{ch[k]:.2f}" if x['kind'] == 'charges' and k in ch else '' for k in demo.CHARGE_KEYS]])
    name = f"niftywhale-demo-statement-{data.now_ist():%Y-%m-%d}.csv"
    return app.response_class(buf.getvalue(), mimetype='text/csv',
                              headers={'Content-Disposition': f'attachment; filename="{name}"'})


@app.route('/api/demo/sync', methods=['POST'])
def api_demo_sync():
    return jsonify(safe({'sync': demo_sync(), **_demo_view()}))


@app.route('/api/smart')
def api_smart():
    """The Smart money tab: FII/DII cash flows, participant positioning in index derivatives,
    the followed stocks' delivery and deals, and the latest bulk / block deals."""
    fl = store.flows(30)
    by_day = {}
    for r in fl:
        by_day.setdefault(r['day'], {})[r['category']] = r
    days = sorted(by_day)

    def total(cat, n):
        v = [by_day[d][cat]['net'] for d in days[-n:] if cat in by_day[d] and by_day[d][cat]['net'] is not None]
        return {'sum': round(sum(v), 2), 'days': len(v)} if v else None
    watch = news_watch()
    symbols = [w['symbol'] for w in watch]
    hist = store.delivery_history(symbols, smart.DELIV_SESSIONS + 1)
    since = (data.now_ist().date() - timedelta(days=SMART_DEAL_DAYS)).isoformat()
    ds = smart.deal_summary(store.deals(symbols, since=since, limit=2000))
    stocks = [{**w, 'delivery': smart.delivery_read(hist.get(w['symbol'], [])), 'deals': ds.get(w['symbol'])}
              for w in watch]
    recent = store.deals(None, since=(data.now_ist().date() - timedelta(days=10)).isoformat(), limit=2000)  # the tab pages them
    followed = set(symbols)
    pos = smart.positioning(store.poi(SMART_BACKFILL + 15))
    return jsonify(safe({
        'flows': {'days': [{'day': d, **{c: by_day[d].get(c) for c in ('FII', 'DII')}} for d in days],
                  'latest': by_day[days[-1]] if days else None,
                  'sum5': {c: total(c, 5) for c in ('FII', 'DII')}, 'sum20': {c: total(c, 20) for c in ('FII', 'DII')}},
        'positioning': {**pos, 'stance': smart.stance(((pos.get('latest') or {}).get('FII') or {}).get('long_pct'))},
        'stocks': stocks,
        'deals': [{**d, 'followed': d['symbol'] in followed} for d in recent],
        'held': {k: len(store.smart_days(t)) for k, t in (('delivery', 'sm_delivery'), ('positioning', 'sm_poi'),
                                                           ('flows', 'sm_flows'), ('deals', 'sm_deals'))},
        'state': dict(SMART_STATE),
    }))


@app.route('/api/smart/stock/<symbol>')
def api_smart_stock(symbol):
    sym = symbol.upper().split('.')[0]
    if not SYMBOL_RE.match(sym):
        return jsonify({'error': 'bad symbol'}), 400
    return jsonify(safe({'symbol': sym, **smart_stock(sym)}))


@app.route('/api/smart/refresh', methods=['POST'])
def api_smart_refresh():
    if SMART_STATE['running']:
        return jsonify({'status': 'busy'}), 409
    threading.Thread(target=smart_pass, daemon=True, name='smart-manual').start()
    return jsonify({'status': 'started'})


@app.route('/api/news/refresh', methods=['POST'])
def api_news_refresh():
    """Fetch now: one stock ({"symbol": "X"}, at most every 5 minutes, answered when done)
    or every followed stock (in the background)."""
    body = request.get_json(silent=True) or {}
    sym = str(body.get('symbol') or '').upper().split('.')[0]
    if sym:
        if not SYMBOL_RE.match(sym):
            return jsonify({'error': 'bad symbol'}), 400
        if time.time() - NEWS_STATE['fetched'].get(sym, 0) < NEWS_ONE_GAP_S:
            return jsonify({'status': 'fresh', 'symbol': sym})
        name = _names().get(sym) or next((w['name'] for w in news_watch() if w['symbol'] == sym), sym)
        r = news_pass(full=False, only=[{'symbol': sym, 'name': name, 'why': 'drawer'}])
        return (jsonify({'status': 'busy', **r}), 409) if 'skipped' in r else jsonify({'status': 'done', **r})
    if NEWS_STATE['running']:
        return jsonify({'status': 'busy'}), 409
    threading.Thread(target=news_pass, kwargs={'full': True}, daemon=True, name='news-manual').start()
    return jsonify({'status': 'started'})


@app.route('/api/dhan/token', methods=['POST'])
def api_dhan_token():
    """Connect Dhan with a token from web.dhan.co. Checked against the profile
    endpoint before it is kept; never sent back to the browser."""
    body = request.get_json(silent=True) or {}
    tok = str(body.get('access_token') or '').strip()
    cid = str(body.get('client_id') or '').strip() or None
    if len(tok) < 20:
        return jsonify({'error': 'paste the access token from web.dhan.co → My Profile → Access DhanHQ APIs'}), 400
    previous = (store.get_kv(dhan.KV_TOKEN), store.get_kv(dhan.KV_EXPIRY), store.get_kv(dhan.KV_CLIENT))
    dhan.save_token(tok, cid)
    if not dhan.profile(force=True):
        # Put back whatever we had: a typo must not disconnect a working token,
        # nor leave it paired with the client ID that came with the typo.
        if previous[0]:
            store.set_kv(dhan.KV_TOKEN, previous[0])
            store.set_kv(dhan.KV_EXPIRY, previous[1] or '')
        else:
            dhan.forget_token()
        store.set_kv(dhan.KV_CLIENT, previous[2] or '')
        dhan._state.update(profile=None, profile_at=0.0)       # re-check the restored token next time
        return jsonify({'error': 'Dhan rejected that token (expired, revoked, or mistyped)'}), 400
    logger.info('Dhan connected with a pasted token')
    return jsonify(dhan.status())


@app.route('/api/dhan/disconnect', methods=['POST'])
def api_dhan_disconnect():
    dhan.forget_token()
    logger.info('Dhan disconnected; back to yfinance')
    return jsonify(dhan.status())


@app.route('/api/intraday/scan', methods=['POST'])
def api_intraday_scan():
    """Scan now, in the background. Outside the session it is a preview: no zones."""
    if STATE['intraday']['scanning']:
        return jsonify({'error': 'an intraday scan is already running'}), 409
    start_intraday_scan_thread('manual')
    return jsonify({'status': 'started'})


@app.route('/api/intraday/check', methods=['POST'])
def api_intraday_check():
    return jsonify(check_intraday('manual'))


@app.route('/api/intraday/chart/<symbol>')
def api_intraday_chart(symbol):
    """15m (three sessions) and 5m (latest session) candles, today's zone, and
    a fresh 15m analysis, for the intraday stock panel."""
    sym = symbol.upper().split('.')[0]
    t = sym + '.NS'
    now = data.now_ist()
    f15 = data.intraday_cached([t], 15).get(t)
    if f15 is None or f15.empty:
        return jsonify({'error': f'no 15m data for {sym}'}), 404
    f5 = data.intraday_cached([t], 5).get(t)
    rules = irules()
    raw = request.args.get('rules')
    if raw:
        try:
            rules = ENV_INTRA.with_overrides(json.loads(raw))
        except ValueError:
            pass
    analysis = intraday.evaluate(f15, rules, now, shorts=store.settings()['intraday_shorts'] == '1')
    zone = next((z for z in store.intraday_zones(session=now.date().isoformat()) if z['symbol'] == sym), None)

    def bars(frame, minutes, sessions):
        df = intraday.last_sessions(smc.clean(frame), sessions)
        done = intraday.completed(df, now, minutes)
        return [{'d': i.strftime('%m-%d %H:%M'), 'day': str(i.date()), 't': i.isoformat(),
                 'o': round(float(r.Open), 2), 'h': round(float(r.High), 2),
                 'l': round(float(r.Low), 2), 'c': round(float(r.Close), 2),
                 'live': i not in done.index} for i, r in df.iterrows()]
    member = next((m for m in universe.load().get('stocks', []) if m['symbol'] == sym), None)
    m15 = bars(f15, 15, 3)
    m5 = bars(f5, 5, 1) if f5 is not None and not f5.empty else []
    fine = _fine(t)
    ind = {'m15': chart_indicators(smc.clean(f15), len(m15), 15, fine),
           'm5': chart_indicators(smc.clean(f5), len(m5), 5, fine) if m5 else {}}
    return jsonify(safe({'symbol': sym, 'name': member['name'] if member else sym,
                         'm15': m15, 'm5': m5, 'ind': ind,
                         'analysis': analysis, 'zone': zone, 'rules': rules.as_dict()}))


@app.route('/api/intraday/whatif', methods=['POST'])
def api_intraday_whatif():
    """Re-screen the last intraday scan's 15m candles under candidate rules. Saves nothing."""
    body = request.get_json(silent=True) or {}
    rules = ENV_INTRA.with_overrides(body.get('rules') if isinstance(body, dict) else None)
    snap = LAST_INTRA                     # one scan's candles, even if another scan finishes meanwhile
    if not snap:
        return jsonify({'error': 'no intraday candles yet — run an intraday scan first (Scan now)'}), 409
    funnel, passed, results = intraday_screen(
        snap['stocks'], snap['dailies'], snap['frames'], rules, snap['now'],
        store.settings()['intraday_shorts'] == '1')
    drops = {}
    for r in results:
        if r.get('failed_at'):
            drops.setdefault(r['failed_at'], []).append({'symbol': r['symbol'], 'reason': r['reason']})
    return jsonify(safe({
        'universe': snap['universe'], 'universe_label': universe.label(snap['universe']),
        'as_of': snap['now'].isoformat(timespec='minutes'),
        'rules': rules.as_dict(), 'funnel': funnel, 'drops': drops,
        'setups': [{'symbol': p['symbol'], 'name': p['name'], 'in_zone': p['analysis']['in_zone'],
                    'side': p['analysis']['side'], 'score': p['analysis']['score'],
                    'rr': p['analysis']['plan']['rr'], 'position': p['analysis']['position'],
                    'close': p['analysis']['close'], 'distance_pct': p['analysis']['distance_pct'],
                    'target_label': p['analysis'].get('target_label'),
                    'zone': p['analysis']['zone']} for p in passed],
    }))


@app.route('/api/intraday/rules', methods=['POST'])
def api_intraday_rules():
    """Save intraday tuner values ({} resets to .env). Used from the next scan and check."""
    body = request.get_json(silent=True) or {}
    overrides = (body.get('rules') if isinstance(body, dict) else None) or {}
    if not isinstance(overrides, dict):
        return jsonify({'error': 'rules must be an object'}), 400
    applied = ENV_INTRA.with_overrides(overrides).as_dict()
    env = ENV_INTRA.as_dict()
    keep = {k: applied[k] for k in overrides if k in intraday.TUNABLE and applied[k] != env[k]}
    store.set_settings({'intraday_rules': keep})
    logger.info(f'Intraday rules saved: {keep or "reset to .env defaults"}')
    return jsonify({'rules': irules().as_dict(), 'overrides': keep})


@app.route('/api/intraday/zones/<int:zone_id>/dismiss', methods=['POST'])
def api_intraday_dismiss(zone_id):
    return _dismiss(zone_id, 'intraday')


@app.route('/api/search')
def api_search():
    """Universe stocks matching a symbol prefix or a word of the name."""
    q = (request.args.get('q') or '').strip().upper()
    if not q:
        return jsonify([])
    stocks = universe.load().get('stocks', [])
    starts = [s for s in stocks if s['symbol'].startswith(q)]
    contains = [s for s in stocks if s not in starts and (q in s['symbol'] or q in s['name'].upper())]
    out = [{'symbol': s['symbol'], 'name': s['name'],
            'tag': universe.tag(s)} for s in (starts + contains)[:10]]
    # Any NSE symbol can be analysed, universe or not.
    if SYMBOL_RE.match(q) and not any(o['symbol'] == q for o in out):
        out.append({'symbol': q, 'name': 'Not in the universe — analyse anyway', 'tag': 'NSE'})
    return jsonify(out)


@app.route('/api/palette')
def api_palette():
    """What the command palette searches, sent once: every universe stock (symbol, name, list
    tag, F&O or not, industry) and the option indices."""
    stocks = universe.load().get('stocks', [])
    # The last field breaks ties towards the better-known name: Nifty 50, then Nifty 100, then F&O.
    weight = lambda s: 3 if 'nifty50' in s.get('indices', []) else 2 if s.get('nifty100') else 1 if s.get('fo') else 0  # noqa: E731
    return jsonify({'stocks': [[s['symbol'], s.get('name') or '', universe.tag(s), bool(s.get('fo')), s.get('industry') or '', weight(s)]
                               for s in stocks],
                    'indices': [[k, v[2]] for k, v in options.INDICES.items()]})


@app.route('/api/funnel/<step>')
def api_funnel(step):
    """Which stocks dropped out at this step of the latest scan (?mode=intraday
    for the intraday one), and why."""
    scan = store.latest_scan(mode='intraday' if request.args.get('mode') == 'intraday' else 'swing')
    if not scan:
        return jsonify({'error': 'no scan yet'}), 404
    return jsonify({'scan_id': scan['id'], 'step': step, 'stocks': store.dropped_at(scan['id'], step)})


@app.route('/api/whatif', methods=['POST'])
def api_whatif():
    """Re-screen the cached candles under candidate rules. Saves nothing."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'send a JSON object'}), 400
    rules = ENV_RULES.with_overrides(body.get('rules'))
    which = body.get('universe') or store.settings()['universe']
    if not universe.valid(which):
        return jsonify({'error': 'unknown universe'}), 400
    stocks = universe.members(which)
    frames = data.frames_for([m['ticker'] for m in stocks])
    funnel, passed, results = screen(stocks, frames, rules, store.settings()['shorts'] == '1')
    drops = {}
    for r in results:
        if r.get('failed_at'):
            drops.setdefault(r['failed_at'], []).append({'symbol': r['symbol'], 'reason': r['reason']})
    return jsonify(safe({
        'universe': which, 'universe_label': universe.label(which),
        'rules': rules.as_dict(), 'funnel': funnel, 'drops': drops,
        'setups': [{'symbol': p['symbol'], 'name': p['name'], 'in_zone': p['analysis']['in_zone'],
                    'side': p['analysis']['side'],
                    'score': p['analysis']['score'], 'rr': p['analysis']['plan']['rr'],
                    'position': p['analysis']['position'], 'close': p['analysis']['close'],
                    'distance_pct': p['analysis']['distance_pct'],
                    'zone': p['analysis']['zone']} for p in passed],
    }))


@app.route('/api/rules', methods=['POST'])
def api_rules():
    """Save tuner values as the rules every scan and check uses ({} resets)."""
    body = request.get_json(silent=True) or {}
    overrides = (body.get('rules') if isinstance(body, dict) else None) or {}
    if not isinstance(overrides, dict):
        return jsonify({'error': 'rules must be an object'}), 400
    applied = ENV_RULES.with_overrides(overrides).as_dict()
    # Keep only what differs from .env, as the clamped, typed value.
    keep = {k: applied[k] for k in overrides if k in smc.TUNABLE and applied[k] != ENV_RULES.as_dict()[k]}
    store.set_settings({'rules': keep})
    logger.info(f'Rules saved: {keep or "reset to .env defaults"}')
    return jsonify({'rules': current_rules().as_dict(), 'overrides': keep})


SYMBOL_RE = re.compile(r'^[A-Z0-9][A-Z0-9&-]{0,19}$')      # NSE symbols: letters, digits, & and -


@app.route('/api/zones', methods=['POST'])
def api_zone_add():
    """Watch a zone you set yourself (Step 9 by hand)."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'send the zone as a JSON object'}), 400
    sym = str(body.get('symbol') or '').upper().split('.')[0]
    try:
        low, high, target = (float(body[k]) for k in ('zone_low', 'zone_high', 'target'))
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'zone_low, zone_high and target must be numbers'}), 400
    if not all(math.isfinite(v) for v in (low, high, target)):
        return jsonify({'error': 'zone_low, zone_high and target must be numbers'}), 400
    if not SYMBOL_RE.match(sym):                 # M&M and BAJAJ-AUTO are symbols too
        return jsonify({'error': 'symbol is required'}), 400
    if not (0 < low < high) or low <= target <= high or target <= 0:
        return jsonify({'error': 'need 0 < zone low < zone high, and a target above the zone (long) '
                                 'or below it (short)'}), 400
    member = next((m for m in universe.load().get('stocks', []) if m['symbol'] == sym), None)
    z = store.watch_manual(sym, member['name'] if member else sym, low, high, target)
    logger.info(f'Manual zone: {sym} {low}-{high} -> {target}')
    return jsonify(safe(z))


@app.route('/api/universe/refresh', methods=['POST'])
def api_universe_refresh():
    try:
        u = refresh_universe()
    except Exception as e:
        logger.warning(f'Universe refresh failed: {e}')
        return jsonify({'error': str(e)}), 502
    return jsonify({'built_at': u['built_at'], 'failed': u['failed'], 'stocks': len(u['stocks']),
                    'indices': len(universe.INDICES)})


def refresh_universe():
    """Re-download every list. An index that fails keeps its previous members."""
    try:
        previous = universe.load()
    except (OSError, ValueError):
        previous = None
    u = universe.build(previous)
    universe.save(u)
    logger.info(f"Universe refreshed: {len(u['stocks'])} stocks in {len(universe.INDICES)} indices"
                + (f"; kept old members for {', '.join(u['failed'])}" if u['failed'] else ''))
    return u


@app.route('/api/perf')
def api_perf():
    """Performance of one mode: live paper trades, or the latest backtest of the current rules."""
    mode = request.args.get('mode', 'swing')
    if mode not in autopilot.MODES:
        return jsonify({'error': 'mode must be swing, intraday or options'}), 400
    if request.args.get('source') == 'backtest':
        run = store.latest_run(mode, 'baseline')
        if not run:
            return jsonify({'source': 'backtest', 'mode': mode, 'run': None})
        extra = run.get('extra') or {}
        return jsonify(safe({'source': 'backtest', 'mode': mode, 'run': {k: run[k] for k in (
            'id', 'created', 'params', 'period_from', 'period_to', 'universe')}, 'summary': run['summary'],
            'breakdown': run['breakdown'], 'equity': run['equity'], 'highlights': extra.get('highlights', []),
            'by_symbol': extra.get('by_symbol'), 'symbols': extra.get('symbols')}))
    trades = store.live_trades(mode)
    bd = perf.breakdown(trades)
    return jsonify(safe({'source': 'live', 'mode': mode, 'summary': perf.summary(trades), 'week': perf.by_period(trades, 7),
                         'breakdown': bd, 'equity': perf.equity(trades), 'highlights': perf.highlights(bd),
                         'recent': trades[-25:][::-1],
                         # The open trades' feed symbols, so the curve's "now" point follows live prices.
                         'open_live': [{'lp': _live_keys.get(t['ref'], '') if mode == 'options' else t['symbol'],
                                        'entry': t['entry'], 'stop': t['stop'], 'side': 'long' if mode == 'options' else t['side'],
                                        'r_open': t.get('r_open')}
                                       for t in trades if t['status'] == 'open' and t.get('stop') is not None]}))


_COVERAGE = {'at': 0.0, 'value': None}


@app.route('/api/lab')
def api_lab():
    """The lab and the autopilot: jobs, history on disk, each mode's autopilot state, the change log."""
    from niftywhale import lab
    if time.time() - _COVERAGE['at'] > 120:
        try:
            _COVERAGE.update(at=time.time(), value=history.coverage())
        except Exception as e:
            _COVERAGE.update(at=time.time(), value={'error': str(e)})
    st = store.settings()
    modes = {}
    for m in autopilot.MODES:
        a = lab.state(m)
        try:
            current = lab.current_thresholds(m)
        except Exception:
            current = {}
        modes[m] = {**a, 'current': current, 'specs': lab.specs(m), 'paused': st.get(f'paused_{m}') == '1'}
    return jsonify(safe({
        'jobs': [lab.job_view(j) for j in store.jobs(12)], 'coverage': _COVERAGE['value'], 'modes': modes, 'log': store.autopilot_log(40),
        'policy': lab.policy(), 'last_report': store.get_json('lab:last_report'),
        'settings': {**{k: st[k] for k in ('lab_swing_universe', 'lab_intraday_universe', 'lab_years_swing',
                                           'lab_years_intraday', 'weekly_report')},
                     'swing_universe': lab.universe_key('swing'), 'intraday_universe': lab.universe_key('intraday')},
        'candidates': store.count_candidates(), 'upcoming': lab.next_runs(),
    }))


@app.route('/api/lab/job', methods=['POST'])
def api_lab_job():
    body = request.get_json(silent=True) or {}
    kind = body.get('kind') if isinstance(body, dict) else None
    if kind not in ('backtest', 'tune', 'report', 'nightly'):
        return jsonify({'error': 'kind must be backtest, tune, report or nightly'}), 400
    args = {}
    if kind == 'backtest':
        args['download'] = body.get('download', True) is not False
    if kind == 'tune' and isinstance(body.get('modes'), list):
        args['modes'] = [m for m in body['modes'] if m in autopilot.MODES]
    return jsonify({'job': store.add_job(kind, args)})


def _ap_mode(body):
    mode = body.get('mode') if isinstance(body, dict) else None
    return mode if mode in autopilot.MODES else None


@app.route('/api/autopilot/policy', methods=['POST'])
def api_autopilot_policy():
    from niftywhale import lab
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'send the policy as a JSON object'}), 400
    current = json.loads(store.settings().get('autopilot_policy') or '{}')
    merged = autopilot.policy({**current, **body})
    # Keep only what differs from the defaults.
    saved = {k: v for k, v in merged.items() if v != autopilot.DEFAULT_POLICY.get(k)}
    limits = body.get('limits')
    if isinstance(limits, dict):
        clean = {}
        for m, ps in limits.items():
            if m in autopilot.MODES and isinstance(ps, dict):
                sp = lab.specs(m)
                clean[m] = {p: [float(v[0]), float(v[1])] for p, v in ps.items()
                            if p in sp and isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)
                            and v[0] <= v[1]}
        saved['limits'] = clean
    store.set_settings({'autopilot_policy': saved})
    store.log_autopilot('all', 'policy', current, saved, 'changed on the Performance tab', 'you')
    return jsonify({'policy': lab.policy()})


@app.route('/api/autopilot/<action>', methods=['POST'])
def api_autopilot_action(action):
    from niftywhale import lab
    body = request.get_json(silent=True) or {}
    mode = _ap_mode(body)
    if not mode:
        return jsonify({'error': 'mode must be swing, intraday or options'}), 400
    st = lab.state(mode)
    if action == 'approve':
        aw = st.get('awaiting') or st.get('challenger')
        if not aw:
            return jsonify({'error': 'nothing to approve'}), 409
        lab.apply_thresholds(mode, aw['params'], f"approved by you: {aw.get('evidence', '')}", by='you', base=aw.get('base'))
    elif action == 'reject':
        if not (st.get('awaiting') or st.get('challenger')):
            return jsonify({'error': 'nothing to reject'}), 409
        prop = st.pop('awaiting', None) or st.pop('challenger', None)
        lab.save_state(mode, st)
        store.log_autopilot(mode, 'reject', None, prop.get('params'), 'rejected on the Performance tab', 'you')
    elif action == 'undo':
        lc = st.get('last_change')
        if not lc or lc.get('reverted'):
            return jsonify({'error': 'no change to undo'}), 409
        lab.revert_change(mode, lc)
        lc['reverted'] = data.now_ist().isoformat(timespec='seconds')
        lab.save_state(mode, st)
        store.log_autopilot(mode, 'undo', {k: a for k, (_, a) in (lc.get('changed') or {}).items()},
                            {k: b for k, (b, _) in (lc.get('changed') or {}).items()}, 'undone on the Performance tab', 'you')
    elif action == 'pause':
        on = body.get('paused') is not False
        store.set_settings({f'paused_{mode}': on})
        if on:
            st['paused_at'] = data.now_ist().isoformat(timespec='seconds')
        else:
            st.pop('paused_at', None)
            st['watch_from'] = data.now_ist().isoformat(timespec='seconds')     # the old streak no longer counts
        lab.save_state(mode, st)
        store.log_autopilot(mode, 'pause' if on else 'resume', None, None, 'on the Performance tab', 'you')
    else:
        return jsonify({'error': 'unknown action'}), 404
    return jsonify({'ok': True, 'state': lab.state(mode), 'paused': paused(mode)})


# ---------------------------------------------------------------------------
# The Charts tab (charts.py, livefeed.py)
# ---------------------------------------------------------------------------
CHART_MAX = 16
CHART_DEFAULT = [{'symbol': 'IDX:13', 'tf': 300}, {'symbol': 'IDX:25', 'tf': 300},
                 {'symbol': 'RELIANCE', 'tf': 900}, {'symbol': 'IDX:13', 'tf': 86400}]


def _chart_layout(raw) -> list:
    out = []
    for c in raw if isinstance(raw, list) else []:
        try:
            sym, tf = str(c.get('symbol', '')).upper(), int(c.get('tf'))
        except (AttributeError, TypeError, ValueError):
            continue
        if charts.known(sym) and tf in charts.TF_SECONDS:
            out.append({'symbol': sym, 'tf': tf, 'levels': c.get('levels', True) is not False})
    return out[:CHART_MAX]


# Drawings on the Charts tab (trend lines, horizontal lines, rectangles), per instrument, anchored in time and
# price so they show on every timeframe; one list per instrument, every device.
DRAW_TYPES = ('trend', 'hline', 'rect')
DRAW_MAX = 60
DRAW_ID = re.compile(r'^[A-Za-z0-9_-]{1,24}$')
DRAW_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


def _point(raw):
    try:
        t, pr = int(raw['t']), float(raw['p'])
    except (KeyError, TypeError, ValueError):
        return None
    return {'t': t, 'p': round(pr, 4)} if 0 < t < 4_102_444_800 and math.isfinite(pr) else None


def _drawings(raw) -> list:
    out = []
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict) or d.get('type') not in DRAW_TYPES or not DRAW_ID.match(str(d.get('id', ''))):
            continue
        p1, p2 = _point(d.get('p1') or {}), _point(d.get('p2') or {})
        if not p1 or (d['type'] != 'hline' and not p2):
            continue
        item = {'id': d['id'], 'type': d['type'], 'p1': p1}
        if d['type'] != 'hline':
            item['p2'] = p2
        if isinstance(d.get('color'), str) and DRAW_COLOR.match(d['color']):
            item['color'] = d['color'].lower()
        out.append(item)
    return out[:DRAW_MAX]


def chart_levels(sym: str) -> list:
    """The app's own levels for a chart: open zones (both edges), their targets and stops, and
    triggered trades' entry, stop and target."""
    if sym.startswith('IDX:'):
        return []
    now = data.now_ist()
    session = now.date().isoformat()
    out = []
    for z in store.chart_zones(sym):
        mode = z.get('mode') or 'swing'
        if mode == 'intraday':
            if (z.get('expires_at') or '')[:10] < session:
                continue
        elif not _live_zone(z, now):
            continue
        tag = 'Intraday' if mode == 'intraday' else 'Swing'
        side = z.get('side') or 'long'
        t = z.get('trigger') or {}
        if z['status'] == 'triggered' and t.get('entry'):
            out += [{'price': t['entry'], 'kind': 'entry', 'label': f'{tag} {side} entry'},
                    {'price': t.get('stop'), 'kind': 'stop', 'label': f'{tag} stop'},
                    {'price': t.get('target') or z.get('target'), 'kind': 'target', 'label': f'{tag} target'}]
        else:
            word = 'tapped' if z['status'] == 'tapped' else 'zone'
            out += [{'price': z['zone_high'], 'kind': 'zone', 'label': f'{tag} {side} {word}'},
                    {'price': z['zone_low'], 'kind': 'zone', 'label': ''},
                    {'price': z.get('target'), 'kind': 'target', 'label': f'{tag} target'},
                    {'price': (z.get('meta') or {}).get('stop'), 'kind': 'stop', 'label': f'{tag} stop'}]
    return [x for x in out if x['price']]


@app.route('/api/charts/config')
def api_charts_config():
    saved = store.get_json('charts:layout')
    return jsonify(safe({'instruments': charts.instruments(), 'tfs': [{'s': s, 'label': l} for s, l in charts.TFS],
                         'layout': _chart_layout(saved) if saved is not None else CHART_DEFAULT,
                         'max': CHART_MAX, 'price': 'inr', 'tz': 'IST', 'shift': charts.SHIFT, 'day_shift': charts.SHIFT,
                         'market_open': data.market_open(), 'drawings': store.get_json('charts:drawings', {}) or {},
                         **charts.live()}))


@app.route('/api/charts/drawings', methods=['POST'])
def api_charts_drawings():
    """One instrument's drawings, replaced: {"symbol": "IDX:13", "drawings": [{"id", "type": "trend" | "hline" | "rect",
    "p1": {"t": seconds, "p": price}, "p2": {...}, "color": "#rrggbb"}]} (no p2 for a horizontal line; no colour: the
    theme's). An empty list removes them."""
    body = request.get_json(silent=True) or {}
    sym = str(body.get('symbol') or '').upper()
    if not charts.known(sym):
        return jsonify({'error': 'unknown instrument'}), 400
    drawings = _drawings(body.get('drawings'))
    allm = store.get_json('charts:drawings', {}) or {}
    if drawings:
        allm[sym] = drawings
    else:
        allm.pop(sym, None)
    store.set_json('charts:drawings', allm)
    return jsonify({'ok': True, 'symbol': sym, 'drawings': drawings})


@app.route('/api/charts/layout', methods=['POST'])
def api_charts_layout():
    layout = _chart_layout((request.get_json(silent=True) or {}).get('layout'))
    store.set_json('charts:layout', layout)
    return jsonify({'ok': True, 'layout': layout})


@app.route('/api/charts/candles')
def api_charts_candles():
    sym = (request.args.get('symbol') or '').upper()
    try:
        tf = int(request.args.get('tf', 900))
    except ValueError:
        tf = 0
    if not charts.known(sym) or tf not in charts.TF_SECONDS:
        return jsonify({'error': 'unknown instrument or timeframe'}), 400
    charts.FEED.watch([sym])
    out = dict(charts.candles(sym, tf))
    out['levels'] = chart_levels(sym)
    return jsonify(safe(out))


def chart_syms(raw) -> list:
    return [s for s in raw if charts.known(s)][:CHART_MAX]


@app.route('/api/charts/live')
def api_charts_live():
    """New ticks for the Charts tab, when the page has no WebSocket (its ticks channel pushes them)."""
    syms = chart_syms((request.args.get('symbols') or '').upper().split(','))
    try:
        since = float(request.args.get('since') or 0)
    except ValueError:
        since = 0.0
    charts.FEED.watch(syms)
    ticks = {s: [[round(t, 3), p] for t, p in charts.FEED.ticks(s, since)[-600:]] for s in syms}
    return jsonify(safe({'now': time.time(), 'ticks': ticks, 'market_open': data.market_open(),
                         'error': charts.FEED.error, **charts.live()}))


@app.route('/api/ticker')
def api_ticker():
    return jsonify(safe(ticker_state()))


@app.route('/api/options')
def api_options():
    return jsonify(safe(options_state()))


@app.route('/api/options/chain/<symbol>/live')
def api_options_chain_live(symbol):
    """The chain drawer's live refresh (every 3 s while it is open in session); 204 when there is
    nothing live to give (market closed, no Dhan, the latest read is from another session)."""
    sym = symbol.upper()
    expiry = request.args.get('expiry')
    if not SYMBOL_RE.match(sym) or (expiry and not re.match(r'^\d{4}-\d{2}-\d{2}$', expiry)):
        return jsonify({'error': 'bad symbol or expiry'}), 400
    try:
        out = chain_live(sym, expiry)
    except Exception as e:
        return jsonify({'error': dhan._redact(e)[:160]}), 502
    return (jsonify(safe(out)), 200) if out else ('', 204)


@app.route('/api/options/chain/<symbol>')
def api_options_chain(symbol):
    sym = symbol.upper()
    if not SYMBOL_RE.match(sym):
        return jsonify({'error': 'bad symbol'}), 400
    expiry = request.args.get('expiry')
    if expiry and not re.match(r'^\d{4}-\d{2}-\d{2}$', expiry):
        return jsonify({'error': 'expiry must be YYYY-MM-DD'}), 400
    view = options_chain_view(sym, expiry)
    if not view:
        return jsonify({'error': f'No option chain for {sym} yet' + ('' if dhan.available() else ' (needs Dhan)')}), 404
    return jsonify(safe(view))


@app.route('/api/options/refresh', methods=['POST'])
def api_options_refresh():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'send a JSON object'}), 400
    sym = str(body.get('symbol') or '').upper() or None
    if sym and not (sym in options.INDICES or (SYMBOL_RE.match(sym) and dhan.security_id(sym))):
        return jsonify({'error': f'unknown underlying {sym}'}), 400
    if not dhan.available():
        return jsonify({'error': 'Option chains need Dhan — connect it in the sidebar'}), 409
    if OPT_LOCK.locked():
        return jsonify({'error': 'A pass is already running'}), 409
    threading.Thread(target=run_options_cycle, args=('manual', [sym] if sym else None), daemon=True,
                     name='options-refresh').start()
    return jsonify({'started': True, 'symbol': sym})


@app.route('/api/options/rules', methods=['POST'])
def api_options_rules():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'send the rules as a JSON object'}), 400
    clean = {k: v for k, v in body.items() if k in options.TUNABLE}
    # Store only what differs from the defaults, already clamped.
    ruled = options.rules_from(clean)
    saved = {k: ruled[k] for k in clean if ruled[k] != options.TUNABLE[k][0]}
    store.set_settings({'options_rules': saved})
    return jsonify({'rules': orules(), 'rule_overrides': saved})


@app.route('/api/options/stocks', methods=['POST'])
def api_options_stocks():
    body = request.get_json(silent=True) or {}
    raw = body.get('stocks') if isinstance(body, dict) else None
    if not isinstance(raw, str):
        return jsonify({'error': 'send {"stocks": "RELIANCE TCS ..."}'}), 400
    try:
        fo = {s['symbol'] for s in universe.load().get('stocks', []) if s.get('fo')}
    except (OSError, ValueError):
        fo = set()
    syms, bad = [], []
    for s in raw.replace(',', ' ').upper().split():
        if s in syms:
            continue
        (syms if SYMBOL_RE.match(s) and (not fo or s in fo) and s not in options.INDICES else bad).append(s)
    if len(syms) > options.MAX_STOCKS:
        return jsonify({'error': f'at most {options.MAX_STOCKS} stocks'}), 400
    store.set_settings({'options_stocks': ' '.join(syms)})
    return jsonify({'stocks': option_stocks(), 'rejected': bad})


@app.route('/api/telegram/test', methods=['POST'])
def api_telegram_test():
    if not notify.configured():
        return jsonify({'error': 'TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set'}), 400
    ok = notify.send('🐋 <b>NiftyWhale</b> test message. Alerts will arrive here.')
    return (jsonify({'status': 'sent'}), 200) if ok else (jsonify({'error': 'Telegram rejected it'}), 502)


# ---------------------------------------------------------------------------
# The page's WebSocket (niftywhale/hub.py): one connection per open page, through which the server
# pushes prices, ticks, the bell, the open panels and "this changed" as they happen. A page without
# it (refused, or a proxy in the way) polls the HTTP routes above as before.
# ---------------------------------------------------------------------------
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25, 'max_message_size': 64 * 1024}
sock = Sock(app)
HUB = hub.Hub(lambda: store.DB_PATH)
WS_RENEW_S = 5               # a connection renews its symbols' interest on the feed this often
_nc = {'v': None, 'counts': None}


def notice_counts_now() -> dict:
    """The bell's counts, read again only when the notices table changed (one query for every page)."""
    v = HUB.topics().get('db:notices')
    if v is None or v != _nc['v'] or _nc['counts'] is None:
        _nc['counts'], _nc['v'] = store.notice_counts(), v
    return _nc['counts']


def ws_px(p: dict, mem: dict):
    """Live prices: each symbol's price once it changes, as soon as the feed has it."""
    if mem.get('p') is not p:
        mem.update(p=p, syms=live_symbols(p.get('s') or []), sent={}, renewed=0.0)
    on, now, syms = live_on(), time.time(), mem['syms']
    out = {'on': on, 'stream': charts.streaming(), 'px': {}} if on != mem.get('on') else None
    mem['on'] = on
    if not on or not syms:
        return out
    if now - mem['renewed'] > WS_RENEW_S:
        charts.FEED.mark(syms)
        mem['renewed'] = now
    sent = mem['sent']
    for sym, (at, price) in charts.FEED.since(syms, now - LIVE_FRESH_S).items():
        if sent.get(sym, 0) < at:
            sent[sym] = at
            if out is None:
                out = {'on': on, 'stream': charts.streaming(), 'px': {}}
            out['px'][sym] = [round(price, 3), at]
    return out


def ws_ticks(p: dict, mem: dict):
    """The Charts tab's ticks since each symbol's last one sent, and the feed's state every few seconds."""
    if mem.get('p') is not p:
        try:
            since = float(p.get('since') or 0)
        except (TypeError, ValueError):
            since = 0.0
        mem.update(p=p, syms=chart_syms([str(x).upper() for x in p.get('s') or []]), since={}, start=since,
                   renewed=0.0, said=0.0)
    now, syms = time.time(), mem['syms']
    if not syms:
        return None
    if now - mem['renewed'] > WS_RENEW_S:
        charts.FEED.watch(syms)
        mem['renewed'] = now
    ticks = {}
    for sym in syms:
        got = charts.FEED.ticks(sym, mem['since'].get(sym, mem['start']))[-600:]
        if got:
            mem['since'][sym] = got[-1][0]
            ticks[sym] = [[round(t, 3), px] for t, px in got]
    if not ticks and now - mem['said'] < 5:
        return None
    mem['said'] = now
    return {'now': now, 'ticks': ticks, 'market_open': data.market_open(), 'error': charts.FEED.error, **charts.live()}


def ws_demo(p: dict, mem: dict):
    return demo_live() if live_on() else None


def ws_chain(p: dict, mem: dict):
    sym, expiry = str(p.get('symbol') or '').upper(), p.get('expiry')
    if not SYMBOL_RE.match(sym) or (expiry and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(expiry))):
        raise hub.BadRequest('bad symbol or expiry')
    out = chain_live(sym, expiry)
    return {**out, 'symbol': sym} if out else None


def ws_ticker(p: dict, mem: dict):
    return ticker_state()


def market_mood():
    """The market's mood for the page's background (ticker.mood): from the strip's latest rows, read
    again through ticker_state() (its cache) when Dhan is on; the last session's after the close."""
    try:
        rows = ticker_state().get('rows') if dhan.available() else None
    except Exception:
        rows = None
    m = ticker.mood(rows or TICKER['rows'] or [])
    return {**m, 'live': data.market_open(), 'session': TICKER['session'].isoformat() if TICKER['session'] else None} if m else None


def ws_mood(p: dict, mem: dict):
    return market_mood() or {'label': None}


HUB.channel('px', ws_px, every=0.2, dedupe=False)
HUB.channel('ticks', ws_ticks, every=0.25, dedupe=False)
HUB.channel('notices', lambda p, mem: notice_counts_now(), every=1.0, background=True)
HUB.channel('demo', ws_demo, every=1.0)
HUB.channel('chain', ws_chain, every=1.0)
HUB.channel('ticker', ws_ticker, every=1.0)
HUB.channel('mood', ws_mood, every=2.0)
# In-memory state whose change means a panel should load again (the tables are seen by db_changes).
_public = lambda d: {k: v for k, v in d.items() if not k.startswith('_')}
HUB.probe('run', lambda: {k: _public(v) for k, v in STATE.items()})
HUB.probe('market', lambda: [data.market_open(), dhan.available()])
HUB.probe('news_run', lambda: _public(NEWS_STATE))
HUB.probe('smart_run', lambda: _public(SMART_STATE))


@sock.route('/ws')
def ws_page(ws):
    """The page's live connection (niftywhale/hub.py): sign-in as for any page (a cookie from the
    app's own origin, or an API token)."""
    who = getattr(g, 'auth', None)
    HUB.serve(ws, still_signed_in=lambda: auth.still_valid(who),
              hello=lambda: {'on': live_on(), 'live': charts.live(), 'notices': notice_counts_now()})


@app.route('/api/live/status')
def api_live_status():
    """The live plumbing: Dhan's stream, the feed and the pages connected."""
    return jsonify(safe({**charts.live(), 'on': live_on(), 'stream': charts.SOURCE.status(), 'feed': {
        'error': charts.FEED.error, 'last_poll': charts.FEED.last_poll or None, 'streamed': charts.FEED.streamed,
        'polled': charts.FEED.polled}, 'pages': HUB.clients()}))


# ---------------------------------------------------------------------------
store.init()


def _upgrade_universe():
    """Universe files from before the index list have no per-stock `indices`;
    rebuild once so every index option has members."""
    try:
        stocks = universe.load().get('stocks', [])
    except (OSError, ValueError):
        stocks = []
    if stocks and all('indices' in s for s in stocks):
        return
    logger.info('Universe file predates the index list; rebuilding it in the background')
    try:
        refresh_universe()
    except Exception as e:
        logger.warning(f'Universe upgrade failed, will retry on next start: {e}')


if os.getenv('NIFTYWHALE_SCHEDULER', '1') == '1':      # off for tests and second copies
    threading.Thread(target=_upgrade_universe, daemon=True, name='universe-upgrade').start()
    threading.Thread(target=scheduler_loop, daemon=True, name='scheduler').start()
    threading.Thread(target=intraday_loop, daemon=True, name='intraday').start()
    threading.Thread(target=options_loop, daemon=True, name='options').start()
    threading.Thread(target=news_loop, daemon=True, name='news').start()
    threading.Thread(target=smart_loop, daemon=True, name='smart').start()
    threading.Thread(target=demo_loop, daemon=True, name='demo').start()
    threading.Thread(target=marks_loop, daemon=True, name='marks').start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5058')))
