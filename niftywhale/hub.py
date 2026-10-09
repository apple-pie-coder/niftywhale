"""
The browser's WebSocket (/ws): one connection per open page, in place of the page asking the server on
timers. The server pushes what changed as it changes:

  px        live prices of what the page shows (as they trade, with Dhan's stream)
  ticks     the Charts tab's ticks
  notices   the bell's count
  topics    "this changed": a table in the database (any process, the lab too) or a piece of the
            app's state (a scan running, the market opening), so the page reloads that panel now
            rather than on its next timer
  demo, ticker, chain, ...   panels the page subscribes to, sent again whenever they change

The page says what it wants in JSON messages: {"t": "sub", "ch": "<channel>", "p": {...}} subscribes
(again, with new parameters), {"t": "unsub", "ch": ...} stops, {"t": "vis", "hidden": true} pauses
everything but notices and topics while the page is in the background, {"t": "ping"} is answered
{"t": "pong"}. The server sends {"t": "<channel>", "d": <payload>}.

Channels are registered by the app: channel(name, make, every) where make(params, mem) returns the
payload, or None for nothing to send, and raises BadRequest for wrong parameters. `mem` is the
connection's own dict for that channel (what it sent last, say). With `dedupe` a payload equal to
the last one sent is not sent again.

Each connection holds a server thread for its life (gunicorn's threaded worker): MAX_CLIENTS keeps
some free for ordinary requests, and a page refused (close code 1013) polls over HTTP instead.

Database changes are counted by triggers (TRIGGERED below) in db_changes, so the lab's writes from
its own container are seen too.
"""
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

MAX_CLIENTS = 12
TICK_S = 0.1                    # the connection loop's beat: how soon a message or a price goes out
TOPICS_EVERY_S = 0.5
RECHECK_S = 60                  # the sign-in is checked again this often; ended, the socket closes
CLOSE_SIGNED_OUT = 4401
CLOSE_BUSY = 1013

# Tables whose changes the page hears about (the auth tables never).
TRIGGERED = ('scans', 'candidates', 'zones', 'alerts', 'settings', 'results', 'signals', 'oc_snapshots', 'oc_daily',
             'oc_ideas', 'oc_candidates', 'bt_runs', 'lab_jobs', 'autopilot_log', 'news', 'sm_deals', 'sm_flows',
             'sm_poi', 'sm_delivery', 'demo_ledger', 'demo_positions', 'notices')


def install_triggers(conn: sqlite3.Connection) -> None:
    """db_changes(tbl, n): n goes up with every row written to tbl, by any connection or process."""
    conn.execute('CREATE TABLE IF NOT EXISTS db_changes (tbl TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)')
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in TRIGGERED:
        if t not in have:
            continue
        conn.execute('INSERT OR IGNORE INTO db_changes (tbl, n) VALUES (?, 0)', (t,))
        for op, short in (('INSERT', 'i'), ('UPDATE', 'u'), ('DELETE', 'd')):
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS nw_chg_{t}_{short} AFTER {op} ON {t} '
                         f"BEGIN UPDATE db_changes SET n = n + 1 WHERE tbl = '{t}'; END")


class BadRequest(ValueError):
    """A channel's parameters are wrong: the page is told, and the channel stops until subscribed again."""


