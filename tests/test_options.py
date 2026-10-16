"""
Options mode: option-chain metrics on hand-built chains whose answers are
worked out by hand, the idea rules, paper-trade following, and the whole
fetch -> store -> idea -> result path with Dhan and Telegram mocked.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import data, options, store  # noqa: E402

IST = data.IST


def opt(oi, poi=None, ltp=10.0, pc=None, vol=1000, pvol=1000, iv=15.0, delta=0.5, bid=None, ask=None):
    """One side of a strike in Dhan's shape."""
    return {'oi': oi, 'previous_oi': oi if poi is None else poi, 'last_price': ltp,
            'previous_close_price': ltp if pc is None else pc, 'volume': vol, 'previous_volume': pvol,
            'implied_volatility': iv, 'greeks': {'delta': delta},
            'top_bid_price': ltp - 0.1 if bid is None else bid, 'top_ask_price': ltp + 0.1 if ask is None else ask}


def raw(spot, rows):
    """rows: {strike: (ce kwargs, pe kwargs)} -> Dhan's {last_price, oc}."""
    return {'last_price': spot, 'oc': {f'{k:.6f}': {'ce': opt(**ce) if ce else None, 'pe': opt(**pe) if pe else None}
                                       for k, (ce, pe) in rows.items()}}


# A small chain around 100: call OI piles up at 110, put OI at 90.
BASE = {
    80: ({'oi': 100}, {'oi': 500}),
    90: ({'oi': 200}, {'oi': 3000}),
    100: ({'oi': 1000, 'iv': 14.0, 'delta': 0.5}, {'oi': 1200, 'iv': 16.0, 'delta': -0.5}),
    110: ({'oi': 4000, 'iv': 12.0, 'delta': 0.24}, {'oi': 300, 'iv': 20.0, 'delta': -0.75}),
    120: ({'oi': 800, 'iv': 13.0, 'delta': 0.1}, {'oi': 50}),
}


def chain(spot=100.0, rows=BASE, window_pct=25.0, **kw):
    """The fixture's strikes run 80-120, so a wider window than the 8 % used for indices."""
    return options.parse_chain(raw(spot, rows), window_pct=window_pct, **kw)


class Parsing(unittest.TestCase):
    def test_sorted_and_trimmed_around_spot(self):
        c = chain(rows={**BASE, 200: ({'oi': 5}, {'oi': 5})})
        self.assertEqual([r['k'] for r in c['strikes']], [80, 90, 100, 110, 120])      # 200 is 100 % away
        self.assertEqual(c['spot'], 100.0)
        self.assertEqual(c['strikes'][2]['ce']['iv'], 14.0)

    def test_max_strikes_keeps_the_nearest(self):
        c = chain(window_pct=50, max_strikes=3)
        self.assertEqual([r['k'] for r in c['strikes']], [90, 100, 110])

    def test_compact_round_trip(self):
        c = chain()
        self.assertEqual(options.expand(options.compact(c)), c)

    def test_junk_strikes_and_missing_sides(self):
        c = options.parse_chain({'last_price': 100, 'oc': {'abc': {}, '100': {'ce': None, 'pe': None},
                                                           '105': {'ce': opt(10), 'pe': None}}})
        self.assertEqual(len(c['strikes']), 1)
        self.assertIsNone(c['strikes'][0]['pe'])


