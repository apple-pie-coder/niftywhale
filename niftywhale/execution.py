"""
Live trading: what the app does with a trade on a real Dhan account. Pure functions; app.exec_sync applies
them to the app's trades (the same ones the paper journal and the demo account follow) and broker.py sends.

Each mode (swing, intraday, options) has its own level:

  off       nothing beyond the paper journal and the demo account
  shadow    every trade becomes the exact order it would send (instrument, quantity, prices, legs), logged and
            followed to its exit in rupees; nothing is sent
  confirm   the order is offered (the Trading tab, a Telegram notice) and sent only when you press Place, within
            `offer_minutes` and while the price is still near the alert's
  auto      sent on the trigger

Confirm and auto arm only from a fresh sign-in, with real sending on (the static IP Dhan requires) and the
mode's evidence passing (gate): the backtest positive after costs, positive on its last months, and the paper
record positive after costs over enough trades.

Every entry is one super order (entry, target and stop loss together): Dhan accepts CNC, INTRADAY and MARGIN for
it. Prices are limits a few ticks through the alert's price, never market orders. Sizing is the demo account's
(demo.size: risk_pct of the capital between entry and stop, max_alloc_pct of it at most) on the instrument you
would trade: the shares (CNC) for a swing long, the near-month future (MARGIN) for a swing short, the shares
intraday (INTRADAY), the option bought (MARGIN). A swing holding whose super-order legs did not carry over to
the next session gets its stop and target back as a forever OCO (protect_body); Dhan's page describes the OCO's
legs two ways, so the reading here (the first leg the target, the second the stop) is checked in Dhan's sandbox
before real sending is switched on.

Each order carries a correlation ID made from the trade's ref: before any retry the app asks Dhan for it, so a
lost answer never turns into two orders.
"""
import math
import re
from typing import Any, Dict, List, Optional

LEVELS = ('off', 'shadow', 'confirm', 'auto')
LEVEL_LABEL = {'off': 'Off', 'shadow': 'Shadow', 'confirm': 'Confirm each', 'auto': 'Automatic'}
REAL = ('confirm', 'auto')            # levels that send
MODES = ('swing', 'intraday', 'options')

DEFAULTS: Dict[str, Any] = {
    'swing': 'off', 'intraday': 'off', 'options': 'off',
    'capital': 0.0,             # ₹ to size from; 0: the account's available balance at Dhan
    'sizing': 'risk',           # 'risk': risk_pct of the capital a trade; 'lots': `lots` lots a trade, whatever the stop
    'lots': 1,                  # fixed lots: options and stock futures in their exchange lots, shares one at a time
    'risk_pct': 0.25,           # of the capital, between entry and stop, each trade
    'max_alloc_pct': 20.0,      # most of the capital one position may tie up
    'intraday_leverage': 5.0,   # INTRADAY margin = value / this (Dhan sets the real figure per stock)
    'futures_margin_pct': 20.0,
    'max_open': 5,              # positions open at once, all modes
    'max_per_day': 5,           # new entries a day
    'daily_loss_pct': 1.0,      # of the capital: the day's realised + open loss that disarms everything
    'max_trade_loss': 5000.0,   # ₹: no single trade may risk more
    'buffer_bps': 10.0,         # the entry limit this far through the alert's price
    'max_drift_pct': 0.5,       # skip if the price has run this far from the entry toward the target
    'offer_minutes': 5,         # confirm: an offer lapses after this
    'shorts': False,            # swing shorts (stock futures) live
}
LIMITS = {'capital': (0.0, 1e9), 'lots': (1, 100), 'risk_pct': (0.05, 2.0), 'max_alloc_pct': (1.0, 100.0), 'intraday_leverage': (1.0, 5.0),
          'futures_margin_pct': (5.0, 50.0), 'max_open': (1, 50), 'max_per_day': (1, 100), 'daily_loss_pct': (0.1, 10.0),
          'max_trade_loss': (100.0, 1e7), 'buffer_bps': (0.0, 100.0), 'max_drift_pct': (0.05, 5.0), 'offer_minutes': (1, 60)}
COUNTS = ('max_open', 'max_per_day', 'offer_minutes', 'lots')
CHOICES = {'sizing': ('risk', 'lots')}
TICK = 0.05                   # every price a multiple of 5 paise: valid at every NSE tick size

# The evidence a mode needs before it may send (gate).
GATE = {'bt_trades': {'swing': 60, 'intraday': 100, 'options': 30}, 'paper_trades': 30}