def _clean(obj):
    """NaN / inf -> None, recursively (no browser parses NaN)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def dumps(obj) -> str:
    return json.dumps(_clean(obj), default=str, separators=(',', ':'))


class Channel:
    def __init__(self, name: str, make: Callable[[dict, dict], Any], every: float, dedupe: bool, background: bool):
        self.name, self.make, self.every, self.dedupe, self.background = name, make, every, dedupe, background


class Hub:
    def __init__(self, db_path: Callable[[], str]):
        self._db_path = db_path
        self.channels: Dict[str, Channel] = {}
        self.probes: Dict[str, Callable[[], Any]] = {}
        self._lock = threading.Lock()
        self._clients = 0
        self._topics: Dict[str, Any] = {}
        self._topics_at = 0.0
        self._conn: Optional[sqlite3.Connection] = None
        self.sent = 0

    # ------------------------------------------------------------ registration
    def channel(self, name: str, make: Callable[[dict, dict], Any], every: float = 1.0, dedupe: bool = True,
                background: bool = False) -> None:
        self.channels[name] = Channel(name, make, every, dedupe, background)

    def probe(self, name: str, fn: Callable[[], Any]) -> None:
        """A piece of in-memory state: when fn()'s value changes, topic `name` changes."""
        self.probes[name] = fn

    # ------------------------------------------------------------ what changed
    def topics(self) -> Dict[str, Any]:
        """{topic: version}: a table's change count, a probe's fingerprint. Read at most every
        TOPICS_EVERY_S, however many pages are connected."""
        with self._lock:
            if time.time() - self._topics_at < TOPICS_EVERY_S:
                return self._topics
            out: Dict[str, Any] = {}
            try:
                if self._conn is None:
                    self._conn = sqlite3.connect(self._db_path(), timeout=5, check_same_thread=False)
                out.update({f'db:{t}': n for t, n in self._conn.execute('SELECT tbl, n FROM db_changes')})
            except sqlite3.Error as e:
                logger.debug(f'hub: db_changes unreadable: {e}')
                try:
                    if self._conn:
                        self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
            for name, fn in self.probes.items():
                try:
                    raw = json.dumps(fn(), sort_keys=True, default=str)
                    out[name] = hashlib.blake2b(raw.encode(), digest_size=6).hexdigest()
                except Exception:
                    logger.exception(f'hub: probe {name} failed')
            self._topics, self._topics_at = out, time.time()
            return out

    def clients(self) -> int:
        return self._clients

    # ------------------------------------------------------------ one connection
    def serve(self, ws, still_signed_in: Callable[[], bool] = lambda: True, hello: Callable[[], dict] = dict) -> None:
        """Run one page's connection until it closes. `ws` is flask-sock's (simple-websocket):
        send(text), receive(timeout) -> text or None, close(reason, message)."""
        with self._lock:
            full = self._clients >= MAX_CLIENTS
            if not full:
                self._clients += 1
        if full:
            try:
                ws.close(reason=CLOSE_BUSY, message='too many live connections: polling instead')
            except Exception:
                pass
            return
        try:
            self._loop(ws, still_signed_in, hello)
        except Exception as e:
            if type(e).__name__ != 'ConnectionClosed':
                logger.exception('hub: connection failed')
        finally:
            with self._lock:
                self._clients -= 1

    def _loop(self, ws, still_signed_in, hello) -> None:
        subs: Dict[str, dict] = {}           # channel -> {'p': params, 'mem': {}, 'due': t, 'last': hash}
        hidden = False
        seen_topics = dict(self.topics())
        checked = time.time()
        self._send(ws, {'t': 'hello', 'topics': seen_topics, 'channels': sorted(self.channels), **(hello() or {})})
        next_topics = 0.0
        while True:
            msg = ws.receive(timeout=TICK_S)
            while msg is not None:
                hidden = self._handle(ws, msg, subs, hidden)
                msg = ws.receive(timeout=0)
            now = time.time()
            if now - checked > RECHECK_S:
                checked = now
                if not still_signed_in():
                    ws.close(reason=CLOSE_SIGNED_OUT, message='signed out')
                    return
            if now >= next_topics:
                next_topics = now + TOPICS_EVERY_S
                cur = self.topics()
                changed = {k: v for k, v in cur.items() if seen_topics.get(k) != v}
                if changed:
                    seen_topics.update(changed)
                    self._send(ws, {'t': 'topics', 'd': changed})
            for name, sub in subs.items():
                ch = self.channels[name]
                if (hidden and not ch.background) or now < sub['due']:
                    continue
                sub['due'] = now + ch.every
                try:
                    payload = ch.make(sub['p'], sub['mem'])
                except BadRequest as e:
                    self._send(ws, {'t': name, 'error': str(e)})
                    sub['due'] = float('inf')
                    continue
                except Exception:
                    logger.exception(f'hub: channel {name} failed')
                    sub['due'] = now + max(5.0, ch.every)
                    continue
                if payload is None:
                    continue
                text = dumps({'t': name, 'd': payload}) if ch.dedupe else None
                if ch.dedupe:
                    h = hashlib.blake2b(text.encode(), digest_size=8).digest()
                    if h == sub['last']:
                        continue
                    sub['last'] = h
                    ws.send(text)
                    self.sent += 1
                else:
                    self._send(ws, {'t': name, 'd': payload})

    def _handle(self, ws, raw, subs: Dict[str, dict], hidden: bool) -> bool:
        try:
            m = json.loads(raw)
            kind = m.get('t')
        except (ValueError, AttributeError):
            return hidden
        if kind == 'ping':
            self._send(ws, {'t': 'pong', 'now': time.time()})
        elif kind == 'vis':
            hidden = bool(m.get('hidden'))
            if not hidden:
                for sub in subs.values():                # back in front: everything at once
                    sub['due'] = 0.0
        elif kind == 'sub' and m.get('ch') in self.channels:
            p = m.get('p') if isinstance(m.get('p'), dict) else {}
            old = subs.get(m['ch'])
            # The same channel again with new parameters keeps what it remembers (what it sent last).
            subs[m['ch']] = {'p': p, 'mem': old['mem'] if old else {}, 'due': 0.0, 'last': None}
        elif kind == 'unsub':
            subs.pop(m.get('ch'), None)
        return hidden

    def _send(self, ws, obj: dict) -> None:
        ws.send(dumps(obj))
        self.sent += 1