class Metrics(unittest.TestCase):
    def test_pcr(self):
        # puts 500+3000+1200+300+50 = 5050; calls 100+200+1000+4000+800 = 6100
        self.assertAlmostEqual(options.pcr(chain()['strikes']), round(5050 / 6100, 3))

    def test_pcr_and_max_pain_use_the_whole_expiry(self):
        # A far strike outside the kept window still counts: the PCR must not drift just
        # because spot moved and the window moved with it.
        rows = {**BASE, 200: ({'oi': 1000}, {'oi': 50})}
        c = chain(rows=rows)
        self.assertNotIn(200, [r['k'] for r in c['strikes']])
        self.assertEqual(c['all']['ce_oi'], 7100)
        s = options.summarize(c)
        self.assertAlmostEqual(s['pcr'], round(5100 / 7100, 3))
        self.assertEqual(options.expand(options.compact(c))['all'], c['all'])

    def test_max_pain_by_hand(self):
        # Payout at K = sum calls below K * (K - k) + puts above K * (k - K). At 100:
        # calls 80: 100*20 + 90: 200*10 = 4000; puts 110: 300*10 + 120: 50*20 = 4000 -> 8000.
        # At 90: calls 80: 1000; puts 100: 1200*10 + 110: 300*20 + 120: 50*30 = 19500 -> 20500.
        # At 110: calls 80: 3000, 90: 4000, 100: 10000 = 17000; puts 120: 500 -> 17500.
        self.assertEqual(options.max_pain(chain()['strikes']), 100)

    def test_walls(self):
        c = chain()
        self.assertEqual(options.walls(c['strikes'], 100, 'ce'), [(110, 4000), (100, 1000)])
        self.assertEqual(options.walls(c['strikes'], 100, 'pe'), [(90, 3000), (100, 1200)])
        s = options.summarize(c)
        self.assertEqual((s['support'], s['resistance']), (90, 110))

    def test_buildup_quadrants(self):
        self.assertEqual(options.buildup({'ltp': 12, 'pc': 10, 'oi': 120, 'poi': 100}), 'long_buildup')
        self.assertEqual(options.buildup({'ltp': 8, 'pc': 10, 'oi': 120, 'poi': 100}), 'short_buildup')
        self.assertEqual(options.buildup({'ltp': 12, 'pc': 10, 'oi': 80, 'poi': 100}), 'short_covering')
        self.assertEqual(options.buildup({'ltp': 8, 'pc': 10, 'oi': 80, 'poi': 100}), 'long_unwinding')
        self.assertEqual(options.buildup({'ltp': 10.05, 'pc': 10, 'oi': 150, 'poi': 100}), 'neutral')   # price flat
        self.assertEqual(options.buildup({'ltp': 12, 'pc': 0, 'oi': 150, 'poi': 100}), 'neutral')       # no reference
        # Against an earlier snapshot instead of the previous session.
        self.assertEqual(options.buildup({'ltp': 8, 'pc': 20, 'oi': 150, 'poi': 300}, {'ltp': 10, 'oi': 100}),
                         'short_buildup')

    def test_bias_from_writing_near_the_money(self):
        rows = {k: (dict(ce, poi=ce['oi']), dict(pe, poi=pe['oi'] - 400)) for k, (ce, pe) in BASE.items()}
        self.assertEqual(options.bias(chain(rows=rows)), 1.0)                       # only puts added
        rows = {k: (dict(ce, poi=ce['oi'] - 300), dict(pe, poi=pe['oi'] - 100)) for k, (ce, pe) in BASE.items()}
        self.assertAlmostEqual(options.bias(chain(rows=rows)), round((500 - 1500) / 2000, 3))
        self.assertEqual(options.bias_label(0.3), 'bullish')
        self.assertEqual(options.bias_label(-0.3), 'bearish')
        self.assertEqual(options.bias_label(0.1), 'neutral')

    def test_intraday_bias_against_a_reference(self):
        ref = chain()
        later = {k: (ce, dict(pe, oi=pe['oi'] + 100)) for k, (ce, pe) in BASE.items()}
        self.assertEqual(options.bias(chain(rows=later), ref), 1.0)

    def test_atm_iv_and_skew(self):
        c = chain()
        self.assertEqual(options.atm_iv(c), 15.0)                  # (14 + 16) / 2 at 100
        # 25-delta put: none near -0.25 within 0.15 (nearest -0.5) -> no skew.
        self.assertIsNone(options.skew(c))
        rows = dict(BASE)
        rows[90] = ({'oi': 200}, {'oi': 3000, 'iv': 19.0, 'delta': -0.26})
        self.assertEqual(options.skew(chain(rows=rows)), 19.0 - 12.0)

    def test_iv_percentile_needs_history(self):
        self.assertIsNone(options.iv_percentile(15, [10] * 19))
        self.assertEqual(options.iv_percentile(15, [10] * 15 + [20] * 5), 75.0)

    def test_unusual_activity(self):
        rows = dict(BASE)
        rows[110] = ({'oi': 4000, 'poi': 1500, 'iv': 12.0, 'delta': 0.24, 'ltp': 8, 'pc': 10}, {'oi': 300})
        rows[80] = ({'oi': 100, 'poi': 10}, {'oi': 500, 'vol': 50000, 'pvol': 1000})     # 900 % but tiny OI
        u = options.unusual(chain(rows=rows))
        self.assertEqual(u[0]['strike'], 110)
        self.assertEqual(u[0]['side'], 'CE')
        self.assertEqual(u[0]['buildup'], 'short_buildup')
        self.assertEqual(u[0]['oi_jump_pct'], round(2500 / 1500 * 100, 1))
        flagged = {(x['strike'], x['side']) for x in u}
        self.assertNotIn((80, 'CE'), flagged)           # +90 contracts next to a 4000 wall is noise
        self.assertIn((80, 'PE'), flagged)              # 50x the volume, and big next to the chain's volume

    def test_annotate_marks_changes(self):
        ref = chain()
        later = dict(BASE)
        later[90] = ({'oi': 200}, {'oi': 3600, 'poi': 3000, 'ltp': 9, 'pc': 10})
        rows = options.annotate(chain(rows=later), ref)
        r90 = next(r for r in rows if r['k'] == 90)
        self.assertEqual(r90['pe']['doi'], 600)
        self.assertEqual(r90['pe']['doi_intraday'], 600)
        self.assertEqual(r90['pe']['buildup'], 'short_buildup')
        self.assertEqual(r90['pe']['chg_pct'], -10.0)


