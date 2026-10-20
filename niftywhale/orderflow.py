"""
Orderflow: who was aggressive, from Dhan's live packets (dhanws.py), as 1-minute footprints.

Dhan's packets do not say whether a buyer or a seller started a trade, and one packet can stand for
several trades. So, per instrument (Book):

  size      the change in the day's volume since the previous packet (vol), which counts merged
            trades too; `ltq` is only the last trade's size (and a 2-byte number), so a packet whose
            volume change differs from it carried several trades: `merged`
  side      the trade price against the best bid and ask of the packet before (the quote it met): at
            or above the ask a buy, at or below the bid a sell (`by_quote`); in between, or with no
            quote yet, the tick rule: above the previous price a buy, below a sell, the same as the
            last call when unchanged (`by_tick`); with nothing to go on, `unknown`
  footprint per price level (a step about 1 basis point of the price, fixed for the day) the volume
            sold into the bid and bought from the ask; per bar its delta (buys - sells), the day's
            running delta (cvd) and the busiest level (poc)

The calls are an approximation: merged trades are booked at the last price, and a quote can change
between two packets. `merged`, `by_quote` and `by_tick` say how good the day's data was.

Readings over bars (all pure, for the chart and for the record at an intraday entry):
  stacked imbalances  3+ adjacent levels where one side traded IMBALANCE times the other side's
                      volume diagonally (buys at a level against sells one level below)
  absorption          a bar with heavy volume and a one-sided delta whose price did not follow:
                      sold into but closed in its upper half (bullish), or the reverse
  exhaustion          almost no aggressive volume at the bar's extreme: buyers stopped at the high
                      (bearish), sellers at the low (bullish)
  divergence          a new low with the running delta above its low of the bars before (bullish),
                      or a new high with it below its high (bearish)

Nothing here trades: the readings are recorded as context (context.py), not used as gates.
"""
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

BAR_S = 60
STEPS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0)
LEVEL_BPS = 1.0                 # a footprint level: the smallest step of STEPS at least this many bps of the price
IMBALANCE = 3.0                 # one side this many times the other, diagonally
IMBALANCE_MIN_SHARE = 0.02      # ... with at least this share of the bar's volume (no 2-lot "imbalances")
STACK = 3                       # adjacent imbalanced levels that make a stack
ABSORB_VOLUME = 1.5             # a bar's volume at least this many times the median of the bars before
ABSORB_DELTA = 0.2              # its delta at least this share of its volume, one way
ABSORB_LOOKBACK = 10
EXHAUST_SHARE = 0.1             # the extreme level's aggressive volume under this share of an average level's
DIVERGENCE_LOOKBACK = 10


def level_step(price: float) -> float:
    want = price * LEVEL_BPS / 10000
    return next((s for s in STEPS if s >= want), STEPS[-1])


def level_of(price: float, step: float) -> float:
    return round(math.floor(price / step + 1e-9) * step, 2)


