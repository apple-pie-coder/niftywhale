"""
SQLite persistence: scans, their candidates, the zones being watched, the
alerts those zones produced, and a handful of settings.

A zone is the hand-off between the evening screen and the market-hours
watcher (Step 9): the screen writes it, the watcher reads it every 15
minutes and moves it along watching -> tapped -> triggered / rejected.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

DB_PATH = os.getenv('DB_PATH', 'var/niftywhale.db')

SETTING_DEFAULTS = {
    'universe': 'nifty100',     # nifty100 | fo | both
    'auto_scan': '1',           # run the screen every weekday evening
    'scan_time': '16:15',       # IST, after the close has settled
    'watcher': '1',             # check zones on 15m candles during market hours
    'telegram': '1',            # send alerts to Telegram
    'rules': '{}',              # JSON: protocol thresholds saved from the rule tuner
    'pattern_alerts': '0',      # Telegram for 15m candlestick patterns at a zone
    'intraday': '1',            # intraday mode: 15m scans + 5m triggers during the session
    'intraday_universe': 'nifty50',
    'shorts': '1',              # swing: short setups too (F&O stocks only: cash shorts are intraday-only)
    'intraday_shorts': '1',     # intraday: short setups too
    'intraday_rules': '{}',     # JSON: intraday thresholds saved from the intraday tuner
    'options': '1',             # options mode: option chains of the indices + options_stocks
    'options_stocks': '',       # space-separated F&O symbols; empty = options.DEFAULT_STOCKS
    'options_alerts': '1',      # Telegram for option ideas and their results
    'options_level_alerts': '0',  # Telegram when an index's OI support / resistance moves
    'options_rules': '{}',      # JSON: idea thresholds saved from the options tuner
    'autopilot_policy': '{}',   # JSON: the autopilot's mode (auto / approve / off) and limits
    'paused_swing': '0',        # entry alerts muted by the autopilot (or you): still recorded
    'paused_intraday': '0',
    'paused_options': '0',
    'weekly_report': '1',       # Telegram performance report, Saturday morning
    'news': '1',                # news desk: NSE filings and media headlines for the board's stocks
    'news_alerts': '1',         # Telegram when an NSE filing lands on an open trade or a tapped zone
    'indicators': '{}',         # JSON: chart indicator settings (indicators.SETTINGS); {} = the defaults
    'demo': '{}',               # JSON: demo funds settings (demo.DEFAULTS); {} = the defaults
    'demo_since': '',           # ISO time the demo account started: trades from then on are taken
    'lab_swing_universe': '',       # empty: backtest what the live mode trades (`universe`)
    'lab_intraday_universe': '',    # empty: the live intraday universe (`intraday_universe`)
    'lab_years_swing': '3',
    'lab_years_intraday': '1',      # intraday replays are the heavy ones (~0.5 s per stock-session)
}
ZONE_LIFETIME_DAYS = 10         # a setup that has not played out in two weeks is stale
LIFETIME_NOTE = 'older than the zone lifetime'
STRUCTURE_FAILED = 'structure failed'     # the tail of an intraday zone's note when its leg origin broke


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init() -> None:
    with connect() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT, finished_at TEXT,
                universe TEXT, origin TEXT,
                status TEXT,                 -- running | done | stopped | error
                scanned INTEGER, passed INTEGER,
                funnel TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER, symbol TEXT, name TEXT,
                score INTEGER, in_zone INTEGER,
                close REAL, zone_low REAL, zone_high REAL,
                stop REAL, target REAL, rr REAL,
                position REAL, atr_pct REAL, avg_volume REAL,
                analysis TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cand_scan ON candidates(scan_id);
            CREATE TABLE IF NOT EXISTS zones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, name TEXT, scan_id INTEGER,
                zone_low REAL, zone_high REAL, target REAL,
                created_at TEXT, expires_at TEXT,
                status TEXT,                 -- watching | tapped | triggered | rejected | expired | dismissed
                last_checked TEXT, note TEXT, trigger TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_zone_status ON zones(status);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER, symbol TEXT, created_at TEXT,
                kind TEXT,                   -- entry | rejected
                entry REAL, stop REAL, target REAL, rr REAL,
                choch_time TEXT, sent INTEGER, message TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            -- Every stock's verdict per scan, so "why didn't X pass?" has an answer.
            CREATE TABLE IF NOT EXISTS results (
                scan_id INTEGER, symbol TEXT, name TEXT,
                failed_at TEXT, reason TEXT, passed INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_results_scan ON results(scan_id, failed_at);
            -- Candlestick patterns the watcher saw on 15m candles. One row per
            -- candle and pattern, however many checks see it.
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, timeframe TEXT, pattern TEXT, label TEXT,
                bar_time TEXT, close REAL, low REAL, high REAL,
                at_zone INTEGER, zone_id INTEGER, created_at TEXT, sent INTEGER DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique
                ON signals(symbol, timeframe, pattern, bar_time);
            -- Options mode. A snapshot is one fetch of one expiry's chain (compact JSON) and
            -- its headline numbers; chains are kept for a few sessions, numbers for a year.
            CREATE TABLE IF NOT EXISTS oc_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, expiry TEXT, ts TEXT, session TEXT,
                spot REAL, summary TEXT, chain TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ocs ON oc_snapshots(symbol, expiry, session, ts);
            CREATE INDEX IF NOT EXISTS idx_ocs_session ON oc_snapshots(session);
            -- One row per underlying per session: the closing ATM IV etc., for IV percentiles.
            CREATE TABLE IF NOT EXISTS oc_daily (
                symbol TEXT, session TEXT, spot REAL, atm_iv REAL, pcr REAL, max_pain REAL,
                PRIMARY KEY (symbol, session)
            );
            -- Option-buying ideas and their paper trades.
            CREATE TABLE IF NOT EXISTS oc_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, expiry TEXT, strike REAL, side TEXT, direction TEXT,
                created_at TEXT, session TEXT, status TEXT,    -- open | won | lost | closed
                entry REAL, stop REAL, target REAL, spot REAL, level REAL, lot INTEGER,
                last REAL, r_open REAL, exit REAL, exit_time TEXT, r REAL,
                note TEXT, reason TEXT, sent INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ideas ON oc_ideas(session, symbol);
            -- Options learning: every look at a chain that came near an idea, and later what
            -- it would have done for each stop / target pair (JSON {"25/50": {...}}).
            CREATE TABLE IF NOT EXISTS oc_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, expiry TEXT, session TEXT, ts TEXT, direction TEXT, side TEXT, strike REAL,
                entry REAL, level REAL, features TEXT, taken INTEGER DEFAULT 0, outcomes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cand ON oc_candidates(session, symbol);
            -- The lab: backtest runs, queued jobs, the autopilot's state and every change it made.
            CREATE TABLE IF NOT EXISTS bt_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT, kind TEXT, created TEXT, params TEXT, period_from TEXT, period_to TEXT,
                universe TEXT, summary TEXT, breakdown TEXT, equity TEXT, extra TEXT
            );
            CREATE TABLE IF NOT EXISTS lab_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, args TEXT, status TEXT, created TEXT, started TEXT, finished TEXT,
                progress TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS autopilot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, mode TEXT, action TEXT, before TEXT, after TEXT, why TEXT, by TEXT
            );
            -- The news desk (news.py): NSE filings and media headlines per stock, each kept once.
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, uid TEXT, kind TEXT, source TEXT, publisher TEXT, title TEXT, detail TEXT,
                url TEXT, published TEXT, seen TEXT, tags TEXT, routine INTEGER DEFAULT 0,
                UNIQUE(symbol, uid)
            );
            CREATE INDEX IF NOT EXISTS idx_news_published ON news(published);
            CREATE INDEX IF NOT EXISTS idx_news_symbol ON news(symbol, published);
            -- Smart money (smart.py): what NSE publishes after each session.
            CREATE TABLE IF NOT EXISTS sm_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, symbol TEXT, name TEXT, client TEXT, side TEXT,
                qty INTEGER, price REAL, value_cr REAL, kind TEXT, remarks TEXT, ctype TEXT,
                UNIQUE(day, symbol, client, side, qty, price, kind)
            );
            CREATE INDEX IF NOT EXISTS idx_sm_deals_symbol ON sm_deals(symbol, day);
            CREATE TABLE IF NOT EXISTS sm_flows (day TEXT, category TEXT, buy REAL, sell REAL, net REAL,
                                                 PRIMARY KEY (day, category));
            CREATE TABLE IF NOT EXISTS sm_poi (day TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS sm_delivery (
                day TEXT, symbol TEXT, close REAL, prev_close REAL, qty INTEGER, deliv_qty INTEGER, deliv_pct REAL,
                turnover_cr REAL, PRIMARY KEY (day, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_sm_delivery_symbol ON sm_delivery(symbol, day);
            -- Demo funds (demo.py): money in and out, and a position for every trade the app takes.
            CREATE TABLE IF NOT EXISTS demo_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, amount REAL, note TEXT);
            CREATE TABLE IF NOT EXISTS demo_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, mode TEXT, symbol TEXT, instrument TEXT,
                side TEXT, direction TEXT, product TEXT, qty INTEGER, lots INTEGER, lot_size INTEGER,
                entry REAL, entry_time TEXT, stop REAL, target REAL, margin REAL, risk REAL,
                status TEXT, exit REAL, exit_time TEXT, gross REAL, charges REAL, net REAL, charges_detail TEXT,
                last REAL, unreal REAL, note TEXT, created TEXT, updated TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_demo_status ON demo_positions(status, entry_time);
            -- In-app notices (the bell): one row per thing worth a look. `key` keeps each event
            -- once (a later step may add to it: the demo position to its trade's entry); `seq` grows
            -- on every insert and update, so the page asks only for what changed since it last looked.
            CREATE TABLE IF NOT EXISTS notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, seq INTEGER, ts TEXT,
                kind TEXT, level TEXT, title TEXT, body TEXT, symbol TEXT, mode TEXT, link TEXT,
                read INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_notices_seq ON notices(seq);
        ''')
        # Swing / intraday trades: the market context recorded at the entry (features.py).
        for col, typ in (('ctx', 'TEXT'),):
            if col not in {r[1] for r in c.execute('PRAGMA table_info(zones)')}:
                c.execute(f'ALTER TABLE zones ADD COLUMN {col} {typ}')
        if 'features' not in {r[1] for r in c.execute('PRAGMA table_info(oc_ideas)')}:
            c.execute('ALTER TABLE oc_ideas ADD COLUMN features TEXT')
        # Lab jobs: each step's start, end and counts (lab.JobLog), for the job detail view.
        if 'steps' not in {r[1] for r in c.execute('PRAGMA table_info(lab_jobs)')}:
            c.execute('ALTER TABLE lab_jobs ADD COLUMN steps TEXT')
        # Zones gained a source: 'scan' zones follow the screen, 'manual' ones
        # are yours and a later scan never expires or overwrites them.
        cols = {r[1] for r in c.execute('PRAGMA table_info(zones)')}
        if 'source' not in cols:
            c.execute("ALTER TABLE zones ADD COLUMN source TEXT DEFAULT 'scan'")
        # The intraday mode shares these tables; `mode` keeps the two apart
        # so an intraday scan can never expire a swing zone, or the reverse.
        for table in ('scans', 'zones', 'alerts'):
            if 'mode' not in {r[1] for r in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN mode TEXT DEFAULT 'swing'")
        if 'meta' not in cols:
            c.execute('ALTER TABLE zones ADD COLUMN meta TEXT')     # intraday: stop, target pool, session
        # Shorts: every setup, zone and alert says which way it trades.
        for table in ('candidates', 'zones', 'alerts'):
            if 'side' not in {r[1] for r in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN side TEXT DEFAULT 'long'")
        c.execute('CREATE INDEX IF NOT EXISTS idx_zone_mode ON zones(mode, status)')
        # News desk: FinBERT's read of each item, and how the stock moved after it (vs Nifty, %).
        have = {r[1] for r in c.execute('PRAGMA table_info(news)')}
        for col, typ in (('sentiment', 'REAL'), ('sent_label', 'TEXT'), ('sent_model', 'TEXT'),
                         ('react_1h', 'REAL'), ('react_close', 'REAL'), ('react_next', 'REAL'),
                         ('react_basis', 'TEXT'), ('react_done', 'INTEGER DEFAULT 0')):
            if col not in have:
                c.execute(f'ALTER TABLE news ADD COLUMN {col} {typ}')


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _meta(z: Dict[str, Any]) -> Dict[str, Any]:
    m = z.get('meta')
    if isinstance(m, dict):
        return m
    try:
        return json.loads(m) if m else {}
    except ValueError:
        return {}


def same_setup(z: Dict[str, Any], side: str, a: Dict[str, Any]) -> bool:
    """
    Is the analysis `a` (for `side`) the setup zone `z` was watching? One
    setup is one stock, one side and one order block. Zones made before the
    order block's date was recorded are matched on the block's own edge (the
    low of a long's zone, the high of a short's), which a setup never moves.
    """
    if (z.get('side') or 'long') != side:
        return False
    ob = (a.get('order_block') or {}).get('date')
    known = _meta(z).get('ob_date')
    if known:
        return known == ob
    key, edge = ('zone_low', 'low') if side == 'long' else ('zone_high', 'high')
    old, new = z.get(key), (a.get('zone') or {}).get(edge)
    return old is not None and new is not None and abs(old - new) <= 1e-6 * max(1.0, abs(new))


def _rows(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- settings
def settings() -> Dict[str, str]:
    out = dict(SETTING_DEFAULTS)
    with connect() as c:
        for k, v in c.execute('SELECT key, value FROM settings'):
            if k in out:
                out[k] = v
    return out


def get_kv(key: str) -> Optional[str]:
    """A raw value from the settings table, for keys the dashboard never reads
    back (like the Dhan token)."""
    with connect() as c:
        row = c.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row and row['value'] else None


def set_kv(key: str, value: str) -> None:
    with connect() as c:
        c.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                  'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))


def set_settings(updates: Dict[str, Any]) -> Dict[str, str]:
    with connect() as c:
        for k, v in updates.items():
            if k not in SETTING_DEFAULTS:
                continue
            if isinstance(v, bool):
                v = '1' if v else '0'
            elif isinstance(v, (dict, list)):
                v = json.dumps(v)
            c.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                      'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (k, str(v)))
    return settings()


# ---------------------------------------------------------------- scans
def start_scan(universe: str, origin: str, mode: str = 'swing') -> int:
    with connect() as c:
        cur = c.execute('INSERT INTO scans (started_at, universe, origin, status, mode) VALUES (?, ?, ?, ?, ?)',
                        (_now(), universe, origin, 'running', mode))
        return cur.lastrowid


def finish_scan(scan_id: int, status: str, scanned: int, passed: int,
                funnel: Dict[str, int], error: str = None) -> None:
    with connect() as c:
        c.execute('UPDATE scans SET finished_at = ?, status = ?, scanned = ?, passed = ?, '
                  'funnel = ?, error = ? WHERE id = ?',
                  (_now(), status, scanned, passed, json.dumps(funnel), error, scan_id))


def save_candidates(scan_id: int, rows: List[Dict[str, Any]]) -> None:
    with connect() as c:
        for r in rows:
            a = r['analysis']
            c.execute('''INSERT INTO candidates (scan_id, symbol, name, score, in_zone, close,
                         zone_low, zone_high, stop, target, rr, position, atr_pct, avg_volume, analysis, side)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (scan_id, r['symbol'], r['name'], a['score'], int(a['in_zone']), a['close'],
                       a['zone']['low'], a['zone']['high'], a['plan']['stop'], a['plan']['target'],
                       a['plan']['rr'], a['position'], a['atr_pct'], a['avg_volume'],
                       json.dumps(a), a.get('side', 'long')))


def save_results(scan_id: int, rows: List[Dict[str, Any]]) -> None:
    with connect() as c:
        c.executemany('INSERT INTO results (scan_id, symbol, name, failed_at, reason, passed) '
                      'VALUES (?, ?, ?, ?, ?, ?)',
                      [(scan_id, r['symbol'], r['name'], r.get('failed_at'), r.get('reason', ''),
                        int(bool(r.get('passed')))) for r in rows])


def dropped_at(scan_id: int, step: str) -> List[Dict[str, Any]]:
    """The stocks whose first failed step in this scan was `step`."""
    with connect() as c:
        return _rows(c.execute('SELECT symbol, name, reason FROM results '
                               'WHERE scan_id = ? AND failed_at = ? ORDER BY symbol', (scan_id, step)))


def result_for(scan_id: int, symbol: str) -> Optional[Dict[str, Any]]:
    with connect() as c:
        row = c.execute('SELECT * FROM results WHERE scan_id = ? AND symbol = ?',
                        (scan_id, symbol)).fetchone()
    return dict(row) if row else None


def latest_scan(finished_only: bool = True, mode: str = 'swing') -> Optional[Dict[str, Any]]:
    q = "SELECT * FROM scans WHERE COALESCE(mode, 'swing') = ? " + \
        ("AND status IN ('done','stopped') " if finished_only else '') + 'ORDER BY id DESC LIMIT 1'
    with connect() as c:
        row = c.execute(q, (mode,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['funnel'] = json.loads(d['funnel'] or '{}')
    return d


def scan_ran_today(date_str: str) -> bool:
    return any(r['status'] in ('done', 'stopped') for r in scheduled_scans(date_str))


def scheduled_scans(date_str: str) -> List[Dict[str, Any]]:
    """The scheduled swing scans started on this date, oldest first."""
    with connect() as c:
        return _rows(c.execute("SELECT id, started_at, status FROM scans WHERE origin = 'schedule' "
                               "AND COALESCE(mode, 'swing') = 'swing' AND started_at LIKE ? ORDER BY id",
                               (date_str + '%',)))


def candidates(scan_id: int) -> List[Dict[str, Any]]:
    with connect() as c:
        rows = _rows(c.execute('SELECT * FROM candidates WHERE scan_id = ? '
                               'ORDER BY in_zone DESC, score DESC', (scan_id,)))
    for r in rows:
        r['analysis'] = json.loads(r['analysis'] or '{}')
    return rows


# ---------------------------------------------------------------- zones
def sync_zones(scan_id: int, rows: List[Dict[str, Any]],
               no_data: Optional[set] = None) -> Dict[str, int]:
    """
    Make the watch list match the latest screen: new setups start watching,
    existing ones get their levels refreshed, and setups the screen no longer
    passes expire -- unless they already triggered, which is history.

    A setup whose zone is spent -- it triggered (and its paper trade may
    since have won or lost), its CHoCH was rejected (Step 12: "delete the
    stock from your watchlist ... wait for the next setup"), you dismissed
    it, or it outlived the zone lifetime -- is not armed again by a later
    scan. A new setup on the same stock (another order block, or the other
    side) is, once the stock has no paper trade open: one trade per stock.

    `no_data` names the stocks this scan could not judge (no candles from the
    data source): their zones are left as they are, not expired.
    """
    now = datetime.now()
    expires = now.replace(microsecond=0) + timedelta(days=ZONE_LIFETIME_DAYS)
    live = {r['symbol'] for r in rows}
    no_data = no_data or set()
    added = refreshed = expired = spent_n = 0
    with connect() as c:
        open_zones = {z['symbol']: z for z in _rows(c.execute(
            "SELECT * FROM zones WHERE status IN ('watching','tapped') AND COALESCE(mode, 'swing') = 'swing'"))}
        spent: Dict[str, List[Dict[str, Any]]] = {}
        for z in _rows(c.execute(
                "SELECT * FROM zones WHERE COALESCE(mode, 'swing') = 'swing' AND created_at >= ? AND "
                "(status IN ('triggered','won','lost','rejected','dismissed') OR (status = 'expired' AND note = ?))",
                ((now - timedelta(days=400)).isoformat(), LIFETIME_NOTE))):
            spent.setdefault(z['symbol'], []).append(z)
        trading = {sym for sym, zs in spent.items() for z in zs if z['status'] == 'triggered' and is_paper(z)}
        for r in rows:
            a = r['analysis']
            z = open_zones.get(r['symbol'])
            if z and z.get('source') == 'manual':
                continue                    # your levels win over the screen's
            side = a.get('side', 'long')
            meta = json.dumps({'ob_date': (a.get('order_block') or {}).get('date'), 'bos_date': a.get('bos_date')})
            if z and (z.get('side') or 'long') != side:
                # The structure flipped: the old zone is for the other direction.
                c.execute("UPDATE zones SET status = 'expired', note = ? WHERE id = ?",
                          (f'structure flipped; now a {side} setup', z['id']))
                expired += 1
                z = None
            if any(same_setup(old, side, a) for old in spent.get(r['symbol'], [])):
                # Played out, rejected, dismissed or stale: wait for the next setup.
                # An open zone that would be moved onto it closes instead.
                spent_n += 1
                if z:
                    c.execute("UPDATE zones SET status = 'expired', note = ? WHERE id = ?",
                              ("the screen's setup here has already played out", z['id']))
                    expired += 1
            elif z:
                c.execute('UPDATE zones SET zone_low = ?, zone_high = ?, target = ?, scan_id = ?, meta = ? '
                          'WHERE id = ?',
                          (a['zone']['low'], a['zone']['high'], a['plan']['target'], scan_id, meta, z['id']))
                refreshed += 1
            elif r['symbol'] in trading:
                spent_n += 1                # its paper trade is still open: one trade per stock
            else:
                c.execute('''INSERT INTO zones (symbol, name, scan_id, zone_low, zone_high, target,
                             created_at, expires_at, status, side, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                          (r['symbol'], r['name'], scan_id, a['zone']['low'], a['zone']['high'],
                           a['plan']['target'], _now(), expires.isoformat(), 'watching', side, meta))
                added += 1
        for sym, z in open_zones.items():
            if sym not in live and sym not in no_data and z.get('source') != 'manual':
                c.execute("UPDATE zones SET status = 'expired', note = ? WHERE id = ?",
                          ('no longer passes the daily screen', z['id']))
                expired += 1
        expired += c.execute("UPDATE zones SET status = 'expired', note = ? "
                             "WHERE status IN ('watching','tapped') AND COALESCE(source, 'scan') = 'scan' "
                             "AND COALESCE(mode, 'swing') = 'swing' AND expires_at < ?",
                             (LIFETIME_NOTE, _now())).rowcount
    return {'added': added, 'refreshed': refreshed, 'expired': expired, 'spent': spent_n}


def is_paper(z: Dict[str, Any]) -> bool:
    """A swing zone whose trigger is followed as a paper trade (triggers from
    before paper trading are not)."""
    t = z.get('trigger')
    if isinstance(t, str):
        try:
            t = json.loads(t) if t else {}
        except ValueError:
            t = {}
    return bool((t or {}).get('paper'))


SWING_TRADES = ('triggered', 'won', 'lost')


def swing_trades(statuses=SWING_TRADES, limit: int = 500) -> List[Dict[str, Any]]:
    """Swing paper trades, newest first: open ones ('triggered') and closed
    ones ('won', 'lost'). Each row's `trigger` holds the trade."""
    q = (f"SELECT * FROM zones WHERE COALESCE(mode, 'swing') = 'swing' "
         f"AND status IN ({','.join('?' * len(statuses))}) ORDER BY id DESC LIMIT ?")
    with connect() as c:
        rows = _rows(c.execute(q, list(statuses) + [limit]))
    return [r for r in _zone_rows(rows) if (r['trigger'] or {}).get('paper')]


def open_zones() -> List[Dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM zones WHERE status IN ('watching','tapped') "
                               "AND COALESCE(mode, 'swing') = 'swing' ORDER BY status DESC, symbol"))


