"""
NSE's dated files backfilled for the lab (niftywhale/backfill.py): the F&O bhavcopy in both of its
formats, the event calendar, futures buildup and results timing, the backtester's view of them
(NseHistory, the same reads as a live entry's smart context), and the gates on them.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

from niftywhale import backfill, features, smart, smc, store  # noqa: E402

OLD = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,
FUTSTK,AARTIIND,26-Oct-2023,0,XX,494.65,494.75,488,492.25,492.25,1838,9030.14,14000000,19000,03-OCT-2023,
FUTSTK,AARTIIND,30-Nov-2023,0,XX,497.65,497.65,491.9,495.75,495.75,147,726.99,518000,27000,03-OCT-2023,
OPTSTK,AARTIIND,26-Oct-2023,500,CE,1,1,1,1,1,1,1,100,1,03-OCT-2023,
FUTIDX,NIFTY,26-Oct-2023,0,XX,1,1,1,1,1,1,1,999,1,03-OCT-2023,
"""
NEW = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol
2026-10-09,2026-10-09,FO,NSE,STF,61476,,ABCAPITAL,,2026-11-23,2026-11-23,,,X,374.4,388.25,374.4,384.40,383.5,372.75,380.64,384.4,1000,50,10
2026-10-09,2026-10-09,FO,NSE,STF,48708,,ABCAPITAL,,2026-10-27,2026-10-27,,,X,373.2,386.4,370.1,382.75,382.2,371.35,380.64,382.75,9000,-20,10
2026-10-09,2026-10-09,FO,NSE,IDF,1,,NIFTY,,2026-10-27,2026-10-27,,,X,1,1,1,1,1,1,1,1,5,5,1
"""


class Parsing(unittest.TestCase):
    def test_fo_bhav_both_formats(self):
        self.assertEqual(smart.parse_fo_bhav(OLD), {'AARTIIND': {'oi': 14518000, 'oi_chg': 46000, 'close': 492.25}})
        self.assertEqual(smart.parse_fo_bhav(NEW), {'ABCAPITAL': {'oi': 10000, 'oi_chg': 30, 'close': 382.75}})   # nearest expiry's close

    def test_events(self):
        rows = [{'symbol': 'ZEEMEDIA', 'purpose': 'Financial Results', 'date': '31-Oct-2023'}, {'symbol': '', 'date': 'x'}]
        self.assertEqual(smart.parse_events(rows), [{'symbol': 'ZEEMEDIA', 'day': '2023-10-31', 'purpose': 'Financial Results'}])


class Reads(unittest.TestCase):
    def test_futures_buildup(self):
        rows = lambda oi, px: [{'oi': 1000, 'close': 100.0}] * 5 + [{'oi': oi, 'close': px}]     # noqa: E731
        self.assertEqual(features.futures_context(rows(1100, 104))['fut_buildup'], 'long buildup')
        self.assertEqual(features.futures_context(rows(1100, 96))['fut_buildup'], 'short buildup')
        self.assertEqual(features.futures_context(rows(900, 104))['fut_buildup'], 'short covering')
        self.assertEqual(features.futures_context(rows(900, 96))['fut_buildup'], 'long unwinding')
        self.assertEqual(features.futures_context(rows(1010, 104))['fut_buildup'], 'neutral')      # OI moved < 2 %
        self.assertEqual(features.futures_context(rows(1100, 104)[:3]), {})

    def test_results_timing(self):
        r = features.results_context(['2026-07-20', '2026-10-20'], date(2026, 10, 12))
        self.assertEqual(r, {'days_to_results': 8, 'days_from_results': 84})


class History(unittest.TestCase):
    def setUp(self):
        store.init()
        with store.connect() as c:
            for t in ('sm_delivery', 'sm_futoi', 'sm_poi', 'sm_events'):
                c.execute(f'DELETE FROM {t}')
        self.addCleanup(store.set_json, 'backfill:delivery_keep', {})

    def test_the_backtester_reads_what_a_live_entry_does(self):
        days = [date(2026, 9, 1) + timedelta(days=k) for k in range(25)]
        for k, d in enumerate(days):
            last = k == len(days) - 1          # the last session: heavy volume, high delivery, up: accumulation
            store.add_delivery(d.isoformat(), {'ABC': {'close': 101.0 if last else 100.0, 'prev_close': 100.0,
                                                        'qty': 300_000 if last else 100_000, 'deliv_qty': 1, 'deliv_pct': 60.0 if last else 30.0}})
            store.add_futoi(d.isoformat(), {'ABC': {'oi': 1000 + (k >= 20) * 200, 'oi_chg': 0, 'close': 100.0 + (k >= 20) * 3}})
        store.set_poi(days[-1].isoformat(), {'FII': {'Future Index Long': 70, 'Future Index Short': 30}})
        store.add_events([{'symbol': 'ABC', 'day': '2026-10-05', 'purpose': 'Financial Results'}])
        h = backfill.NseHistory(['ABC'])
        ctx = h.context('ABC', days[-1] + timedelta(days=1))
        self.assertEqual(ctx['deliv_read'], 'accumulation')
        self.assertEqual(ctx['fut_buildup'], 'long buildup')
        self.assertEqual(ctx['days_to_results'], 9)
        self.assertNotIn('sm', ctx)                                      # the live set's flag stays live
        before = h.context('ABC', days[10])                              # as of the day: nothing from later
        self.assertNotEqual(before.get('deliv_read'), 'accumulation')
        self.assertEqual(before.get('fut_buildup'), 'neutral')

    def test_run_fills_what_is_missing_once(self):
        days = [date(2026, 10, 5), date(2026, 10, 6)]
        f = mock.Mock()
        f.bhav.return_value = {'ABC': {'close': 1, 'prev_close': 1, 'qty': 1, 'deliv_qty': 1, 'deliv_pct': 1}, 'ZZZ': {'qty': 1}}
        f.participant_oi.return_value = {'FII': {}}
        f.fo_bhav.return_value = {'ABC': {'oi': 5, 'oi_chg': 1, 'close': 2.0}}
        f.events.return_value = [{'symbol': 'ABC', 'day': '2026-10-20', 'purpose': 'Financial Results'}]
        with mock.patch.object(backfill, 'trading_days', return_value=days), mock.patch.object(backfill.smart, 'Fetcher', return_value=f):
            got = backfill.run(lambda text: None, 1, symbols=['ABC'])
            self.assertEqual((got['delivery'], got['positioning'], got['futures']), (2, 2, 2))
            self.assertEqual(set(store.series('sm_delivery', ['ABC', 'ZZZ'], 'deliv_pct')), {'ABC'})   # only the lab's stocks
            f.reset_mock()
            again = backfill.run(lambda text: None, 1, symbols=['ABC'])
        self.assertEqual((again['delivery'], again['positioning'], again['futures']), (0, 0, 0))
        f.bhav.assert_not_called()
        self.assertEqual(store.results_dates(['ABC']), {'ABC': ['2026-10-20']})

    def test_the_live_prune_leaves_the_lab_its_years(self):
        # The live app's prune once took the backfilled rows too, so each night's backfill fetched and wrote
        # years of delivery files again, holding the database long enough to lock out the app.
        old, older, recent = '2025-01-06', '2023-01-02', store.now_local().date().isoformat()
        store.set_json('backfill:delivery_keep', {'since': '2024-01-01', 'symbols': ['ABC']})
        for d in (old, older, recent):
            store.add_delivery(d, {'ABC': {'qty': 1}, 'ZZZ': {'qty': 1}})
        store.prune_smart()
        with store.connect() as c:
            left = {(r[0], r[1]) for r in c.execute('SELECT day, symbol FROM sm_delivery')}
        self.assertEqual(left, {(old, 'ABC'), (recent, 'ABC'), (recent, 'ZZZ')})
        store.set_json('backfill:delivery_keep', {})
        store.prune_smart()                                              # no lab yet: four months for every stock
        with store.connect() as c:
            self.assertEqual({r[0] for r in c.execute('SELECT DISTINCT day FROM sm_delivery')}, {recent})

    def test_run_tells_the_prune_what_to_keep(self):
        f = mock.Mock(bhav=mock.Mock(return_value=None), participant_oi=mock.Mock(return_value=None),
                      fo_bhav=mock.Mock(return_value=None), events=mock.Mock(return_value=[]))
        with mock.patch.object(backfill, 'trading_days', return_value=[date(2024, 3, 1)]), \
                mock.patch.object(backfill.smart, 'Fetcher', return_value=f):
            backfill.run(lambda text: None, 1, symbols=['XYZ', 'ABC'])
        self.assertEqual(store.get_json('backfill:delivery_keep'),
                         {'since': '2024-03-01', 'symbols': ['ABC', 'XYZ'], 'filled': ['ABC', 'XYZ']})

    def test_a_stock_new_to_the_lab_gets_its_history(self):
        days = [date(2026, 10, 5), date(2026, 10, 6)]
        f = mock.Mock()
        f.bhav.return_value = {'ABC': {'qty': 1}, 'NEW': {'qty': 1}}
        f.participant_oi.return_value = {'FII': {}}
        f.fo_bhav.return_value = {'ABC': {'oi': 5, 'oi_chg': 1, 'close': 2.0}}
        f.events.return_value = []
        with mock.patch.object(backfill, 'trading_days', return_value=days), mock.patch.object(backfill.smart, 'Fetcher', return_value=f):
            backfill.run(lambda text: None, 1, symbols=['ABC'])
            f.reset_mock()
            backfill.run(lambda text: None, 1, symbols=['ABC', 'NEW'], stop=lambda: True)   # stopped: not filled yet
            self.assertEqual(store.get_json('backfill:delivery_keep')['filled'], ['ABC'])
            got = backfill.run(lambda text: None, 1, symbols=['ABC', 'NEW'])
            self.assertEqual(got['delivery'], 2)
            self.assertEqual(set(store.series('sm_delivery', ['ABC', 'NEW'], 'qty')['NEW'][i]['day'] for i in (0, 1)),
                             {d.isoformat() for d in days})
            f.reset_mock()
            self.assertEqual(backfill.run(lambda text: None, 1, symbols=['ABC', 'NEW'])['delivery'], 0)
        f.bhav.assert_not_called()


class BusyDatabase(unittest.TestCase):
    """A lab job's bookkeeping with the database locked by another process's long write."""

    def setUp(self):
        from niftywhale import lab
        self.lab = lab
        store.init()
        with store.connect() as c:
            c.execute('DELETE FROM lab_jobs')
        self.addCleanup(lambda: store.connect().execute('DELETE FROM lab_jobs').connection.commit())
        self.jid = store.add_job('backfill')
        store.next_job()

    def test_progress_while_locked_does_not_fail_the_job(self):
        log = self.lab.JobLog({'id': self.jid, 'kind': 'backfill'})
        with mock.patch.object(self.lab.store, 'update_job', side_effect=self.lab.sqlite3.OperationalError('database is locked')):
            log('NSE files 1/2 (2026-10-05)')
            log.note('a note')
            log.close(error=True)

    def test_the_outcome_is_written_once_the_lock_clears(self):
        real, calls = store.update_job, []

        def flaky(jid, **fields):
            calls.append(fields)
            if len(calls) < 3:
                raise self.lab.sqlite3.OperationalError('database is locked')
            real(jid, **fields)
        with mock.patch.object(self.lab.store, 'update_job', side_effect=flaky), mock.patch.object(self.lab.time, 'sleep'):
            self.lab.finish_job({'id': self.jid, 'kind': 'backfill'}, status='error', message='x')
        job = next(j for j in store.jobs() if j['id'] == self.jid)
        self.assertEqual((job['status'], job['message']), ('error', 'x'))
        self.assertNotEqual(store.add_job('backfill'), self.jid)        # a finished job no longer blocks the next


class Gates(unittest.TestCase):
    def test_futures_results_and_delivery(self):
        r = {**smc.Rules().as_dict(), 'oi_align': 1}
        self.assertIn('short buildup', smc.context_gate({'fut_buildup': 'short buildup'}, 'long', r))
        self.assertIsNone(smc.context_gate({'fut_buildup': 'neutral'}, 'long', r))
        self.assertIn('with the trade', smc.context_gate({'fut_buildup': 'neutral'}, 'long', {**r, 'oi_align': 2}))
        self.assertIsNone(smc.context_gate({'fut_buildup': 'short buildup'}, 'short', {**r, 'oi_align': 2}))
        r = {**smc.Rules().as_dict(), 'results_gap': 7}
        self.assertIn('results due in 3', smc.context_gate({'days_to_results': 3}, 'long', r))
        self.assertIsNone(smc.context_gate({'days_to_results': 20}, 'long', r))
        r = {**smc.Rules().as_dict(), 'deliv_align': 1}
        self.assertIn('distribution', smc.context_gate({'deliv_read': 'distribution'}, 'long', r))
        self.assertIsNone(smc.context_gate({'deliv_read': 'distribution'}, 'short', r))
        self.assertIsNone(smc.context_gate({'fut_buildup': 'short buildup', 'days_to_results': 1}, 'long', smc.Rules().as_dict()))


if __name__ == '__main__':
    unittest.main()
