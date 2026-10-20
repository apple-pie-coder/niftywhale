"""
Orderflow (niftywhale/orderflow.py): trades sized and sided from Dhan's packets, 1-minute footprints,
and the readings over them, on packet sequences whose answer is known.

Run:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime

from niftywhale import orderflow as of

T0 = datetime(2026, 10, 21, 9, 20, tzinfo=of.IST).timestamp()


def pkt(vol, ltp, at, bid=None, ask=None, ltq=None):
    p = {'vol': vol, 'ltp': ltp, 'ltt': at}
    if bid is not None:
        p.update(bid=bid, ask=ask)
    if ltq is not None:
        p['ltq'] = ltq
    return p


def feed(book, packets, now=None):
    bars = [book.on_packet(p, now or p['ltt']) for p in packets]
    return [b for b in bars if b]


def bar(levels, step=1.0, o=None, c=None, t=T0, cvd=0):
    """A closed bar from [[level, sold, bought], ...]."""
    lv = [[px, s, b, 0] for px, s, b in levels]
    buy, sell = sum(x[2] for x in lv), sum(x[1] for x in lv)
    lo, hi = lv[0][0], lv[-1][0]
    return {'t': t, 'step': step, 'levels': lv, 'buy': buy, 'sell': sell, 'unknown': 0, 'delta': buy - sell,
            'open': o if o is not None else lo, 'close': c if c is not None else hi, 'high': hi, 'low': lo,
            'cvd': cvd, 'trades': 1, 'merged': 0, 'by_quote': 1, 'by_tick': 0}


class Trades(unittest.TestCase):
    def test_level_steps(self):
        self.assertEqual(of.level_step(25000), 5.0)            # Nifty future: 2.5 points wanted, 5 the step
        self.assertEqual(of.level_step(1400), 0.25)
        self.assertEqual(of.level_step(300), 0.05)
        self.assertEqual(of.level_of(25003.0, 5.0), 25000.0)

    def test_sided_by_the_quote_met_then_the_tick_rule(self):
        b = of.Book('NSE_FNO:1')
        out = feed(b, [pkt(1000, 100.0, T0, bid=99.95, ask=100.05),       # the day's baseline
                       pkt(1010, 100.05, T0 + 1, bid=100.0, ask=100.1),   # at the ask: bought
                       pkt(1030, 100.0, T0 + 2, bid=99.95, ask=100.05),   # at the bid: sold
                       pkt(1035, 100.02, T0 + 3),                          # inside: uptick, bought
                       pkt(1040, 100.02, T0 + 4)])                         # same price: as the last call
        self.assertEqual(out, [])
        done = b.flush(T0 + 60)
        self.assertEqual((done['buy'], done['sell'], done['delta']), (20, 20, 0))
        self.assertEqual((done['by_quote'], done['by_tick'], done['trades']), (2, 2, 4))
        self.assertEqual((done['open'], done['high'], done['low'], done['close']), (100.05, 100.05, 100.0, 100.02))

    def test_merged_trades_counted_at_their_full_size(self):
        b = of.Book('k')
        feed(b, [pkt(500, 50.0, T0, bid=49.95, ask=50.05), pkt(800, 50.05, T0 + 1, ltq=100)])
        done = b.flush(T0 + 60)
        self.assertEqual((done['buy'], done['merged']), (300, 1))

    def test_bars_close_on_the_minute_with_levels_poc_and_cvd(self):
        b = of.Book('k')
        out = feed(b, [pkt(0, 100.0, T0, bid=99.95, ask=100.05),
                       pkt(10, 100.05, T0 + 5), pkt(40, 100.05, T0 + 6),   # 40 bought at 100.05
                       pkt(45, 99.95, T0 + 7),                              # 5 sold
                       pkt(60, 99.95, T0 + 61)])                            # next minute: closes the first
        self.assertEqual(len(out), 1)
        first = out[0]
        self.assertEqual((first['t'], first['delta'], first['cvd'], first['poc']), (T0, 35, 35, 100.05))
        self.assertEqual(first['levels'], [[99.95, 5, 0, 0], [100.05, 0, 40, 0]])
        second = b.flush(T0 + 121)
        self.assertEqual((second['t'], second['delta'], second['cvd']), (T0 + 60, -15, 20))

    def test_a_new_day_starts_again(self):
        b = of.Book('k')
        feed(b, [pkt(0, 100.0, T0, bid=99.95, ask=100.05), pkt(10, 100.05, T0 + 1)])
        out = feed(b, [pkt(5, 101.0, T0 + 86400, bid=100.95, ask=101.05), pkt(9, 101.05, T0 + 86401)])
        self.assertEqual(out[0]['cvd'], 10)                     # yesterday's last bar
        self.assertEqual(b.flush(T0 + 86400 + 60)['cvd'], 4)    # today's delta alone

    def test_quality(self):
        bars = [{**bar([[1, 1, 1]]), 'trades': 4, 'merged': 1, 'by_tick': 2, 'unknown': 2}]
        self.assertEqual(of.quality(bars), {'bars': 1, 'trades': 4, 'merged': 0.25, 'by_tick': 0.5, 'unknown_volume': 0.5})


class Readings(unittest.TestCase):
    def test_stacked_buy_imbalances(self):
        b = bar([[100, 10, 5], [101, 5, 60], [102, 4, 50], [103, 3, 40], [104, 30, 2]])
        self.assertEqual(of.stacked_imbalances(b), {'buy': [[101, 103]], 'sell': []})

    def test_absorption_sold_into_and_held(self):
        quiet = [bar([[100, 5, 5], [101, 5, 5]], t=T0 + i * 60) for i in range(5)]
        hit = bar([[99, 60, 5], [100, 40, 5], [101, 10, 5]], o=100, c=101, t=T0 + 300)   # delta -95 of 125
        self.assertEqual(of.absorption(quiet + [hit], 5), 1)
        dropped = bar([[99, 60, 5], [100, 40, 5], [101, 10, 5]], o=101, c=99, t=T0 + 300)
        self.assertEqual(of.absorption(quiet + [dropped], 5), 0)

    def test_exhaustion_at_the_high(self):
        self.assertEqual(of.exhaustion(bar([[100, 30, 30], [101, 30, 30], [102, 20, 1]])), -1)
        self.assertEqual(of.exhaustion(bar([[100, 1, 20], [101, 30, 30], [102, 30, 30]])), 1)
        self.assertEqual(of.exhaustion(bar([[100, 30, 30], [101, 30, 30], [102, 30, 30]])), 0)

    def test_divergence_new_low_on_a_higher_delta(self):
        bars = [bar([[100 - i, 5, 5], [101 - i, 5, 5]], t=T0 + i * 60, cvd=-50 - 10 * i) for i in range(4)]
        bars.append(bar([[95, 5, 5], [96, 5, 5]], t=T0 + 240, cvd=-60))            # new low, cvd above -80
        self.assertEqual(of.divergence(bars, 4), 1)
        bars[-1]['cvd'] = -100
        self.assertEqual(of.divergence(bars, 4), 0)

    def test_a_reading_over_an_entry_window(self):
        quiet = [bar([[100, 5, 5], [101, 5, 5]], t=T0 + i * 60) for i in range(5)]
        hit = bar([[99, 60, 5], [100, 40, 5], [101, 10, 5]], o=100, c=101, t=T0 + 300)
        r = of.reading(quiet + [hit], T0 + 240, T0 + 540, 'long')                  # 5 minutes, 2 with bars
        self.assertEqual((r['coverage'], r['bars'], r['absorption'], r['delta']), (0.4, 2, 1, -95))
        self.assertEqual(of.reading(quiet + [hit], T0 + 240, T0 + 540, 'short')['absorption'], -1)
        self.assertEqual(of.reading([], T0, T0 + 300, 'long'), {'coverage': 0.0, 'bars': 0})


if __name__ == '__main__':
    unittest.main()