def chart_zones(symbol: str, limit: int = 12) -> List[Dict[str, Any]]:
    """The symbol's latest open or triggered zones, both modes, for the Charts tab's levels."""
    with connect() as c:
        rows = _rows(c.execute("SELECT * FROM zones WHERE symbol = ? AND status IN ('watching','tapped','triggered') "
                               "ORDER BY id DESC LIMIT ?", (symbol, limit)))
    return _zone_rows(rows)


def recent_zones(limit: int = 60) -> List[Dict[str, Any]]:
    with connect() as c:
        rows = _rows(c.execute(
            "SELECT * FROM zones WHERE COALESCE(mode, 'swing') = 'swing' ORDER BY CASE status WHEN 'triggered' THEN 0 WHEN 'tapped' THEN 1 "
            "WHEN 'watching' THEN 2 ELSE 3 END, COALESCE(last_checked, created_at) DESC LIMIT ?",
            (limit,)))
    for r in rows:
        r['trigger'] = json.loads(r['trigger']) if r['trigger'] else None
    return rows


def watch_manual(symbol: str, name: str, zone_low: float, zone_high: float,
                 target: float) -> Dict[str, Any]:
    """Watch a zone you set yourself; replaces the levels of an open zone for the
    same stock. A target above the zone is a long, below it a short. Returns the zone row."""
    side = 'short' if target < zone_low else 'long'
    with connect() as c:
        z = c.execute("SELECT id FROM zones WHERE symbol = ? AND status IN ('watching','tapped') "
                      "AND COALESCE(mode, 'swing') = 'swing'",
                      (symbol,)).fetchone()
        if z:
            # New levels restart the watch: price action from before they were set
            # (a tap, a sweep, a CHoCH) was not action at these levels.
            c.execute("UPDATE zones SET zone_low = ?, zone_high = ?, target = ?, source = 'manual', "
                      "status = 'watching', note = 'levels set by you', trigger = NULL, side = ?, "
                      "created_at = ?, meta = NULL WHERE id = ?",
                      (zone_low, zone_high, target, side, _now(), z['id']))
            zone_id = z['id']
        else:
            cur = c.execute('''INSERT INTO zones (symbol, name, scan_id, zone_low, zone_high, target,
                               created_at, expires_at, status, note, source, side)
                               VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, 'watching', 'added by you', 'manual', ?)''',
                            (symbol, name, zone_low, zone_high, target, _now(), side))
            zone_id = cur.lastrowid
        return dict(c.execute('SELECT * FROM zones WHERE id = ?', (zone_id,)).fetchone())


