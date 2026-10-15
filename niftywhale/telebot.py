"""
Telegram commands: what you send the bot, answered by the app. The bot's alerts go out through notify.py; this
reads the other way.

It asks Telegram for new messages (getUpdates, long polling), so nothing has to reach the Pi from outside: no
webhook, no open port. Only the configured chat (telegram_chat_id, where the alerts go) is answered; anything
from another chat is ignored and logged once. In a private chat that is you alone; in a group, everyone in it.

A command that changes something (pause, square off, apply, halt, ...) is never done on the message: the reply
asks with a Yes / No button pair (`confirm`), and only the Yes, within CONFIRM_S, acts. Placing a real order
also needs a code from your authenticator, in the command itself (app.py).

Pure transport and parsing here; app.py holds the commands (TG_COMMANDS) and starts `loop`.
"""
import logging
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from niftywhale import config, store

logger = logging.getLogger(__name__)

POLL_S = 25                 # how long one getUpdates waits for a message (long polling)
CONFIRM_S = 120             # a Yes button lapses after this
STALE_S = 300               # a message older than this (the app was down) is not acted on
MAX_TEXT = 4000             # Telegram's limit is 4096 characters a message
OFFSET_KEY = 'telegram_offset'

_pending: Dict[str, Dict[str, Any]] = {}       # confirm token -> {do, label, until}
_lock = threading.Lock()
_ignored: set = set()                           # chats already logged as not ours


def _token() -> Optional[str]:
    return config.get('telegram_token') or None


def _chat() -> str:
    return str(config.get('telegram_chat_id') or '')


def call(method: str, timeout: float = 15, **payload) -> Optional[Any]:
    """One Bot API call; its `result`, or None (logged, the token kept out of the log)."""
    tok = _token()
    if not tok:
        return None
    try:
        r = requests.post(f'https://api.telegram.org/bot{tok}/{method}', json=payload, timeout=timeout)
        body = r.json() if r.headers.get('Content-Type', '').startswith('application/json') else {}
        if r.ok and body.get('ok'):
            return body.get('result')
        logger.warning(f"Telegram {method}: {r.status_code} {str(body.get('description') or r.text)[:200]}")
    except (requests.RequestException, ValueError) as e:
        logger.warning(f'Telegram {method}: ' + str(e).replace(tok, '***'))
    return None


def chunks(html: str, size: int = MAX_TEXT) -> List[str]:
    """A long reply split on line breaks, each part under Telegram's limit."""
    out, cur = [], ''
    for line in html.split('\n'):
        while len(line) > size:                 # one line too long on its own: cut it
            if cur:
                out.append(cur)
                cur = ''
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f'{cur}\n{line}' if cur else line
    if cur:
        out.append(cur)
    return out


def reply(html: str, buttons: Optional[List[List[Tuple[str, str]]]] = None, edit: Optional[int] = None) -> bool:
    """Send (or, with `edit`, replace) a message in our chat. `buttons`: rows of (label, callback data)."""
    chat = _chat()
    if not chat:
        return False
    markup = {'inline_keyboard': [[{'text': t, 'callback_data': d} for t, d in row] for row in buttons]} if buttons else None
    parts = chunks(html) or ['(nothing)']
    if edit is not None:
        payload = {'chat_id': chat, 'message_id': edit, 'text': parts[0][:MAX_TEXT], 'parse_mode': 'HTML',
                   'disable_web_page_preview': True}
        if markup:
            payload['reply_markup'] = markup
        return call('editMessageText', **payload) is not None
    ok = True
    for n, part in enumerate(parts):
        payload = {'chat_id': chat, 'text': part, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        if markup and n == len(parts) - 1:      # the buttons go under the last part
            payload['reply_markup'] = markup
        ok = call('sendMessage', **payload) is not None and ok
    return ok


def confirm(question: str, label: str, do: Callable[[], str]) -> bool:
    """Ask before acting: `question` with Yes / No buttons. Yes (within CONFIRM_S) runs `do`, whose return is the
    answer shown in place of the question. Tokens are random and single-use."""
    tok = secrets.token_urlsafe(9)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if v['until'] < now]:
            _pending.pop(k, None)
        _pending[tok] = {'do': do, 'label': label, 'until': now + CONFIRM_S}
    return reply(question, [[(f'Yes, {label}', f'y:{tok}'), ('No', f'n:{tok}')]])


def command_button(label: str, command: str) -> Tuple[str, str]:
    """A button that sends `command` ('/squareoff o12') as if typed (it still asks before acting)."""
    return label, f'c:{command}'[:64]