class Rules(unittest.TestCase):
    def test_clamped_and_junk_ignored(self):
        r = options.rules_from({'stop_pct': 99, 'target_pct': float('nan'), 'min_bias': True, 'max_per_day': 3.6})
        self.assertEqual(r['stop_pct'], 60.0)
        self.assertEqual(r['target_pct'], 50.0)
        self.assertEqual(r['min_bias'], 0.35)
        self.assertEqual(r['max_per_day'], 4)

    def test_pick_expiry(self):
        exps = ['2026-10-06', '2026-10-13', '2026-09-29']
        self.assertEqual(options.pick_expiry(exps, '2026-10-06', for_trade=False), '2026-10-06')
        self.assertEqual(options.pick_expiry(exps, '2026-10-06', for_trade=True), '2026-10-13')
        self.assertEqual(options.pick_expiry(['2026-10-06'], '2026-10-06', for_trade=True), '2026-10-06')
        self.assertIsNone(options.pick_expiry(['2026-09-29'], '2026-10-06', for_trade=False))


NOW = datetime(2026, 10, 7, 11, 0, tzinfo=IST)


def bullish(spot=100.4):
    """Puts written near the money since `chain()`, spot up, PCR up, a tight ATM call."""
    rows = {k: (ce, dict(pe, oi=pe['oi'] + 600)) for k, (ce, pe) in BASE.items()}
    rows[100] = ({'oi': 1000, 'iv': 14.0, 'delta': 0.5, 'ltp': 4.0, 'bid': 3.95, 'ask': 4.0},
                 {'oi': 1800, 'iv': 16.0, 'delta': -0.5})
    c = chain(spot, rows)
    s = options.summarize(c, chain())
    return c, s


class Ideas(unittest.TestCase):
    R = options.rules_from({})
    HIST = [{'ts': (NOW - timedelta(minutes=30)).isoformat(), 'spot': 100.0}]

    def test_bullish_idea_buys_the_atm_call(self):
        c, s = bullish()
        i, why = options.idea('NIFTY', c, s, self.HIST, self.R, NOW, 0, False)
        self.assertEqual(why, '')
        self.assertEqual((i['side'], i['strike'], i['direction']), ('CE', 100, 'long'))
        self.assertEqual(i['entry'], 4.0)                              # at the ask
        self.assertEqual((i['stop'], i['target']), (3.0, 6.0))         # -25 %, +50 %
        self.assertEqual(i['level'], s['support'])
        self.assertIn('put writing', i['reason'])

    def test_bearish_mirror(self):
        rows = {k: (dict(ce, oi=ce['oi'] + 600), pe) for k, (ce, pe) in BASE.items()}
        rows[100] = ({'oi': 1600, 'iv': 14.0, 'delta': 0.5}, {'oi': 1200, 'iv': 16.0, 'delta': -0.5, 'ltp': 5.0, 'bid': 4.95, 'ask': 5.0})
        c = chain(99.5, rows)
        s = options.summarize(c, chain())
        i, why = options.idea('NIFTY', c, s, self.HIST, self.R, NOW, 0, False)
        self.assertEqual((i['side'], i['direction'], i['level']), ('PE', 'short', s['resistance']), why)

    def test_reasons_for_no_idea(self):
        c, s = bullish()
        cases = [
            (dict(now=NOW.replace(hour=9, minute=30)), 'outside the entry window'),
            (dict(now=NOW.replace(hour=14, minute=45)), 'outside the entry window'),
            (dict(open_now=True), 'already open'),
            (dict(taken=2), 'idea(s) today'),
            (dict(hist=[]), 'not 30 minutes'),
            (dict(hist=[{'ts': (NOW - timedelta(minutes=30)).isoformat(), 'spot': 100.39}]), 'moved'),
        ]
        for kw, expect in cases:
            i, why = options.idea('NIFTY', c, s, kw.get('hist', self.HIST), self.R, kw.get('now', NOW),
                                  kw.get('taken', 0), kw.get('open_now', False))
            self.assertIsNone(i)
            self.assertIn(expect, why)

    def test_expensive_iv_and_wide_spread_skip(self):
        c, s = bullish()
        i, why = options.idea('NIFTY', c, {**s, 'iv_pct': 90.0}, self.HIST, self.R, NOW, 0, False)
        self.assertIsNone(i)
        self.assertIn('percentile', why)
        c['strikes'][2]['ce'].update(bid=3.0, ask=4.0)
        i, why = options.idea('NIFTY', c, s, self.HIST, self.R, NOW, 0, False)
        self.assertIn('spread', why)

    def test_pcr_falling_vetoes_a_long(self):
        c, s = bullish()
        i, why = options.idea('NIFTY', c, {**s, 'pcr': 0.5, 'pcr_open': 0.9}, self.HIST, self.R, NOW, 0, False)
        self.assertIsNone(i)
        self.assertIn('PCR', why)


