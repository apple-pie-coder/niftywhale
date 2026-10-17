"""
Years of NSE's dated files, so the context live trades record can be backtested too (and tested as
gates through the walk-forward and the shadow run):

  delivery      sec_bhavdata_full, every stock's delivery % a day (smart.parse_bhav)       sm_delivery
  positioning   fao_participant_oi, FII / DII / Pro / Client index futures (participant OI) sm_poi
  futures OI    the F&O bhavcopy, each stock's futures open interest (smart.parse_fo_bhav)  sm_futoi
  results dates NSE's event calendar, board meetings past and coming (smart.parse_events)  sm_events

run() fills whatever days are missing within the lab's history (the first run is long, an hour or two
at NSE's pace; after that a night adds one day), and re-reads the event calendar's recent and coming
months. NseHistory hands the backtester, per entry, what app.smart_context hands a live one, from the
same functions (smart.delivery_read, smart.positioning, features.futures_context / results_context).
FII / DII cash flows have no dated archive (NSE's API answers only the latest day): their history is
what the live app has collected.
"""
import bisect
import logging
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from niftywhale import features, history, smart, store

logger = logging.getLogger(__name__)

MAX_ERRORS = 10              # NSE refusing this many days in a row: stop for tonight, carry on tomorrow
EVENT_REREAD_MONTHS = 2      # the last months (and the next one) are read again each run: meetings get added


def trading_days(first: date, last: date) -> List[date]:
    """The sessions between two days, from the market history (Nifty's daily candles)."""
    m = history.load('market', 'NIFTY_VIX')
    if m is None or m.empty:
        return []
    return [d for d in sorted({ix.date() for ix in m.index}) if first <= d <= last]


def run(progress: Callable[[str], None], years: float, stop: Callable[[], bool] = lambda: False,
        symbols: Optional[List[str]] = None) -> Dict[str, int]:
    """Fill the missing days of the three dated files, then the event calendar. Returns what was added.
    `symbols`: the stocks whose delivery rows are kept (all of a day's would be millions of rows)."""
    today = store.now_local().date()
    days = trading_days(today - timedelta(days=int(365 * years)), today)
    keep = set(symbols or [])
    # What the live app's prune_smart leaves alone, and which stocks have their delivery history in full:
    # a stock new to the lab (its universe changed) has none, so every delivery day is read again, once.
    filled = set((store.get_json('backfill:delivery_keep') or {}).get('filled') or []) & keep
    record = {'since': days[0].isoformat() if days else today.isoformat(), 'symbols': sorted(keep)}
    if keep:
        store.set_json('backfill:delivery_keep', {**record, 'filled': sorted(filled)})
    held = {t: set(store.smart_days(t)) for t in ('sm_delivery', 'sm_poi', 'sm_futoi')}
    if keep - filled:
        held['sm_delivery'] = set()
    todo = [d for d in days if any(d.isoformat() not in held[t] for t in held)]
    f = smart.Fetcher()
    added = {'delivery': 0, 'positioning': 0, 'futures': 0, 'events': 0}
    errors = 0
    for i, d in enumerate(todo):
        if stop():
            break
        iso = d.isoformat()
        progress(f'NSE files {i + 1}/{len(todo)} ({iso})')
        try:
            if iso not in held['sm_delivery']:
                rows = f.bhav(d)
                if rows:
                    store.add_delivery(iso, {k: v for k, v in rows.items() if not keep or k in keep})
                    added['delivery'] += 1
            if iso not in held['sm_poi']:
                poi = f.participant_oi(d)
                if poi:
                    store.set_poi(iso, poi)
                    added['positioning'] += 1
            if iso not in held['sm_futoi']:
                fut = f.fo_bhav(d)
                if fut:
                    store.add_futoi(iso, fut)
                    added['futures'] += 1
            errors = 0
        except Exception as e:                       # a day NSE will not serve now: tomorrow's run tries again
            errors += 1
            logger.warning(f'NSE files for {iso}: {str(e)[:160]}')
            if errors >= MAX_ERRORS:
                logger.warning(f'NSE refused {MAX_ERRORS} days in a row: the rest waits for the next run')
                break
    else:
        if keep:                                     # a day that failed is missing by date: the next run reads it
            store.set_json('backfill:delivery_keep', {**record, 'filled': sorted(keep)})
    added['events'] = events(f, days[0] if days else today, today, progress)
    return added


def events(f: smart.Fetcher, first: date, today: date, progress: Callable[[str], None]) -> int:
    """The event calendar a month at a time: months not read yet, and the recent and coming ones again."""
    done = set(store.event_months())
    months, cur = [], date(first.year, first.month, 1)
    end = date(today.year + (today.month // 12), today.month % 12 + 1, 1)          # the coming month too
    recent = {(date(today.year, today.month, 1) - timedelta(days=31 * k)).strftime('%Y-%m') for k in range(EVENT_REREAD_MONTHS)}
    while cur <= end:
        key = cur.strftime('%Y-%m')
        if key not in done or key in recent or cur > today:
            months.append(cur)
        cur = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
    n = 0
    for k, m in enumerate(months):
        last = date(m.year + m.month // 12, m.month % 12 + 1, 1) - timedelta(days=1)
        progress(f'NSE results calendar {k + 1}/{len(months)} ({m:%Y-%m})')
        try:
            n += store.add_events(f.events(m, last))
            if last < today:
                done.add(m.strftime('%Y-%m'))
        except Exception as e:
            logger.warning(f'event calendar {m:%Y-%m}: {str(e)[:160]}')
    store.set_json('backfill:event_months', sorted(done))
    return n


class NseHistory:
    """The backfilled files for the backtester, loaded once for its stocks. context(symbol, day) is what a
    live entry's app.smart_context records (delivery read, FII positioning) plus futures OI and results
    timing, as of the session before `day`. No 'sm' flag: that marks the live set, deals included."""

    def __init__(self, symbols: List[str]):
        self.deliv = store.series('sm_delivery', symbols, 'close, prev_close, qty, deliv_qty, deliv_pct')
        self.fut = store.series('sm_futoi', symbols, 'oi, oi_chg, close')
        self.deliv_days = {s: [r['day'] for r in rows] for s, rows in self.deliv.items()}
        self.fut_days = {s: [r['day'] for r in rows] for s, rows in self.fut.items()}
        self.poi = store.poi(100_000)
        self.poi_days = [r['day'] for r in self.poi]
        self.results = store.results_dates(symbols)

    def context(self, symbol: str, day: date) -> Dict[str, Any]:
        iso, out = day.isoformat(), {}
        rows = self.deliv.get(symbol)
        if rows:
            i = bisect.bisect_left(self.deliv_days[symbol], iso)
            rd = smart.delivery_read(rows[max(0, i - smart.DELIV_SESSIONS - 1):i])
            if rd:
                out.update(deliv_read=rd['read'], deliv_ratio=rd['ratio'], deliv_vol_ratio=rd['vol_ratio'])
        i = bisect.bisect_left(self.poi_days, iso)
        if i:
            fii = (smart.positioning(self.poi[max(0, i - 2):i]).get('latest') or {}).get('FII') or {}
            if fii.get('long_pct') is not None:
                out['fii_long_pct'] = fii['long_pct']
        rows = self.fut.get(symbol)
        if rows:
            i = bisect.bisect_left(self.fut_days[symbol], iso)
            out.update(features.futures_context(rows[max(0, i - features.FUT_DAYS - 1):i]))
        out.update(features.results_context(self.results.get(symbol, []), day))
        return out
