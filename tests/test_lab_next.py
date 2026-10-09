"""
The Lab's "Coming up": the next nightly update, tuning and report with their date and time, queued jobs
first, and what can change them (a job still running, a mode cooling down, the autopilot off).

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import datetime

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import autopilot, lab  # noqa: E402

POL = autopilot.policy({})


def at(s):
    return datetime.fromisoformat(s + '+05:30')


def runs(now, marks=None, report_on=True, jobs=(), states=None, pol=POL):
    return lab.upcoming(at(now), marks or {}, report_on, list(jobs), states or {}, pol)


class Upcoming(unittest.TestCase):
    def test_friday_evening_before_the_nightly(self):
        out = runs('2026-10-09T20:00')                                  # a Friday
        self.assertEqual([(r['kind'], r['at']) for r in out], [('nightly', '2026-10-09T20:30+05:30'),
                                                               ('tune', '2026-10-10T06:00+05:30'),
                                                               ('report', '2026-10-10T09:00+05:30')])
        self.assertEqual(out[1]['day'], 'Saturday')

    def test_after_tonights_nightly_the_next_is_monday(self):
        out = runs('2026-10-09T21:00', marks={'nightly': '2026-10-09'})
        self.assertEqual(out[0]['kind'], 'tune')
        self.assertEqual(next(r['at'] for r in out if r['kind'] == 'nightly'), '2026-10-12T20:30+05:30')

    def test_past_its_time_and_not_queued_yet_is_due_now(self):
        out = runs('2026-10-09T20:31')
        self.assertEqual((out[0]['kind'], out[0]['at']), ('nightly', '2026-10-09T20:30+05:30'))   # its time, not now
        self.assertIn('queued within a minute', out[0]['notes'][0])

    def test_report_off_is_left_out(self):
        self.assertNotIn('report', [r['kind'] for r in runs('2026-10-09T20:00', report_on=False)])

    def test_queued_first_and_a_running_job_noted(self):
        jobs = [{'id': 3, 'kind': 'nightly', 'status': 'running'}, {'id': 4, 'kind': 'backtest', 'status': 'queued'}]
        out = runs('2026-10-09T21:00', marks={'nightly': '2026-10-09'}, jobs=jobs)
        self.assertTrue(out[0]['queued'])
        self.assertEqual(out[0]['job'], 4)
        self.assertIn('Nightly update (running now)', out[0]['notes'][0])
        nightly = next(r for r in out if r['kind'] == 'nightly')
        self.assertIn('skipped if the nightly update', nightly['notes'][0])
        tune = next(r for r in out if r['kind'] == 'tune')
        self.assertIn('waits for Nightly update', tune['notes'][0])

    def test_a_mode_cooling_down_says_until_when(self):
        states = {'swing': {'last_change': {'at': '2026-10-05T07:00:00+05:30'}}, 'intraday': {'challenger': {'x': 1}}}
        tune = next(r for r in runs('2026-10-09T20:00', states=states) if r['kind'] == 'tune')
        self.assertTrue(any('swing: cooling down' in n and 'Mon 19 Oct' in n for n in tune['notes']))
        self.assertTrue(any('intraday: skipped, a proposal is in shadow' in n for n in tune['notes']))
        nightly = next(r for r in runs('2026-10-09T20:00', states=states) if r['kind'] == 'nightly')
        self.assertTrue(any('shadow for intraday' in n for n in nightly['notes']))

    def test_autopilot_off(self):
        tune = next(r for r in runs('2026-10-09T20:00', pol={**POL, 'mode': 'off'}) if r['kind'] == 'tune')
        self.assertIn('autopilot is off', tune['notes'][0])

    def test_saturday_morning_soonest_first(self):
        out = runs('2026-10-10T07:00', marks={'tune': '2026-10-10'})
        self.assertEqual([(r['kind'], r['at'][:16]) for r in out],
                         [('report', '2026-10-10T09:00'), ('nightly', '2026-10-12T20:30'), ('tune', '2026-10-17T06:00')])

    def test_the_api_carries_it(self):
        import app
        d = app.app.test_client().get('/api/lab').get_json()
        self.assertIn('upcoming', d)
        self.assertTrue(all(r['at'] or r['queued'] for r in d['upcoming']))


if __name__ == '__main__':
    unittest.main()