def settings_from(saved: Any, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Saved settings made safe: levels from LEVELS, numbers clamped to LIMITS, switches as booleans. `base`: the
    settings a change applies to (else the defaults); a value that is not valid leaves its setting as it was there."""
    saved = saved if isinstance(saved, dict) else {}
    out = dict(base or DEFAULTS)
    for k, default in DEFAULTS.items():
        if k not in saved:
            continue
        v = saved[k]
        if k in MODES:
            out[k] = v if v in LEVELS else out[k]
        elif k in CHOICES:
            out[k] = v if v in CHOICES[k] else out[k]
        elif isinstance(default, bool):
            out[k] = v in (True, 1, '1', 'true', 'on')
        else:
            try:
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError
            except (TypeError, ValueError):
                continue
            lo, hi = LIMITS[k]
            out[k] = int(round(min(hi, max(lo, v)))) if k in COUNTS else round(min(hi, max(lo, v)), 2)
    return out


def fixed_size(trade: Dict[str, Any], lots: int, lot: Optional[int], free: float, st: Dict[str, Any]) -> Dict[str, Any]:
    """`lots` lots of the instrument a trade is taken on, whatever its stop (sizing 'lots'): options and stock
    futures in their exchange lots (`lot`), shares one at a time (an NSE equity lot is one share). The same
    instruments and products as demo.size; skipped, with why, when the money it ties up is more than `free`.
    The per-trade loss cap still applies afterwards (guards)."""
    mode, side = trade['mode'], trade.get('side') or 'long'
    entry, stop = float(trade.get('entry') or 0), float(trade.get('stop') or 0)
    if entry <= 0 or stop <= 0 or entry == stop:
        return {'skip': 'no usable entry and stop'}
    n, per_unit = int(lots), abs(entry - stop)
    if mode == 'options' or (mode == 'swing' and side == 'short'):
        if not lot:
            return {'skip': 'no lot size for this option' if mode == 'options' else 'not an F&O stock: a swing short needs its futures'}
        qty = n * lot
        product = 'OPT' if mode == 'options' else 'FUT'
        margin = qty * entry if product == 'OPT' else qty * entry * st['futures_margin_pct'] / 100
        lots_out, lot_size = n, lot
    else:
        qty, lots_out, lot_size = n, None, 1
        product = 'MIS' if mode == 'intraday' else 'CNC'
        margin = qty * entry / st['intraday_leverage'] if product == 'MIS' else qty * entry
    if margin > free + 1e-9:
        what = f"{n} lot{'s' if n != 1 else ''}" if lots_out else f"{qty} share{'s' if qty != 1 else ''}"
        return {'skip': f'{what} need ₹{margin:,.2f}, more than the ₹{max(0.0, free):,.2f} available'}
    return {'product': product, 'qty': qty, 'lots': lots_out, 'lot_size': lot_size, 'margin': round(margin, 2),
            'risk': round(qty * per_unit, 2), 'value': round(qty * entry, 2)}


def correlation_id(ref: str) -> str:
    """Our ID on Dhan's side: at most 30 characters of letters, digits, - and _."""
    return ('nw-' + re.sub(r'[^A-Za-z0-9_-]', '', str(ref)))[:30]


def tick(px: float, up: bool) -> float:
    """A price on the tick grid, rounded up or down."""
    n = px / TICK
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * TICK, 2)


def entry_limit(px: float, buy: bool, bps: float) -> float:
    """The entry's limit: `bps` through the price (above it to buy, below it to sell), on the tick grid."""
    return tick(px * (1 + bps / 1e4) if buy else px * (1 - bps / 1e4), up=buy)


PRODUCT = {'CNC': ('NSE_EQ', 'CNC'), 'MIS': ('NSE_EQ', 'INTRADAY'), 'FUT': ('NSE_FNO', 'MARGIN'), 'OPT': (None, 'MARGIN')}


def plan(trade: Dict[str, Any], sz: Dict[str, Any], inst: Dict[str, Any], st: Dict[str, Any],
         client_id: str) -> Dict[str, Any]:
    """
    The order for `trade` ({ref, mode, symbol, side, entry, stop, target}), sized by demo.size (`sz`: product,
    qty, risk, margin), on `inst` ({security_id, segment?, basis?}: the instrument; `basis` the future's price
    over the stock's, which moves a swing short's prices onto the future). Returns {segment, product, buy,
    security_id, qty, price, stop, target, correlation_id, body} or {skip: why}.
    """
    if not inst.get('security_id'):
        return {'skip': 'no Dhan security ID for this instrument'}
    seg, product = PRODUCT[sz['product']]
    seg = inst.get('segment') or seg
    short = trade['mode'] != 'options' and trade.get('side') == 'short'
    buy = not short
    shift = float(inst.get('basis') or 0) if sz['product'] == 'FUT' else 0.0
    entry, stop, target = (float(trade[k]) + shift for k in ('entry', 'stop', 'target'))
    price = entry_limit(entry, buy, st['buffer_bps'])
    # The stop is rounded away from the entry and the target toward it: neither ends up nearer the risk.
    stop_px, target_px = tick(stop, up=short), tick(target, up=short)
    if (buy and not stop_px < price < target_px) or (not buy and not target_px < price < stop_px):
        return {'skip': f'the stop {stop_px:g} and target {target_px:g} do not sit either side of the limit {price:g}'}
    cid = correlation_id(trade['ref'])
    body = {'dhanClientId': client_id, 'correlationId': cid, 'transactionType': 'BUY' if buy else 'SELL',
            'exchangeSegment': seg, 'productType': product, 'orderType': 'LIMIT', 'securityId': str(inst['security_id']),
            'quantity': int(sz['qty']), 'price': price, 'targetPrice': target_px, 'stopLossPrice': stop_px, 'trailingJump': 0}
    return {'segment': seg, 'product': product, 'buy': buy, 'security_id': str(inst['security_id']), 'qty': int(sz['qty']),
            'price': price, 'stop': stop_px, 'target': target_px, 'correlation_id': cid, 'body': body}


