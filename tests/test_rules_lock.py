"""
The rules lock: while it is on, nothing changes the three tuners' rules or the autopilot's policy, by hand or by
the autopilot; an unlock lasts app.RULES_UNLOCK_S and then they lock again by themselves.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import time
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
from niftywhale import lab, store  # noqa: E402


class RulesLock(unittest.TestCase):
    def setUp(self):
        store.set_settings({'rules_lock': {}, 'options_rules': {}, 'autopilot_policy': {}})
        self.c = app.app.test_client()

    def tearDown(self):
        store.set_settings({'rules_lock': {}, 'options_rules': {}, 'autopilot_policy': {}})

    def lock(self, on=True):
        return self.c.post('/api/rules/lock', json={'locked': on}).get_json()

    def test_locked_rules_refuse_every_change(self):
        self.assertEqual(self.lock(), {'locked': True, 'open': False, 'until': None, 'unlock_s': app.RULES_UNLOCK_S})
        for path, body in (('/api/options/rules', {'min_bias': 0.5}), ('/api/rules', {'rules': {}}),
                           ('/api/intraday/rules', {'rules': {}}), ('/api/autopilot/policy', {'mode': 'auto'}),
                           ('/api/autopilot/approve', {'mode': 'options', 'since': '2026-10-01'}),
                           ('/api/autopilot/undo', {'mode': 'options'})):
            r = self.c.post(path, json=body)
            self.assertEqual((r.status_code, r.get_json().get('rules_locked')), (423, True), path)
        self.assertEqual(store.settings()['options_rules'], '{}')
        # Not a rule change: pausing alerts and rejecting a proposal stay open.
        self.assertNotEqual(self.c.post('/api/autopilot/pause', json={'mode': 'options', 'paused': False}).status_code, 423)
        # Nor through the generic settings route.
        self.assertEqual(self.c.post('/api/settings', json={'rules_lock': {}}).status_code, 400)
        self.assertIn('locked', app.autopilot_decide('options', 'approve', '2026-10-01'))

    def test_the_autopilot_changes_nothing_while_locked(self):
        self.lock()
        self.assertEqual(lab.apply_thresholds('options', {'min_bias': 0.5}, 'test'), {})
        self.assertEqual(store.settings()['options_rules'], '{}')
        store.set_settings({'rules_lock': {}})
        self.assertEqual(lab.apply_thresholds('options', {'min_bias': 0.5}, 'test'), {'min_bias': [None, 0.5]})

    def test_an_unlock_runs_out(self):
        self.lock()
        self.assertTrue(self.lock(False)['open'])            # sign-in is off in the tests: no password to give
        self.assertEqual(self.c.post('/api/options/rules', json={'min_bias': 0.5}).status_code, 200)
        self.assertFalse(store.rules_locked())
        self.assertTrue(store.rules_locked(now=time.time() + app.RULES_UNLOCK_S + 1))   # locked again by itself
        self.assertFalse(self.lock()['open'])                 # and locked at once on asking
        with mock.patch.object(store, 'rules_locked', return_value=False):
            self.assertEqual(self.c.post('/api/options/rules', json={}).status_code, 200)

    def test_off_until_first_locked(self):
        self.assertEqual(self.c.get('/api/state').get_json()['rules_lock']['open'], True)
        self.assertFalse(self.lock(False)['locked'])          # nothing to unlock: stays as it was


if __name__ == '__main__':
    unittest.main()