class MarketGate(unittest.TestCase):
    """The market's day (Nifty since its open), the last entry hour and one direction a day."""
    DAY = '2026-10-14T'

    def spots(self, *pairs):
        return [(self.DAY + t + ':00+05:30', s) for t, s in pairs]

    def at(self, hhmm):
        h, m = map(int, hhmm.split(':'))
        return datetime(2026, 10, 14, h, m, tzinfo=IST)

    def test_market_move_from_the_open(self):
        sp = self.spots(('09:10', 99.0), ('09:15', 100.0), ('10:00', 100.5), ('13:15', 99.4))
        self.assertEqual(options.market_move(sp, self.at('10:01')), 0.5)        # pre-open read left out
        self.assertEqual(options.market_move(sp, self.at('13:20')), -0.6)
        self.assertEqual(options.market_move(list(reversed(sp)), self.at('13:20')), -0.6)   # any order

    def test_market_move_unknown_when_it_cannot_be_trusted(self):
        late = self.spots(('11:00', 100.0), ('11:30', 101.0))                    # the app started late
        self.assertIsNone(options.market_move(late, self.at('11:31')))
        stale = self.spots(('09:15', 100.0), ('10:00', 100.5))                  # the index stopped coming
        self.assertIsNone(options.market_move(stale, self.at('10:30')))
        self.assertIsNone(options.market_move([], self.at('10:30')))
        self.assertIsNone(options.market_move(self.spots(('09:15', 0.0)), self.at('09:20')))

    def test_market_swing_and_the_bounce_gate(self):
        # 16 Oct: down 0.5 % by 09:33, back above the open by 10:18.
        sp = self.spots(('09:15', 23058.35), ('09:33', 22942.2), ('09:45', 22957.9), ('10:18', 23130.65), ('10:36', 23144.35))
        early, late = options.market_swing(sp, self.at('09:46')), options.market_swing(sp, self.at('10:40'))
        self.assertEqual((early['off_low'], early['off_high']), (0.068, 0.436))
        self.assertEqual((late['off_low'], late['off_high']), (0.881, 0.0))
        self.assertEqual(options.market_swing(sp[3:], self.at('10:40')), {'off_low': None, 'off_high': None})   # no open
        self.assertIsNone(options.bounce_gate('short', early['off_low'], early['off_high'], 0.5))   # the first puts
        self.assertIn('0.88% up off its low', options.bounce_gate('short', late['off_low'], late['off_high'], 0.5))
        self.assertIsNone(options.bounce_gate('long', late['off_low'], late['off_high'], 0.5))     # calls with the turn
        self.assertIn('down off its high', options.bounce_gate('long', 0.1, 0.6, 0.5))
        self.assertIsNone(options.bounce_gate('short', 0.88, 0.0, 2.0))         # off
        self.assertIsNone(options.bounce_gate('short', None, None, 0.5))        # unknown passes

    def test_idea_skips_into_the_turn(self):
        c, s = bullish()
        hist = [{'ts': (NOW - timedelta(minutes=30)).isoformat(), 'spot': 100.0}]
        on = options.rules_from({'max_bounce_pct': 0.5})
        i, why = options.idea('NIFTY', c, s, hist, on, NOW, 0, False, swing={'off_low': 0.9, 'off_high': 0.7})
        self.assertIsNone(i)
        self.assertIn('no bullish trade into the turn', why)
        i, why = options.idea('NIFTY', c, s, hist, on, NOW, 0, False, swing={'off_low': 0.9, 'off_high': 0.2})
        self.assertEqual(why, '')
        i, why = options.idea('NIFTY', c, s, hist, options.rules_from({}), NOW, 0, False, swing={'off_low': 0.9, 'off_high': 0.7})
        self.assertEqual(why, '')                                               # off by default
        cand = options.candidate('NIFTY', c, s, hist, NOW, on, market=0.4, swing={'off_low': 0.9, 'off_high': 0.7})
        self.assertEqual((cand['f']['mkt_off_low'], cand['f']['mkt_off_high']), (0.9, 0.7))

    def test_market_gate_band(self):
        self.assertIsNone(options.market_gate('long', 0.1, 0.3))                # flat: both sides pass
        self.assertIsNone(options.market_gate('short', 0.1, 0.3))
        self.assertIsNone(options.market_gate('short', -0.4, 0.3))              # with the market
        self.assertIn('against', options.market_gate('long', -0.4, 0.3))
        self.assertIn('against', options.market_gate('short', 0.3, 0.3))
        self.assertIsNone(options.market_gate('long', -1.5, 2.0))               # off
        self.assertIsNone(options.market_gate('long', None, 0.3))               # unknown

    def test_flip_gate(self):
        self.assertIsNone(options.flip_gate('long', {'short'}, False))
        self.assertIsNone(options.flip_gate('long', {'long'}, True))
        self.assertIsNone(options.flip_gate('long', set(), True))
        self.assertIn('one direction', options.flip_gate('long', {'short'}, True))

    def test_rules_default_off_and_cutoff_clock(self):
        r = options.rules_from({})
        self.assertEqual((r['market_band_pct'], r['one_direction'], r['no_entry_after']), (2.0, 0, '14:30'))
        r = options.rules_from({'last_entry_hour': 13.5, 'one_direction': 1, 'market_band_pct': 0.3})
        self.assertEqual((r['no_entry_after'], r['one_direction'], r['market_band_pct']), ('13:30', 1, 0.3))
        self.assertEqual(options.rules_from({'last_entry_hour': 16})['no_entry_after'], '14:30')   # clamped

    def test_idea_skips_against_the_market_and_flips(self):
        c, s = bullish()
        hist = [{'ts': (NOW - timedelta(minutes=30)).isoformat(), 'spot': 100.0}]
        on = options.rules_from({'market_band_pct': 0.3, 'one_direction': 1})
        i, why = options.idea('NIFTY', c, s, hist, on, NOW, 0, False, market=-0.5)
        self.assertIsNone(i)
        self.assertIn('against', why)
        i, why = options.idea('NIFTY', c, s, hist, on, NOW, 1, False, market=0.1, traded={'short'})
        self.assertIsNone(i)
        self.assertIn('one direction', why)
        i, why = options.idea('NIFTY', c, s, hist, on, NOW, 1, False, market=0.5, traded={'long'})
        self.assertEqual(why, '')
        self.assertIn('NIFTY +0.50% on the day', i['reason'])
        # Off (the defaults): the same idea goes through, as before the rules existed.
        i, why = options.idea('NIFTY', c, s, hist, options.rules_from({}), NOW, 1, False, market=-0.5, traded={'short'})
        self.assertEqual(why, '')
        i, why = options.idea('NIFTY', c, s, hist, options.rules_from({'last_entry_hour': 10.75}), NOW, 0, False)
        self.assertIn('outside the entry window', why)


