"""
Dhan's order APIs (v2), for live trading (execution.py decides what to send; this only talks to Dhan).

  POST   /super/orders               entry + target + stop loss in one request (every live entry)
  DELETE /super/orders/{id}/{leg}    cancel a leg; ENTRY_LEG cancels the whole order
  GET    /super/orders               the day's super orders, legs included
  POST   /forever/orders             a forever OCO: the stop and target of a swing holding, re-placed when a
                                     super order's legs did not carry over to the next session
  DELETE /forever/orders/{id}
  GET    /orders/external/{corr}     one order by our correlation ID: a retry asks first, never sends twice
  GET    /positions, /holdings       what the account holds (the reconciler compares)
  GET    /fundlimit                  the account's balance (sizing)
  DELETE /positions                  exit everything and cancel the day's orders (the kill switch)
  POST   /killswitch                 Dhan's own kill switch: no more trading today (after everything is flat)
  GET    /ip/getIP                   the static IPs whitelisted for orders

Sending is refused unless real sending is switched on (App settings: `exec_send`, set only from a fresh
sign-in once the static IP is confirmed): Dhan accepts orders only from a whitelisted static IP (SEBI's
retail algo framework), and a refused order should never be the first sign that it is not set up.
Every request and its answer go to the broker log (store.broker_log), without the access token.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from niftywhale import dhan, store

logger = logging.getLogger(__name__)
TIMEOUT = 10


def sending() -> Optional[str]:
    """None when real orders may be sent, or why not."""
    if store.settings().get('exec_send') != '1':
        return 'real sending is off (Trading → Sending: switch it on once the static IP is confirmed)'
    if not dhan.available():
        return 'Dhan is not connected'
    return None


def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None,
          order: bool = False) -> Tuple[bool, Any, Optional[str]]:
    """(ok, parsed answer, error). `order`: a call that places, changes or cancels, refused unless sending is on."""
    if order:
        why = sending()
        if why:
            return False, None, why
    try:
        r = requests.request(method, dhan.API + path, headers=dhan._headers(), json=body, params=params, timeout=TIMEOUT)
        text = r.text or ''
        try:
            data = r.json() if text else None
        except ValueError:
            data = None
        ok = r.ok and not (isinstance(data, dict) and (data.get('errorCode') or data.get('status') == 'failure'))
        err = None if ok else _error(r.status_code, data, text)
    except requests.RequestException as e:
        ok, data, err, text = False, None, dhan._redact(e), ''
    store.add_broker_log(method, path, body, ok, (text or err or '')[:4000])
    if not ok:
        logger.warning(f'Dhan {method} {path}: {err}')
    return ok, data, err


def _error(status: int, data: Any, text: str) -> str:
    if isinstance(data, dict):
        code = data.get('errorCode') or data.get('errorType') or ''
        msg = data.get('errorMessage') or data.get('message') or data.get('remarks') or ''
        if code or msg:
            return f'{code} {msg}'.strip()
    return f'HTTP {status}: {text[:200]}'


# ------------------------------------------------------------------ orders
def place_super(body: Dict[str, Any]):
    return _call('POST', '/super/orders', body, order=True)


def cancel_super(order_id: str, leg: str = 'ENTRY_LEG'):
    return _call('DELETE', f'/super/orders/{order_id}/{leg}', order=True)


def super_orders() -> List[Dict[str, Any]]:
    ok, data, _ = _call('GET', '/super/orders')
    return data if ok and isinstance(data, list) else []


def place_forever(body: Dict[str, Any]):
    return _call('POST', '/forever/orders', body, order=True)


def cancel_forever(order_id: str):
    return _call('DELETE', f'/forever/orders/{order_id}', order=True)


def forever_orders() -> List[Dict[str, Any]]:
    # Dhan's page names both /forever/orders and /forever/all for the list: the first, then the second.
    for path in ('/forever/orders', '/forever/all'):
        ok, data, _ = _call('GET', path)
        if ok and isinstance(data, list):
            return data
    return []


def order_by_correlation(cid: str) -> Optional[Dict[str, Any]]:
    ok, data, _ = _call('GET', f'/orders/external/{cid}')
    if ok and isinstance(data, list):
        data = data[0] if data else None
    return data if ok and isinstance(data, dict) else None


# ------------------------------------------------------------------ the account
def positions() -> Optional[List[Dict[str, Any]]]:
    ok, data, _ = _call('GET', '/positions')
    return data if ok and isinstance(data, list) else None


def holdings() -> Optional[List[Dict[str, Any]]]:
    ok, data, _ = _call('GET', '/holdings')
    return data if ok and isinstance(data, list) else None


def funds() -> Optional[float]:
    """The balance available to trade (Dhan spells the field availabelBalance)."""
    ok, data, _ = _call('GET', '/fundlimit')
    if not ok or not isinstance(data, dict):
        return None
    v = data.get('availabelBalance', data.get('availableBalance'))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def static_ip() -> Optional[Dict[str, Any]]:
    """{primaryIP, secondaryIP, modifyDatePrimary, modifyDateSecondary} or None."""
    ok, data, _ = _call('GET', '/ip/getIP')
    return data if ok and isinstance(data, dict) else None


def exit_all():
    """Every position exited and the day's open orders cancelled (Dhan: DELETE /positions)."""
    return _call('DELETE', '/positions', order=True)


def killswitch(on: bool = True):
    """Dhan's kill switch: no trading for the rest of the day. Dhan wants every position closed and no
    order pending first. Its page shows the status as a query parameter in its example."""
    return _call('POST', '/killswitch', params={'killSwitchStatus': 'ACTIVATE' if on else 'DEACTIVATE'}, order=True)