def get_zone(zone_id: int) -> Optional[Dict[str, Any]]:
    with connect() as c:
        row = c.execute('SELECT * FROM zones WHERE id = ?', (zone_id,)).fetchone()
    return dict(row) if row else None


def update_zone(zone_id: int, only_if=None, **fields) -> int:
    """Set `fields` on a zone. With `only_if` (a tuple of statuses), only while
    the zone is still in one of them: a check that read the zone a moment ago
    must not undo a dismissal or an expiry that landed since. Returns whether
    the zone was updated."""
    if 'trigger' in fields and not isinstance(fields['trigger'], (str, type(None))):
        fields['trigger'] = json.dumps(fields['trigger'])
    keys = ', '.join(f'{k} = ?' for k in fields)
    q, args = f'UPDATE zones SET {keys} WHERE id = ?', list(fields.values()) + [zone_id]
    if only_if:
        q += f" AND status IN ({','.join('?' * len(only_if))})"
        args += list(only_if)
    with connect() as c:
        return c.execute(q, args).rowcount


# ---------------------------------------------------------------- alerts
def add_alert(zone: Dict[str, Any], kind: str, t: Dict[str, Any], sent: bool, message: str) -> None:
    with connect() as c:
        c.execute('''INSERT INTO alerts (zone_id, symbol, created_at, kind, entry, stop, target, rr,
                     choch_time, sent, message, mode, side) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (zone['id'], zone['symbol'], _now(), kind, t.get('entry'), t.get('stop'),
                   t.get('target'), t.get('rr'), t.get('choch_time'), int(sent), message,
                   zone.get('mode') or 'swing', zone.get('side') or 'long'))


def add_signal(symbol: str, timeframe: str, hit: Dict[str, Any], zone_id: Optional[int]) -> bool:
    """Record a pattern. Returns True only the first time this candle is seen."""
    with connect() as c:
        cur = c.execute('''INSERT OR IGNORE INTO signals (symbol, timeframe, pattern, label, bar_time,
                           close, low, high, at_zone, zone_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (symbol, timeframe, hit['key'], hit['label'], hit['time'], hit['close'],
                         hit['low'], hit['high'], int(bool(hit.get('at_zone'))), zone_id, _now()))
        return cur.rowcount == 1


def mark_signal_sent(symbol: str, timeframe: str, pattern: str, bar_time: str) -> None:
    with connect() as c:
        c.execute('UPDATE signals SET sent = 1 WHERE symbol = ? AND timeframe = ? AND pattern = ? '
                  'AND bar_time = ?', (symbol, timeframe, pattern, bar_time))


def signals(limit: int = 40, symbol: str = None) -> List[Dict[str, Any]]:
    q, args = 'SELECT * FROM signals', []
    if symbol:
        q, args = q + ' WHERE symbol = ?', [symbol]
    with connect() as c:
        return _rows(c.execute(q + ' ORDER BY bar_time DESC, id DESC LIMIT ?', args + [limit]))


def zone_alerts(zone_id: int) -> List[Dict[str, Any]]:
    """Every alert one zone raised, oldest first (a trade's timeline)."""
    with connect() as c:
        return _rows(c.execute('SELECT * FROM alerts WHERE zone_id = ? ORDER BY id', (zone_id,)))


def alerts(limit: int = 50, mode: str = 'swing') -> List[Dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM alerts WHERE COALESCE(mode, 'swing') = ? "
                               "ORDER BY id DESC LIMIT ?", (mode, limit)))