class Book:
    """One instrument's orderflow for the day: feed it every packet (on_packet), get back each bar as
    its minute ends (on_packet or flush). Not thread-safe: one feed thread calls it."""

    def __init__(self, key: str):
        self.key = key
        self.day: Optional[str] = None
        self.step: Optional[float] = None
        self.vol: Optional[int] = None
        self.ltp: Optional[float] = None
        self.bid: Optional[float] = None
        self.ask: Optional[float] = None
        self.side = 0                               # the last call: +1 buy, -1 sell
        self.cvd = 0
        self.bar: Optional[Dict[str, Any]] = None
        self.levels: Dict[float, List[int]] = {}    # the open bar's: level -> [sold, bought, unknown]

    def on_packet(self, p: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
        """A packet with the day's volume (quote or full); others are ignored. Returns the bar this
        packet closed, if it began a new minute."""
        vol, ltp = p.get('vol'), p.get('ltp')
        if vol is None or not ltp or ltp <= 0:
            return None
        at = p.get('ltt') or now
        day = time_day(at)
        done = None
        if day != self.day:                         # a new session: the day's volume starts again
            done = self._close()
            self.day, self.step, self.vol, self.ltp, self.side, self.cvd = day, level_step(ltp), vol, ltp, 0, 0
            self._quote(p)
            return done
        start = int(at // BAR_S) * BAR_S
        if self.bar and start > self.bar['t']:
            done = self._close()
        dv = vol - (self.vol or 0)
        if dv < 0:                                  # the counter went back: take it as the new baseline
            self.vol = vol
            self._quote(p)
            return done
        if dv > 0:
            self._trade(start, ltp, dv, p.get('ltq'))
            self.vol, self.ltp = vol, ltp
        self._quote(p)                              # the quote after this packet: the next trade meets it
        return done

    def flush(self, now: float) -> Optional[Dict[str, Any]]:
        """The open bar, once its minute is over (an instrument that went quiet)."""
        if self.bar and now >= self.bar['t'] + BAR_S:
            return self._close()
        return None

    def _quote(self, p: Dict[str, Any]) -> None:
        bid, ask = p.get('bid'), p.get('ask')
        if bid and ask and 0 < bid < ask:
            self.bid, self.ask = bid, ask

    def _trade(self, start: int, price: float, size: int, ltq: Any) -> None:
        b = self.bar
        if b is None:
            b = self.bar = {'key': self.key, 't': start, 'open': price, 'high': price, 'low': price, 'close': price,
                            'step': self.step, 'buy': 0, 'sell': 0, 'unknown': 0, 'trades': 0, 'merged': 0,
                            'by_quote': 0, 'by_tick': 0}
            self.levels = {}
        b['high'], b['low'], b['close'] = max(b['high'], price), min(b['low'], price), price
        b['trades'] += 1
        if isinstance(ltq, int) and ltq > 0 and ltq != size:
            b['merged'] += 1
        side, how = self._side(price)
        if how:
            b[how] += 1
        lv = self.levels.setdefault(level_of(price, self.step), [0, 0, 0])
        if side > 0:
            b['buy'] += size
            lv[1] += size
        elif side < 0:
            b['sell'] += size
            lv[0] += size
        else:
            b['unknown'] += size
            lv[2] += size
        if side:
            self.side = side

    def _side(self, price: float) -> Tuple[int, Optional[str]]:
        if self.bid and self.ask:
            if price >= self.ask:
                return 1, 'by_quote'
            if price <= self.bid:
                return -1, 'by_quote'
        if self.ltp is not None and price != self.ltp:
            return (1 if price > self.ltp else -1), 'by_tick'
        if self.side:
            return self.side, 'by_tick'             # zero tick: as the last call
        return 0, None

    def _close(self) -> Optional[Dict[str, Any]]:
        b, self.bar = self.bar, None
        if b is None:
            return None
        b['delta'] = b['buy'] - b['sell']
        self.cvd += b['delta']
        b['cvd'] = self.cvd
        b['levels'] = [[px, s, by, u] for px, (s, by, u) in sorted(self.levels.items())]
        b['poc'] = max(b['levels'], key=lambda lv: lv[1] + lv[2] + lv[3])[0] if b['levels'] else None
        self.levels = {}
        return b


def time_day(epoch: float) -> str:
    """The IST date of an epoch second."""
    return datetime.fromtimestamp(epoch, IST).date().isoformat()



# ---------------------------------------------------------------- readings
def volume(bar: Dict[str, Any]) -> int:
    return bar['buy'] + bar['sell'] + bar.get('unknown', 0)


def stacked_imbalances(bar: Dict[str, Any]) -> Dict[str, List[List[float]]]:
    """{'buy': [[low level, high level], ...], 'sell': [...]}: runs of STACK+ adjacent imbalanced levels."""
    lv = bar.get('levels') or []
    total = volume(bar)
    floor = total * IMBALANCE_MIN_SHARE
    buys, sells = [], []
    for i, (px, sold, bought, _u) in enumerate(lv):
        # Against the neighbouring level only where it traded: the bar's edge has nothing to compare with.
        below = lv[i - 1][1] if i > 0 and abs(lv[i - 1][0] - (px - bar['step'])) < 1e-6 else None
        above = lv[i + 1][2] if i + 1 < len(lv) and abs(lv[i + 1][0] - (px + bar['step'])) < 1e-6 else None
        buys.append(below is not None and bought >= floor and bought > 0 and bought >= IMBALANCE * below)
        sells.append(above is not None and sold >= floor and sold > 0 and sold >= IMBALANCE * above)
    return {'buy': _runs(lv, buys, bar['step']), 'sell': _runs(lv, sells, bar['step'])}


def _runs(lv: List[list], flags: List[bool], step: float) -> List[List[float]]:
    out, run = [], []
    for (px, *_), on in zip(lv, flags):
        if on and run and abs(px - run[-1] - step) < 1e-6:
            run.append(px)
            continue
        if len(run) >= STACK:
            out.append([run[0], run[-1]])
        run = [px] if on else []
    if len(run) >= STACK:
        out.append([run[0], run[-1]])
    return out


def absorption(bars: List[Dict[str, Any]], i: int) -> int:
    """+1 bullish (sold into, held up), -1 bearish (bought into, held down), 0 neither, for bars[i]."""
    b = bars[i]
    before = [volume(x) for x in bars[max(0, i - ABSORB_LOOKBACK):i] if volume(x)]
    v = volume(b)
    if len(before) < 3 or not v or v < ABSORB_VOLUME * sorted(before)[len(before) // 2]:
        return 0
    rng = b['high'] - b['low']
    pos = (b['close'] - b['low']) / rng if rng > 0 else 0.5
    if b['delta'] <= -ABSORB_DELTA * v and pos >= 0.5:
        return 1
    if b['delta'] >= ABSORB_DELTA * v and pos <= 0.5:
        return -1
    return 0


def exhaustion(bar: Dict[str, Any]) -> int:
    """-1 buyers gave out at the high, +1 sellers gave out at the low, 0 neither (or both)."""
    lv = bar.get('levels') or []
    if len(lv) < 3:
        return 0
    avg = volume(bar) / len(lv)
    top = lv[-1][2] <= EXHAUST_SHARE * avg                 # bought at the highest level
    bottom = lv[0][1] <= EXHAUST_SHARE * avg               # sold at the lowest
    return 0 if top == bottom else (-1 if top else 1)


def divergence(bars: List[Dict[str, Any]], i: int) -> int:
    """+1 a new low without a new delta low, -1 a new high without a new delta high, 0 neither."""
    before = bars[max(0, i - DIVERGENCE_LOOKBACK):i]
    if len(before) < 3:
        return 0
    b = bars[i]
    if b['low'] < min(x['low'] for x in before) and b['cvd'] > min(x['cvd'] for x in before):
        return 1
    if b['high'] > max(x['high'] for x in before) and b['cvd'] < max(x['cvd'] for x in before):
        return -1
    return 0


def quality(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How far the calls can be trusted: the share of packets that carried several trades, and of
    trades called by the tick rule (no quote to go on) or not at all."""
    trades = sum(b['trades'] for b in bars)
    vol = sum(volume(b) for b in bars)
    return {'bars': len(bars), 'trades': trades,
            'merged': round(sum(b['merged'] for b in bars) / trades, 3) if trades else None,
            'by_tick': round(sum(b['by_tick'] for b in bars) / trades, 3) if trades else None,
            'unknown_volume': round(sum(b.get('unknown', 0) for b in bars) / vol, 3) if vol else None}


def reading(bars: List[Dict[str, Any]], start: float, end: float, side: str) -> Dict[str, Any]:
    """Orderflow over [start, end) (epoch seconds) for a trade on `side` ('long' / 'short'), from a day's
    bars oldest first: the window's delta and its share of volume, and whether any bar in it showed
    absorption, divergence, exhaustion or a stacked imbalance with the trade (+1 each), against it, or
    not. `coverage` is the share of the window's minutes that had bars (a quiet or missing feed is low)."""
    sign = 1 if side == 'long' else -1
    idx = [i for i, b in enumerate(bars) if start <= b['t'] < end]
    minutes = max(1, int(math.ceil((end - start) / BAR_S)))
    out = {'coverage': round(min(1.0, len(idx) / minutes), 2), 'bars': len(idx)}
    if not idx:
        return out
    win = [bars[i] for i in idx]
    vol = sum(volume(b) for b in win)
    delta = sum(b['delta'] for b in win)
    marks = {'absorption': [absorption(bars, i) for i in idx], 'divergence': [divergence(bars, i) for i in idx],
             'exhaustion': [exhaustion(b) for b in win]}
    stacks = [stacked_imbalances(b) for b in win]
    marks['stacked'] = [(1 if s['buy'] else 0) - (1 if s['sell'] else 0) for s in stacks]
    out.update(delta=delta, delta_share=round(delta / vol, 3) if vol else None, volume=vol,
               **{k: _with(v, sign) for k, v in marks.items()})
    return out


def _with(marks: List[int], sign: int) -> int:
    """+1 if any mark is with the trade, -1 if only against it, 0 if none."""
    if any(m == sign for m in marks):
        return 1
    return -1 if any(m == -sign for m in marks) else 0
