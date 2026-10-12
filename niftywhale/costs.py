"""
What a trade costs, in R: the backtester's trades are fills at the alert's prices with no costs, and
with stops a fraction of a percent away, costs are a large part of a trade measured in R. A 0.3 %
intraday stop with 0.08 % of round-trip costs gives away about a quarter of R on every trade, before
the market does anything.

Two parts, both turned into R by dividing by the trade's risk (entry to stop, per share or per unit
of premium):

  charges    Dhan's schedule (demo.charges: brokerage, STT, exchange, IPFT, SEBI fee, stamp duty, GST,
             the DP charge), on a position sized as the demo account sizes it (demo.size) for an
             account of NOMINAL_CAPITAL, so the fixed parts (brokerage, the DP charge) weigh what they
             would on a real position. Swing longs are delivery (CNC), swing shorts stock futures,
             intraday MIS, options a premium buy.
  slippage   `slippage_bps` of the price on the entry (an alert reaches you after the candle closed,
             and you enter at the market) and on an exit at the stop or the square-off (market
             orders). A target is a limit order: no slippage there.

Pure: trades in, trades out. The lab applies it to every simulated trade (lab.Bench), so baselines,
tuning, shadow runs and rollbacks all judge rules after costs; each trade keeps its `r_gross`.
"""
import math
from typing import Any, Dict, List, Optional

from niftywhale import demo, execution

NOMINAL_CAPITAL = 1_000_000          # the account the backtest sizes positions for (₹10 lakh)
DEFAULT_SLIPPAGE_BPS = 5.0           # a side, as a share of the price (5 bps = 0.05 %)
MARKET_EXITS = ('lost', 'closed')    # the stop and the square-off; a target is a limit order


def product(mode: str, side: str) -> str:
    if mode == 'options':
        return 'OPT'
    if mode == 'intraday':
        return 'MIS'
    return 'FUT' if side == 'short' else 'CNC'


def live_account(exec_saved: Any, day_capital: Optional[float] = None) -> Dict[str, Any]:
    """The account live trading would size from (Trading settings), for costs at your size: a fixed number of
    lots a trade, or risk_pct of the set capital, else of the day's capital at Dhan (`day_capital`), else of
    NOMINAL_CAPITAL. `label` says which, and two figures worked out on the same label are comparable."""
    st = execution.settings_from(exec_saved if isinstance(exec_saved, dict) else {})
    if st['sizing'] == 'lots':
        return {'sizing': 'lots', 'lots': st['lots'], 'capital': None, 'st': st,
                'label': f"{st['lots']} lot{'s' if st['lots'] != 1 else ''} a trade (a share for stocks)"}
    capital = float(st['capital'] or day_capital or NOMINAL_CAPITAL)
    return {'sizing': 'risk', 'lots': None, 'capital': capital, 'st': st,
            'label': f"{st['risk_pct']:g}% risk a trade on ₹{capital:,.0f}"}


def cost_r(t: Dict[str, Any], mode: str, stop: Optional[float] = None, lot: Optional[int] = None,
           slippage_bps: float = DEFAULT_SLIPPAGE_BPS, settings: Optional[Dict[str, Any]] = None,
           account: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """The R a trade gives away to charges and slippage (None when it has no usable entry and stop).
    `stop` overrides the trade's (options trades carry their stop as a share of the premium). Sized for
    NOMINAL_CAPITAL at the demo's default risk (stable, for comparing rule sets), or for `account`
    (live_account: what live trading would actually trade, where a flat ₹20 brokerage weighs far more)."""
    entry = float(t.get('entry') or 0)
    stop = float(stop if stop is not None else (t.get('stop') or 0))
    per_unit = abs(entry - stop)
    if entry <= 0 or stop <= 0 or per_unit <= 0:
        return None
    side = 'long' if mode == 'options' else (t.get('side') or 'long')
    prod = product(mode, side)
    st = settings or demo.settings_from({})
    trade = {'mode': mode, 'side': side, 'entry': entry, 'stop': stop}
    if account and account['sizing'] == 'lots':
        sized = execution.fixed_size(trade, account['lots'], lot, math.inf, account['st'])
    elif account:
        sized = demo.size(trade, account['capital'], account['capital'], account['st'], lot)
    else:
        sized = demo.size(trade, NOMINAL_CAPITAL, NOMINAL_CAPITAL, st, lot)
    if 'skip' in sized:                  # too dear for the account at that risk: one unit stands in
        qty = (lot or 1)
    else:
        qty = sized['qty']
    exit_ = float(t['exit']) if t.get('exit') is not None else entry
    buy, sell = (entry * qty, exit_ * qty) if side == 'long' else (exit_ * qty, entry * qty)
    a, b = demo._day(t.get('entry_time')), demo._day(t.get('exit_time'))
    charges = demo.charges(prod, buy, sell, same_day=bool(a and b and a == b))['total'] / qty
    slip = entry * slippage_bps / 1e4
    if t.get('status') in MARKET_EXITS:
        slip += exit_ * slippage_bps / 1e4
    return (charges + slip) / per_unit


def apply(trades: List[Dict[str, Any]], mode: str, slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
          stop_pct: Optional[float] = None, lots: Optional[Dict[str, int]] = None,
          account: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The trades with `r` after costs, `r_gross` before and `cost_r` the difference. An open trade
    (no `r` yet) is left as it is. `stop_pct`: the options stop, as a share of the premium. A trade's lot is
    `lots[symbol]`, else its own `lot` (an option idea carries it): without one, an option's charges would be
    spread over a single unit. `account`: costs at that size (live_account) instead of the nominal account."""
    st = demo.settings_from({})
    out = []
    for t in trades:
        if t.get('r') is None:
            out.append(t)
            continue
        stop = float(t['entry']) * (1 - stop_pct / 100) if mode == 'options' and stop_pct else None
        c = cost_r(t, mode, stop, (lots or {}).get(t['symbol']) or t.get('lot'), slippage_bps, st, account)
        if c is None or not math.isfinite(c):
            out.append({**t, 'r_gross': t['r'], 'cost_r': 0.0})
            continue
        out.append({**t, 'r_gross': t['r'], 'cost_r': round(c, 4), 'r': round(float(t['r']) - c, 3)})
    return out
