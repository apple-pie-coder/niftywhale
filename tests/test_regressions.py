"""
Regression tests for bugs found in the October 2026 bug hunts.

Run:  python -m unittest discover -s tests
"""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import numpy as np
import pandas as pd

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

# Never touch the live database: the store reads DB_PATH once, at its first
# import, so whichever test module imports it first decides. Refuse to run
# unless that is a temporary file.
from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import requests  # noqa: E402

import app  # noqa: E402
from niftywhale import data, dhan, intraday, notify, smc, store  # noqa: E402
from niftywhale.intraday import IntradayRules  # noqa: E402

from tests.test_intraday import AFTER, m5  # noqa: E402
from tests.test_smc import BULLISH, CHOCH, daily_frame, m15  # noqa: E402


def _well_formed(html):
    """Telegram's HTML mode refuses a message with a bare & or an unknown tag."""
    import re
    return not re.search(r'&(?!amp;|lt;|gt;|quot;)', html)


class TelegramEscaping(unittest.TestCase):
    def test_summary_with_an_ampersand_universe_and_symbol(self):
        rows = [{'symbol': 'M&M', 'name': 'Mahindra', 'analysis': {
            'in_zone': True, 'side': 'long', 'zone': {'low': 1, 'high': 2}, 'distance_pct': 0}}]
        msg = app.scan_summary(rows, 'both')                 # "Nifty 100 + F&O"
        self.assertTrue(_well_formed(msg), msg)
        self.assertIn('M&amp;M', msg)

    def test_short_entry_alert(self):
        z = {'symbol': 'M&M', 'zone_low': 100, 'zone_high': 105}
        t = {'side': 'short', 'entry': 100.0, 'stop': 103.0, 'target': 90.0, 'rr': 3.3,
             'choch_level': 101.0, 'choch_time': '2026-10-05T11:00:00+05:30',
             'swept_level': 104.0, 'sweep_low': 104.5}
        self.assertTrue(_well_formed(app.entry_message(z, t)))
        z.update(meta={'target_label': "Today's low"})
        self.assertTrue(_well_formed(app.intraday_entry_message(z, t)))


class DhanBackoff(unittest.TestCase):
    def setUp(self):
        store.init()
        dhan.save_token('x.eyJleHAiOjk5OTk5OTk5OTl9.y', '123')
        dhan._state.update(profile=None, profile_at=0.0, next_renew=0.0, error=None)

    def tearDown(self):
        dhan.forget_token()

    def test_a_failed_profile_check_is_not_repeated_on_every_call(self):
        with mock.patch.object(dhan.requests, 'get', side_effect=requests.ConnectionError('down')) as get:
            for _ in range(20):
                dhan.available()
            self.assertEqual(get.call_count, 1)

    def test_a_rejected_token_is_not_rechecked_on_every_call(self):
        resp = mock.Mock(status_code=401)
        with mock.patch.object(dhan.requests, 'get', return_value=resp) as get:
            for _ in range(20):
                self.assertFalse(dhan.available())
            self.assertEqual(get.call_count, 1)

    def test_a_refused_renewal_backs_off(self):
        resp = mock.Mock(ok=False, status_code=400, content=b'{}', json=lambda: {})
        with mock.patch.object(dhan.requests, 'get', return_value=resp) as get:
            for _ in range(10):
                dhan.renew_token()
            self.assertEqual(get.call_count, 1)
        dhan._state['next_renew'] = time.time() - 1
        with mock.patch.object(dhan.requests, 'get', return_value=resp) as get:
            dhan.renew_token()
            self.assertEqual(get.call_count, 1)


class Dismiss(unittest.TestCase):
    def test_unknown_or_wrong_mode_zone(self):
        store.init()
        client = app.app.test_client()
        self.assertEqual(client.post('/api/zones/987654/dismiss').status_code, 404)
        z = store.watch_manual('TCS', 'TCS', 100, 110, 130)
        self.assertEqual(client.post(f"/api/intraday/zones/{z['id']}/dismiss").status_code, 404)
        self.assertEqual(client.post(f"/api/zones/{z['id']}/dismiss").status_code, 200)
        self.assertEqual(client.post(f"/api/zones/{z['id']}/dismiss").status_code, 409)



# ---------------------------------------------------------------------------
# Second hunt: zone lifecycle, stale data, races and secrets
# ---------------------------------------------------------------------------
IST_NOW = datetime(2026, 10, 5, 15, 29, tzinfo=data.IST)      # Monday, after the CHOCH fixture's session
FRESH_15M = datetime(2026, 10, 5, 12, 16, 30, tzinfo=data.IST)  # just after the 15m CHoCH candle closed
FRESH_5M = datetime(2026, 10, 5, 11, 1, 30, tzinfo=data.IST)    # just after the 5m CHoCH candle closed


