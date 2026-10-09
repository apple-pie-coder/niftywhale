"""
Demo funds: a pretend trading account that takes every trade the app takes, at real tradable
quantities, so the paper record turns into rupees: what each trade made or lost after charges,
what the account is worth, what is tied up in open positions.

Each trade the app opens (a swing CHoCH entry, an intraday entry, an option idea) is sized here
from the account at that moment, on the instrument you would actually trade:

  swing long     the shares, bought for delivery (CNC): any whole number of shares, paid in full
  swing short    the stock's futures (NRML), in whole lots: a cash short can't be held overnight;
                 margin blocked: FUTURES_MARGIN_PCT of the contract value (an approximation of
                 NSE's SPAN + exposure margin)
  intraday       the shares, intraday (MIS), long or short: margin = value / INTRADAY_LEVERAGE
  options        the option bought, in whole lots: the premium paid in full

Lot sizes are NSE's own, from Dhan's instrument master (dhan.lot_size). The quantity risks
`risk_pct` of the account between entry and stop, and no position ties up more than
`max_alloc_pct` of it; a trade that can't be funded (or where one lot already risks too much)
is recorded as skipped, with the reason, so the record shows what the account could not take.

Charges are what a contract note would show (RATES: Dhan's brokerage, STT, NSE's transaction
charge and IPFT, SEBI fee, stamp duty, GST, the DP charge on delivery sales), plus a roll for
each expiry a futures position is held across: computed from the published rates, not read off
a real contract note.

Pure functions; the app (app.demo_sync) applies them to its trades and keeps the account.
"""
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

DEFAULTS: Dict[str, Any] = {
    'risk_pct': 1.0,              # of the account, risked between entry and stop on each trade
    'max_alloc_pct': 25.0,        # most of the account one position may tie up (value, margin or premium)
    'intraday_leverage': 5.0,     # MIS: margin = value / this
    'futures_margin_pct': 20.0,   # stock futures: margin as % of the contract value (approx.)
    'charges': True,
    'swing': True, 'intraday': True, 'options': True,
    # Hard stops on how many trades the account takes (0 = no limit): open at once, all modes
    # together, and new entries a day. Once one is reached, further trades are skipped.
    'max_open': 20, 'max_per_day': 20,
}
LIMITS = {'risk_pct': (0.1, 10.0), 'max_alloc_pct': (1.0, 100.0), 'intraday_leverage': (1.0, 5.0),
          'futures_margin_pct': (5.0, 50.0), 'max_open': (0, 200), 'max_per_day': (0, 500)}
COUNTS = ('max_open', 'max_per_day')       # whole numbers

PRODUCTS = {'CNC': 'Delivery', 'MIS': 'Intraday', 'FUT': 'Futures', 'OPT': 'Option'}


def settings_from(saved: Any) -> Dict[str, Any]:
    """Saved demo settings made safe: numbers clamped to LIMITS, switches as booleans."""
    saved = saved if isinstance(saved, dict) else {}
    out = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        if k not in saved:
            continue
        v = saved[k]
        if isinstance(default, bool):
            out[k] = v in (True, 1, '1', 'true', 'on')
            continue
        try:
            v = float(v)
            if not math.isfinite(v):
                raise ValueError
        except (TypeError, ValueError):
            continue
        lo, hi = LIMITS[k]
        out[k] = int(round(min(hi, max(lo, v)))) if k in COUNTS else round(min(hi, max(lo, v)), 2)
    return out


