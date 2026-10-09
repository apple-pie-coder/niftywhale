"""
Dhan's live market feed: one WebSocket (wss://api-feed.dhan.co, version 2) that pushes every trade of
every instrument the app has subscribed, in place of asking the REST quote API once a second.

  the app says what it wants          want({(segment, security id): mode})
  the stream keeps one connection     subscribes / unsubscribes the difference, 100 instruments a message
  each packet goes to on_packet()     {'seg', 'sid', 'ltp', 'oi', 'vol', 'bid', 'ask', 'open', ...}

Modes: 'ticker' (price and time), 'quote' (+ day open / high / low / volume), 'full' (+ OI and five
levels of depth). An instrument wanted in two modes is subscribed in the higher one.

With nothing wanted for IDLE_S the connection is closed; a dropped connection is opened again with
a growing pause (BACKOFF), and Dhan's own reasons for closing one (a bad token, too many connections,
no data plan) wait longer. While it is down, connected() is False and the app polls REST instead.

Packets are little-endian: an 8-byte header (code, length, segment, security id) and a body by code.
One WebSocket message can hold several packets back to back. Trade times (LTT) are epoch seconds
shifted to IST; they are turned back into real epoch seconds here.
"""
import json
import logging
import struct
import threading
import time
from typing import Callable, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

URL = 'wss://api-feed.dhan.co?version=2&token={token}&clientId={client}&authType=2'
SEG = {'IDX_I': 0, 'NSE_EQ': 1, 'NSE_FNO': 2, 'NSE_CURRENCY': 3, 'BSE_EQ': 4, 'MCX_COMM': 5, 'BSE_CURRENCY': 7, 'BSE_FNO': 8}
SEG_NAME = {v: k for k, v in SEG.items()}
MODES = {'ticker': 15, 'quote': 17, 'full': 21}          # subscribe request codes; +1 unsubscribes
RANK = {'ticker': 0, 'quote': 1, 'full': 2}
BATCH = 100                     # instruments per subscribe message (Dhan's limit)
MAX_INSTRUMENTS = 5000          # per connection (Dhan's limit)
IDLE_S = 60                     # nothing wanted this long: close the connection
BACKOFF = (2, 5, 10, 30, 60)
REFUSED_WAIT = 300              # Dhan closed it for a reason a retry will not fix soon
IST_SHIFT = 19800
# Dhan's reasons for a disconnect packet (code 50).
REASONS = {805: 'too many connections on this Dhan account', 806: 'the Data API subscription is not active',
           807: 'access token expired', 808: 'authentication failed', 809: 'access token invalid',
           810: 'client ID invalid'}

Key = Tuple[str, int]


def _ltt(raw: int) -> Optional[float]:
    return float(raw - IST_SHIFT) if raw > IST_SHIFT else None


def parse(buf: bytes) -> Iterator[dict]:
    """Every packet in one WebSocket message."""
    at = 0
    while at + 8 <= len(buf):
        code, size, seg, sid = struct.unpack_from('<BHBi', buf, at)
        if size < 8 or at + size > len(buf):
            size = {1: 16, 2: 16, 4: 50, 5: 12, 6: 16, 7: 8, 8: 162, 50: 10}.get(code, 0)
            if not size or at + size > len(buf):
                return                                  # a packet we cannot read: drop the rest
        p = {'code': code, 'seg': SEG_NAME.get(seg, str(seg)), 'sid': sid}
        b = at + 8
        if code in (1, 2):                              # index / ticker
            ltp, ltt = struct.unpack_from('<fi', buf, b)
            p.update(ltp=ltp, ltt=_ltt(ltt))
        elif code == 4:                                 # quote
            ltp, _, ltt, atp, vol, sell, buy, o, c, h, lo = struct.unpack_from('<fhifiiiffff', buf, b)
            p.update(ltp=ltp, ltt=_ltt(ltt), atp=atp, vol=vol, open=o, close=c, high=h, low=lo)
        elif code == 5:                                 # open interest
            p['oi'] = struct.unpack_from('<i', buf, b)[0]
        elif code == 6:                                 # previous close
            p['prev_close'], p['prev_oi'] = struct.unpack_from('<fi', buf, b)
        elif code == 8:                                 # full: quote + OI + depth
            ltp, _, ltt, atp, vol, sell, buy, oi, _hi, _lo, o, c, h, lo = struct.unpack_from('<fhifiiiiiiffff', buf, b)
            bq, aq, _bo, _ao, bid, ask = struct.unpack_from('<iihhff', buf, b + 54)
            p.update(ltp=ltp, ltt=_ltt(ltt), atp=atp, vol=vol, oi=oi, open=o, close=c, high=h, low=lo, bid=bid, ask=ask)
        elif code == 50:                                # disconnect
            p['reason'] = struct.unpack_from('<h', buf, b)[0]
        yield p
        at += size