class Book(unittest.TestCase):
    """Ideas open at once in one direction, and the order a pass opens its ideas in."""

    def test_side_gate_rank_and_pass(self):
        self.assertIsNone(options.side_gate('short', 3, 50))                   # off: no limit
        self.assertIsNone(options.side_gate('short', 1, 2))
        self.assertIn('2 bearish ideas open', options.side_gate('short', 2, 2))
        f = [{'atm_iv': 20, 'bias': 0.4, 'move_pct': 0.2}, {'atm_iv': 12, 'bias': 1.0, 'move_pct': 0.9}, {'bias': 0.6}]
        order = lambda rank: sorted(range(3), key=lambda i: options.rank_key(rank, f[i]))      # noqa: E731
        self.assertEqual((order(0), order(1), order(2), order(3)), ([0, 1, 2], [1, 0, 2], [1, 2, 0], [0, 1, 2]))
        self.assertEqual(options.pass_of('2026-10-16T09:47:59+05:30'), '2026-10-16T09:45')
        self.assertEqual(options.pass_of('2026-10-16T09:48:05+05:30'), '2026-10-16T09:48')
        self.assertEqual(options.rules_from({})['max_open_side'], options.OFF['max_open_side'])

    def test_a_pass_opens_its_best_ideas_first(self):
        store.init()
        _reset_options()
        with store.connect() as c:
            c.execute('DELETE FROM oc_candidates')
        store.set_settings({'options_rules': {'max_open_side': 2, 'rank_by': 1}})
        at = NOW.replace(hour=9, minute=45, second=10)

        def look(sym, iv):
            i = {'symbol': sym, 'side': 'PE', 'direction': 'short', 'strike': 100.0, 'entry': 10.0, 'stop': 7.5,
                 'target': 15.0, 'spot': 100.0, 'level': 105.0, 'bias': -0.6, 'move_pct': -0.3, 'reason': 'test',
                 'trail_pct': 0.0, 'expiry': '2026-10-27', 'session': '2026-10-07', 'lot': 65}
            cand = {'symbol': sym, 'direction': 'short', 'side': 'PE', 'strike': 100.0, 'entry': 10.0, 'level': 105.0,
                    'f': {'bias': 0.6, 'move_pct': 0.3, 'atm_iv': iv}}
            return {'symbol': sym, 'idea': i, 'cand': cand, 'at': at, 'trade': '2026-10-27', 'today': '2026-10-07',
                    'rank': cand['f']}
        pending = [look('NIFTY', 20.0), look('BANKNIFTY', 12.0), look('FINNIFTY', 15.0)]     # in the order read
        with mock.patch.object(app.data, 'now_ist', lambda: at.replace(second=50)):
            self.assertEqual(app.open_ideas(pending), 2)
        self.assertEqual([t['symbol'] for t in store.live_trades('options')], ['BANKNIFTY', 'FINNIFTY'])   # lowest IV first
        self.assertIn('the most at once is 2', app.OPT_WHY['NIFTY'])
        taken = {c['symbol']: c['taken'] for c in store.option_candidates()}
        self.assertEqual(taken, {'BANKNIFTY': 1, 'FINNIFTY': 1, 'NIFTY': 0})
        # One opened straight away (not in a pass) is entered at its look's time, as before.
        store.set_settings({'options_rules': {}})
        self.assertIsNotNone(app.open_idea(look('MIDCPNIFTY', 18.0)))
        self.assertEqual(store.ideas(symbol='MIDCPNIFTY')[0]['created_at'], at.isoformat(timespec='seconds'))