def _reset():
    """A clean store (the Dhan token and secrets aside)."""
    store.init()
    with store.connect() as c:
        for t in ('zones', 'alerts', 'scans', 'candidates', 'results', 'signals'):
            c.execute(f'DELETE FROM {t}')
        c.execute("DELETE FROM settings WHERE key IN (%s)" % ','.join('?' * len(store.SETTING_DEFAULTS)),
                  list(store.SETTING_DEFAULTS))


def _zone(symbol='ABC', status='watching', created_at='2026-10-04T16:15:04', side='long', meta=None,
          mode='swing', zl=122.0, zh=125.0, target=151.0, note=None, source='scan', last_checked=None):
    with store.connect() as c:
        return c.execute(
            'INSERT INTO zones (symbol, name, zone_low, zone_high, target, created_at, expires_at, status, side, '
            'meta, mode, note, source, last_checked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (symbol, symbol, zl, zh, target, created_at, '2099-01-01T00:00:00', status, side, meta, mode, note,
             source, last_checked)).lastrowid


def _analysis(ob_date='2026-09-01', zone=(122.0, 125.0), side='long', target=151.0):
    return {'side': side, 'zone': {'low': zone[0], 'high': zone[1]},
            'plan': {'target': target, 'stop': 118.0, 'rr': 2.0},
            'order_block': {'date': ob_date, 'low': zone[0], 'high': zone[1]}, 'bos_date': '2026-09-10'}


def _row(symbol='ABC', **kw):
    return {'symbol': symbol, 'name': symbol, 'analysis': _analysis(**kw)}


class SwingWatcherOnlyCountsActionAfterTheZoneWasSet(unittest.TestCase):
    """A scan at 13:48 armed OLAELEC, and the watcher reported the 09:30 CHoCH
    as a fresh entry at 13:50 (and would have again the next morning)."""
    def setUp(self):
        _reset()

    def check(self, zones=None, now=FRESH_15M):
        frame = m15(CHOCH)              # 2026-10-05: tap 10:00, sweep, CHoCH candle 12:00-12:15
        patches = [mock.patch.object(app.data, 'intraday', return_value={'ABC.NS': frame}),
                   mock.patch.object(app.data, 'now_ist', return_value=now),
                   mock.patch.object(app.notify, 'send', return_value=True)]
        if zones is not None:
            patches.append(mock.patch.object(app.store, 'open_zones', return_value=zones))
        with patches[0], patches[1], patches[2] as send, (patches[3] if zones is not None else mock.MagicMock()):
            return app.check_zones('manual'), send

    def test_a_choch_from_before_the_zone_is_not_an_entry(self):
        zid = _zone(created_at='2026-10-05T13:48:42')
        summary, send = self.check()
        self.assertEqual(summary['triggered'], 0)
        self.assertEqual(store.get_zone(zid)['status'], 'watching')
        self.assertEqual(store.alerts(), [])
        send.assert_not_called()

    def test_a_choch_after_the_zone_was_set_is_an_entry(self):
        zid = _zone(created_at='2026-10-04T16:15:04')
        summary, send = self.check()
        self.assertEqual(summary['triggered'], 1)
        self.assertEqual(store.get_zone(zid)['status'], 'triggered')
        self.assertEqual([a['kind'] for a in store.alerts()], ['entry'])
        send.assert_called_once()

    def test_the_candle_the_zone_was_set_in_counts(self):
        _zone(created_at='2026-10-05T10:07:00')          # inside the 10:00 tap candle
        summary, _ = self.check()
        self.assertEqual(summary['triggered'], 1)

    def test_a_choch_found_late_is_recorded_not_sent(self):
        # The watcher was off at 12:15; at 15:29 that CHoCH is history, not an entry.
        zid = _zone()
        summary, send = self.check(now=IST_NOW)
        self.assertEqual((summary['triggered'], summary['rejected']), (0, 1))
        self.assertEqual(store.get_zone(zid)['status'], 'rejected')
        alert = store.alerts()[0]
        self.assertEqual(alert['kind'], 'rejected')
        self.assertIn('too late to enter', alert['message'])
        send.assert_not_called()

    def test_a_zone_dismissed_mid_check_gets_no_alert(self):
        zid = _zone()
        read = store.open_zones()                       # the check read it ...
        store.update_zone(zid, status='dismissed', note='dismissed by you')   # ... then you dismissed it
        summary, send = self.check(zones=read)
        self.assertEqual(store.get_zone(zid)['status'], 'dismissed')
        self.assertEqual(store.alerts(), [])
        send.assert_not_called()

    def test_an_older_choch_is_dated_in_the_alert(self):
        z = {'symbol': 'ABC', 'zone_low': 122, 'zone_high': 125}
        t = {'side': 'long', 'entry': 126.5, 'stop': 122.7, 'target': 151.0, 'rr': 6.4, 'choch_level': 126.0,
             'choch_time': '2026-10-05T12:00:00+05:30', 'swept_level': 123.5, 'sweep_low': 122.8}
        self.assertIn('at 12:00 IST,', app.entry_message(z, t, IST_NOW))
        self.assertIn('at 12:00 IST on 05 Oct,', app.entry_message(z, t, IST_NOW + timedelta(days=1)))

    def test_new_manual_levels_restart_the_watch(self):
        zid = _zone(symbol='TCS', status='tapped', created_at='2026-10-01T16:15:00')
        z = store.watch_manual('TCS', 'TCS', 100, 110, 130)
        self.assertEqual((z['id'], z['status']), (zid, 'watching'))
        self.assertGreater(z['created_at'], '2026-10-01T16:15:00')