# ---------------------------------------------------------------- intraday
# Intraday zones live for one session. Lifecycle:
#   watching -> tapped -> triggered -> won | lost | closed (squared off)
#                      -> rejected (R:R too low, or after the entry cutoff)
#   watching/tapped -> expired (no longer a setup, structure failed, session over)
# One trade per stock per session: once a stock has triggered, a later scan
# that finds it again does not open a second zone that day.
INTRADAY_OPEN = ('watching', 'tapped')
INTRADAY_TRADES = ('triggered', 'won', 'lost', 'closed')


def _zone_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for r in rows:
        r['trigger'] = json.loads(r['trigger']) if r.get('trigger') else None
        r['meta'] = json.loads(r['meta']) if r.get('meta') else {}
    return rows


def sync_intraday_zones(scan_id: int, rows: List[Dict[str, Any]], session: str) -> Dict[str, int]:
    """
    Make today's intraday watch list match the latest 15m scan. New setups
    start watching; watching zones get fresh levels or expire when the scan
    no longer passes them. Tapped zones are left alone: price is in the zone,
    and a sweep there often breaks the 15m picture just before the CHoCH.

    A setup is not armed again the same day once you dismissed it, its CHoCH
    was rejected, or its leg origin broke (same stock, side and 15m order
    block); a new setup on the stock is. When a watching zone's levels change,
    `meta.since` restarts its watch: a tap of levels nobody knew yet is not a tap.
    """
    added = refreshed = expired = 0
    live = {r['symbol'] for r in rows}
    with connect() as c:
        today = _zone_rows(_rows(c.execute(
            "SELECT * FROM zones WHERE mode = 'intraday' AND created_at LIKE ?", (session + '%',))))
        by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for z in today:
            by_sym.setdefault(z['symbol'], []).append(z)
        for r in rows:
            a = r['analysis']
            mine = by_sym.get(r['symbol'], [])
            if any(z['status'] in INTRADAY_TRADES + ('tapped',) for z in mine):
                continue
            side = a.get('side', 'long')
            if any(same_setup(z, side, a) and (z['status'] in ('dismissed', 'rejected') or
                                               STRUCTURE_FAILED in (z.get('note') or ''))
                   for z in mine):
                continue
            meta = {'session': session, 'side': side, 'stop': a['plan']['stop'], 'rr': a['plan']['rr'],
                    'target_kind': a.get('target_kind'), 'target_label': a.get('target_label'),
                    'levels': a.get('levels', {}), 'ob_date': (a.get('order_block') or {}).get('date')}
            watching = next((z for z in mine if z['status'] == 'watching'), None)
            if watching:
                moved = (watching['zone_low'], watching['zone_high'], watching['target']) != \
                    (a['zone']['low'], a['zone']['high'], a['plan']['target'])
                since = _now() if moved else watching['meta'].get('since')
                if since:
                    meta['since'] = since
                c.execute('UPDATE zones SET zone_low = ?, zone_high = ?, target = ?, scan_id = ?, meta = ?, '
                          "side = ? WHERE id = ? AND status = 'watching'",
                          (a['zone']['low'], a['zone']['high'], a['plan']['target'],
                           scan_id, json.dumps(meta), side, watching['id']))
                refreshed += 1
            else:
                c.execute('''INSERT INTO zones (symbol, name, scan_id, zone_low, zone_high, target, created_at,
                             expires_at, status, source, mode, meta, side)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'watching', 'scan', 'intraday', ?, ?)''',
                          (r['symbol'], r['name'], scan_id, a['zone']['low'], a['zone']['high'],
                           a['plan']['target'], _now(), session + 'T15:30:00', json.dumps(meta), side))
                added += 1
        for z in today:
            if z['status'] == 'watching' and z['symbol'] not in live:
                expired += c.execute("UPDATE zones SET status = 'expired', note = ? WHERE id = ? "
                                     "AND status = 'watching'",
                                     ('no longer a setup on the 15m chart', z['id'])).rowcount
    return {'added': added, 'refreshed': refreshed, 'expired': expired}