def size(trade: Dict[str, Any], funds: float, free: float, st: Dict[str, Any],
         lot: Optional[int] = None) -> Dict[str, Any]:
    """
    How much of `trade` the account takes. `trade` has mode, side ('long' / 'short'), entry,
    stop; `funds` is the account's value for sizing (deposits - withdrawals + realised P&L),
    `free` what is not tied up in open positions; `lot` the contract's lot size (futures,
    options). Returns {product, qty, lots, lot_size, margin, risk, value} or {skip: why}.
    """
    mode, side = trade['mode'], trade.get('side') or 'long'
    entry, stop = float(trade.get('entry') or 0), float(trade.get('stop') or 0)
    if entry <= 0 or stop <= 0 or entry == stop:
        return {'skip': 'no usable entry and stop'}
    if funds <= 0:
        return {'skip': 'no demo funds yet'}
    risk_budget = funds * st['risk_pct'] / 100
    cap = min(funds * st['max_alloc_pct'] / 100, free)
    per_unit = abs(entry - stop)
    avail = f'the ₹{cap:,.0f} available'

    if mode == 'options':
        if not lot:
            return {'skip': 'no lot size for this option'}
        lots = math.floor(risk_budget / (per_unit * lot))
        lots = min(lots, math.floor(cap / (entry * lot)))
        if lots < 1:
            one = entry * lot
            why = (f'one lot costs ₹{one:,.0f}, more than {avail}' if one > cap
                   else f'one lot risks ₹{per_unit * lot:,.0f}, over the ₹{risk_budget:,.0f} allowed')
            return {'skip': why}
        qty = lots * lot
        return {'product': 'OPT', 'qty': qty, 'lots': lots, 'lot_size': lot, 'margin': round(qty * entry, 2),
                'risk': round(qty * per_unit, 2), 'value': round(qty * entry, 2)}

    if mode == 'swing' and side == 'short':
        if not lot:
            return {'skip': 'not an F&O stock: a swing short needs its futures'}
        margin_per_lot = entry * lot * st['futures_margin_pct'] / 100
        lots = min(math.floor(risk_budget / (per_unit * lot)), math.floor(cap / margin_per_lot))
        if lots < 1:
            why = (f'one lot needs ₹{margin_per_lot:,.0f} margin, more than {avail}'
                   if margin_per_lot > cap else f'one lot risks ₹{per_unit * lot:,.0f}, over the ₹{risk_budget:,.0f} allowed')
            return {'skip': why}
        qty = lots * lot
        return {'product': 'FUT', 'qty': qty, 'lots': lots, 'lot_size': lot, 'margin': round(lots * margin_per_lot, 2),
                'risk': round(qty * per_unit, 2), 'value': round(qty * entry, 2)}

    if mode == 'intraday':
        lev = st['intraday_leverage']
        qty = min(math.floor(risk_budget / per_unit), math.floor(cap * lev / entry))
        if qty < 1:
            return {'skip': f'one share needs ₹{entry / lev:,.0f} margin, more than {avail}'}
        return {'product': 'MIS', 'qty': qty, 'lots': None, 'lot_size': 1, 'margin': round(qty * entry / lev, 2),
                'risk': round(qty * per_unit, 2), 'value': round(qty * entry, 2)}

    # Swing long: delivery, paid in full.
    qty = min(math.floor(risk_budget / per_unit), math.floor(cap / entry))
    if qty < 1:
        return {'skip': f'one share costs ₹{entry:,.0f}, more than {avail}'}
    return {'product': 'CNC', 'qty': qty, 'lots': None, 'lot_size': 1, 'margin': round(qty * entry, 2),
            'risk': round(qty * per_unit, 2), 'value': round(qty * entry, 2)}


# The charges on an NSE trade through Dhan, as rates of the traded value (premium for options).
# Sources, checked 2026-10-08:
#   brokerage   dhan.co/pricing: delivery free; intraday Rs 20 or 0.03 % an executed order,
#               whichever is lower; F&O Rs 20 an executed order
#   STT         Finance Act 2026, from 2026-04-01: delivery 0.1 % on buy and sell; intraday
#               0.025 % on the sell; futures 0.05 % on the sell; options 0.15 % of the premium sold
#   exchange    NSE circular NSE/FA/73061, from 2026-03-01, a side: cash Rs 306.99 a crore,
#               equity futures Rs 182.99, equity options Rs 3,552.99 of premium
#   IPFT        the same circular: Rs 0.01 a crore a side (NSE's investor protection fund)
#   SEBI fee    Rs 10 a crore a side
#   stamp duty  on the buy only: delivery 0.015 %, intraday 0.003 %, futures 0.002 %, options 0.003 %
#   GST         18 % of brokerage + exchange + IPFT + SEBI fee (+ the DP charge)
#   DP charge   Dhan Rs 12.50 + GST a scrip sold out of the demat account (delivery sells)
SCHEDULE_AS_OF = '2026-04-01'
RATES = {
    #        brokerage per order                       STT (buy, sell)           exchange txn           stamp (buy)
    'CNC': {'brok': lambda v: 0.0,                     'stt': (0.001, 0.001),    'exch': 306.99 / 1e7,  'stamp': 0.00015},
    'MIS': {'brok': lambda v: min(20.0, v * 0.0003),   'stt': (0.0, 0.00025),    'exch': 306.99 / 1e7,  'stamp': 0.00003},
    'FUT': {'brok': lambda v: 20.0,                    'stt': (0.0, 0.0005),     'exch': 182.99 / 1e7,  'stamp': 0.00002},
    'OPT': {'brok': lambda v: 20.0,                    'stt': (0.0, 0.0015),     'exch': 3552.99 / 1e7, 'stamp': 0.00003},
}
IPFT = 0.01 / 1e7        # Rs 0.01 a crore a side
SEBI = 10 / 1e7          # Rs 10 a crore a side
GST = 0.18
DP_CHARGE = 12.5         # a scrip sold out of the demat account (delivery), plus GST
CHARGE_KEYS = ('brokerage', 'stt', 'exchange', 'ipft', 'sebi', 'stamp', 'dp', 'gst')


