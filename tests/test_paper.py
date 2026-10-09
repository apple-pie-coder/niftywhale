"""
Swing paper trades: every swing entry alert followed on 15m candles (daily
candles after a data gap) to its target or its stop.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import numpy as np
import pandas as pd

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

# Never touch the live database (see test_regressions.py).
from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import data, smc, store  # noqa: E402

from tests.test_regressions import FRESH_15M, _reset, _row, _zone  # noqa: E402
from tests.test_smc import CHOCH, m15  # noqa: E402

TZ = 'Asia/Kolkata'
# The CHOCH fixture's trade: in at 126.5 when the 12:00 candle closes, stop 0.1% under the 122.8 sweep.
LONG = {'side': 'long', 'entry': 126.5, 'stop': 122.68, 'target': 135.0,
        'choch_time': '2026-10-05T12:00:00+05:30'}
SHORT = {'side': 'short', 'entry': 100.0, 'stop': 103.0, 'target': 90.0,
         'choch_time': '2026-10-05T12:00:00+05:30'}
LATE = pd.Timestamp('2026-10-05 15:29', tz=TZ)


def bars(rows, start='2026-10-05 12:15'):
    """15m candles (open, high, low, close) from `start`."""
    idx = pd.date_range(start, periods=len(rows), freq='15min', tz=TZ)
    o, h, l, c = zip(*rows)
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': [1e5] * len(rows)}, index=idx)


def days(rows):
    """Daily candles {date: (open, high, low, close)}, indexed like yfinance (naive dates)."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in rows])
    o, h, l, c = zip(*rows.values())
    return pd.DataFrame({'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': [1e6] * len(rows)}, index=idx)


QUIET = (126.8, 127.4, 126.3, 127.0)        # touches neither 122.68 nor 135


class FollowTrade(unittest.TestCase):
    def test_target(self):
        o = smc.follow_trade(LONG, bars([QUIET, (127.0, 135.4, 126.9, 135.1)]), None, LATE)
        self.assertEqual((o['status'], o['exit']), ('won', 135.0))
        self.assertAlmostEqual(o['r'], 8.5 / 3.82, places=6)
        self.assertEqual(o['exit_time'][11:16], '12:30')

    def test_stop(self):
        o = smc.follow_trade(LONG, bars([QUIET, (127.0, 127.1, 122.5, 122.9)]), None, LATE)
        self.assertEqual((o['status'], o['exit']), ('lost', 122.68))
        self.assertAlmostEqual(o['r'], -1.0)

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        overnight = bars([(121.0, 121.5, 119.8, 120.4)], start='2026-10-06 09:15')
        o = smc.follow_trade(LONG, pd.concat([bars([QUIET]), overnight]), None,
                             pd.Timestamp('2026-10-06 10:00', tz=TZ))
        self.assertEqual((o['status'], o['exit']), ('lost', 121.0))
        self.assertLess(o['r'], -1.0)

    def test_both_levels_in_one_candle_counts_as_stopped(self):
        o = smc.follow_trade(LONG, bars([(126.8, 136.0, 122.0, 130.0)]), None, LATE)
        self.assertEqual(o['status'], 'lost')

    def test_open_keeps_a_cursor_and_examines_each_candle_once(self):
        f = bars([QUIET, QUIET])
        o = smc.follow_trade(LONG, f, None, LATE)
        self.assertEqual(o['status'], 'open')
        self.assertEqual(o['followed_to'][11:16], '12:45')       # the end of the last candle examined
        self.assertAlmostEqual(o['r'], (127.0 - 126.5) / 3.82)
        t = {**LONG, 'followed_to': o['followed_to']}
        # A candle already examined that "touches" the target cannot close it now ...
        rigged = f.copy()
        rigged.iloc[0, rigged.columns.get_loc('High')] = 140.0
        self.assertEqual(smc.follow_trade(t, rigged, None, LATE)['status'], 'open')
        # ... a new one can.
        o = smc.follow_trade(t, pd.concat([f, bars([(127, 135.2, 126.9, 135)], start='2026-10-05 12:45')]),
                             None, LATE)
        self.assertEqual(o['status'], 'won')

    def test_a_candle_still_forming_is_not_used(self):
        f = bars([QUIET, (127.0, 135.4, 126.9, 135.1)])
        mid = pd.Timestamp('2026-10-05 12:40', tz=TZ)            # the 12:30 candle closes at 12:45
        self.assertEqual(smc.follow_trade(LONG, f, None, mid)['status'], 'open')

    def test_short(self):
        up = bars([(100.2, 100.8, 99.6, 100.1), (100.1, 103.4, 100.0, 103.2)])
        self.assertEqual(smc.follow_trade(SHORT, up, None, LATE)['status'], 'lost')
        down = bars([(100.2, 100.8, 99.6, 100.1), (99.0, 99.2, 89.6, 89.9)])
        o = smc.follow_trade(SHORT, down, None, LATE)
        self.assertEqual((o['status'], o['exit']), ('won', 90.0))
        self.assertAlmostEqual(o['r'], 10 / 3)
        gap = pd.concat([bars([(100.2, 100.8, 99.6, 100.1)]),
                         bars([(104.0, 104.5, 103.6, 104.2)], start='2026-10-06 09:15')])
        o = smc.follow_trade(SHORT, gap, None, pd.Timestamp('2026-10-06 10:00', tz=TZ))
        self.assertEqual((o['status'], o['exit']), ('lost', 104.0))  # gapped up through the stop

    def test_no_15m_candles_waits(self):
        o = smc.follow_trade(LONG, None, None, LATE)
        self.assertEqual(o['status'], 'open')
        self.assertTrue(o['wait'])