def intraday_zones(statuses=None, session: str = None, limit: int = 200) -> List[Dict[str, Any]]:
    q, args = "SELECT * FROM zones WHERE mode = 'intraday'", []
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    if session:
        q += ' AND created_at LIKE ?'
        args.append(session + '%')
    with connect() as c:
        return _zone_rows(_rows(c.execute(q + ' ORDER BY id DESC LIMIT ?', args + [limit])))


def expire_intraday(before_session: str = None, note: str = 'session over') -> int:
    """Close the watch on every open intraday zone (from sessions before
    `before_session`, or all of them)."""
    q, args = "UPDATE zones SET status = 'expired', note = ? WHERE mode = 'intraday' " \
              "AND status IN ('watching','tapped')", [note]
    if before_session:
        q += ' AND created_at < ?'
        args.append(before_session)
    with connect() as c:
        return c.execute(q, args).rowcount


def prune_intraday(keep_days: int = 7) -> None:
    """Intraday scans run every 15 minutes; keep a week of their funnels."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with connect() as c:
        old = [r[0] for r in c.execute("SELECT id FROM scans WHERE mode = 'intraday' AND started_at < ?",
                                       (cutoff,))]
        for sid in old:
            c.execute('DELETE FROM candidates WHERE scan_id = ?', (sid,))
            c.execute('DELETE FROM results WHERE scan_id = ?', (sid,))
            c.execute('DELETE FROM scans WHERE id = ?', (sid,))


# ---------------------------------------------------------------- options
OC_KEEP_CHAINS_DAYS = 3         # full chains: today's intraday comparisons, plus a margin
OC_KEEP_SUMMARY_DAYS = 30         # intraday summaries; oc_daily keeps one row per session for good


def add_snapshot(symbol: str, expiry: str, ts: str, session: str, spot: float,
                 summary: Dict[str, Any], chain: Dict[str, Any]) -> int:
    with connect() as c:
        return c.execute('INSERT INTO oc_snapshots (symbol, expiry, ts, session, spot, summary, chain) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (symbol, expiry, ts, session, spot, json.dumps(summary), json.dumps(chain))).lastrowid


def _snap(row) -> Dict[str, Any]:
    d = dict(row)
    d['summary'] = json.loads(d['summary']) if d.get('summary') else {}
    if 'chain' in d:
        d['chain'] = json.loads(d['chain']) if d.get('chain') else None
    return d


def latest_snapshot(symbol: str, expiry: str = None) -> Optional[Dict[str, Any]]:
    q, args = 'SELECT * FROM oc_snapshots WHERE symbol = ?', [symbol]
    if expiry:
        q += ' AND expiry = ?'
        args.append(expiry)
    with connect() as c:
        row = c.execute(q + ' ORDER BY ts DESC, id DESC LIMIT 1', args).fetchone()
    return _snap(row) if row and row['chain'] else None


def first_snapshot(symbol: str, expiry: str, session: str) -> Optional[Dict[str, Any]]:
    """The session's first chain for this expiry: the base for intraday OI changes."""
    with connect() as c:
        row = c.execute('SELECT * FROM oc_snapshots WHERE symbol = ? AND expiry = ? AND session = ? '
                        'AND chain IS NOT NULL ORDER BY ts, id LIMIT 1', (symbol, expiry, session)).fetchone()
    return _snap(row) if row else None


def snapshot_series(symbol: str, expiry: str, session: str) -> List[Dict[str, Any]]:
    """The session's summaries for one expiry, oldest first (no chains)."""
    with connect() as c:
        rows = c.execute('SELECT id, ts, spot, summary FROM oc_snapshots WHERE symbol = ? AND expiry = ? '
                         'AND session = ? ORDER BY ts, id', (symbol, expiry, session)).fetchall()
    return [_snap(r) for r in rows]


def latest_summaries(since_session: str) -> List[Dict[str, Any]]:
    """The newest snapshot (summary only) of every underlying and expiry seen since `since_session`."""
    with connect() as c:
        rows = c.execute('SELECT s.id, s.symbol, s.expiry, s.ts, s.session, s.spot, s.summary FROM oc_snapshots s '
                         'JOIN (SELECT symbol, expiry, MAX(id) AS id FROM oc_snapshots WHERE session >= ? '
                         'GROUP BY symbol, expiry) m ON m.id = s.id ORDER BY s.ts DESC', (since_session,)).fetchall()
    return [_snap(r) for r in rows]


def expiries_seen(symbol: str) -> List[str]:
    with connect() as c:
        return [r[0] for r in c.execute('SELECT DISTINCT expiry FROM oc_snapshots WHERE symbol = ? ORDER BY expiry',
                                        (symbol,))]


def save_daily(symbol: str, session: str, spot: float, atm_iv: Optional[float], pcr: Optional[float],
               max_pain: Optional[float]) -> None:
    """The session's last reading wins: called after every snapshot of the nearest expiry."""
    with connect() as c:
        c.execute('INSERT INTO oc_daily (symbol, session, spot, atm_iv, pcr, max_pain) VALUES (?, ?, ?, ?, ?, ?) '
                  'ON CONFLICT(symbol, session) DO UPDATE SET spot = excluded.spot, atm_iv = excluded.atm_iv, '
                  'pcr = excluded.pcr, max_pain = excluded.max_pain', (symbol, session, spot, atm_iv, pcr, max_pain))


def iv_history(symbol: str, before_session: str, days: int = 250) -> List[float]:
    with connect() as c:
        return [r[0] for r in c.execute('SELECT atm_iv FROM oc_daily WHERE symbol = ? AND session < ? AND atm_iv > 0 '
                                        'ORDER BY session DESC LIMIT ?', (symbol, before_session, days))]


def prune_options(today: str) -> None:
    keep_chain = (datetime.fromisoformat(today) - timedelta(days=OC_KEEP_CHAINS_DAYS)).date().isoformat()
    keep_all = (datetime.fromisoformat(today) - timedelta(days=OC_KEEP_SUMMARY_DAYS)).date().isoformat()
    with connect() as c:
        # Older sessions keep their summaries (the intraday PCR / IV lines) but not the chains,
        # except each session's last chain per expiry.
        c.execute('UPDATE oc_snapshots SET chain = NULL WHERE session < ? AND chain IS NOT NULL AND id NOT IN '
                  '(SELECT MAX(id) FROM oc_snapshots WHERE session < ? GROUP BY symbol, expiry, session)',
                  (keep_chain, keep_chain))
        c.execute('DELETE FROM oc_snapshots WHERE session < ?', (keep_all,))


def add_idea(i: Dict[str, Any]) -> int:
    cols = ('symbol', 'expiry', 'strike', 'side', 'direction', 'created_at', 'session', 'status', 'entry', 'stop',
            'target', 'spot', 'level', 'lot', 'last', 'r_open', 'reason')
    with connect() as c:
        return c.execute(f'INSERT INTO oc_ideas ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                         [i.get(k) for k in cols]).lastrowid


def update_idea(idea_id: int, only_if: str = None, **fields) -> int:
    """Set fields; with `only_if`, only while the idea still has that status (so two
    threads can never both close it and send two messages)."""
    if not fields:
        return 0
    q = f'UPDATE oc_ideas SET {", ".join(f"{k} = ?" for k in fields)} WHERE id = ?'
    args = list(fields.values()) + [idea_id]
    if only_if:
        q += ' AND status = ?'
        args.append(only_if)
    with connect() as c:
        return c.execute(q, args).rowcount


def idea(idea_id: int) -> Optional[Dict[str, Any]]:
    with connect() as c:
        rows = _rows(c.execute('SELECT * FROM oc_ideas WHERE id = ?', (idea_id,)))
    return rows[0] if rows else None


def ideas(status: str = None, session: str = None, symbol: str = None, limit: int = 500) -> List[Dict[str, Any]]:
    q, args = 'SELECT * FROM oc_ideas WHERE 1 = 1', []
    for col, val in (('status', status), ('session', session), ('symbol', symbol)):
        if val:
            q += f' AND {col} = ?'
            args.append(val)
    with connect() as c:
        return _rows(c.execute(q + ' ORDER BY created_at DESC, id DESC LIMIT ?', args + [limit]))


# ---------------------------------------------------------------- options candidates
def add_candidate(c: Dict[str, Any], expiry: str, session: str, ts: str, taken: bool) -> int:
    with connect() as conn:
        return conn.execute('INSERT INTO oc_candidates (symbol, expiry, session, ts, direction, side, strike, entry, level, '
                            'features, taken) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (c['symbol'], expiry, session, ts, c['direction'], c['side'], c['strike'], c['entry'],
                             c.get('level'), json.dumps(c['f']), int(taken))).lastrowid