def charges(product: str, buy_value: float, sell_value: float) -> Dict[str, float]:
    """The charges on a round trip (one buy and one sell order), in rupees, line by line as on a
    contract note, plus their total."""
    r = RATES[product]
    turnover = buy_value + sell_value
    brok = r['brok'](buy_value) + r['brok'](sell_value)
    stt = buy_value * r['stt'][0] + sell_value * r['stt'][1]
    exch = turnover * r['exch']
    ipft = turnover * IPFT
    sebi = turnover * SEBI
    stamp = buy_value * r['stamp']
    dp = DP_CHARGE if product == 'CNC' else 0.0
    gst = (brok + exch + ipft + sebi + dp) * GST
    out = dict(zip(CHARGE_KEYS, (brok, stt, exch, ipft, sebi, stamp, dp, gst)))
    out = {k: round(v, 2) for k, v in out.items()}
    out['total'] = round(sum(out.values()), 2)
    return out


def _day(when: Any) -> Optional[date]:
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    try:
        return datetime.fromisoformat(str(when)).date()
    except (TypeError, ValueError):
        return None


def monthly_expiries(start: date, end: date) -> List[date]:
    """NSE's monthly stock-futures expiries from `start` to `end`: the last Tuesday of each month
    (since September 2025). A holiday moves an expiry a day or two earlier; that is not modelled."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        last = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
        exp = last - timedelta(days=(last.weekday() - 1) % 7)
        if start <= exp <= end:
            out.append(exp)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def rollovers(entry_time: Any, exit_time: Any) -> int:
    """How many times a futures position opened at `entry_time` and closed at `exit_time` had to
    be rolled to the next month: once for each expiry strictly between the two days (one opened
    on an expiry day takes the next month; one closed on an expiry day just closes)."""
    a, b = _day(entry_time), _day(exit_time)
    if not a or not b or b <= a:
        return 0
    return sum(1 for e in monthly_expiries(a, b) if a < e < b)


def settle(pos: Dict[str, Any], exit_price: float, with_charges: bool = True,
           exit_time: Any = None) -> Dict[str, Any]:
    """A closed position's result: gross P&L, charges and net, in rupees. A futures position held
    across an expiry also pays for each roll (closing the expiring month and opening the next: a
    round trip, valued at the entry price)."""
    qty, entry = pos['qty'], float(pos['entry'])
    sign = -1 if pos['side'] == 'short' else 1
    gross = round((exit_price - entry) * qty * sign, 2)
    if not with_charges:
        return {'gross': gross, 'charges': 0.0, 'charges_detail': {'total': 0.0}, 'net': gross}
    buy, sell = (qty * entry, qty * exit_price) if sign > 0 else (qty * exit_price, qty * entry)
    ch = charges(pos['product'], buy, sell)
    rolls = rollovers(pos.get('entry_time'), exit_time) if pos['product'] == 'FUT' else 0
    if rolls:
        roll = charges('FUT', qty * entry, qty * entry)
        ch = {k: round(ch[k] + rolls * roll[k], 2) for k in ch}
        ch['rollovers'] = rolls
    return {'gross': gross, 'charges': ch['total'], 'charges_detail': ch, 'net': round(gross - ch['total'], 2)}


def mark(pos: Dict[str, Any], last: Optional[float]) -> Optional[float]:
    """An open position's unrealised P&L at `last` (before charges)."""
    if last is None:
        return None
    sign = -1 if pos['side'] == 'short' else 1
    return round((float(last) - float(pos['entry'])) * pos['qty'] * sign, 2)