class DataGaps(unittest.TestCase):
    """The app was off longer than the 15m candles reach back."""
    NOW = pd.Timestamp('2026-10-12 10:00', tz=TZ)
    LATER = bars([QUIET] * 2, start='2026-10-12 09:15')       # the 15m candles start on Monday 12 Oct

    def trade(self, followed_to):
        return {**LONG, 'followed_to': followed_to}

    def test_daily_candles_fill_the_gap(self):
        t = self.trade('2026-10-06T15:30:00+05:30')
        o = smc.follow_trade(t, self.LATER, None, self.NOW)
        self.assertTrue(o['need_daily'])
        daily = days({'2026-10-07': (127, 128, 126, 127.5), '2026-10-08': (127.5, 128, 122.0, 123),
                      '2026-10-09': (123, 136, 122.9, 135)})
        o = smc.follow_trade(t, self.LATER, daily, self.NOW)
        self.assertEqual((o['status'], o['exit'], o['source']), ('lost', 122.68, 'daily'))
        self.assertTrue(o['approx'])
        self.assertEqual(o['exit_time'][:10], '2026-10-08')

    def test_a_gap_that_missed_nothing(self):
        daily = days({'2026-10-07': (127, 128, 126, 127.5), '2026-10-08': (127.5, 129, 126.5, 128)})
        o = smc.follow_trade(self.trade('2026-10-06T15:30:00+05:30'), self.LATER, daily, self.NOW)
        self.assertEqual(o['status'], 'open')
        self.assertTrue(o['followed_to'].startswith('2026-10-12T09:45'))

    def test_the_entry_days_candle_is_never_used(self):
        # Before the 12:00 entry that day traded down to 120: no stop, it was not in the trade yet.
        daily = days({'2026-10-05': (124, 128, 120.0, 127), '2026-10-06': (127, 128, 126, 127.5)})
        o = smc.follow_trade(self.trade('2026-10-05T12:15:00+05:30'), self.LATER, daily, self.NOW)
        self.assertEqual(o['status'], 'open')
        self.assertTrue(o['approx'])                              # the rest of 5 Oct went unexamined

    def test_an_overnight_or_weekend_gap_is_not_a_gap(self):
        # Friday's close to Monday's open: nothing to fill, nothing to download.
        o = smc.follow_trade(self.trade('2026-10-09T15:30:00+05:30'), self.LATER, None, self.NOW)
        self.assertNotIn('need_daily', o)
        o = smc.follow_trade(self.trade('2026-10-09T15:30:00+05:30'), self.LATER,
                             days({'2026-10-09': (127, 128, 126, 127.5)}), self.NOW)
        self.assertEqual(o['status'], 'open')


