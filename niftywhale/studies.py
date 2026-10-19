"""
The Charts tab's indicators: what each one is, the settings it takes (defaults, limits, choices) and the
lines it draws, with their colours. The page builds its menu and settings from CATALOG, and works the
values out itself (static/studies.js), so they move with every live tick; here a saved layout's
indicators are checked against it (clean), so a bad or old entry never breaks a chart.

Defaults are TradingView's for the same indicator, so a chart reads the same in both.
`place`: 'price' draws on the candles, 'panel' in its own pane below them. `volume`: needs the
candles' volume, so an index (no volume) cannot have it.
"""
import math
import re
from typing import Any, Dict, List

MAX_STUDIES = 10                     # a chart's indicators at most (16 charts, each a second's update)
MAX_PANELS = 4                       # of them in panes below the candles
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,24}')       # whole strings only (fullmatch: no trailing newline)
COLOR_RE = re.compile(r'#[0-9a-fA-F]{6}')

SOURCES = ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4']
BLUE, ORANGE, PURPLE, TEAL, RED, GREEN, YELLOW, PINK, GREY = (
    '#2962ff', '#ff6d00', '#7e57c2', '#26a69a', '#ef5350', '#43a047', '#f9a825', '#e91e63', '#787b86')


def _n(key: str, label: str, default: float, lo: float, hi: float, step: float = 1) -> Dict[str, Any]:
    return {'key': key, 'label': label, 'default': default, 'min': lo, 'max': hi, 'step': step}


def _pick(key: str, label: str, values: List[str], default: str = None) -> Dict[str, Any]:
    return {'key': key, 'label': label, 'default': default or values[0], 'values': values}


def _src(default: str = 'close') -> Dict[str, Any]:
    return _pick('source', 'Source', SOURCES, default)


def _length(default: int, hi: int = 500, lo: int = 1, label: str = 'Length') -> Dict[str, Any]:
    return _n('length', label, default, lo, hi)


def _l(key: str, label: str, color: str, kind: str = 'line') -> Dict[str, Any]:
    return {'key': key, 'label': label, 'color': color, 'kind': kind}


def _ma(key: str, short: str, label: str, volume: bool = False, lo: int = 1) -> Dict[str, Any]:
    return {'key': key, 'short': short, 'label': label, 'group': 'Moving averages', 'place': 'price', 'volume': volume,
            'params': [_length(20, lo=lo), _src()], 'lines': [_l('ma', short, BLUE)]}


