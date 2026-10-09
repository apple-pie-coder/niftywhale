"""
The live feed behind the Charts tab: one thread that polls the last price of every instrument a chart
is showing, and keeps the ticks so the charts can draw candles down to one second.

However many browsers and charts are open, the price source is asked once per `interval` seconds, for
every watched instrument together. An instrument is watched while some browser asked for it in the
last WATCH_S seconds; with nothing watched (or the market closed) the thread asks nothing. Ticks are
kept KEEP_S seconds per instrument, in memory only: candles under a minute exist from the moment a chart
started watching, they are not history anyone sells.

Open positions ride the same poll (mark()): an instrument marked rather than watched keeps only its
latest price, not a tick history, so the demo account's twenty-odd contracts cost no memory. A mark
can ask for more than the price ('quote': the day's open / high / low, 'full': OI and depth too),
which only a stream gives: the ticker's indices and an open option chain's contracts.

With a stream (Dhan's WebSocket feed, charts.DhanSource) the prices are pushed as they trade: each
round the thread tells the stream what is wanted, and polls only what the stream does not carry
(nothing, while it is connected). Without one, or while it is down, it polls as before.

Pure Python around the callables the app gives it:
  fetch(symbols) -> {symbol: price}     one request for all of them
  interval() -> seconds                 how often to ask (the source's real-time-ness)
  active() -> bool                      whether to ask at all (market open)
  stream.cover({symbol: mode}) -> set   the symbols the stream is carrying now (optional)
"""
import logging
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

WATCH_S = 30              # a symbol nobody asked about for this long is dropped
STREAM_ROUND_S = 0.5      # with everything streamed, how often the wanted set is passed on
RANK = {'ticker': 0, 'quote': 1, 'full': 2}
KEEP_S = 3 * 3600         # ticks kept per symbol
MAX_TICKS = 40000         # a stream sends every trade; a repeat of the same price within a second is dropped
REPEAT_S = 1.0