def option_candidates(session: str = None, pending: bool = False, since: str = None) -> List[Dict[str, Any]]:
    q, args = 'SELECT * FROM oc_candidates WHERE 1 = 1', []
    if session:
        q += ' AND session = ?'
        args.append(session)
    if since:
        q += ' AND session >= ?'
        args.append(since)
    if pending:
        q += ' AND outcomes IS NULL'
    with connect() as c:
        rows = _rows(c.execute(q + ' ORDER BY ts', args))
    for r in rows:
        r['f'] = json.loads(r.pop('features') or '{}')
        r['outcomes'] = json.loads(r['outcomes']) if r.get('outcomes') else None
    return rows


def count_candidates() -> int:
    with connect() as c:
        return c.execute('SELECT COUNT(*) FROM oc_candidates').fetchone()[0]


def set_candidate_outcomes(cid: int, outcomes: Dict[str, Any]) -> None:
    with connect() as c:
        c.execute('UPDATE oc_candidates SET outcomes = ? WHERE id = ?', (json.dumps(outcomes), cid))


def snapshots_for(symbol: str, expiry: str, session: str) -> List[Dict[str, Any]]:
    """The session's snapshots with their chains, oldest first (for candidate outcomes)."""
    with connect() as c:
        rows = c.execute('SELECT id, ts, spot, chain FROM oc_snapshots WHERE symbol = ? AND expiry = ? AND session = ? '
                         'AND chain IS NOT NULL ORDER BY ts, id', (symbol, expiry, session)).fetchall()
    return [_snap(r) for r in rows]


# ---------------------------------------------------------------- lab
def add_run(mode: str, kind: str, params: Dict[str, Any], period: tuple, universe: str, summary: Dict[str, Any],
            breakdown: Any, equity: Any, extra: Any = None) -> int:
    with connect() as c:
        return c.execute('INSERT INTO bt_runs (mode, kind, created, params, period_from, period_to, universe, summary, '
                         'breakdown, equity, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (mode, kind, _now(), json.dumps(params), period[0], period[1], universe, json.dumps(summary),
                          json.dumps(breakdown), json.dumps(equity), json.dumps(extra))).lastrowid