CATALOG: List[Dict[str, Any]] = [
    _ma('sma', 'SMA', 'Simple moving average'),
    _ma('ema', 'EMA', 'Exponential moving average'),
    _ma('wma', 'WMA', 'Weighted moving average'),
    _ma('hma', 'HMA', 'Hull moving average', lo=2),
    _ma('vwma', 'VWMA', 'Volume-weighted moving average', volume=True),
    {'key': 'vwap', 'short': 'VWAP', 'label': 'VWAP', 'group': 'Trend', 'place': 'price', 'volume': True,
     'params': [_pick('anchor', 'Resets each', ['session', 'week', 'month']),
                _n('band1', 'Band 1 (std devs, 0 = off)', 1.0, 0, 5, 0.1),
                _n('band2', 'Band 2 (std devs, 0 = off)', 2.0, 0, 5, 0.1)],
     'lines': [_l('vwap', 'VWAP', PURPLE), _l('up1', 'Upper 1', GREEN), _l('lo1', 'Lower 1', GREEN),
               _l('up2', 'Upper 2', TEAL), _l('lo2', 'Lower 2', TEAL)]},
    {'key': 'bb', 'short': 'BB', 'label': 'Bollinger Bands', 'group': 'Bands', 'place': 'price',
     'params': [_length(20), _n('mult', 'Std devs', 2.0, 0.1, 10, 0.1), _src()],
     'lines': [_l('basis', 'Basis', ORANGE), _l('upper', 'Upper', BLUE), _l('lower', 'Lower', BLUE)], 'fill': ['upper', 'lower']},
    {'key': 'kc', 'short': 'KC', 'label': 'Keltner Channels', 'group': 'Bands', 'place': 'price',
     'params': [_length(20), _n('mult', 'ATR multiple', 2.0, 0.1, 10, 0.1), _n('atr_length', 'ATR length', 10, 1, 500)],
     'lines': [_l('basis', 'Basis', BLUE), _l('upper', 'Upper', BLUE), _l('lower', 'Lower', BLUE)], 'fill': ['upper', 'lower']},
    {'key': 'dc', 'short': 'DC', 'label': 'Donchian Channels', 'group': 'Bands', 'place': 'price',
     'params': [_length(20)],
     'lines': [_l('basis', 'Basis', ORANGE), _l('upper', 'Upper', BLUE), _l('lower', 'Lower', BLUE)], 'fill': ['upper', 'lower']},
    {'key': 'supertrend', 'short': 'Supertrend', 'label': 'Supertrend', 'group': 'Trend', 'place': 'price',
     'params': [_n('atr_length', 'ATR length', 10, 1, 500), _n('factor', 'Factor', 3.0, 0.1, 20, 0.1)],
     'lines': [_l('up', 'Up trend', GREEN), _l('down', 'Down trend', RED)]},
    {'key': 'psar', 'short': 'SAR', 'label': 'Parabolic SAR', 'group': 'Trend', 'place': 'price',
     'params': [_n('start', 'Start', 0.02, 0.001, 1, 0.001), _n('increment', 'Increment', 0.02, 0.001, 1, 0.001),
                _n('max', 'Maximum', 0.2, 0.01, 1, 0.01)],
     'lines': [_l('sar', 'SAR', BLUE, 'dots')]},
    {'key': 'ichimoku', 'short': 'Ichimoku', 'label': 'Ichimoku Cloud', 'group': 'Trend', 'place': 'price',
     'params': [_n('conversion', 'Conversion line', 9, 1, 200), _n('base', 'Base line', 26, 1, 200),
                _n('span_b', 'Leading span B', 52, 1, 400), _n('displacement', 'Displacement', 26, 1, 200)],
     'lines': [_l('conv', 'Conversion', BLUE), _l('base', 'Base', RED), _l('lag', 'Lagging span', GREEN),
               _l('span_a', 'Leading span A', '#a5d6a7'), _l('span_b', 'Leading span B', '#ef9a9a')], 'fill': ['span_a', 'span_b']},
    {'key': 'pivots', 'short': 'Pivots', 'label': 'Pivot points', 'group': 'Levels', 'place': 'price',
     'params': [_pick('kind', 'Type', ['traditional', 'fibonacci', 'camarilla']),
                _pick('period', 'From the previous', ['auto', 'day', 'week', 'month'])],
     'lines': [_l('p', 'P', ORANGE), _l('r1', 'R1', RED), _l('r2', 'R2', RED), _l('r3', 'R3', RED), _l('r4', 'R4', RED),
               _l('s1', 'S1', GREEN), _l('s2', 'S2', GREEN), _l('s3', 'S3', GREEN), _l('s4', 'S4', GREEN)]},
    {'key': 'volume', 'short': 'Vol', 'label': 'Volume', 'group': 'Volume', 'place': 'panel', 'volume': True,
     'params': [_n('ma_length', 'Average (0 = off)', 20, 0, 500)],
     'lines': [_l('vol', 'Volume', GREY, 'histogram'), _l('ma', 'Average', BLUE)]},
    {'key': 'rsi', 'short': 'RSI', 'label': 'Relative strength index', 'group': 'Momentum', 'place': 'panel',
     'params': [_length(14, lo=2), _src(), _n('upper', 'Overbought', 70, 50, 100), _n('lower', 'Oversold', 30, 0, 50)],
     'lines': [_l('rsi', 'RSI', PURPLE)]},
    {'key': 'macd', 'short': 'MACD', 'label': 'MACD', 'group': 'Momentum', 'place': 'panel',
     'params': [_n('fast', 'Fast length', 12, 1, 200), _n('slow', 'Slow length', 26, 1, 400), _n('signal', 'Signal length', 9, 1, 200), _src()],
     'lines': [_l('macd', 'MACD', BLUE), _l('signal', 'Signal', ORANGE), _l('hist', 'Histogram', TEAL, 'histogram')]},
    {'key': 'stoch', 'short': 'Stoch', 'label': 'Stochastic', 'group': 'Momentum', 'place': 'panel',
     'params': [_n('k_length', '%K length', 14, 1, 200), _n('k_smooth', '%K smoothing', 3, 1, 50), _n('d_smooth', '%D smoothing', 3, 1, 50),
                _n('upper', 'Upper band', 80, 50, 100), _n('lower', 'Lower band', 20, 0, 50)],
     'lines': [_l('k', '%K', BLUE), _l('d', '%D', ORANGE)]},
    {'key': 'stochrsi', 'short': 'Stoch RSI', 'label': 'Stochastic RSI', 'group': 'Momentum', 'place': 'panel',
     'params': [_n('rsi_length', 'RSI length', 14, 2, 200), _n('stoch_length', 'Stochastic length', 14, 1, 200),
                _n('k_smooth', '%K smoothing', 3, 1, 50), _n('d_smooth', '%D smoothing', 3, 1, 50), _src(),
                _n('upper', 'Upper band', 80, 50, 100), _n('lower', 'Lower band', 20, 0, 50)],
     'lines': [_l('k', '%K', BLUE), _l('d', '%D', ORANGE)]},
    {'key': 'atr', 'short': 'ATR', 'label': 'Average true range', 'group': 'Volatility', 'place': 'panel',
     'params': [_length(14)], 'lines': [_l('atr', 'ATR', RED)]},
    {'key': 'adx', 'short': 'DMI', 'label': 'ADX / DMI', 'group': 'Trend', 'place': 'panel',
     'params': [_n('length', 'DI length', 14, 1, 200), _n('smoothing', 'ADX smoothing', 14, 1, 200), _n('level', 'Trend line (0 = off)', 25, 0, 100)],
     'lines': [_l('adx', 'ADX', RED), _l('plus', '+DI', BLUE), _l('minus', '−DI', ORANGE)]},
    {'key': 'cci', 'short': 'CCI', 'label': 'Commodity channel index', 'group': 'Momentum', 'place': 'panel',
     'params': [_length(20), _src('hlc3'), _n('upper', 'Upper band', 100, 0, 500), _n('lower', 'Lower band', -100, -500, 0)],
     'lines': [_l('cci', 'CCI', BLUE)]},
    {'key': 'willr', 'short': '%R', 'label': 'Williams %R', 'group': 'Momentum', 'place': 'panel',
     'params': [_length(14), _n('upper', 'Upper band', -20, -50, 0), _n('lower', 'Lower band', -80, -100, -50)],
     'lines': [_l('r', '%R', PURPLE)]},
    {'key': 'mfi', 'short': 'MFI', 'label': 'Money flow index', 'group': 'Volume', 'place': 'panel', 'volume': True,
     'params': [_length(14), _n('upper', 'Overbought', 80, 50, 100), _n('lower', 'Oversold', 20, 0, 50)],
     'lines': [_l('mfi', 'MFI', PURPLE)]},
    {'key': 'obv', 'short': 'OBV', 'label': 'On-balance volume', 'group': 'Volume', 'place': 'panel', 'volume': True,
     'params': [], 'lines': [_l('obv', 'OBV', BLUE)]},
    {'key': 'roc', 'short': 'ROC', 'label': 'Rate of change', 'group': 'Momentum', 'place': 'panel',
     'params': [_length(9), _src()], 'lines': [_l('roc', 'ROC', BLUE)]},
]
BY_KEY = {s['key']: s for s in CATALOG}


