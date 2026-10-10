"""
Looking back at what the lab's runs did: a job's backtest steps lead to the run they saved (the
Performance view of that run, with the rules it used), any kept run can be picked, and older
jobs page in below the recent ones.

Run:  python -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import lab, store  # noqa: E402

from tests.test_changelog import call  # noqa: E402


def run(mode, trades, total_r, params=None):
    return store.add_run(mode, 'baseline', params or {'min_rr': 3.0}, ('2025-10-01', '2026-10-01'), 'nifty100',
                         {'trades': trades, 'total_r': total_r, 'win_rate': 40.0}, [], [['2026-01-01', 1.0]], {})


def job(kind, steps, status='done'):
    jid = store.add_job(kind)
    store.update_job(jid, status=status, steps=json.dumps(steps), started='2026-10-09T20:30:00',
                     finished='2026-10-09T21:20:00', message='m')
    return jid


class Runs(unittest.TestCase):
    def setUp(self):
        store.init()
        with store.connect() as c:
            c.execute('DELETE FROM bt_runs')
            c.execute('DELETE FROM lab_jobs')

    def test_a_job_step_leads_to_its_run(self):
        rid = run('swing', 167, -40.29)
        with store.connect() as c:          # an older job: no run id with the step, only its times
            c.execute("UPDATE bt_runs SET created = '2026-10-09T21:09:28' WHERE id = ?", (rid,))
        steps = {'swing_sim': {'started': '2026-10-09T20:58:54', 'finished': '2026-10-09T21:09:35'},
                 'intraday_sim': {'started': '2026-10-09T21:09:35', 'finished': '2026-10-09T21:18:10'}}
        job('nightly', steps)
        v = lab.job_view(store.jobs(1)[0], runs=store.runs())
        by = {s['key']: s for s in v['steps']}
        self.assertEqual((by['swing_sim']['run_id'], by['swing_sim']['run_mode']), (rid, 'swing'))
        self.assertIsNone(by['intraday_sim']['run_id'])                  # no intraday run in its window

    def test_the_run_id_recorded_with_the_step_and_a_pruned_run(self):
        rid = run('intraday', 173, -1.17)
        jid = job('nightly', {'intraday_sim': {'started': '2026-01-01T00:00:00', 'finished': '2026-01-01T00:01:00', 'run_id': rid}})
        v = lab.job_view(store.jobs(1)[0], runs=store.runs())
        self.assertEqual(next(s for s in v['steps'] if s['key'] == 'intraday_sim')['run_id'], rid)
        store.prune_runs(keep=0)
        v = lab.job_view(store.jobs(1)[0], runs=store.runs())
        self.assertIsNone(next(s for s in v['steps'] if s['key'] == 'intraday_sim')['run_id'])
        self.assertEqual(v['id'], jid)

    def test_joblog_records_the_run(self):
        jid = store.add_job('backtest')
        log = lab.JobLog({'id': jid, 'kind': 'backtest'})
        log('swing: simulating the current rules')
        log.link_run(42)
        steps = json.loads(store.jobs(1)[0]['steps'])
        self.assertEqual(next(iter(steps.values()))['run_id'], 42)

    def test_pick_an_earlier_run(self):
        old = run('swing', 100, 5.0, {'min_rr': 2.5})
        new = run('swing', 120, -3.0)
        d = call(app.api_perf, '/api/perf?mode=swing&source=backtest')
        self.assertEqual((d['run']['id'], d['latest'], [r['id'] for r in d['runs']]), (new, True, [new, old]))
        d = call(app.api_perf, f'/api/perf?mode=swing&source=backtest&run={old}')
        self.assertEqual((d['run']['id'], d['latest'], d['run']['params'], d['summary']['trades']),
                         (old, False, {'min_rr': 2.5}, 100))
        self.assertIn('error', call(app.api_perf, f'/api/perf?mode=intraday&source=backtest&run={old}'))   # another mode's
        self.assertIn('error', call(app.api_perf, '/api/perf?mode=swing&source=backtest&run=x'))

    def test_older_jobs_page_in(self):
        ids = [job('nightly', {}) for _ in range(5)]
        d = call(app.api_lab_jobs, f'/api/lab/jobs?before={ids[3]}&limit=2')
        self.assertEqual(([j['id'] for j in d['jobs']], d['more']), ([ids[2], ids[1]], True))
        d = call(app.api_lab_jobs, f'/api/lab/jobs?before={ids[1]}&limit=2')
        self.assertEqual(([j['id'] for j in d['jobs']], d['more']), ([ids[0]], False))
        self.assertIn('error', call(app.api_lab_jobs, '/api/lab/jobs'))


if __name__ == '__main__':
    unittest.main()