class SpentSetupsAreNotArmedAgain(unittest.TestCase):
    """Step 12: a setup that failed 1:3 is deleted from the watchlist; and a
    dismissed, triggered or stale one came back with the very next scan."""
    def setUp(self):
        _reset()

    def test_triggered_rejected_dismissed_or_stale(self):
        for status, note in (('triggered', None), ('rejected', None), ('dismissed', 'dismissed by you'),
                             ('expired', store.LIFETIME_NOTE)):
            _reset()
            store.sync_zones(1, [_row()])
            store.update_zone(store.open_zones()[0]['id'], status=status, note=note)
            r = store.sync_zones(2, [_row()])
            self.assertEqual((r['added'], r['spent']), (0, 1), status)
            self.assertEqual(store.open_zones(), [], status)
            # Another order block on the same stock is a new setup.
            r = store.sync_zones(3, [_row(ob_date='2026-09-20', zone=(130.0, 133.0))])
            self.assertEqual(r['added'], 1, status)

    def test_a_setup_that_left_the_screen_can_come_back(self):
        store.sync_zones(1, [_row()])
        store.sync_zones(2, [])                          # price left the discount half
        self.assertEqual(store.open_zones(), [])
        self.assertEqual(store.sync_zones(3, [_row()])['added'], 1)

    def test_the_other_side_is_a_new_setup(self):
        store.sync_zones(1, [_row()])
        store.update_zone(store.open_zones()[0]['id'], status='rejected')
        r = store.sync_zones(2, [_row(side='short', zone=(122.0, 125.0), target=100.0)])
        self.assertEqual(r['added'], 1)

    def test_zones_from_before_the_order_block_was_recorded(self):
        _zone(status='triggered', zl=122.0, zh=125.0, meta=None)
        # An FVG filling moved the zone's top, not the block's low: the same setup.
        self.assertEqual(store.sync_zones(2, [_row(zone=(122.0, 124.2))])['added'], 0)
        self.assertEqual(store.sync_zones(3, [_row(ob_date='2026-09-25', zone=(127.0, 129.0))])['added'], 1)

    def test_an_open_zone_is_not_moved_onto_a_spent_setup(self):
        _zone(status='rejected', meta='{"ob_date": "2026-09-20"}')
        zid = _zone(status='watching', meta='{"ob_date": "2026-09-01"}', created_at='2026-10-05T16:15:00')
        r = store.sync_zones(2, [_row(ob_date='2026-09-20')])
        self.assertEqual(r['refreshed'], 0)
        self.assertEqual(store.get_zone(zid)['status'], 'expired')

    def test_stocks_the_scan_could_not_judge_keep_their_zones(self):
        store.sync_zones(1, [_row('AAA'), _row('BBB')])
        store.sync_zones(2, [], no_data={'BBB'})
        self.assertEqual([z['symbol'] for z in store.open_zones()], ['BBB'])


