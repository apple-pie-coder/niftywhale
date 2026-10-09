"""Telegram delivery. Configured in App settings (or .env, config.py); silent if unset."""
import logging
import time

import requests

from niftywhale import config

logger = logging.getLogger(__name__)
_me = {'token': None, 'at': 0.0, 'bot': None}


def configured() -> bool:
    return bool(config.get('telegram_token') and config.get('telegram_chat_id'))


def bot_name() -> str:
    """The bot's @username for the configured token (asked once an hour), or None if it is not valid."""
    tok = config.get('telegram_token')
    if not tok:
        return None
    if _me['token'] == tok and time.time() - _me['at'] < 3600:
        return _me['bot']
    try:
        r = requests.get(f'https://api.telegram.org/bot{tok}/getMe', timeout=10)
        bot = (r.json().get('result') or {}).get('username') if r.ok else None
    except (requests.RequestException, ValueError):
        return _me['bot'] if _me['token'] == tok else None      # the network, not the token: don't cache
    _me.update(token=tok, at=time.time(), bot=bot)
    return bot


def send(html: str) -> bool:
    """Send one HTML-formatted message. Returns whether Telegram accepted it."""
    if not configured():
        logger.info('Telegram not configured; message not sent')
        return False
    TOKEN, CHAT_ID = config.get('telegram_token'), config.get('telegram_chat_id')
    try:
        r = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                          json={'chat_id': CHAT_ID, 'text': html, 'parse_mode': 'HTML',
                                'disable_web_page_preview': True},
                          timeout=15)
        if r.ok:
            return True
        logger.warning(f'Telegram rejected the message: {r.status_code} {r.text[:200]}')
    except requests.RequestException as e:
        # requests puts the URL in its errors, and the URL holds the bot token.
        logger.warning('Telegram error: ' + str(e).replace(TOKEN, '***'))
    return False
