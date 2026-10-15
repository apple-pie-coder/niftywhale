"""
Telegram commands (telebot.py, app.py's tg_*): parsing, who is answered, the Yes / No confirmation, mute, and
the commands themselves with Telegram mocked (nothing leaves the machine).

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import auth, data, notify, store, telebot  # noqa: E402


class Transport(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(telebot.parse('/squareoff@NiftyWhaleBot o12'), ('squareoff', ['o12']))
        self.assertEqual(telebot.parse('/STATUS'), ('status', []))
        self.assertEqual(telebot.parse('hello there'), (None, ['hello', 'there']))
        self.assertEqual(telebot.parse(''), (None, []))

    def test_long_replies_are_split_under_the_limit(self):
        text = '\n'.join(f'line {n} ' + 'x' * 50 for n in range(200))
        parts = telebot.chunks(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual('\n'.join(parts), text)
        self.assertEqual([len(p) for p in telebot.chunks('y' * 2500, 1000)], [1000, 1000, 500])

    def test_only_the_configured_chat(self):
        with mock.patch.object(telebot, '_chat', lambda: '42'):
            self.assertTrue(telebot.ours(42))
            self.assertTrue(telebot.ours('42'))
            self.assertFalse(telebot.ours(43))
            handled = []
            telebot._one({'message': {'chat': {'id': 99}, 'date': time.time(), 'text': '/status'}},
                         lambda n, a: handled.append(n))
            self.assertEqual(handled, [])
            telebot._one({'message': {'chat': {'id': 42}, 'date': time.time(), 'text': '/status'}},
                         lambda n, a: handled.append(n))
            self.assertEqual(handled, ['status'])

    def test_a_message_from_while_the_app_was_down_is_not_acted_on(self):
        said, handled = [], []
        with mock.patch.object(telebot, '_chat', lambda: '42'), mock.patch.object(telebot, 'reply', lambda html, *a, **k: said.append(html)):
            telebot._one({'message': {'chat': {'id': 42}, 'date': time.time() - 3600, 'text': '/halt'}},
                         lambda n, a: handled.append(n))
        self.assertEqual(handled, [])
        self.assertIn('send it again', said[0])


class Confirm(unittest.TestCase):
    def setUp(self):
        self.said, self.calls = [], []
        for p in (mock.patch.object(telebot, '_chat', lambda: '42'),
                  mock.patch.object(telebot, 'call', lambda method, **kw: self.calls.append((method, kw)) or {}),
                  mock.patch.object(telebot, 'reply', lambda html, buttons=None, edit=None: self.said.append((html, buttons, edit)))):
            p.start()
            self.addCleanup(p.stop)
        telebot._pending.clear()

    def press(self, data):
        telebot.answer_button({'id': 'cb1', 'data': data, 'message': {'message_id': 7, 'text': 'Square off?', 'chat': {'id': 42}}})

    def test_yes_acts_once_no_cancels(self):
        done = []
        telebot.confirm('Square off?', 'square off', lambda: done.append(1) or 'Closed.')
        (q, buttons, _), = self.said
        yes, no = buttons[0][0][1], buttons[0][1][1]
        self.press(yes)
        self.assertEqual(done, [1])
        self.assertIn('Closed.', self.said[-1][0])
        self.assertEqual(self.said[-1][2], 7)                    # the question is replaced, not a new message
        self.press(yes)                                          # a second press: the token is spent
        self.assertEqual(done, [1])
        self.assertIn('lapsed', self.said[-1][0])
        telebot.confirm('Again?', 'do', lambda: done.append(2) or 'x')
        self.press(self.said[-1][1][0][1][1])                   # No
        self.assertEqual(done, [1])
        self.assertIn('Cancelled', self.said[-1][0])

    def test_a_late_yes_does_nothing(self):
        done = []
        telebot.confirm('Halt?', 'halt', lambda: done.append(1) or 'Halted.')
        tok = self.said[-1][1][0][0][1]
        for p in telebot._pending.values():
            p['until'] = time.time() - 1
        self.press(tok)
        self.assertEqual(done, [])

    def test_a_failing_action_says_so(self):
        telebot.confirm('Go?', 'go', lambda: 1 / 0)
        self.press(self.said[-1][1][0][0][1])
        self.assertIn('Could not go', self.said[-1][0])

    def test_a_command_button_runs_the_command(self):
        got = []
        telebot.answer_button({'id': 'c', 'data': 'c:/squareoff o12', 'message': {'message_id': 1, 'chat': {'id': 42}}},
                              lambda n, a: got.append((n, a)))
        self.assertEqual(got, [('squareoff', ['o12'])])


class Mute(unittest.TestCase):
    def setUp(self):
        store.init()
        store.set_settings({'telegram_mute_until': ''})

    def test_mute_holds_back_all_but_urgent(self):
        self.assertEqual(app.tg_mute(['1h'])[:16], 'Alerts muted unt')
        self.assertTrue(notify.muted())
        posted = []
        with mock.patch.object(notify, 'configured', lambda: True), \
                mock.patch.object(notify.config, 'get', lambda k: 'x'), \
                mock.patch.object(notify.requests, 'post', lambda *a, **k: posted.append(k['json']['text']) or mock.Mock(ok=True)):
            self.assertFalse(notify.send('an idea'))
            self.assertTrue(notify.send('order filled', urgent=True))
        self.assertEqual(posted, ['order filled'])
        self.assertEqual(app.tg_mute(['off']), 'Alerts are on again.')
        self.assertEqual(notify.muted(), '')

    def test_past_and_bad_times_are_not_muted(self):
        store.set_settings({'telegram_mute_until': (data.now_ist() - timedelta(minutes=1)).isoformat()})
        self.assertEqual(notify.muted(), '')
        store.set_settings({'telegram_mute_until': 'soon'})
        self.assertEqual(notify.muted(), '')
        self.assertIn('how long', app.tg_mute(['forever']))
        self.assertIn('Between', app.tg_mute(['0m']))


class Commands(unittest.TestCase):
    def setUp(self):
        store.init()
        with store.connect() as c:
            for t in ('oc_ideas', 'demo_positions', 'exec_orders'):
                c.execute(f'DELETE FROM {t}')
        self.said = []
        for p in (mock.patch.object(telebot, 'reply', lambda html, buttons=None, edit=None: self.said.append((html, buttons))),
                  mock.patch.object(telebot, 'confirm', lambda q, label, do: self.said.append((q, [[('Yes', do)]])) or True)):
            p.start()
            self.addCleanup(p.stop)

    def yes(self):
        """Press the last confirmation's Yes."""
        return self.said[-1][1][0][0][1]()

    def test_refs(self):
        self.assertEqual(app.tg_ref('options:12'), 'o12')
        self.assertEqual((app.tg_unref('o12'), app.tg_unref('S7'), app.tg_unref('x1'), app.tg_unref('o')),
                         ('options:12', 'swing:7', '', ''))

    def test_help_unknown_and_every_command_listed(self):
        app.tg_handle('nonsense', [])
        self.assertIn('/help', self.said[-1][0])
        app.tg_handle('help', [])
        for c, _ in app.TG_COMMANDS:
            self.assertIn(f'/{c}', self.said[-1][0])
            self.assertIn(c, app.TG_HANDLERS)

    def test_read_only_commands_answer(self):
        for name, args in (('status', []), ('open', []), ('today', []), ('ideas', []), ('demo', []), ('chain', ['NIFTY']),
                           ('rules', ['options']), ('rules', ['bogus']), ('stock', []), ('autopilot', [])):
            with mock.patch.object(app.dhan, 'available', lambda: False):
                app.tg_handle(name, args)
            self.assertTrue(self.said[-1][0], name)
        self.assertEqual(app.tg_open([]), 'Nothing open.')
        self.assertIn('Which mode', app.tg_rules(['bogus']))
        self.assertIn('Min', app.tg_rules(['options']))

    def test_ideas_lists_today_with_its_trail(self):
        today = data.now_ist().date().isoformat()
        iid = store.add_idea({'symbol': 'NIFTY', 'expiry': '2026-10-20', 'strike': 22800, 'side': 'CE', 'direction': 'long',
                              'created_at': data.now_ist().isoformat(), 'session': today, 'status': 'open', 'entry': 100.0,
                              'stop': 75.0, 'target': 150.0, 'lot': 65, 'trail_pct': 20})
        store.start_trail(iid, 160.0, 128.0)
        store.update_idea(iid, r_open=2.4)
        out = app.tg_ideas([])
        self.assertIn(f'o{iid}', out)
        self.assertIn('trailing', out)
        self.assertIn('+2.40R', out)

    def test_pause_asks_then_pauses(self):
        store.set_settings({'paused_options': '0'})
        self.assertIn('Which mode', app.tg_pause(['bogus']))
        app.tg_pause(['options'])
        self.assertFalse(app.paused('options'))                 # nothing until Yes
        self.assertIn('Paused: options', self.yes())
        self.assertTrue(app.paused('options'))
        app.tg_handle('resume', ['all'])
        self.yes()
        self.assertFalse(any(app.paused(m) for m in app.TG_MODES))

    def test_squareoff_asks_with_the_price_then_closes(self):
        items = {'paper': [{'ref': 'options:5', 'mode': 'options', 'symbol': 'NIFTY', 'side': 'long', 'label': 'NIFTY 22800 CE',
                            'entry': 100.0, 'stop': 75.0, 'target': 150.0, 'price': 120.0, 'r': 0.8, 'why': None}], 'demo': []}
        closed = []
        with mock.patch.object(app, 'squareoff_items', lambda *a: items), \
                mock.patch.object(app, 'square_off_now', lambda refs, ids: closed.append((refs, ids)) or
                                  {'paper': [{'ref': 'options:5', 'ok': True, 'price': 120.0, 'r': 0.8}], 'demo': []}):
            self.assertIn('Not an open trade', app.tg_squareoff(['o6']))
            app.tg_squareoff(['o5'])
            self.assertIn('NIFTY 22800 CE at about 120 (+0.80R)', self.said[-1][0])
            self.assertEqual(closed, [])
            self.assertIn('o5: closed at 120', self.yes())
        self.assertEqual(closed, [(['options:5'], [])])

    def test_apply_names_the_proposal_it_showed(self):
        from niftywhale import lab
        st = {'awaiting': {'since': '2026-10-12', 'change': 'stop_pct 25 → 20', 'evidence': 'out of sample ...', 'params': {}}}
        decided = []
        with mock.patch.object(lab, 'state', lambda m: st if m == 'options' else {}), \
                mock.patch.object(app, 'autopilot_decide', lambda *a: decided.append(a) or ''):
            self.assertIn('No swing proposal', app.tg_decide(['swing'], 'approve'))
            app.tg_decide(['options'], 'approve')
            self.assertIn('stop_pct 25 → 20', self.said[-1][0])
            self.assertIn('Applied', self.yes())
        self.assertEqual(decided, [('options', 'approve', '2026-10-12', 'from Telegram')])

    def test_place_needs_a_right_code(self):
        self.assertEqual(app.tg_place([]), 'No live order is waiting for you.')
        row = {'id': 3, 'symbol': 'NIFTY', 'instrument': 'NIFTY 22800 CE', 'mode': 'options', 'expires': '2026-10-15T10:05:00'}
        placed = []
        with mock.patch.object(app.store, 'exec_orders', lambda st: [row]), \
                mock.patch.object(app, 'exec_place', lambda r: placed.append(r['id']) or ('', 200)):
            self.assertIn('3: NIFTY 22800 CE', app.tg_place([]))
            with mock.patch.object(app.auth, 'use_totp', lambda code: 'that code is not right'):
                self.assertIn('not right', app.tg_place(['3', '000000']))
            self.assertEqual(placed, [])
            with mock.patch.object(app.auth, 'use_totp', lambda code: ''):
                self.assertIn('Sent', app.tg_place(['3', '123456']))
            self.assertIn('not waiting', app.tg_place(['4', '123456']))
        self.assertEqual(placed, [3])

    def test_halt_asks_then_halts(self):
        killed = []
        with mock.patch.object(app, 'exec_kill', lambda flatten, where='': killed.append(flatten) or []):
            app.tg_halt([])
            self.assertEqual(killed, [])
            self.assertIn('Halted', self.yes())
            app.tg_halt(['exit'])
            self.assertIn('EXIT every position', self.said[-1][0])
            self.yes()
        self.assertEqual(killed, [False, True])

    def test_scan_asks_first(self):
        started = []
        with mock.patch.object(app, 'start_scan_thread', lambda origin: started.append(origin) or True):
            self.assertIn('Which scan', app.tg_scan([]))
            app.tg_scan(['swing'])
            self.assertEqual(started, [])
            self.assertEqual(self.yes(), 'Started.')
        self.assertEqual(started, ['manual'])


class Authenticator(unittest.TestCase):
    def test_no_authenticator_no_order(self):
        with mock.patch.object(auth, 'owner', lambda: None):
            self.assertIn('set up an authenticator', auth.use_totp('123456'))