class IntradaySpentSetups(unittest.TestCase):
    """A dismissed intraday zone was re-armed by the next 15m scan."""
    def setUp(self):
        _reset()
        self.session = datetime.now().date().isoformat()

    def row(self, ob='10:15', zone=(122.0, 125.0)):
        r = _row(ob_date=f'{self.session} {ob}', zone=zone)
        r['analysis']['levels'] = {'session': self.session}
        return r

    def zones(self):
        return store.intraday_zones(session=self.session)

    def test_dismissed_rejected_or_failed_setup_stays_off_for_the_day(self):
        for status, note in (('dismissed', None), ('rejected', None),
                             ('expired', 'broke below the 15m leg origin (121.00) — structure failed')):
            _reset()
            store.sync_intraday_zones(1, [self.row()], self.session)
            store.update_zone(self.zones()[0]['id'], status=status, note=note)
            self.assertEqual(store.sync_intraday_zones(2, [self.row()], self.session)['added'], 0, status)
            self.assertEqual(store.sync_intraday_zones(3, [self.row('11:30', (126.0, 127.0))],
                                                       self.session)['added'], 1, status)

    def test_moved_levels_restart_the_watch(self):
        store.sync_intraday_zones(1, [self.row()], self.session)
        store.sync_intraday_zones(2, [self.row()], self.session)              # unchanged: no restart
        self.assertNotIn('since', self.zones()[0]['meta'])
        store.sync_intraday_zones(3, [self.row(zone=(121.0, 125.0))], self.session)
        self.assertIn('since', self.zones()[0]['meta'])

    def test_zone_since_floors_to_the_candle(self):
        tz = 'Asia/Kolkata'
        self.assertEqual(app.zone_since({'created_at': '2026-10-05T10:16:29', 'meta': {}}, 5),
                         pd.Timestamp('2026-10-05 10:15', tz=tz))
        self.assertEqual(app.zone_since({'created_at': '2026-10-05T10:16:29',
                                         'meta': {'since': '2026-10-05T11:31:40'}}, 5),
                         pd.Timestamp('2026-10-05 11:30', tz=tz))
        self.assertEqual(app.zone_since({'created_at': '2026-10-05T13:48:42', 'meta': '{}'}, 15),
                         pd.Timestamp('2026-10-05 13:45', tz=tz))

    def check(self, now, zones=None):
        with mock.patch.object(app.data, 'intraday', return_value={'ABC.NS': m5(CHOCH)}), \
                mock.patch.object(app.data, 'now_ist', return_value=now), \
                mock.patch.object(app.store, 'intraday_zones', side_effect=[zones, []]), \
                mock.patch.object(app.notify, 'send', return_value=True) as send:
            return app.check_intraday('manual'), send

    def test_a_fresh_choch_is_an_entry_and_a_late_one_is_not(self):
        zid = _zone(mode='intraday', created_at='2026-10-05T09:30:00', target=135.0, meta='{"stop": 110}')
        summary, send = self.check(FRESH_5M, store.intraday_zones(store.INTRADAY_OPEN))
        self.assertEqual(summary['triggered'], 1)
        self.assertEqual(store.get_zone(zid)['status'], 'triggered')
        send.assert_called_once()
        _reset()
        zid = _zone(mode='intraday', created_at='2026-10-05T09:30:00', target=135.0, meta='{"stop": 110}')
        late = FRESH_5M + timedelta(minutes=30)
        summary, send = self.check(late, store.intraday_zones(store.INTRADAY_OPEN))
        self.assertEqual((summary['triggered'], summary['rejected']), (0, 1))
        self.assertEqual(store.get_zone(zid)['status'], 'rejected')
        self.assertIn('too late to enter', store.get_zone(zid)['note'])
        send.assert_not_called()

    def test_a_zone_dismissed_mid_check_gets_no_alert(self):
        zid = _zone(mode='intraday', created_at='2026-10-05T09:30:00', target=135.0, meta='{"stop": 110}')
        read = store.intraday_zones(store.INTRADAY_OPEN)
        store.update_zone(zid, status='dismissed')
        with mock.patch.object(app.data, 'intraday', return_value={'ABC.NS': m5(CHOCH)}), \
                mock.patch.object(app.data, 'now_ist', return_value=FRESH_5M), \
                mock.patch.object(app.store, 'intraday_zones', side_effect=[read, []]), \
                mock.patch.object(app.notify, 'send', return_value=True) as send:
            app.check_intraday('manual')
        self.assertEqual(store.get_zone(zid)['status'], 'dismissed')
        self.assertEqual(store.alerts(mode='intraday'), [])
        send.assert_not_called()