class SwingWatcherTrades(unittest.TestCase):
    def setUp(self):
        _reset()

    def check(self, frame, now):
        with mock.patch.object(app.data, 'intraday', return_value={'ABC.NS': frame}), \
                mock.patch.object(app.data, 'now_ist', return_value=now), \
                mock.patch.object(app.notify, 'send', return_value=True) as send:
            return app.check_zones('manual'), send

    def test_an_entry_alert_becomes_a_paper_trade_that_closes_on_target(self):
        zid = _zone(target=151.0)
        summary, _ = self.check(m15(CHOCH), FRESH_15M)
        self.assertEqual(summary['triggered'], 1)
        z = store.get_zone(zid)
        self.assertEqual(z['status'], 'triggered')
        t = store.swing_trades()[0]['trigger']
        self.assertTrue(t['paper'])
        self.assertEqual(t['followed_to'][11:16], '12:15')
        self.assertIn('open trade', z['note'])
        # Two candles later the target trades.
        more = m15(CHOCH + [(126.8, 130.0, 126.5, 129.0), (129.0, 152.0, 128.8, 151.0)])
        summary, send = self.check(more, datetime(2026, 10, 5, 13, 1, 30, tzinfo=data.IST))
        self.assertEqual(summary['closed'], 1)
        z = store.get_zone(zid)
        self.assertEqual(z['status'], 'won')
        self.assertIn('Target hit at 151.00', z['note'])
        self.assertEqual([a['kind'] for a in store.alerts()], ['won', 'entry'])
        msg = send.call_args[0][0]
        self.assertIn('Swing long · ABC · Target hit', msg)
        st = app.swing_trades_state()['stats']
        self.assertEqual((st['open'], st['trades'], st['wins']), (0, 1, 1))
        self.assertGreater(st['r'], 6)
        # Settled once: a later check sends nothing more.
        summary, send = self.check(more, datetime(2026, 10, 5, 13, 16, 30, tzinfo=data.IST))
        send.assert_not_called()
        self.assertEqual(len(store.alerts()), 2)

    def test_a_trigger_from_before_paper_trading_is_not_followed(self):
        zid = _zone(status='triggered', last_checked='2026-10-05T12:16:30+05:30')
        with store.connect() as c:
            c.execute('UPDATE zones SET trigger = ? WHERE id = ?',
                      ('{"entry": 126.5, "stop": 122.68, "target": 151, "choch_time": "2026-10-05T12:00:00+05:30"}',
                       zid))
        self.assertEqual(store.swing_trades(), [])
        self.check(m15(CHOCH + [(126.8, 152.0, 126.5, 151.0)]), datetime(2026, 10, 5, 12, 46, 30, tzinfo=data.IST))
        self.assertEqual(store.get_zone(zid)['status'], 'triggered')
        self.assertFalse(app._live_zone(store.recent_zones()[0], datetime(2026, 11, 1, tzinfo=data.IST)))

    def test_an_open_trade_stays_on_the_board_however_old(self):
        z = {'status': 'triggered', 'last_checked': '2026-09-01T12:00:00+05:30', 'trigger': {'paper': True}}
        self.assertTrue(app._live_zone(z, datetime(2026, 11, 1, tzinfo=data.IST)))
        self.assertFalse(app._live_zone({**z, 'status': 'won'}, datetime(2026, 11, 1, tzinfo=data.IST)))


class Journal(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_newest_entry_first_whatever_order_the_zones_were_set_in(self):
        early = _zone(symbol='AAA', status='won')          # set first, triggered last
        late = _zone(symbol='BBB', status='lost')
        with store.connect() as c:
            for zid, when, r in ((early, '2026-10-05T11:00:00+05:30', 3.1), (late, '2026-10-01T10:00:00+05:30', -1)):
                c.execute('UPDATE zones SET trigger = ? WHERE id = ?',
                          (f'{{"paper": true, "choch_time": "{when}", "r": {r}}}', zid))
        st = app.swing_trades_state()
        self.assertEqual([z['symbol'] for z in st['journal']], ['AAA', 'BBB'])
        self.assertEqual((st['stats']['trades'], st['stats']['wins'], st['stats']['r']), (2, 1, 2.1))


class OneTradePerStock(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_new_zone_while_a_trade_is_open_and_never_the_same_setup_again(self):
        store.sync_zones(1, [_row()])
        zid = store.open_zones()[0]['id']
        store.update_zone(zid, status='triggered', trigger={'paper': True, 'entry': 126.5, 'stop': 122.68,
                                                            'target': 151, 'choch_time': '2026-10-05T12:00:00'})
        new_setup = _row(ob_date='2026-09-25', zone=(127.0, 129.0))
        r = store.sync_zones(2, [new_setup])
        self.assertEqual((r['added'], r['spent']), (0, 1))          # the trade is still open
        store.update_zone(zid, status='won')
        self.assertEqual(store.sync_zones(3, [_row()])['added'], 0)  # its own setup: played out
        self.assertEqual(store.sync_zones(4, [new_setup])['added'], 1)


if __name__ == '__main__':
    unittest.main()