def _value(spec: Dict[str, Any], raw: Any) -> Any:
    """One setting made safe: a choice from its list, or a number clamped to its range on its step."""
    if 'values' in spec:
        return raw if raw in spec['values'] else spec['default']
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return spec['default']
    if not math.isfinite(v):
        return spec['default']
    v = min(spec['max'], max(spec['min'], v))
    step = spec['step']
    v = round(round((v - spec['min']) / step) * step + spec['min'], 6)
    return int(v) if float(step).is_integer() and float(spec['min']).is_integer() else v


def clean(raw: Any) -> List[Dict[str, Any]]:
    """A chart's saved indicators, checked: known kinds only, every setting in range (missing ones get their
    default), colours #rrggbb for lines the kind has, ids unique, at most MAX_STUDIES and MAX_PANELS."""
    out, ids, panels = [], set(), 0
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        kind, sid = s.get('type'), s.get('id')
        spec = BY_KEY.get(kind) if isinstance(kind, str) else None
        if not spec or not isinstance(sid, str) or not ID_RE.fullmatch(sid) or sid in ids:
            continue
        if spec['place'] == 'panel':
            if panels >= MAX_PANELS:
                continue
            panels += 1
        params = s.get('params') if isinstance(s.get('params'), dict) else {}
        colors = s.get('colors') if isinstance(s.get('colors'), dict) else {}
        lines = {ln['key'] for ln in spec['lines']}
        out.append({'id': sid, 'type': spec['key'],
                    'params': {p['key']: _value(p, params.get(p['key'], p['default'])) for p in spec['params']},
                    'colors': {k: v.lower() for k, v in colors.items() if k in lines and isinstance(v, str) and COLOR_RE.fullmatch(v)},
                    'hidden': s.get('hidden') is True})
        ids.add(sid)
        if len(out) >= MAX_STUDIES:
            break
    return out


def catalog() -> Dict[str, Any]:
    """What the page needs: every kind, and the limits."""
    return {'kinds': CATALOG, 'max': MAX_STUDIES, 'max_panels': MAX_PANELS}