class ScanSurvivesOutagesAndErrors(unittest.TestCase):
    """yfinance down at 16:15 gave a 'done' scan of 0 stocks that expired
    every zone; and a database error before the scan's try leaked its lock."""
    STOCKS = [{'symbol': s, 'name': s, 'ticker': s + '.NS', 'fo': True} for s in ('AAA', 'BBB', 'CCC', 'DDD')]

    def setUp(self):
        _reset()

    def scan(self, frames):
        app.SCAN_LOCK.acquire()
        with mock.patch.object(app.universe, 'members', return_value=self.STOCKS), \
                mock.patch.object(app.data, 'daily', return_value=frames):
            app.run_scan('schedule', 'nifty100')
        self.assertFalse(app.SCAN_LOCK.locked())
        return store.latest_scan(finished_only=False)

    def test_no_data_fails_the_scan_and_keeps_the_zones(self):
        store.sync_zones(1, [_row('AAA')])
        scan = self.scan({})
        self.assertEqual(scan['status'], 'error')
        self.assertIn('data source', scan['error'])
        self.assertEqual([z['symbol'] for z in store.open_zones()], ['AAA'])

    def test_a_good_scan_still_syncs(self):
        scan = self.scan({s['ticker']: daily_frame(BULLISH) for s in self.STOCKS})
        self.assertEqual(scan['status'], 'done')
        self.assertEqual(len(store.open_zones()), 4)

    def test_the_lock_is_released_when_the_database_fails(self):
        app.SCAN_LOCK.acquire()
        with mock.patch.object(app.store, 'start_scan', side_effect=sqlite3.OperationalError('database is locked')):
            app.run_scan('manual', 'nifty100')
        self.assertFalse(app.SCAN_LOCK.locked())
        self.assertFalse(app.STATE['scan']['running'])
        with mock.patch.object(app.store, 'start_scan', side_effect=sqlite3.OperationalError('database is locked')):
            app.run_intraday_scan('manual')
        self.assertFalse(app.INTRA_SCAN_LOCK.locked())


class ScheduledScanRetries(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 17, 0, tzinfo=data.IST)          # Monday, after 16:15

    def setUp(self):
        _reset()
        self.local = self.NOW.astimezone().replace(tzinfo=None)

    def due(self, now=None):
        return app.scheduled_scan_due(now or self.NOW, store.settings())

    def add(self, minutes_ago, status):
        with store.connect() as c:
            c.execute("INSERT INTO scans (started_at, universe, origin, status, mode) "
                      "VALUES (?, 'nifty100', 'schedule', ?, 'swing')",
                      ((self.local - timedelta(minutes=minutes_ago)).isoformat(timespec='seconds'), status))

    def test_a_failed_scan_is_retried_after_a_while(self):
        self.assertTrue(self.due())
        self.add(5, 'error')
        self.assertFalse(self.due())                    # too soon
        _reset()
        self.add(20, 'error')
        self.assertTrue(self.due())
        self.add(10, 'done')
        self.assertFalse(self.due())                    # done for today

    def test_a_scan_a_crash_left_running_is_retried(self):
        self.add(30, 'running')
        self.assertTrue(self.due())

    def test_it_gives_up_after_a_few_attempts(self):
        for m in (100, 80, 60, 40):
            self.add(m, 'error')
        self.assertFalse(self.due())

    def test_not_before_the_time_nor_at_weekends(self):
        self.assertFalse(self.due(self.NOW.replace(hour=16, minute=0)))
        self.assertFalse(self.due(self.NOW + timedelta(days=5)))    # Saturday


