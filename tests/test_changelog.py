"""
The change log on the Performance tab: your Tune… saves and App settings changes logged next to
the autopilot's, as the values that moved (a secret never by its value), and older entries
paged in.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import config, store  # noqa: E402

from tests.test_regressions import _reset  # noqa: E402


def call(view, path, body=None):
    """An endpoint's own function, as the page would reach it (sign-in is tested elsewhere)."""
    with app.app.test_request_context(path, method='POST' if body is not None else 'GET', json=body):
        r = view()
    return r[0].get_json() if isinstance(r, tuple) else r.get_json()


class ChangeLog(unittest.TestCase):
    def setUp(self):
        _reset()
        with store.connect() as c:
            c.execute('DELETE FROM autopilot_log')
        self.env = mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '', 'INTRADAY_SQUARE_OFF': ''})   # not .env's
        self.env.start()
        config.save({'intraday_square_off': '', 'telegram_token': ''})

    def tearDown(self):
        config.save({'intraday_square_off': '', 'telegram_token': ''})
        self.env.stop()

    def test_a_tuner_save_logs_what_moved(self):
        call(app.api_intraday_rules, '/api/intraday/rules', {'rules': {'min_rr': 3.0}})
        r = store.autopilot_log()[0]
        self.assertEqual((r['mode'], r['action'], r['by']), ('intraday', 'tuned', 'you'))
        self.assertEqual((r['before'], r['after']), ({'min_rr': 2.0}, {'min_rr': 3.0}))
        call(app.api_intraday_rules, '/api/intraday/rules', {'rules': {'min_rr': 3.0}})          # nothing moved
        self.assertEqual(len(store.autopilot_log()), 1)
        call(app.api_intraday_rules, '/api/intraday/rules', {'rules': {}})                       # reset
        r = store.autopilot_log()[0]
        self.assertEqual((r['after'], r['why']), ({'min_rr': 2.0}, 'reset to .env with Tune…'))

    def test_swing_and_options_saves(self):
        call(app.api_rules, '/api/rules', {'rules': {'min_rr': 4.0}})
        self.assertEqual(store.autopilot_log()[0]['mode'], 'swing')
        key = next(iter(app.options.TUNABLE))
        default, lo, hi = app.options.TUNABLE[key][:3]
        call(app.api_options_rules, '/api/options/rules', {key: hi})
        r = store.autopilot_log()[0]
        self.assertEqual((r['mode'], r['after']), ('options', {key: hi}))

    def test_app_settings_logged_secrets_never_by_value(self):
        call(app.api_config_save, '/api/config', {'intraday_square_off': '15:05'})
        r = store.autopilot_log()[0]
        self.assertEqual((r['mode'], r['action'], r['before'], r['after']),
                         ('app', 'settings', {'intraday_square_off': '15:10'}, {'intraday_square_off': '15:05'}))
        token = '123456789:' + 'A' * 35
        with mock.patch.object(app.auth, '_need_session', return_value=None):     # a fresh browser sign-in
            call(app.api_config_save, '/api/config', {'telegram_token': token})
            call(app.api_config_save, '/api/config', {'telegram_token': token[:-1] + 'B'})
        got = [(r['before'], r['after']) for r in store.autopilot_log()[:2]]
        self.assertEqual(got, [({'telegram_token': 'set'}, {'telegram_token': 'replaced'}),
                               ({'telegram_token': 'not set'}, {'telegram_token': 'set'})])
        with store.connect() as c:
            self.assertNotIn('AAAA', ' '.join(r[0] + r[1] for r in c.execute('SELECT before, after FROM autopilot_log')))

    def test_older_entries_page_in(self):
        for i in range(45):
            store.log_autopilot('swing', 'tuned', {'min_rr': i}, {'min_rr': i + 1}, 'x', 'you')
        lab = store.autopilot_log(41)
        self.assertEqual(len(lab), 41)
        oldest_shown = lab[39]['id']
        d = call(app.api_lab_log, f'/api/lab/log?before={oldest_shown}&limit=3')
        self.assertEqual(([r['before']['min_rr'] for r in d['log']], d['more']), ([4, 3, 2], True))
        d = call(app.api_lab_log, f'/api/lab/log?before={oldest_shown}&limit=50')
        self.assertEqual((len(d['log']), d['more']), (5, False))

    def test_labels_name_each_value_as_the_tuners_do(self):
        labels = app.change_labels()
        self.assertEqual(labels['intraday']['min_rr'], app.intraday.TUNABLE['min_rr']['label'])
        self.assertEqual(labels['app']['intraday_square_off'], 'Intraday square-off')
        self.assertTrue(set(config.FIELDS) <= set(labels['app']))


if __name__ == '__main__':
    unittest.main()