class Follow(unittest.TestCase):
    def setUp(self):
        self.i = {'strike': 100, 'side': 'CE', 'direction': 'long', 'entry': 4.0, 'stop': 3.0, 'target': 6.0, 'level': 90}

    def at(self, ltp, spot=100.5):
        rows = dict(BASE)
        rows[100] = ({'oi': 1000, 'ltp': ltp}, {'oi': 1200})
        return chain(spot, rows)

    def test_open_tracks_last_and_r(self):
        self.assertIsNone(options.follow(self.i, self.at(5.0), NOW))
        self.assertEqual((self.i['last'], self.i['r_open']), (5.0, 1.0))

    def test_target_fills_at_target(self):
        self.assertEqual(options.follow(self.i, self.at(6.4), NOW),
                         {'status': 'won', 'exit': 6.0, 'r': 2.0, 'note': 'target'})

    def test_stop_fills_where_seen(self):
        self.assertEqual(options.follow(self.i, self.at(2.6), NOW),
                         {'status': 'lost', 'exit': 2.6, 'r': -1.4, 'note': 'stop'})

    def test_support_break_exits_at_market(self):
        out = options.follow(self.i, self.at(3.5, spot=89.0), NOW)
        self.assertEqual((out['status'], out['exit'], out['r']), ('lost', 3.5, -0.5))
        self.assertIn('support 90 broke', out['note'])

    def test_square_off(self):
        out = options.follow(self.i, self.at(4.5), NOW.replace(hour=15, minute=16))
        self.assertEqual(out, {'status': 'closed', 'exit': 4.5, 'r': 0.5, 'note': 'squared off'})

    def test_final_without_a_price_uses_the_last_seen(self):
        self.i['last'] = 4.2
        out = options.follow(self.i, {'spot': None, 'strikes': []}, NOW, final=True)
        self.assertEqual((out['exit'], out['status']), (4.2, 'closed'))


class Trail(unittest.TestCase):
    """With a trail the target does not sell: it starts a stop under the best premium, never below halfway."""
    def setUp(self):
        self.i = {'strike': 100, 'side': 'CE', 'direction': 'long', 'entry': 4.0, 'stop': 3.0, 'target': 6.0, 'level': 90,
                  'trail_pct': 20}

    at = Follow.at

    def test_the_target_starts_the_trail_and_it_follows_the_best_premium(self):
        self.assertIsNone(options.follow(self.i, self.at(6.4), NOW))
        self.assertEqual((self.i['peak'], self.i['trail']), (6.4, 5.12))
        self.assertIsNone(options.follow(self.i, self.at(8.0), NOW))
        self.assertEqual(self.i['trail'], 6.4)
        self.assertIsNone(options.follow(self.i, self.at(7.0), NOW))       # below the best: the stop stays
        self.assertEqual(self.i['trail'], 6.4)
        self.assertEqual(options.follow(self.i, self.at(6.3), NOW),
                         {'status': 'won', 'exit': 6.3, 'r': 2.3, 'note': 'trailing stop'})

    def test_never_below_halfway_to_the_target(self):
        self.assertIsNone(options.follow(self.i, self.at(6.1), NOW))
        self.assertEqual(self.i['trail'], 5.0)
        self.assertEqual(options.trail_floor(4.0, 6.0), 5.0)
        self.assertEqual(options.follow(self.i, self.at(4.9), NOW)['r'], 0.9)

    def test_off_sells_at_the_target(self):
        self.i['trail_pct'] = 0
        self.assertEqual(options.follow(self.i, self.at(6.4), NOW)['note'], 'target')

    def test_the_trail_is_a_rule_with_its_off(self):
        self.assertEqual(options.rules_from({})['trail_pct'], 0.0)
        self.assertEqual(options.rules_from({'trail_pct': 20})['trail_pct'], 20.0)
        self.assertEqual(options.OFF['trail_pct'], 0.0)
        self.assertEqual((options.exit_key(25, 50), options.exit_key(25, 50, 20)), ('25/50', '25/50/20'))
        self.assertEqual(len(options.exit_grid()), 5 * 5 * 4)


def _reset_options():
    with store.connect() as c:
        for t in ('oc_snapshots', 'oc_daily', 'oc_ideas'):
            c.execute(f'DELETE FROM {t}')
        c.execute("DELETE FROM settings WHERE key LIKE 'options%' OR key = 'telegram'")
    app.OPT_WHY.clear()
    app.OPT_LEVELS.clear()


