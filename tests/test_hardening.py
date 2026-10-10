"""
The review's fixes outside sign-in (tests/test_auth.py has those): the autopilot's state written in
one step by the app and the lab, so nothing you do while the lab simulates is lost; a proposal applied
twice keeping Undo; approve and reject naming the proposal shown; the autopilot policy clamped; the
generic settings route refusing what has its own endpoint; demo trades sized after earlier closes are
on the books; feed links that are not web addresses dropped; secrets encrypted at rest.

Run:  python -m unittest discover -s tests
"""
import json
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
from niftywhale import autopilot, lab, news, store  # noqa: E402

from tests.test_changelog import call  # noqa: E402
from tests.test_demo import paper_trade  # noqa: E402
from tests.test_regressions import _reset  # noqa: E402

PROPOSAL = {'params': {'min_rr': 2.5}, 'base': {'min_rr': 3.0}, 'since': '2026-10-01', 'change': 'Final R:R 3 → 2.5',
            'evidence': 'better out of sample'}


class FakeBench:
    """A backtest bench whose first simulation runs `during` (you clicking in the middle of it)."""
    def __init__(self, during=None):
        self.during, self.calls = during, 0

    def ready(self):
        return True

    def run(self, thr):
        self.calls += 1
        if self.calls == 1 and self.during:
            self.during()
        return []


class Autopilot(unittest.TestCase):
    def setUp(self):
        _reset()
        with store.connect() as c:
            c.execute('DELETE FROM autopilot_log')
            c.execute("DELETE FROM settings WHERE key LIKE 'autopilot:%'")
        store.set_settings({'rules': {}, 'autopilot_policy': {}})
        self.tg = mock.patch.object(lab, 'tg')
        self.tg.start()

    def tearDown(self):
        self.tg.stop()

    def actions(self):
        return [r['action'] for r in store.autopilot_log()]

    def test_a_rejection_during_the_shadow_run_stands(self):
        lab.save_state('swing', {'challenger': dict(PROPOSAL)})
        # You reject it while the lab is simulating (what the reject button does, in one step).
        reject = lambda: lab.update_state('swing', lambda s: s.pop('challenger', None))     # noqa: E731
        with mock.patch.object(autopilot, 'shadow_decision', return_value=('promote', 'beat the current rules')):
            notes = lab.shadow_and_guards('swing', FakeBench(during=reject))
        st = lab.state('swing')
        self.assertNotIn('challenger', st)                    # the old copy did not bring it back
        self.assertNotIn('last_change', st)                   # and it was not promoted
        self.assertNotIn('promote', self.actions())
        self.assertEqual(notes, [])

    def test_the_shadow_verdict_still_lands_when_nothing_changed(self):
        lab.save_state('swing', {'challenger': dict(PROPOSAL)})
        with mock.patch.object(autopilot, 'shadow_decision', return_value=('promote', 'beat the current rules')):
            lab.shadow_and_guards('swing', FakeBench())
        st = lab.state('swing')
        self.assertEqual(st['last_change']['changed'], {'min_rr': [None, 2.5]})
        self.assertNotIn('challenger', st)

    def test_applied_twice_keeps_undo(self):
        first = lab.apply_thresholds('swing', {'min_rr': 2.5}, 'x', base={'min_rr': 3.0})
        second = lab.apply_thresholds('swing', {'min_rr': 2.5}, 'x', base={'min_rr': 3.0})
        self.assertEqual((first, second), ({'min_rr': [None, 2.5]}, {}))
        self.assertEqual(lab.state('swing')['last_change']['changed'], {'min_rr': [None, 2.5]})   # Undo still knows 'before'
        self.assertEqual(self.actions().count('promote'), 1)

    def test_approve_and_reject_name_the_proposal_shown(self):
        lab.save_state('swing', {'awaiting': dict(PROPOSAL)})
        self.assertIn('since', call(lambda: app.api_autopilot_action('approve'), '/api/autopilot/approve', {'mode': 'swing'})['error'])
        r = call(lambda: app.api_autopilot_action('approve'), '/api/autopilot/approve', {'mode': 'swing', 'since': '2026-09-01'})
        self.assertIn('no longer open', r['error'])
        self.assertNotIn('last_change', lab.state('swing'))
        r = call(lambda: app.api_autopilot_action('reject'), '/api/autopilot/reject', {'mode': 'swing', 'since': PROPOSAL['since']})
        self.assertTrue(r['ok'])
        self.assertNotIn('awaiting', lab.state('swing'))
        self.assertIn('reject', self.actions())

    def test_undo_once(self):
        lab.apply_thresholds('swing', {'min_rr': 2.5}, 'x', base={'min_rr': 3.0})
        self.assertTrue(call(lambda: app.api_autopilot_action('undo'), '/api/autopilot/undo', {'mode': 'swing'})['ok'])
        self.assertIn('no change', call(lambda: app.api_autopilot_action('undo'), '/api/autopilot/undo', {'mode': 'swing'})['error'])
        self.assertEqual(json.loads(store.settings()['rules']), {})

    def test_policy_is_clamped(self):
        p = autopilot.policy({'search_steps': 1e18, 'min_gain_r': -5, 'shadow_days': 0, 'max_steps': True,
                              'min_oos_trades': {'swing': -1, 'intraday': 'x', 'futures': 5}, 'rollback_r': {'swing': 1e9},
                              'limits': {'swing': {'min_rr': [float('-inf'), 4], 'min_atr_pct': [1, 2]}}, 'mode': 'yolo'})
        self.assertEqual((p['search_steps'], p['min_gain_r'], p['shadow_days'], p['max_steps'], p['mode']), (12, 0.0, 5, 2, 'auto'))
        self.assertEqual(p['min_oos_trades'], {'swing': 20, 'intraday': 60, 'options': 40})
        self.assertEqual(p['rollback_r']['swing'], 50.0)
        self.assertEqual(p['limits'], {'swing': {'min_atr_pct': [1.0, 2.0]}})
        self.assertGreaterEqual(p['shadow_max_days'], p['shadow_days'])
        self.assertEqual(len(autopilot.grid('min_rr', 3.0, {'step': 0.5, 'min': 1, 'max': 5}, p, 'swing')), 9)

    def test_generic_settings_route_refuses_what_has_its_own(self):
        r = call(app.api_settings, '/api/settings', {'autopilot_policy': {'min_gain_r': -1}, 'telegram': '1'})
        self.assertIn('autopilot_policy', r['error'])
        for k in ('rules', 'paused_swing', 'demo', 'demo_since'):
            self.assertIn('error', call(app.api_settings, '/api/settings', {k: '1'}))
        self.assertNotIn('error', call(app.api_settings, '/api/settings', {'telegram': '1'}))