def protect_body(p: Dict[str, Any], client_id: str) -> Dict[str, Any]:
    """A forever OCO holding a swing long's target (first leg) and stop (second leg), for when the super
    order's legs are gone. The stop leg's limit sits 0.5 % under its trigger, so a gap still fills."""
    qty = int(p['qty'])
    return {'dhanClientId': client_id, 'correlationId': (p['correlation_id'] + '-p')[:30], 'orderFlag': 'OCO',
            'transactionType': 'SELL', 'exchangeSegment': p['segment'], 'productType': 'CNC', 'orderType': 'LIMIT',
            'validity': 'DAY', 'securityId': p['security_id'], 'quantity': qty,
            'price': p['target'], 'triggerPrice': p['target'],
            'price1': tick(p['stop'] * 0.995, up=False), 'triggerPrice1': p['stop'], 'quantity1': qty}


def guards(p: Dict[str, Any], trade: Dict[str, Any], book: Dict[str, Any], st: Dict[str, Any],
           capital: float, last: Optional[float], session_open: bool) -> List[str]:
    """Why this order must not go now (empty: it may). `book`: {open, today, day_pnl} of the live orders."""
    out = []
    if not session_open:
        out.append('the market is closed for this instrument')
    if trade['mode'] == 'swing' and trade.get('side') == 'short' and not st['shorts']:
        out.append('swing shorts are not live (Trading settings → Shorts)')
    if book['open'] >= st['max_open']:
        out.append(f"{book['open']} positions open: the most at once is {st['max_open']}")
    if book['today'] >= st['max_per_day']:
        out.append(f"{book['today']} entries today: the day's limit is {st['max_per_day']}")
    limit = capital * st['daily_loss_pct'] / 100
    if capital and book['day_pnl'] <= -limit:
        out.append(f"today's loss ₹{-book['day_pnl']:,.0f} has reached the limit ₹{limit:,.0f}")
    risk = float(p.get('risk') or 0)
    if risk > st['max_trade_loss']:
        out.append(f"it risks ₹{risk:,.0f}, over the ₹{st['max_trade_loss']:,.0f} a trade may")
    if last:
        entry, stop = float(trade['entry']), float(trade['stop'])
        up = trade['mode'] == 'options' or trade.get('side') != 'short'
        ran = (last - entry) / entry * 100 * (1 if up else -1)
        if (up and last <= stop) or (not up and last >= stop):
            out.append(f'the price {last:g} is already through the stop {stop:g}')
        elif ran > st['max_drift_pct']:
            out.append(f"the price {last:g} has run {ran:.2f}% from the entry {entry:g} (the most is {st['max_drift_pct']:g}%)")
    return out


def gate(mode: str, ev: Dict[str, Any]) -> Dict[str, Any]:
    """Whether `mode` may trade real money: {ok, checks: [{label, value, need, ok}]}. `ev`: {bt: the latest
    backtest's summary after costs, hold_r: its total R on the last months (None: no trades there),
    paper: {trades, total_r} of the closed paper trades after costs}."""
    bt, paper = ev.get('bt') or {}, ev.get('paper') or {}
    need_bt = GATE['bt_trades'][mode]
    checks = [
        {'label': 'Backtest trades', 'value': bt.get('trades') or 0, 'need': f'≥ {need_bt}', 'ok': (bt.get('trades') or 0) >= need_bt},
        {'label': 'Backtest R a trade, after costs', 'value': bt.get('avg_r'), 'need': '> 0', 'ok': (bt.get('avg_r') or 0) > 0},
        {'label': 'Backtest R on the last 3 months', 'value': ev.get('hold_r'), 'need': '> 0', 'ok': (ev.get('hold_r') or 0) > 0},
        {'label': 'Paper trades closed', 'value': paper.get('trades') or 0, 'need': f"≥ {GATE['paper_trades']}",
         'ok': (paper.get('trades') or 0) >= GATE['paper_trades']},
        {'label': 'Paper R after costs', 'value': paper.get('total_r'), 'need': '> 0', 'ok': (paper.get('total_r') or 0) > 0},
    ]
    return {'ok': all(c['ok'] for c in checks), 'checks': checks}


def hold_r(equity: List[Any], since: str) -> Optional[float]:
    """Total R from a run's equity points ([time, cumulative R]) made from `since` on; None without any."""
    pts = [(str(t), float(v)) for t, v in equity or []]
    before = [v for t, v in pts if t < since]
    after = [v for t, v in pts if t >= since]
    if not after:
        return None
    return round(after[-1] - (before[-1] if before else 0.0), 2)
