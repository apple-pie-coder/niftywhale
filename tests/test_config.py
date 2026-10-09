"""
App settings (config.py, /api/config): what used to need .env. A value saved in the app wins over
.env, clearing it falls back; bad values are refused; secrets never come back out; Telegram and the
Dhan login read the new values at once, with no restart.

Run:  python -m unittest discover -s tests
"""
import json
import logging
import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

import app  # noqa: E402
from niftywhale import config, dhan, notify, store  # noqa: E402

TOKEN = '123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw'


def clear():
    with store.connect() as c:
        c.execute("DELETE FROM settings WHERE key LIKE 'cfg:%' OR key LIKE 'secret:telegram%' OR key LIKE 'secret:dhan_pin' "
                  "OR key LIKE 'secret:dhan_totp'")
    config._cache.clear()


class Precedence(unittest.TestCase):
    def setUp(self):
        clear()

    def test_app_beats_env_beats_default(self):
        with mock.patch.dict(os.environ, {'WATCH_DELAY_SECONDS': '120'}):
            config._cache.clear()
            self.assertEqual((config.get_int('watch_delay'), config.source('watch_delay')), (120, 'env'))
            config.save({'watch_delay': '45'})
            self.assertEqual((config.get_int('watch_delay'), config.source('watch_delay')), (45, 'app'))
            config.save({'watch_delay': ''})                      # cleared: .env again
            self.assertEqual((config.get_int('watch_delay'), config.source('watch_delay')), (120, 'env'))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WATCH_DELAY_SECONDS', None)
            config._cache.clear()
            self.assertEqual((config.get_int('watch_delay'), config.source('watch_delay')), (90, 'default'))

    def test_validation(self):
        self.assertIsNone(config.validate('telegram_token', TOKEN))
        self.assertIsNotNone(config.validate('telegram_token', 'nope'))
        self.assertIsNone(config.validate('telegram_chat_id', '-1001234567890'))
        self.assertIsNone(config.validate('dhan_totp', 'jbsw y3dp ehpk 3pxp'))         # spaces and case are cleaned
        self.assertEqual(config.clean('dhan_totp', 'jbsw y3dp ehpk 3pxp'), 'JBSWY3DPEHPK3PXP')
        self.assertIsNotNone(config.validate('dhan_pin', '12ab'))
        self.assertIsNotNone(config.validate('watch_delay', '-5'))
        self.assertIsNotNone(config.validate('data_source', 'bloomberg'))
        self.assertIsNone(config.validate('dhan_pin', ''))                              # empty clears
        self.assertIsNotNone(config.validate('nonsense', '1'))


class Routes(unittest.TestCase):
    def setUp(self):
        clear()
        self.client = app.app.test_client()

    def test_secrets_never_come_back(self):
        with mock.patch.object(notify, 'bot_name', return_value='whale_bot'), \
             self.assertLogs('niftywhale', level='INFO') as logs:
            r = self.client.post('/api/config', json={'telegram_token': TOKEN, 'telegram_chat_id': '987654321',
                                                      'dhan_pin': '8642', 'dhan_totp': 'JBSWY3DPEHPK3PXP'})
        self.assertEqual(r.status_code, 200)
        raw = r.get_data(as_text=True)
        for secret in (TOKEN, '8642', 'JBSWY3DPEHPK3PXP'):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, '\n'.join(logs.output))
        f = r.get_json()['fields']
        self.assertEqual((f['telegram_token']['set'], f['telegram_token']['bot'], f['dhan_pin']['set']), (True, 'whale_bot', True))
        self.assertNotIn('value', f['telegram_token'])
        self.assertEqual((f['telegram_chat_id']['value'], f['telegram_chat_id']['source']), ('987654321', 'app'))

    def test_bad_values_are_refused_whole(self):
        r = self.client.post('/api/config', json={'telegram_chat_id': '987654321', 'dhan_pin': 'abcd'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('dhan_pin', r.get_json()['fields'])
        self.assertEqual(config.source('telegram_chat_id') in ('env', 'default'), True)     # nothing saved

    def test_telegram_and_dhan_follow_at_once(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': '', 'DHAN_CLIENT_ID': '',
                                          'DHAN_PIN': '', 'DHAN_TOTP_SECRET': ''}):
            config._cache.clear()
            self.assertFalse(notify.configured())
            with mock.patch.object(notify, 'bot_name', return_value=None):
                self.client.post('/api/config', json={'telegram_token': TOKEN, 'telegram_chat_id': '987654321'})
            self.assertTrue(notify.configured())
            with mock.patch.object(notify.requests, 'post') as post:
                post.return_value.ok = True
                self.assertTrue(notify.send('hello'))
            self.assertIn(TOKEN, post.call_args[0][0])
            self.assertEqual(post.call_args[1]['json']['chat_id'], '987654321')
            # Dhan: the three login details make it automatic, and a login starts in the background.
            with mock.patch.object(notify, 'bot_name', return_value=None), \
                 mock.patch.object(dhan, 'keep_alive') as ka, mock.patch.object(app.threading, 'Thread') as th:
                self.client.post('/api/config', json={'dhan_client_id': '1100012345', 'dhan_pin': '8642', 'dhan_totp': 'JBSWY3DPEHPK3PXP'})
            self.assertEqual(dhan.mode(), 'auto')
            self.assertIs(th.call_args[1]['target'], ka)
            self.assertEqual(dhan._state['next_login'], 0.0)

    def test_log_level_applies(self):
        with mock.patch.object(notify, 'bot_name', return_value=None):
            self.client.post('/api/config', json={'log_level': 'warning'})
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.client.post('/api/config', json={'log_level': ''})
        self.assertEqual(config.source('log_level') in ('env', 'default'), True)
        logging.getLogger().setLevel(logging.INFO)


if __name__ == '__main__':
    unittest.main()