class DemoOrder(unittest.TestCase):
    def setUp(self):
        _reset()
        store.clear_demo()
        store.set_settings({'demo': {}, 'demo_since': ''})
        store.set_settings({'demo_since': '2026-10-08T09:00:00+05:30'})
        store.add_demo_ledger('deposit', 100_000, '', '2026-10-08T09:00:00+05:30')

    def test_a_close_between_two_syncs_is_on_the_books_before_the_next_entry(self):
        one = paper_trade('ONE', entry=100, stop=99, at='2026-10-08T10:00:00+05:30')
        app.demo_sync()                                       # ONE opens
        t = json.loads(store.get_zone(one)['trigger'])
        t.update(exit=104, exit_time='2026-10-08T14:00:00+05:30', r=4)
        with store.connect() as c:                            # ONE hits its target, then TWO comes in
            c.execute("UPDATE zones SET status = 'won', trigger = ? WHERE id = ?", (json.dumps(t), one))
        paper_trade('TWO', entry=100, stop=99, at='2026-10-08T15:00:00+05:30')
        app.demo_sync()
        p1, p2 = (next(p for p in store.demo_positions() if p['symbol'] == s) for s in ('ONE', 'TWO'))
        self.assertEqual(p1['status'], 'closed')
        # TWO is sized from the account with ONE's profit in it (as one sync would have).
        self.assertEqual(p2['qty'], int(0.25 * (100_000 + p1['net']) / 100))


class Links(unittest.TestCase):
    def test_only_web_addresses(self):
        self.assertEqual(news.web_url(' https://x.example/a '), 'https://x.example/a')
        self.assertEqual(news.web_url('HTTP://x.example'), 'HTTP://x.example')
        for bad in ('javascript:alert(1)', 'data:text/html,x', '//x.example', '', None):
            self.assertIsNone(news.web_url(bad))


class SecretsAtRest(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        self.dir = tempfile.mkdtemp()
        self.key = os.path.join(self.dir, 'key')
        with open(self.key, 'wb') as f:
            f.write(Fernet.generate_key())
        self.patch = mock.patch.object(store, 'KEY_FILE', self.key)
        self.patch.start()
        store._key.update(loaded=False, fernet=None)
        store.init()

    def tearDown(self):
        store.set_kv('secret:test', '')
        self.patch.stop()
        store._key.update(loaded=False, fernet=None)

    def raw(self, key):
        with store.connect() as c:
            return c.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()[0]

    def test_sealed_in_the_database_plain_to_the_app(self):
        store.set_kv('secret:test', 'my-dhan-pin')
        self.assertTrue(self.raw('secret:test').startswith(store.SEALED))
        self.assertNotIn('my-dhan-pin', self.raw('secret:test'))
        self.assertEqual(store.get_kv('secret:test'), 'my-dhan-pin')
        store.set_kv('cfg:plain', 'x')
        self.assertEqual(self.raw('cfg:plain'), 'x')                     # only secrets are sealed

    def test_plain_secrets_from_before_are_sealed_at_start(self):
        with store.connect() as c:
            c.execute("INSERT INTO settings (key, value) VALUES ('secret:test', 'old-token') "
                      'ON CONFLICT(key) DO UPDATE SET value = excluded.value')
        self.assertEqual(store.seal_stored(), 1)
        self.assertTrue(self.raw('secret:test').startswith(store.SEALED))
        self.assertEqual(store.get_kv('secret:test'), 'old-token')

    def test_another_key_reads_nothing(self):
        from cryptography.fernet import Fernet
        store.set_kv('secret:test', 'my-dhan-pin')
        with open(self.key, 'wb') as f:
            f.write(Fernet.generate_key())
        store._key.update(loaded=False, fernet=None)
        self.assertIsNone(store.get_kv('secret:test'))                  # enter it again, never garbage


if __name__ == '__main__':
    unittest.main()