def _requests(code: int, keys) -> Iterator[str]:
    keys = list(keys)
    for i in range(0, len(keys), BATCH):
        part = keys[i:i + BATCH]
        yield json.dumps({'RequestCode': code, 'InstrumentCount': len(part),
                          'InstrumentList': [{'ExchangeSegment': s, 'SecurityId': str(n)} for s, n in part]})


class Stream:
    """One connection to Dhan's feed, kept open while anything is wanted.

    url() -> the connection URL, or None while Dhan cannot be used (no token, yfinance only).
    on_packet(packet) is called on the stream's thread for every packet."""

    def __init__(self, url: Callable[[], Optional[str]], on_packet: Callable[[dict], None], name: str = 'dhan-feed',
                 connect: Callable = None):
        self._url, self._on_packet, self.name = url, on_packet, name
        self._connect = connect
        self._lock = threading.Lock()
        self._want: Dict[Key, str] = {}
        self._subbed: Dict[Key, str] = {}             # on the open connection
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stop = False
        self.error: Optional[str] = None
        self.since: Optional[float] = None            # connected at
        self.packets = 0
        self.last_packet = 0.0
        self._retry_at = 0.0

    # ------------------------------------------------------------ the app's side
    def want(self, keys: Dict[Key, str]) -> None:
        keys = dict(list(keys.items())[:MAX_INSTRUMENTS])
        with self._lock:
            if keys == self._want:
                return
            self._want = keys
        self._wake.set()
        if keys:
            self._start()

    def connected(self) -> bool:
        return self._ws is not None

    def subscribed(self) -> Dict[Key, str]:
        """What the open connection carries now (empty while it is down)."""
        with self._lock:
            return dict(self._subbed) if self._ws is not None else {}

    def status(self) -> dict:
        return {'connected': self.connected(), 'since': self.since, 'instruments': len(self._subbed) if self._ws else 0,
                'packets': self.packets, 'last_packet': self.last_packet or None, 'error': self.error}

    def close(self) -> None:
        self._stop = True
        self._wake.set()

    # ------------------------------------------------------------ the thread
    def _start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, daemon=True, name=self.name)
            self._thread.start()

    def _open(self, url: str):
        if self._connect:
            return self._connect(url)
        from websockets.sync.client import connect
        # Entered as a context: websockets 17 warns about (and will drop) connections that are not.
        return connect(url, open_timeout=10, close_timeout=3, max_size=2 ** 22, ping_interval=20, ping_timeout=20).__enter__()

    def _sync(self, ws) -> None:
        """Send the difference between what is wanted and what is subscribed."""
        with self._lock:
            want = dict(self._want)
            have = dict(self._subbed)
        drop: Dict[str, list] = {}
        add: Dict[str, list] = {}
        for k, m in have.items():
            if want.get(k) != m:
                drop.setdefault(m, []).append(k)
        for k, m in want.items():
            if have.get(k) != m:
                add.setdefault(m, []).append(k)
        for m, keys in drop.items():
            for msg in _requests(MODES[m] + 1, keys):
                ws.send(msg)
        for m, keys in add.items():
            for msg in _requests(MODES[m], keys):
                ws.send(msg)
        with self._lock:
            self._subbed = want

    def _run(self) -> None:
        tries, idle_since = 0, None
        while not self._stop:
            with self._lock:
                wanted = bool(self._want)
            if not wanted:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > IDLE_S:
                    return                              # started again by the next want()
                self._wake.wait(5)
                self._wake.clear()
                continue
            idle_since = None
            wait = self._retry_at - time.time()
            if wait > 0:
                self._wake.wait(min(wait, 5))
                self._wake.clear()
                continue
            url = self._url()
            if not url:
                self.error = 'Dhan is not connected'
                self._retry_at = time.time() + 30
                continue
            try:
                ws = self._open(url)
            except Exception as e:
                self._failed(f'could not connect: {_clean(e)}', tries)
                tries += 1
                continue
            self._ws, self.since, self.error = ws, time.time(), None
            with self._lock:
                self._subbed = {}
            logger.info(f'{self.name}: connected')
            try:
                end = self._pump(ws)
            except Exception as e:
                end = 'lost'
                self.error = f'connection lost: {_clean(e)}'
            finally:
                self._ws = None
                with self._lock:
                    self._subbed = {}
                try:
                    if not isinstance(end, int):
                        ws.send(json.dumps({'RequestCode': 12}))
                except Exception:
                    pass
                try:
                    ws.close()
                except Exception:
                    pass
            if self._stop:
                return
            if end == 'idle':
                logger.info(f'{self.name}: nothing wanted, closed')
                tries = 0
                continue
            if isinstance(end, int):
                self.error = REASONS.get(end, f'Dhan closed the feed (code {end})')
                logger.warning(f'{self.name}: {self.error}')
                self._retry_at = time.time() + (REFUSED_WAIT if end in REASONS else BACKOFF[-1])
                tries += 1
                continue
            if self.since and time.time() - self.since > 60:
                tries = 0                               # it ran a while: start the backoff over
            self._failed(self.error or 'connection closed', tries)
            tries += 1

    def _pump(self, ws):
        """Read until the connection drops (an exception, or 'lost' when closed), nothing has been
        wanted for IDLE_S ('idle') or Dhan sends a disconnect packet (its reason code)."""
        from websockets.exceptions import ConnectionClosed
        self._sync(ws)
        idle_since = None
        while not self._stop:
            if self._wake.is_set():
                self._wake.clear()
                self._sync(ws)
            with self._lock:
                wanted = bool(self._want)
            if wanted:
                idle_since = None
            else:
                idle_since = idle_since or time.time()
                if time.time() - idle_since > IDLE_S:
                    return 'idle'
            try:
                msg = ws.recv(timeout=0.5)
            except TimeoutError:
                continue
            except ConnectionClosed as e:
                self.error = f'connection closed: {_clean(e)}'
                return 'lost'
            if isinstance(msg, str):
                continue
            for p in parse(msg):
                if p['code'] == 50:
                    return p.get('reason') or 0
                self.packets += 1
                self.last_packet = time.time()
                try:
                    self._on_packet(p)
                except Exception:
                    logger.exception(f'{self.name}: packet handler failed')
        return 'idle'

    def _failed(self, why: str, tries: int) -> None:
        self.error = why
        pause = BACKOFF[min(tries, len(BACKOFF) - 1)]
        self._retry_at = time.time() + pause
        if tries < 3 or tries % 10 == 0:
            logger.warning(f'{self.name}: {why}; trying again in {pause} s')


def _clean(e) -> str:
    """An error without the token: the connection URL carries it."""
    s = str(e) or type(e).__name__
    i = s.find('token=')
    if i >= 0:
        j = s.find('&', i)
        s = s[:i] + 'token=***' + (s[j:] if j >= 0 else '')
    return s[:200]