class EndToEnd(unittest.TestCase):
    """fetch_options through a morning: an opening chain, put writing with the market rising,
    one idea and its message, then its target and exactly one result message."""

    def setUp(self):
        store.init()
        _reset_options()
        store.set_settings({'telegram': '1', 'options_alerts': '1'})
        self.sent = []
        self.feed = {}
        patches = [
            mock.patch.object(app.dhan, 'expiries', lambda sym, ids: ['2026-10-13', '2026-10-27']),
            mock.patch.object(app.dhan, 'option_chain', lambda sym, exp, ids: self.feed[exp]),
            mock.patch.object(app.dhan, 'lot_size', lambda sym: 65),
            mock.patch.object(app.dhan, 'available', lambda: True),
            mock.patch.object(app.notify, 'configured', lambda: True),
            mock.patch.object(app.notify, 'send', lambda msg: self.sent.append(msg) or True),
            mock.patch.dict(app.OPT_WINDOW, {'index': 25.0, 'stock': 25.0}),
            # The clock on the test's session: options_state shows the last 7 days from now, which the
            # real clock had left behind a week after NOW.
            mock.patch.object(app.data, 'now_ist', lambda: NOW.replace(hour=12)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def put(self, rows, spot):
        self.feed['2026-10-13'] = raw(spot, rows)

    def test_morning(self):
        t0 = NOW.replace(hour=9, minute=20)
        self.put(BASE, 100.0)
        app.fetch_options('NIFTY', t0)
        self.assertEqual(len(store.snapshot_series('NIFTY', '2026-10-13', '2026-10-07')), 1)
        self.assertEqual(store.ideas(), [])
        self.assertIn('entry window', app.OPT_WHY['NIFTY'])

        rows = {k: (ce, dict(pe, oi=pe['oi'] + 600)) for k, (ce, pe) in BASE.items()}
        rows[100] = ({'oi': 1000, 'iv': 14.0, 'delta': 0.5, 'ltp': 4.0, 'bid': 3.95, 'ask': 4.0}, {'oi': 1800})
        self.put(rows, 100.1)
        app.fetch_options('NIFTY', NOW.replace(hour=10, minute=30))
        self.put(rows, 100.4)
        app.fetch_options('NIFTY', NOW)                                     # 30 min on, +0.3 %
        ideas = store.ideas()
        self.assertEqual(len(ideas), 1, app.OPT_WHY.get('NIFTY'))
        i = ideas[0]
        self.assertEqual((i['side'], i['strike'], i['status'], i['lot'], i['expiry']), ('CE', 100, 'open', 65, '2026-10-13'))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('OPTION IDEA · NIFTY 100 CE', self.sent[0])
        self.assertIn('risk ₹65.00 per lot', self.sent[0])

        # A second look with the idea open makes no new one; then the target.
        app.fetch_options('NIFTY', NOW + timedelta(minutes=3))
        self.assertEqual(len(store.ideas()), 1)
        rows[100] = ({'oi': 1000, 'ltp': 6.3}, {'oi': 1800})
        self.put(rows, 101.0)
        app.fetch_options('NIFTY', NOW + timedelta(minutes=6))
        done = store.ideas()[0]
        self.assertEqual((done['status'], done['exit'], done['r']), ('won', 6.0, 2.0))
        self.assertEqual(len(self.sent), 2)
        self.assertIn('Target hit', self.sent[1])
        self.assertIn('+₹130.00 per lot', self.sent[1])
        # Closed once: following again changes nothing and sends nothing.
        app.fetch_options('NIFTY', NOW + timedelta(minutes=9))
        self.assertEqual(len(self.sent), 2)

        state = app.options_state()
        nifty = next(r for r in state['rows'] if r['symbol'] == 'NIFTY')
        self.assertEqual(nifty['expiry'], '2026-10-13')
        self.assertEqual(state['stats']['all'], {'trades': 1, 'wins': 1, 'r': 2.0})
        view = app.options_chain_view('NIFTY')
        self.assertEqual(len(view['series']['ts']), 6)
        self.assertEqual(view['chain'][1]['pe']['doi_intraday'], 600)       # vs the 09:20 open

    def test_a_trail_runs_on_from_the_target(self):
        i = {'symbol': 'NIFTY', 'expiry': '2026-10-13', 'strike': 100, 'side': 'CE', 'direction': 'long',
             'created_at': NOW.isoformat(), 'session': '2026-10-07', 'status': 'open', 'entry': 4.0,
             'stop': 3.0, 'target': 6.0, 'level': 90, 'lot': 65, 'last': 4.4, 'trail_pct': 20}
        iid = store.add_idea(i)
        at = lambda ltp: chain(100.5, {**BASE, 100: ({'oi': 1000, 'ltp': ltp}, {'oi': 1200})})     # noqa: E731
        self.assertFalse(app._follow_idea(store.idea(iid), at(6.4), NOW, True))
        self.assertFalse(app._follow_idea(store.idea(iid), at(8.0), NOW + timedelta(minutes=3), True))
        row = store.idea(iid)
        self.assertEqual((row['status'], row['peak'], row['trail']), ('open', 8.0, 6.4))
        self.assertEqual(len(self.sent), 1)                                  # the trail is announced once
        self.assertIn('now trailing', self.sent[0])
        self.assertTrue(app._follow_idea(row, at(6.2), NOW + timedelta(minutes=6), True))
        row = store.idea(iid)
        self.assertEqual((row['status'], row['exit'], row['r'], row['note']), ('won', 6.2, 2.2, 'trailing stop'))
        self.assertIn('Trailing stop hit', self.sent[1])
        t = next(t for t in store.live_trades('options') if t['ref'] == f'options:{iid}')
        self.assertEqual((t['trail_pct'], app.exit_note(t, '')), (20.0, 'trailing stop'))

    def test_settle_after_close(self):
        store.add_idea({'symbol': 'NIFTY', 'expiry': '2026-10-13', 'strike': 100, 'side': 'CE', 'direction': 'long',
                        'created_at': NOW.isoformat(), 'session': '2026-10-07', 'status': 'open', 'entry': 4.0,
                        'stop': 3.0, 'target': 6.0, 'level': 90, 'lot': 65, 'last': 4.4})
        rows = dict(BASE)
        rows[100] = ({'oi': 1000, 'ltp': 4.6}, {'oi': 1200})
        c = chain(100.5, rows)
        store.add_snapshot('NIFTY', '2026-10-13', NOW.isoformat(), '2026-10-07', 100.5, {}, options.compact(c))
        self.assertEqual(app.settle_ideas(NOW.replace(hour=15, minute=40)), 1)
        i = store.ideas()[0]
        self.assertEqual((i['status'], i['exit'], i['note']), ('closed', 4.6, 'squared off'))
        self.assertEqual(app.settle_ideas(NOW.replace(hour=15, minute=41)), 0)

    def test_expiry_day_after_the_close(self):
        # Today's expiry is dead after 15:30: the overview and the IV history move to the next one.
        self.feed = {'2026-10-07': raw(100.0, BASE), '2026-10-13': raw(100.0, BASE)}
        with mock.patch.object(app.dhan, 'expiries', lambda sym, ids: ['2026-10-07', '2026-10-13']):
            app.fetch_options('NIFTY', NOW.replace(hour=12))
            during = {s['expiry'] for s in store.latest_summaries('2026-10-01') if s['summary'].get('is_near')}
            self.assertEqual(during, {'2026-10-07'})
            app.fetch_options('NIFTY', NOW.replace(hour=15, minute=40))
        with store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM oc_snapshots WHERE expiry = '2026-10-07' AND ts > ?",
                                       (NOW.replace(hour=15, minute=31).isoformat(),)).fetchone()[0], 0)
        self.assertEqual(app.options_state()['rows'][0]['expiry'], '2026-10-13')
        with store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM oc_daily").fetchone()[0], 1)

    def test_routes(self):
        c = app.app.test_client()
        self.assertEqual(c.get('/api/options/chain/NIFTY').status_code, 404)
        self.assertEqual(c.get('/api/options/chain/<x>').status_code, 400)
        self.assertEqual(c.get('/api/options/chain/NIFTY?expiry=13-10-2026').status_code, 400)
        r = c.post('/api/options/rules', json={'stop_pct': 30, 'target_pct': 50, 'junk': 1})
        self.assertEqual(r.get_json()['rule_overrides'], {'stop_pct': 30.0})
        self.assertEqual(app.orules()['stop_pct'], 30.0)
        r = c.post('/api/options/stocks', json={'stocks': 'reliance, tcs NIFTY bad$sym TCS'})
        self.assertEqual(r.get_json()['stocks'], ['RELIANCE', 'TCS'])
        self.assertEqual(set(r.get_json()['rejected']), {'NIFTY', 'BAD$SYM'})
        self.assertEqual(c.post('/api/options/stocks', json={'stocks': 5}).status_code, 400)
        self.assertEqual(c.get('/api/options').status_code, 200)

    def test_prune_keeps_each_sessions_last_chain(self):
        c = options.compact(chain())
        for day, n in (('2026-09-25', 3), ('2026-10-07', 2)):
            for k in range(n):
                store.add_snapshot('NIFTY', '2026-10-13', f'{day}T10:0{k}:00+05:30', day, 100, {'pcr': 1}, c)
        store.prune_options('2026-10-07')
        with store.connect() as conn:
            rows = conn.execute('SELECT session, chain IS NOT NULL AS has FROM oc_snapshots ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [('2026-09-25', 0), ('2026-09-25', 0), ('2026-09-25', 1), ('2026-10-07', 1), ('2026-10-07', 1)])


class FeedExits(unittest.TestCase):
    """An idea's stop and target are watched on the live feed every few seconds, not only at the chain passes every
    3 minutes, which miss a premium touching either and coming back in between."""
    def setUp(self):
        _reset_options()
        store.set_settings({'options': '1'})
        self.iid = store.add_idea({'symbol': 'NIFTY', 'expiry': '2026-10-13', 'strike': 100, 'side': 'CE', 'direction': 'long',
                                   'created_at': NOW.isoformat(), 'session': '2026-10-07', 'status': 'open', 'entry': 4.0,
                                   'stop': 3.0, 'target': 6.0, 'level': 90, 'lot': 65, 'last': 4.4})
        app._live_keys[f'options:{self.iid}'] = 'OPT:NSE_FNO:1'
        self.patches = [mock.patch.object(app.session, 'fo_open', return_value=True),
                        mock.patch.object(app.notify, 'send', return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app._live_keys.pop(f'options:{self.iid}', None)
        _reset_options()

    def at(self, px):
        with mock.patch.object(app, 'live_px', return_value=(px, NOW.timestamp())):
            return app.options_feed_exits(NOW)

    def test_inside_the_range_nothing_happens(self):
        self.assertEqual(self.at(4.8), 0)
        self.assertEqual(store.ideas()[0]['status'], 'open')

    def test_a_stop_on_the_feed(self):
        self.assertEqual(self.at(2.6), 1)
        i = store.ideas()[0]
        self.assertEqual((i['status'], i['exit'], i['note']), ('lost', 2.6, 'stop'))     # the price seen: a stop slips
        self.assertEqual(self.at(2.0), 0)                                                  # closed once

    def test_a_target_on_the_feed(self):
        self.assertEqual(self.at(6.4), 1)
        i = store.ideas()[0]
        self.assertEqual((i['status'], i['exit'], i['note']), ('won', 6.0, 'target'))     # a resting limit: at the target

if __name__ == '__main__':
    unittest.main()