class DailyCandlesAfterTheClose(unittest.TestCase):
    """The 16:15 scan on 5 Oct took 2 seconds: it screened daily candles cached
    by a 14:35 manual scan, a 14:35 'close', because they were under 3 hours old."""
    @staticmethod
    def at(*a):
        return datetime(*a, tzinfo=data.IST).timestamp()

    def test_a_frame_from_the_session_is_stale_after_the_close(self):
        self.assertFalse(data._fresh(self.at(2026, 10, 5, 14, 35), self.at(2026, 10, 5, 16, 15)))

    def test_a_frame_from_after_the_close_settled_is_fresh_for_a_while(self):
        self.assertTrue(data._fresh(self.at(2026, 10, 5, 15, 50), self.at(2026, 10, 5, 16, 15)))
        self.assertFalse(data._fresh(self.at(2026, 10, 5, 15, 50), self.at(2026, 10, 5, 19, 0)))

    def test_during_the_session_and_while_the_close_settles(self):
        self.assertTrue(data._fresh(self.at(2026, 10, 5, 11, 0), self.at(2026, 10, 5, 11, 9)))
        self.assertFalse(data._fresh(self.at(2026, 10, 5, 11, 0), self.at(2026, 10, 5, 11, 11)))
        self.assertTrue(data._fresh(self.at(2026, 10, 5, 15, 31), self.at(2026, 10, 5, 15, 38)))
        self.assertFalse(data._fresh(self.at(2026, 10, 5, 15, 20), self.at(2026, 10, 5, 15, 40)))

    def test_weekend(self):
        self.assertEqual(data.last_close(datetime(2026, 10, 5, 10, 0, tzinfo=data.IST)),
                         datetime(2026, 10, 2, 15, 30, tzinfo=data.IST))           # Monday morning: Friday
        self.assertTrue(data._fresh(self.at(2026, 10, 3, 9, 0), self.at(2026, 10, 3, 10, 0)))

    def test_a_scan_never_falls_back_to_a_stale_frame(self):
        with data._lock:
            data._cache['AAA.NS'] = (self.at(2026, 10, 5, 14, 35), daily_frame(BULLISH))
        try:
            with mock.patch.object(data.time, 'time', return_value=self.at(2026, 10, 5, 16, 15)), \
                    mock.patch.object(data.yf, 'download', side_effect=OSError('network down')):
                self.assertEqual(data.daily(['AAA.NS'], stale_ok=False), {})
                self.assertIn('AAA.NS', data.daily(['AAA.NS']))          # a chart still gets it
        finally:
            with data._lock:
                data._cache.pop('AAA.NS', None)

    def test_the_evening_scan_downloads_again(self):
        old = daily_frame(BULLISH)
        new = pd.concat({'AAA.NS': daily_frame(BULLISH, volume=3_000_000)}, axis=1)
        with data._lock:
            data._cache['AAA.NS'] = (self.at(2026, 10, 5, 14, 35), old)
        try:
            with mock.patch.object(data.time, 'time', return_value=self.at(2026, 10, 5, 16, 15)), \
                    mock.patch.object(data.yf, 'download', return_value=new) as dl:
                got = data.daily(['AAA.NS'])
            dl.assert_called_once()
            self.assertEqual(float(got['AAA.NS']['Volume'].iloc[-1]), 3_000_000)
        finally:
            with data._lock:
                data._cache.pop('AAA.NS', None)


class RuleInputs(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_non_finite_and_non_dict_overrides_are_ignored(self):
        base = smc.Rules()
        self.assertEqual(base.with_overrides({'swing_len': float('nan'), 'min_rr': float('inf')}), base)
        self.assertEqual(base.with_overrides([1, 2]), base)
        self.assertEqual(base.with_overrides('{"min_rr": 2}'), base)
        self.assertEqual(IntradayRules().with_overrides({'min_rr': 'nan'}), IntradayRules())

    def test_a_saved_nan_cannot_break_the_app(self):
        store.set_settings({'rules': '{"swing_len": NaN, "min_rr": NaN}'})
        self.assertEqual(app.current_rules(), app.ENV_RULES)
        r = app.app.test_client().post('/api/rules', data='{"rules": {"swing_len": NaN}}',
                                       content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['overrides'], {})

    def test_env_values_that_would_break_the_arithmetic(self):
        env = {'SMC_ATR_LEN': '0', 'SMC_SWING_LEN': 'inf', 'SMC_MIN_RR': 'nan', 'SMC_BOS_LOOKBACK': '-5',
               'SMC_OB_SEARCH': '0', 'SMC_MIN_ATR_PCT': '2'}
        with mock.patch.dict(os.environ, env):
            r = smc.Rules.from_env()
        self.assertEqual((r.atr_len, r.swing_len, r.min_rr, r.bos_lookback), (14, 3, 3.0, 120))
        self.assertEqual((r.ob_search, r.min_atr_pct), (0, 2.0))
        with mock.patch.dict(os.environ, {'INTRADAY_ATR_LEN': '0', 'INTRADAY_SESSIONS': '1e400'}):
            ir = IntradayRules.from_env()
        self.assertEqual((ir.atr_len, ir.sessions), (14, 5))

    def test_missing_volume_fails_liquidity(self):
        f = daily_frame(BULLISH)
        f['Volume'] = np.nan
        self.assertEqual(smc.evaluate(f)['failed_at'], 'liquidity')
        self.assertEqual(intraday.daily_filter(f, IntradayRules())['failed_at'], 'liquidity')


class ManualZonesAndInputs(unittest.TestCase):
    """M&M and BAJAJ-AUTO (both Nifty 100) could not be given a zone."""
    def setUp(self):
        _reset()
        self.client = app.app.test_client()
        stocks = [{'symbol': s, 'name': s, 'fo': True} for s in ('M&M', 'BAJAJ-AUTO', 'TCS')]
        self.p = mock.patch.object(app.universe, 'load', return_value={'stocks': stocks})
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def post(self, **body):
        return self.client.post('/api/zones', json=body)

    def test_symbols_with_an_ampersand_or_a_hyphen(self):
        for sym in ('M&M', 'BAJAJ-AUTO'):
            r = self.post(symbol=sym, zone_low=100, zone_high=110, target=130)
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()['symbol'], sym)

    def test_bad_input_is_refused(self):
        self.assertEqual(self.post(symbol='BAD SYM', zone_low=100, zone_high=110, target=130).status_code, 400)
        self.assertEqual(self.post(symbol='TCS', zone_low='nan', zone_high=110, target=130).status_code, 400)
        self.assertEqual(self.post(symbol='TCS', zone_low=100, zone_high='inf', target=90).status_code, 400)
        self.assertEqual(self.client.post('/api/zones', json=[1]).status_code, 400)
        self.assertEqual(self.client.post('/api/settings', json=[1]).status_code, 400)
        self.assertEqual(self.client.post('/api/whatif', json=[1]).status_code, 400)

    def test_search_offers_any_nse_symbol(self):
        out = self.client.get('/api/search?q=J%26KBANK').get_json()
        self.assertEqual(out[-1]['symbol'], 'J&KBANK')

    def test_palette_index(self):
        d = self.client.get('/api/palette').get_json()
        self.assertEqual([i[0] for i in d['indices']][:2], ['NIFTY', 'BANKNIFTY'])
        for row in d['stocks']:                         # symbol, name, tag, F&O, industry, weight
            self.assertEqual(len(row), 6)
            self.assertIn(row[5], (0, 1, 2, 3))