class Feed:
    def __init__(self, fetch: Callable[[List[str]], Dict[str, float]], interval: Callable[[], float],
                 active: Callable[[], bool] = lambda: True, name: str = 'livefeed', stream=None):
        self._fetch, self._interval, self._active, self.name = fetch, interval, active, name
        self.stream = stream
        self._lock = threading.Lock()
        self._watch: Dict[str, float] = {}
        self._mark: Dict[str, float] = {}       # latest price only (open positions)
        self._more: Dict[str, tuple] = {}       # symbol -> (time, 'quote' | 'full'): streamed only
        self._ticks: Dict[str, deque] = {}
        self._px: Dict[str, tuple] = {}         # symbol -> (time, price) of the latest poll
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self.error: Optional[str] = None
        self.last_poll = 0.0
        self.streamed = 0                       # symbols the stream carried in the last round
        self.polled = 0                         # and the ones polled

    # ------------------------------------------------------------ interest
    def watch(self, symbols: List[str]) -> None:
        now = time.time()
        with self._lock:
            for s in symbols:
                self._watch[s] = now
        self._start()

    def mark(self, symbols: List[str], mode: str = None) -> None:
        """Poll these too, keeping only the latest price (last()), until WATCH_S after the last call.
        `mode` 'quote' or 'full': stream them in that mode instead (not polled: only a stream has it)."""
        now = time.time()
        with self._lock:
            for s in symbols:
                if mode in ('quote', 'full'):
                    old = self._more.get(s)
                    keep = old and old[0] > now - WATCH_S and RANK[old[1]] > RANK[mode]
                    self._more[s] = (now, old[1] if keep else mode)
                else:
                    self._mark[s] = now
        self._start()

    def modes(self, plain: List[str] = None) -> Dict[str, str]:
        """Every symbol wanted now, with the mode it is wanted in (`plain`: watched(), if read already)."""
        out = {s: 'ticker' for s in (self.watched() if plain is None else plain)}
        with self._lock:
            for s, (_, m) in self._more.items():
                if RANK[m] > RANK.get(out.get(s), -1):
                    out[s] = m
        return out

    def watched(self) -> List[str]:
        cut = time.time() - WATCH_S
        with self._lock:
            for book in (self._watch, self._mark):
                for s in [s for s, t in book.items() if t < cut]:
                    del book[s]
            for s in [s for s, (t, _) in self._more.items() if t < cut]:
                del self._more[s]
            for s in [s for s in self._px if s not in self._watch and s not in self._mark and s not in self._more]:
                del self._px[s]
            return sorted(set(self._watch) | set(self._mark))

    # ------------------------------------------------------------ ticks
    def add(self, prices: Dict[str, float], at: Optional[float] = None) -> None:
        at = at or time.time()
        with self._lock:
            for s, p in prices.items():
                if p is None or not p > 0:
                    continue
                self._px[s] = (at, float(p))
                if (s in self._mark or s in self._more) and s not in self._watch and s not in self._ticks:
                    continue                            # marked only: no tick history
                q = self._ticks.setdefault(s, deque(maxlen=MAX_TICKS))
                if q and q[-1][1] == float(p) and at - q[-1][0] < REPEAT_S:
                    continue
                q.append((at, float(p)))
                while q and q[0][0] < at - KEEP_S:
                    q.popleft()

    def ticks(self, symbol: str, since: float = 0.0) -> List[tuple]:
        with self._lock:
            q = self._ticks.get(symbol)
            return [t for t in q if t[0] > since] if q else []

    def last(self, symbol: str) -> Optional[tuple]:
        """(time, price) of the latest poll that priced `symbol`."""
        with self._lock:
            hit = self._px.get(symbol)
            if hit:
                return hit
            q = self._ticks.get(symbol)
            return q[-1] if q else None

    def since(self, symbols, after: float) -> Dict[str, tuple]:
        """{symbol: (time, price)} of those priced after `after`: what a push has to send."""
        with self._lock:
            return {s: self._px[s] for s in symbols if s in self._px and self._px[s][0] > after}

    def first_at(self, symbol: str) -> Optional[float]:
        with self._lock:
            q = self._ticks.get(symbol)
            return q[0][0] if q else None

    def bars(self, symbol: str, seconds: int, shift: int = 0) -> List[list]:
        """Candles of `seconds` from the ticks: [[time + shift, open, high, low, close, 0], ...]."""
        out: List[list] = []
        for at, p in self.ticks(symbol):
            t = int(at // seconds * seconds) + shift
            if out and out[-1][0] == t:
                b = out[-1]
                b[2], b[3], b[4] = max(b[2], p), min(b[3], p), p
            else:
                out.append([t, p, p, p, p, 0])
        return out

    # ------------------------------------------------------------ the thread
    def _start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name=self.name)
            self._thread.start()

    def _run(self) -> None:
        while True:
            plain = self.watched()
            want = self.modes(plain)
            if not want:
                if self.stream:
                    self.stream.cover({})
                return                                  # restarted by the next watch()
            started, polled = time.time(), False
            try:
                if self._active():
                    streamed = self.stream.cover(want) if self.stream else set()
                    # Only plain interest is polled: 'quote' / 'full' marks are for the stream alone.
                    rest = [s for s in plain if s not in streamed]
                    self.streamed, self.polled = len(streamed), len(rest)
                    if rest:
                        got = self._fetch(rest)
                        self.add(got, time.time())
                        self.last_poll, polled = time.time(), True
                    self.error = None
                elif self.stream:
                    self.stream.cover({})
            except Exception as e:                      # a failed poll: try again next round
                self.error = str(e)[:200]
                logger.warning(f'{self.name}: poll failed: {self.error}')
                polled = True
            pause = self._interval() if polled else STREAM_ROUND_S
            time.sleep(max(0.2, pause - (time.time() - started)) if polled else pause)
