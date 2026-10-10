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

from niftywhale import demo

NOMINAL_CAPITAL = 1_000_000          # the account the backtest sizes positions for (₹10 lakh)
DEFAULT_SLIPPAGE_BPS = 5.0           # a side, as a share of the price (5 bps = 0.05 %)
MARKET_EXITS = ('lost', 'closed')    # the stop and the square-off; a target is a limit order


def product(mode: str, side: str) -> str:
    if mode == 'options':
        return 'OPT'
    if mode == 'intraday':
        return 'MIS'
    return 'FUT' if side == 'short' else 'CNC'


def cost_r(t: Dict[str, Any], mode: str, stop: Optional[float] = None, lot: Optional[int] = None,
           slippage_bps: float = DEFAULT_SLIPPAGE_BPS, settings: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """The R a trade gives away to charges and slippage (None when it has no usable entry and stop).
    `stop` overrides the trade's (options trades carry their stop as a share of the premium)."""
    entry = float(t.get('entry') or 0)
    stop = float(stop if stop is not None else (t.get('stop') or 0))
    per_unit = abs(entry - stop)
    if entry <= 0 or stop <= 0 or per_unit <= 0:
        return None
    side = 'long' if mode == 'options' else (t.get('side') or 'long')
    prod = product(mode, side)
    st = settings or demo.settings_from({})
    sized = demo.size({'mode': mode, 'side': side, 'entry': entry, 'stop': stop}, NOMINAL_CAPITAL, NOMINAL_CAPITAL, st, lot)
    if 'skip' in sized:                  # too dear for the account at that risk: one unit stands in
        qty = (lot or 1)
    else:
        qty = sized['qty']
    exit_ = float(t['exit']) if t.get('exit') is not None else entry
    buy, sell = (entry * qty, exit_ * qty) if side == 'long' else (exit_ * qty, entry * qty)
    charges = demo.charges(prod, buy, sell)['total'] / qty
    slip = entry * slippage_bps / 1e4
    if t.get('status') in MARKET_EXITS:
        slip += exit_ * slippage_bps / 1e4
    return (charges + slip) / per_unit


def apply(trades: List[Dict[str, Any]], mode: str, slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
          stop_pct: Optional[float] = None, lots: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """The trades with `r` after costs, `r_gross` before and `cost_r` the difference. An open trade
    (no `r` yet) is left as it is. `stop_pct`: the options stop, as a share of the premium."""
    st = demo.settings_from({})
    out = []
    for t in trades:
        if t.get('r') is None:
            out.append(t)
            continue
        stop = float(t['entry']) * (1 - stop_pct / 100) if mode == 'options' and stop_pct else None
        c = cost_r(t, mode, stop, (lots or {}).get(t['symbol']), slippage_bps, st)
        if c is None or not math.isfinite(c):
            out.append({**t, 'r_gross': t['r'], 'cost_r': 0.0})
            continue
        out.append({**t, 'r_gross': t['r'], 'cost_r': round(c, 4), 'r': round(float(t['r']) - c, 3)})
    return out