def answer_button(cb: Dict[str, Any], handle: Optional[Callable[[str, List[str]], None]] = None) -> None:
    """A button pressed in our chat: Yes / No on a confirmation, or a command_button."""
    data, msg = str(cb.get('data') or ''), cb.get('message') or {}
    call('answerCallbackQuery', callback_query_id=cb.get('id'))
    kind, _, tok = data.partition(':')
    if kind == 'c' and handle:
        name, args = parse(tok)
        handle(name or '', args)
        return
    with _lock:
        p = _pending.pop(tok, None) if kind in ('y', 'n') else None
    mid = msg.get('message_id')
    old = msg.get('text') or ''
    if kind == 'n':
        reply(f'{_esc(old)}\n\n<i>Cancelled.</i>', edit=mid)
        return
    if kind != 'y':
        return
    if not p or p['until'] < time.time():
        reply(f'{_esc(old)}\n\n<i>That button has lapsed: send the command again.</i>', edit=mid)
        return
    try:
        out = p['do']()
    except Exception as e:                       # an action's failure is said, never lost
        logger.exception(f"Telegram action '{p['label']}' failed")
        out = f'Could not {p["label"]}: {_esc(str(e))[:300]}'
    reply(f'{_esc(old)}\n\n{out}', edit=mid)


def parse(text: str) -> Tuple[Optional[str], List[str]]:
    """'/squareoff@NiftyWhaleBot o123' -> ('squareoff', ['o123']); not a command -> (None, words)."""
    words = (text or '').strip().split()
    if not words or not words[0].startswith('/'):
        return None, words
    return words[0][1:].split('@', 1)[0].lower(), words[1:]


def _esc(s: str) -> str:
    return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def ours(chat_id: Any) -> bool:
    chat = _chat()
    return bool(chat) and str(chat_id) == chat


def register(commands: List[Tuple[str, str]]) -> bool:
    """The command menu Telegram shows when you type '/' (setMyCommands)."""
    return call('setMyCommands', commands=[{'command': c, 'description': d[:256]} for c, d in commands]) is not None


def loop(handle: Callable[[str, List[str]], None], commands: List[Tuple[str, str]], enabled: Callable[[], bool],
         stop: Optional[threading.Event] = None) -> None:
    """Read our chat for ever: each command to `handle(name, args)` (it replies itself), each button to
    answer_button. The menu is registered once a token works. Waits quietly while Telegram is not set up or
    commands are switched off; backs off on errors (a second app polling the same bot gets 409s)."""
    stop = stop or threading.Event()
    registered_for, fails = None, 0
    while not stop.is_set():
        if not (_token() and _chat() and enabled()):
            stop.wait(30)
            continue
        if registered_for != _token() and register(commands):
            registered_for = _token()
        saved = store.get_kv(OFFSET_KEY)
        offset = int(saved) if saved and saved.isdigit() else None
        got = _get_updates(offset)
        if got is None:
            fails += 1
            stop.wait(min(300, 5 * 2 ** min(fails, 6)))
            continue
        fails = 0
        for u in got:
            store.set_kv(OFFSET_KEY, str(u['update_id'] + 1))
            try:
                _one(u, handle)
            except Exception:
                logger.exception('Telegram command failed')
                reply('Something went wrong with that command; it is in the app log.')


def _get_updates(offset: Optional[int]) -> Optional[List[Dict[str, Any]]]:
    tok = _token()
    try:
        r = requests.post(f'https://api.telegram.org/bot{tok}/getUpdates', timeout=POLL_S + 10,
                          json={'offset': offset, 'timeout': POLL_S, 'allowed_updates': ['message', 'callback_query']})
        body = r.json() if r.headers.get('Content-Type', '').startswith('application/json') else {}
        if r.ok and body.get('ok'):
            return body.get('result') or []
        logger.warning(f"Telegram getUpdates: {r.status_code} {str(body.get('description') or r.text)[:200]}")
    except (requests.RequestException, ValueError) as e:
        logger.warning('Telegram getUpdates: ' + str(e).replace(tok or '', '***'))
    return None


def _one(u: Dict[str, Any], handle: Callable[[str, List[str]], None]) -> None:
    cb = u.get('callback_query')
    if cb:
        chat = ((cb.get('message') or {}).get('chat') or {}).get('id')
        if ours(chat):
            answer_button(cb, handle)
        return
    m = u.get('message') or {}
    chat = (m.get('chat') or {}).get('id')
    if not ours(chat):
        if chat not in _ignored:
            _ignored.add(chat)
            logger.warning(f'Telegram: a message from chat {chat}, not the configured one, was ignored')
        return
    name, args = parse(m.get('text') or '')
    if time.time() - float(m.get('date') or 0) > STALE_S:      # sent while the app was not listening
        if name:
            reply(f"/{_esc(name)} arrived {time.strftime('%d %b %H:%M', time.localtime(m['date']))}, while the app was "
                  'not listening: send it again if you still want it.')
        return
    handle(name or '', args)