def _cfg(**vals):
    """App settings (config.py) as given, the rest as they are: where the Dhan login and the
    Telegram bot come from now that they can be set on the dashboard."""
    from niftywhale import config
    real = config.get
    return mock.patch.object(config, 'get', side_effect=lambda n: vals[n] if n in vals else real(n))


class SecretsStayOutOfErrors(unittest.TestCase):
    def tearDown(self):
        dhan._state.update(error=None, next_login=0.0, profile=None, profile_at=0.0, rejected=False)

    def test_the_dhan_pin_and_code_are_redacted(self):
        url = '/app/generateAccessToken?dhanClientId=1100012345&pin=482913&totp=123456'
        err = requests.ConnectionError(f"HTTPSConnectionPool(host='auth.dhan.co', port=443): "
                                       f"Max retries exceeded with url: {url} (Caused by x)")
        with _cfg(dhan_pin='482913', dhan_client_id='1100012345', dhan_totp='JBSWY3DPEHPK3PXP'), \
                mock.patch.object(dhan.requests, 'post', side_effect=err):
            dhan._state['next_login'] = 0.0
            self.assertFalse(dhan.generate_token())
        for secret in ('482913', '123456', '1100012345'):
            self.assertNotIn(secret, dhan._state['error'])

    def test_the_telegram_token_is_redacted(self):
        err = requests.ConnectionError('Max retries exceeded with url: /bot123456:SECRETTOKEN/sendMessage')
        with _cfg(telegram_token='123456:SECRETTOKEN', telegram_chat_id='42'), \
                mock.patch.object(notify.requests, 'post', side_effect=err), \
                self.assertLogs('niftywhale.notify', 'WARNING') as logs:
            self.assertFalse(notify.send('hi'))
        self.assertNotIn('SECRETTOKEN', ''.join(logs.output))

    def test_a_rejected_token_stops_a_candle_batch(self):
        with mock.patch.object(dhan, 'security_id', return_value='1333'), mock.patch.object(dhan, '_chart_slot'), \
                mock.patch.object(dhan.requests, 'post', return_value=mock.Mock(status_code=401)) as post:
            self.assertEqual(dhan.intraday(['AAA.NS', 'BBB.NS', 'CCC.NS']), {})
        self.assertEqual(post.call_count, 1)
        self.assertIn('rejected', dhan._state['error'])

    def test_auto_login_replaces_a_revoked_token(self):
        store.init()
        dhan.save_token('x.eyJleHAiOjk5OTk5OTk5OTl9.y', '1100012345')        # expires far in the future
        ok = mock.Mock(ok=True, content=b'{}', json=lambda: {'accessToken': 'x.eyJleHAiOjk5OTk5OTk5OTh9.z'})
        try:
            with _cfg(dhan_client_id='1100012345', dhan_pin='482913', dhan_totp='JBSWY3DPEHPK3PXP'), \
                    mock.patch.object(dhan.requests, 'post', return_value=ok) as post:
                dhan.keep_alive()
                post.assert_not_called()                     # valid and far from expiry: nothing to do
                dhan._state['rejected'] = True               # then Dhan answered 401
                dhan.keep_alive()
                post.assert_called_once()
            self.assertEqual(dhan.token(), 'x.eyJleHAiOjk5OTk5OTk5OTh9.z')
            self.assertFalse(dhan._state['rejected'])
        finally:
            dhan.forget_token()
            store.set_kv(dhan.KV_CLIENT, '')

    def test_a_rejected_paste_keeps_the_working_token_and_client_id(self):
        store.init()
        dhan.save_token('x.eyJleHAiOjk5OTk5OTk5OTl9.y', '1100012345')
        try:
            with mock.patch.object(dhan.requests, 'get', return_value=mock.Mock(status_code=401)):
                r = app.app.test_client().post('/api/dhan/token', json={'access_token': 'y' * 40, 'client_id': '999'})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(store.get_kv(dhan.KV_TOKEN), 'x.eyJleHAiOjk5OTk5OTk5OTl9.y')
            self.assertEqual(store.get_kv(dhan.KV_CLIENT), '1100012345')
        finally:
            dhan.forget_token()
            store.set_kv(dhan.KV_CLIENT, '')