def latest_run(mode: str, kind: str) -> Optional[Dict[str, Any]]:
    with connect() as c:
        row = c.execute('SELECT * FROM bt_runs WHERE mode = ? AND kind = ? ORDER BY id DESC LIMIT 1', (mode, kind)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ('params', 'summary', 'breakdown', 'equity', 'extra'):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def prune_runs(keep: int = 60) -> None:
    with connect() as c:
        c.execute('DELETE FROM bt_runs WHERE id <= (SELECT id FROM bt_runs ORDER BY id DESC LIMIT 1 OFFSET ?)', (keep,))


def add_job(kind: str, args: Dict[str, Any] = None) -> int:
    with connect() as c:
        open_job = c.execute("SELECT id FROM lab_jobs WHERE kind = ? AND status IN ('queued', 'running')", (kind,)).fetchone()
        if open_job:
            return open_job[0]
        return c.execute("INSERT INTO lab_jobs (kind, args, status, created) VALUES (?, ?, 'queued', ?)",
                         (kind, json.dumps(args or {}), _now())).lastrowid


def next_job() -> Optional[Dict[str, Any]]:
    with connect() as c:
        row = c.execute("SELECT * FROM lab_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE lab_jobs SET status = 'running', started = ? WHERE id = ?", (_now(), row['id']))
    d = dict(row)
    d['args'] = json.loads(d['args'] or '{}')
    return d


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    with connect() as c:
        c.execute(f'UPDATE lab_jobs SET {", ".join(f"{k} = ?" for k in fields)} WHERE id = ?',
                  list(fields.values()) + [job_id])


def jobs(limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute('SELECT * FROM lab_jobs ORDER BY id DESC LIMIT ?', (limit,)))


def reset_running_jobs() -> None:
    """At lab start: a job left 'running' died with the previous process."""
    with connect() as c:
        c.execute("UPDATE lab_jobs SET status = 'queued', started = NULL WHERE status = 'running'")


AUTOPILOT_NOTICES = {     # action -> (level, title); the rest (propose, drop, policy) is the lab's own business
    'awaiting': ('warn', 'Autopilot wants your OK for new {mode} rules'),
    'promote': ('info', 'Autopilot changed the {mode} rules'),
    'rollback': ('warn', 'Autopilot rolled the {mode} rules back'),
    'pause': ('warn', 'Autopilot paused {mode}'),
    'resume': ('info', 'Autopilot resumed {mode}'),
}


def log_autopilot(mode: str, action: str, before: Any, after: Any, why: str, by: str = 'autopilot') -> int:
    with connect() as c:
        rid = c.execute('INSERT INTO autopilot_log (ts, mode, action, before, after, why, by) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (_now(), mode, action, json.dumps(before), json.dumps(after), why, by)).lastrowid
    if action in AUTOPILOT_NOTICES and by != 'you':
        level, title = AUTOPILOT_NOTICES[action]
        add_notice(f'autopilot:{rid}', 'autopilot', level, title.format(mode=mode), why or '', mode=mode,
                   link={'tab': 'perf'})
    return rid


# ---------------------------------------------------------------- notices (the bell)
NOTICE_KEEP_DAYS = 30


def add_notice(key: str, kind: str, level: str, title: str, body: str = '', symbol: str = None,
               mode: str = None, link: Dict[str, Any] = None, update: bool = False) -> Optional[int]:
    """Record a notice once per `key`; the id if it is new. With `update`, an existing one gets the
    new title / body / level (and a new seq, so open pages redraw it) but keeps its time and read
    state: a later step adding detail is not news again."""
    with connect() as c:
        seq = c.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM notices').fetchone()[0]
        cur = c.execute('INSERT OR IGNORE INTO notices (key, seq, ts, kind, level, title, body, symbol, mode, link) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (key, seq, _now(), kind, level, title, body, symbol, mode, json.dumps(link) if link else None))
        if cur.rowcount:
            return cur.lastrowid
        if update:
            c.execute('UPDATE notices SET seq = ?, level = ?, title = ?, body = ? WHERE key = ?',
                      (seq, level, title, body, key))
    return None


def notice(key: str) -> Optional[Dict[str, Any]]:
    with connect() as c:
        rows = _rows(c.execute('SELECT * FROM notices WHERE key = ?', (key,)))
    return rows[0] if rows else None


def notices(after_seq: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
    """Notices changed since `after_seq` (newest first), or the latest `limit` with after_seq 0."""
    with connect() as c:
        rows = _rows(c.execute('SELECT * FROM notices WHERE seq > ? ORDER BY id DESC LIMIT ?', (after_seq, limit)))
    for r in rows:
        r['link'] = json.loads(r['link']) if r.get('link') else None
    return rows


def notice_counts() -> Dict[str, int]:
    with connect() as c:
        seq, unread = c.execute('SELECT COALESCE(MAX(seq), 0), COALESCE(SUM(read = 0), 0) FROM notices').fetchone()
    return {'seq': seq, 'unread': unread}


def read_notices(ids: List[int] = None) -> None:
    """Mark these notices read (all of them with no ids); each gets a new seq so other open pages follow."""
    with connect() as c:
        seq = c.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM notices').fetchone()[0]
        if ids:
            c.execute(f"UPDATE notices SET read = 1, seq = ? WHERE read = 0 AND id IN ({','.join('?' * len(ids))})",
                      [seq, *[int(i) for i in ids]])
        else:
            c.execute('UPDATE notices SET read = 1, seq = ? WHERE read = 0', (seq,))


def prune_notices() -> None:
    cut = (datetime.now() - timedelta(days=NOTICE_KEEP_DAYS)).isoformat(timespec='seconds')
    with connect() as c:
        c.execute('DELETE FROM notices WHERE ts < ?', (cut,))


def autopilot_log(limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as c:
        rows = _rows(c.execute('SELECT * FROM autopilot_log ORDER BY id DESC LIMIT ?', (limit,)))
    for r in rows:
        r['before'] = json.loads(r['before']) if r.get('before') else None
        r['after'] = json.loads(r['after']) if r.get('after') else None
    return rows


# ---------------------------------------------------------------- news desk
def add_news(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Store news items (news.py's shape); returns the ones that were new."""
    new = []
    stamp = _now()
    with connect() as c:
        for i in items:
            cur = c.execute(
                'INSERT OR IGNORE INTO news (symbol, uid, kind, source, publisher, title, detail, url, published, '
                'seen, tags, routine) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (i['symbol'], i['uid'], i['kind'], i['source'], i.get('publisher'), i['title'], i.get('detail') or '',
                 i.get('url'), i['published'], stamp, json.dumps(i.get('tags') or []), 1 if i.get('routine') else 0))
            if cur.rowcount:
                new.append({**i, 'seen': stamp})
    return new


def _news_row(r: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r['tags'] = json.loads(r.get('tags') or '[]')
    except ValueError:
        r['tags'] = []
    r['routine'] = bool(r.get('routine'))
    return r


def news(symbols: Optional[List[str]] = None, kind: Optional[str] = None, since: Optional[str] = None,
         limit: int = 200, routine: bool = True) -> List[Dict[str, Any]]:
    """News items, newest first. `since` is an ISO time compared with `published`."""
    q, args = 'SELECT * FROM news WHERE 1=1', []
    if symbols is not None:
        if not symbols:
            return []
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += list(symbols)
    if kind:
        q += ' AND kind = ?'
        args.append(kind)
    if since:
        q += ' AND published >= ?'
        args.append(since)
    if not routine:
        q += ' AND routine = 0'
    with connect() as c:
        return [_news_row(r) for r in _rows(c.execute(q + ' ORDER BY published DESC, id DESC LIMIT ?', args + [limit]))]


def news_counts(symbols: List[str], since: str, until: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Per stock, items published in [since, until): how many, how many are filings that aren't
    housekeeping, and FinBERT's read of them (`tone`: the average score of the scored ones,
    -1 to +1, None when none is scored; `pos` / `neg`: items labelled so). Housekeeping
    filings never count towards the tone: 'trading window closed' says nothing either way."""
    if not symbols:
        return {}
    q = (f"SELECT symbol, COUNT(*) AS n, SUM(kind = 'filing' AND routine = 0) AS filings, "
         f"AVG(CASE WHEN routine = 0 THEN sentiment END) AS tone, "
         f"SUM(routine = 0 AND sent_label = 'positive') AS pos, SUM(routine = 0 AND sent_label = 'negative') AS neg "
         f"FROM news WHERE symbol IN ({','.join('?' * len(symbols))}) AND published >= ?")
    args = list(symbols) + [since]
    if until:
        q += ' AND published < ?'
        args.append(until)
    with connect() as c:
        return {r['symbol']: {'n': r['n'], 'filings': r['filings'] or 0, 'pos': r['pos'] or 0, 'neg': r['neg'] or 0,
                              'tone': round(r['tone'], 3) if r['tone'] is not None else None}
                for r in _rows(c.execute(q + ' GROUP BY symbol', args))}


def news_unscored(limit: int = 150) -> List[Dict[str, Any]]:
    """Items FinBERT hasn't read yet, newest first (housekeeping filings are never read)."""
    with connect() as c:
        return _rows(c.execute('SELECT id, kind, title, detail FROM news WHERE sentiment IS NULL AND routine = 0 '
                               'ORDER BY published DESC LIMIT ?', (limit,)))


def set_sentiment(scores: Dict[int, Dict[str, Any]], model: str) -> None:
    with connect() as c:
        c.executemany('UPDATE news SET sentiment = ?, sent_label = ?, sent_model = ? WHERE id = ?',
                      [(r['score'], r['label'], model, i) for i, r in scores.items()])


def news_unreacted(since: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """Items whose price reaction is still being measured (published since `since`), oldest first:
    today's items stay unfinished until tomorrow's close, and newest-first would let them hold
    every slot while older ones never got measured."""
    with connect() as c:
        return _rows(c.execute('SELECT id, symbol, published FROM news WHERE react_done = 0 AND routine = 0 '
                               'AND published >= ? ORDER BY published ASC LIMIT ?', (since, limit)))


def set_reaction(item_id: int, r: Dict[str, Any]) -> None:
    with connect() as c:
        c.execute('UPDATE news SET react_1h = ?, react_close = ?, react_next = ?, react_basis = ?, react_done = ? '
                  'WHERE id = ?', (r.get('react_1h'), r.get('react_close'), r.get('react_next'), r.get('basis'),
                                   int(r.get('done') or 0), item_id))


def news_graded(since: str) -> List[Dict[str, Any]]:
    """Scored items with at least one measured reaction: what the impact table is made of."""
    with connect() as c:
        return _rows(c.execute('SELECT symbol, kind, sentiment, sent_label, react_1h, react_close, react_next '
                               'FROM news WHERE sent_label IS NOT NULL AND routine = 0 AND published >= ? '
                               'AND (react_1h IS NOT NULL OR react_close IS NOT NULL OR react_next IS NOT NULL)',
                               (since,)))


def drop_news(ids: List[int]) -> int:
    if not ids:
        return 0
    with connect() as c:
        return c.execute(f"DELETE FROM news WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount


def prune_news(keep_days: int = 45) -> int:
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with connect() as c:
        return c.execute('DELETE FROM news WHERE published < ?', (cutoff,)).rowcount



# ---------------------------------------------------------------- smart money
def add_deals(deals: List[Dict[str, Any]]) -> int:
    """Store bulk / block deals (smart.parse_deals); returns how many were new."""
    n = 0
    with connect() as c:
        for d in deals:
            n += c.execute('INSERT OR IGNORE INTO sm_deals (day, symbol, name, client, side, qty, price, value_cr, kind, '
                           'remarks, ctype) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (d['day'], d['symbol'], d.get('name'), d['client'], d['side'], d['qty'], d['price'],
                            d['value_cr'], d['kind'], d.get('remarks') or '', d['ctype'])).rowcount
    return n


def deals(symbols: Optional[List[str]] = None, since: Optional[str] = None, until: Optional[str] = None,
          limit: int = 500) -> List[Dict[str, Any]]:
    """Deals, newest first; `since` / `until` are ISO days, until exclusive."""
    q, args = 'SELECT * FROM sm_deals WHERE 1=1', []
    if symbols is not None:
        if not symbols:
            return []
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += list(symbols)
    if since:
        q += ' AND day >= ?'
        args.append(since)
    if until:
        q += ' AND day < ?'
        args.append(until)
    with connect() as c:
        return _rows(c.execute(q + ' ORDER BY day DESC, value_cr DESC LIMIT ?', args + [limit]))


def add_flows(rows: List[Dict[str, Any]]) -> int:
    with connect() as c:
        return sum(c.execute('INSERT OR REPLACE INTO sm_flows (day, category, buy, sell, net) VALUES (?, ?, ?, ?, ?)',
                             (r['day'], r['category'], r['buy'], r['sell'], r['net'])).rowcount for r in rows)


def flows(limit_days: int = 30) -> List[Dict[str, Any]]:
    """FII / DII cash flows, oldest first."""
    with connect() as c:
        days = [r[0] for r in c.execute('SELECT DISTINCT day FROM sm_flows ORDER BY day DESC LIMIT ?', (limit_days,))]
        if not days:
            return []
        return _rows(c.execute(f"SELECT * FROM sm_flows WHERE day IN ({','.join('?' * len(days))}) ORDER BY day",
                               days))


def set_poi(day: str, data: Dict[str, Any]) -> None:
    with connect() as c:
        c.execute('INSERT OR REPLACE INTO sm_poi (day, data) VALUES (?, ?)', (day, json.dumps(data)))


def poi(limit_days: int = 60, until: Optional[str] = None) -> List[Dict[str, Any]]:
    """Participant-wise OI per day, oldest first; `until` (ISO day) exclusive."""
    q, args = 'SELECT day, data FROM sm_poi', []
    if until:
        q += ' WHERE day < ?'
        args.append(until)
    with connect() as c:
        rows = _rows(c.execute(q + ' ORDER BY day DESC LIMIT ?', args + [limit_days]))
    return [{'day': r['day'], 'data': json.loads(r['data'] or '{}')} for r in reversed(rows)]


def add_delivery(day: str, rows: Dict[str, Dict[str, Any]]) -> int:
    with connect() as c:
        c.executemany('INSERT OR REPLACE INTO sm_delivery (day, symbol, close, prev_close, qty, deliv_qty, deliv_pct, '
                      'turnover_cr) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                      [(day, sym, r.get('close'), r.get('prev_close'), r.get('qty'), r.get('deliv_qty'), r.get('deliv_pct'),
                        r.get('turnover_cr')) for sym, r in rows.items()])
    return len(rows)


def smart_days(table: str) -> List[str]:
    """The sessions held in one of the smart-money tables, oldest first."""
    if table not in ('sm_delivery', 'sm_poi', 'sm_flows', 'sm_deals'):
        raise ValueError(table)
    with connect() as c:
        return [r[0] for r in c.execute(f'SELECT DISTINCT day FROM {table} ORDER BY day')]


def delivery_history(symbols: List[str], sessions: int = 26, until: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Each stock's last `sessions` delivery rows (before `until`, an ISO day, if given), oldest first."""
    if not symbols:
        return {}
    days = [d for d in smart_days('sm_delivery') if not until or d < until][-sessions:]
    if not days:
        return {}
    with connect() as c:
        rows = _rows(c.execute(
            f"SELECT * FROM sm_delivery WHERE symbol IN ({','.join('?' * len(symbols))}) "
            f"AND day IN ({','.join('?' * len(days))}) ORDER BY day", list(symbols) + days))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r['symbol'], []).append(r)
    return out


def prune_smart(keep_days: int = 120) -> None:
    """Delivery rows are the bulk (every stock, every session): keep four months. Deals, flows and
    positioning are small and kept."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).date().isoformat()
    with connect() as c:
        c.execute('DELETE FROM sm_delivery WHERE day < ?', (cutoff,))


# ---------------------------------------------------------------- demo funds
def demo_ledger() -> List[Dict[str, Any]]:
    with connect() as c:
        return _rows(c.execute('SELECT * FROM demo_ledger ORDER BY ts, id'))


def add_demo_ledger(kind: str, amount: float, note: str = '', ts: Optional[str] = None) -> int:
    with connect() as c:
        return c.execute('INSERT INTO demo_ledger (ts, kind, amount, note) VALUES (?, ?, ?, ?)',
                         (ts or datetime.now().astimezone().isoformat(timespec='seconds'), kind, amount, note)).lastrowid


def demo_position(ref: str) -> Optional[Dict[str, Any]]:
    """The demo account's position for one trade ('swing:12', 'options:7'), if it took one."""
    with connect() as c:
        rows = _rows(c.execute('SELECT * FROM demo_positions WHERE ref = ?', (ref,)))
    for r in rows:
        try:
            r['charges_detail'] = json.loads(r['charges_detail']) if r.get('charges_detail') else None
        except ValueError:
            r['charges_detail'] = None
    return rows[0] if rows else None


def trade_notices(ref: str) -> List[Dict[str, Any]]:
    """The notices raised about one trade, oldest first."""
    with connect() as c:
        rows = _rows(c.execute("SELECT * FROM notices WHERE key LIKE ? ORDER BY id", ('%:' + ref,)))
    return [r for r in rows if r['key'].endswith(':' + ref) and r['key'].count(':') == ref.count(':') + 1]


def demo_positions(status: Optional[str] = None, limit: int = 5000) -> List[Dict[str, Any]]:
    q, args = 'SELECT * FROM demo_positions', []
    if status:
        q += ' WHERE status = ?'
        args.append(status)
    with connect() as c:
        rows = _rows(c.execute(q + ' ORDER BY entry_time DESC, id DESC LIMIT ?', args + [limit]))
    for r in rows:
        try:
            r['charges_detail'] = json.loads(r['charges_detail']) if r.get('charges_detail') else None
        except ValueError:
            r['charges_detail'] = None
    return rows


def add_demo_position(p: Dict[str, Any]) -> int:
    cols = ('ref', 'mode', 'symbol', 'instrument', 'side', 'direction', 'product', 'qty', 'lots', 'lot_size', 'entry',
            'entry_time', 'stop', 'target', 'margin', 'risk', 'status', 'exit', 'exit_time', 'gross', 'charges', 'net',
            'charges_detail', 'last', 'unreal', 'note')
    vals = [json.dumps(p[k]) if k == 'charges_detail' and p.get(k) is not None else p.get(k) for k in cols]
    now = _now()
    with connect() as c:
        return c.execute(f"INSERT OR IGNORE INTO demo_positions ({','.join(cols)}, created, updated) "
                         f"VALUES ({','.join('?' * len(cols))}, ?, ?)", vals + [now, now]).lastrowid


def update_demo_position(pid: int, **fields) -> None:
    if not fields:
        return
    if 'charges_detail' in fields and fields['charges_detail'] is not None:
        fields['charges_detail'] = json.dumps(fields['charges_detail'])
    fields['updated'] = _now()
    with connect() as c:
        c.execute(f"UPDATE demo_positions SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                  list(fields.values()) + [pid])


def clear_demo() -> None:
    """A fresh account: every position and ledger row goes."""
    with connect() as c:
        c.execute('DELETE FROM demo_positions')
        c.execute('DELETE FROM demo_ledger')


def get_json(key: str, default: Any = None) -> Any:
    raw = get_kv(key)
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def set_json(key: str, value: Any) -> None:
    set_kv(key, json.dumps(value))


def live_trades(mode: str) -> List[Dict[str, Any]]:
    """Every live paper trade of a mode in the backtester's shape ({symbol, side,
    entry_time, status, r, exit_time, f}), oldest first. `f` is the context
    recorded at the entry (zones.ctx / oc_ideas.features) plus what the trigger says."""
    out = []
    if mode in ('swing', 'intraday'):
        statuses = SWING_TRADES if mode == 'swing' else INTRADAY_TRADES
        with connect() as c:
            rows = _zone_rows(_rows(c.execute(
                f"SELECT * FROM zones WHERE COALESCE(mode, 'swing') = ? AND status IN ({','.join('?' * len(statuses))}) "
                'AND trigger IS NOT NULL', (mode, *statuses))))
        for z in rows:
            t = z['trigger'] or {}
            if mode == 'swing' and not t.get('paper'):
                continue                       # alerts from before paper trading were never followed
            if not t.get('entry') or not t.get('choch_time'):
                continue
            ctx = json.loads(z['ctx']) if z.get('ctx') else {}
            f = dict(ctx)
            ts = t['choch_time']
            try:
                at = datetime.fromisoformat(ts)
                f.setdefault('entry_minute', at.hour * 60 + at.minute)
                f.setdefault('weekday', at.weekday())
            except (TypeError, ValueError):
                pass
            if t.get('rr') is not None:
                f.setdefault('rr', round(float(t['rr']), 2))
            status = {'triggered': 'open'}.get(z['status'], z['status'])
            out.append({'mode': mode, 'symbol': z['symbol'], 'side': z.get('side') or 'long', 'entry_time': ts,
                        'entry': t.get('entry'), 'stop': t.get('stop'), 'target': t.get('target'), 'status': status,
                        'exit': t.get('exit'), 'exit_time': t.get('exit_time'),
                        'r': t.get('r') if status != 'open' else None, 'r_open': t.get('r') if status == 'open' else None,
                        'f': f, 'ref': f"{mode}:{z['id']}", 'last': t.get('last'), 'name': z.get('name')})
    elif mode == 'options':
        for i in ideas(limit=5000):
            f = json.loads(i['features']) if i.get('features') else {}
            out.append({'mode': 'options', 'symbol': i['symbol'], 'side': i['direction'], 'entry_time': i['created_at'],
                        'entry': i['entry'], 'stop': i['stop'], 'target': i['target'], 'status': i['status'],
                        'exit': i['exit'], 'exit_time': i['exit_time'], 'r': i['r'] if i['status'] != 'open' else None,
                        'r_open': i['r_open'] if i['status'] == 'open' else None, 'f': f,
                        'ref': f"options:{i['id']}", 'last': i.get('last'), 'strike': i.get('strike'), 'opt_side': i.get('side'),
                        'expiry': i.get('expiry'), 'lot': i.get('lot')})
    out.sort(key=lambda t: t['entry_time'] or '')
    return out