class SquareOffWaitsForItsCandle(unittest.TestCase):
    """At 15:21 with the 15:15 candle not yet delivered, a trade was squared off
    at the 15:05 candle's close for good."""
    def setUp(self):
        self.t = intraday.trigger(m5(CHOCH), 122, 125, 135, IntradayRules(), AFTER)

    def test_before_the_close_it_stays_open(self):
        f = m5(CHOCH + [(127, 127.5, 126.6, 127.2)] * 49)               # last candle 15:05
        at = pd.Timestamp('2026-10-05 15:21', tz='Asia/Kolkata')
        self.assertEqual(intraday.outcome(f, self.t, IntradayRules(), at)['status'], 'open')
        o = intraday.outcome(f, self.t, IntradayRules(), pd.Timestamp('2026-10-05 15:31', tz='Asia/Kolkata'))
        self.assertEqual(o['status'], 'closed')

    def test_the_square_off_candle_when_it_is_there(self):
        f = m5(CHOCH + [(127, 127.5, 126.6, 127.2)] * 51)               # last candle 15:15
        o = intraday.outcome(f, self.t, IntradayRules(), pd.Timestamp('2026-10-05 15:21', tz='Asia/Kolkata'))
        self.assertEqual((o['status'], o['exit_time'][11:16]), ('closed', '15:15'))


class LiveBoardAndCharts(unittest.TestCase):
    """Swing zones stay 'triggered' for good, so the board kept every one ever."""
    def setUp(self):
        _reset()

    def test_old_triggers_leave_the_board(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=data.IST)
        self.assertFalse(app._live_zone({'status': 'triggered', 'last_checked': '2026-10-01T10:00:00+05:30'}, now))
        self.assertTrue(app._live_zone({'status': 'triggered', 'last_checked': '2026-10-15T10:00:00+05:30'}, now))
        self.assertTrue(app._live_zone({'status': 'watching'}, now))
        self.assertFalse(app._live_zone({'status': 'rejected', 'last_checked': '2026-10-19T10:00:00'}, now))

    def test_a_watched_zone_beats_an_older_trigger(self):
        recent = (datetime.now(data.IST) - timedelta(days=1)).isoformat(timespec='seconds')
        _zone(status='triggered', last_checked=recent)
        zid = _zone(status='watching', created_at=datetime.now().isoformat(timespec='seconds'))
        self.assertEqual(app._open_zone('ABC')['id'], zid)


class IntradayScanDoesNotHoldUpTheCheck(unittest.TestCase):
    """At every quarter hour the 5m check waited ~90 s for the 15m scan of
    Nifty 500: SWANCORP's 12:45 entry went out at 12:46:40."""
    def test_the_scheduled_scan_runs_in_the_background(self):
        started, release = threading.Event(), threading.Event()

        def slow(origin):
            started.set()
            release.wait(5)
        with mock.patch.object(app, 'run_intraday_scan', side_effect=slow):
            t0 = time.time()
            app.start_intraday_scan_thread('schedule')
            self.assertLess(time.time() - t0, 1.0)
            self.assertTrue(started.wait(2))
            release.set()


if __name__ == '__main__':
    unittest.main()
