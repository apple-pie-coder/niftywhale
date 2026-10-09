"""
Smart money (niftywhale/smart.py): parsing what NSE publishes after each session
(bulk / block deals, FII/DII flows, participant-wise OI, delivery), client types,
the delivery read, positioning, storage and backfill, the trade context and its
Performance breakdowns, and the API.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

import app  # noqa: E402
from niftywhale import data, perf, smart, store  # noqa: E402

from tests.test_regressions import _reset, _zone  # noqa: E402

BULK = """Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
07-OCT-2026,ACEVECTOR,AceVector Limited,MATHISYS QUANTCAP LLP,BUY,3045592,28.83,-
07-OCT-2026,ACEVECTOR,AceVector Limited,GOLDMAN SACHS BANK EUROPE SE,SELL,3333333,28.85,-
07-OCT-2026,VEDL,Vedanta Limited,SBI MUTUAL FUND A/C SBI SMALL CAP FUND,BUY,1000000,261.40,-
07-OCT-2026,VEDL,Vedanta Limited,RAMESH JAIN HUF,SELL,50000,262.00,-
garbage,,,
"""
BLOCK_EMPTY = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n"
FLOWS = [{"buyValue": "18707.03", "category": "DII", "date": "07-Oct-2026", "netValue": "4596.57", "sellValue": "14110.46"},
         {"buyValue": "12577.96", "category": "FII/FPI", "date": "07-Oct-2026", "netValue": "-6121.37", "sellValue": "18699.33"}]
POI = '''"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Oct 07, 2026",,,,,,,,,,,,,,
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts      ,Total Short Contracts
Client,303069,57669,3452888,176904,3009102,2238829,2766476,2953709,1934399,735945,1089979,1038264,11674232,8083001
DII,47974,14055,277063,4580005,63899,55191,2456,500,10127,43545,307685,30674,497799,4935375
FII,33426,331711,3465691,2884989,506730,990415,906809,415750,162742,277337,297307,143309,5436341,4979876
Pro,47380,28414,1033446,587170,1356063,1084283,1257977,1022759,409549,320451,632466,465030,4251173,4203316
TOTAL,431849,431849,8229088,8229068,4935794,4368718,4933718,4392718,2516817,1377278,2327437,1677277,21859545,21859545
'''
BHAV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
PCBL, EQ, 07-Oct-2026, 327.70, 328.70, 329.00, 323.65, 327.25, 326.90, 326.47, 499455, 1630.58, 12845, 220049, 44.06
VEDL, EQ, 07-Oct-2026, 265.60, 264.50, 265.20, 258.80, 261.40, 261.40, 261.21, 10190251, 26618.13, 69569, 5271744, 51.73
21STCENMGM, BE, 07-Oct-2026, 38.36, 38.40, 39.12, 37.69, 39.12, 39.12, 38.86, 7291, 2.83, 27, 7286, 99.93
"""


class Parsing(unittest.TestCase):
    def test_deals(self):
        d = smart.parse_deals(BULK, 'bulk')
        self.assertEqual(len(d), 4)                                    # the garbage row is skipped
        self.assertEqual((d[0]['day'], d[0]['side'], d[0]['ctype'], d[0]['value_cr']), ('2026-10-07', 'buy', 'prop / quant', 8.78))
        self.assertEqual([x['ctype'] for x in d[1:]], ['fund', 'mutual fund', 'individual'])
        self.assertEqual(smart.parse_deals(BLOCK_EMPTY, 'block'), [])

    def test_client_types(self):
        for name, want in (('LIFE INSURANCE CORPORATION OF INDIA', 'insurance'), ('HDFC BANK LIMITED', 'bank'),
                           ('NOVA GLOBAL OPPORTUNITIES FUND PCC - TOUCHSTONE', 'fund'), ('SOCIETE GENERALE', 'fund'),
                           ('QE SECURITIES LLP', 'prop / quant'), ('VAYUMIND INNOVATIONS PRIVATE LIMITED', 'company'),
                           ('VIVEK LAHOTI', 'individual')):
            self.assertEqual(smart.client_type(name), want, name)

    def test_flows_poi_bhav(self):
        f = smart.parse_flows(FLOWS)
        self.assertEqual({(x['category'], x['net']) for x in f}, {('DII', 4596.57), ('FII', -6121.37)})
        poi = smart.parse_participant_oi(POI)
        self.assertEqual(sorted(poi), ['Client', 'DII', 'FII', 'Pro'])          # TOTAL left out
        self.assertEqual((poi['FII']['fut_idx_long'], poi['FII']['fut_idx_short']), (33426, 331711))
        pos = smart.positioning([{'day': '2026-10-07', 'data': poi}])
        self.assertEqual(pos['latest']['FII']['long_pct'], 9.2)
        parts = pos['series'][0]['parts']
        self.assertEqual(sorted(parts), ['Client', 'DII', 'FII', 'Pro'])            # every participant, for the chart
        self.assertEqual(parts['FII'], {'long_pct': 9.2, 'fut_net': -298285, 'calls_net': -400079, 'puts_net': 574665})
        self.assertEqual(smart.stance(9.2), 'short-heavy')
        b = smart.parse_bhav(BHAV)
        self.assertEqual(sorted(b), ['PCBL', 'VEDL'])                              # EQ only
        self.assertEqual((b['VEDL']['deliv_pct'], b['VEDL']['qty']), (51.73, 10190251))


def series(pcts, qtys, closes):
    days = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(len(pcts))]
    return [{'day': d, 'deliv_pct': p, 'qty': q, 'close': c, 'prev_close': c0, 'deliv_qty': int(q * p / 100)}
            for d, p, q, c, c0 in zip(days, pcts, qtys, closes, [closes[0]] + closes[:-1])]


class DeliveryRead(unittest.TestCase):
    base_p, base_q = [40.0] * 20, [100_000] * 20

    def read(self, p, q, up=True):
        closes = [100.0] * 20 + [103.0 if up else 97.0]
        return smart.delivery_read(series(self.base_p + [p], self.base_q + [q], closes))

    def test_reads(self):
        self.assertEqual(self.read(60, 200_000)['read'], 'accumulation')
        self.assertEqual(self.read(60, 200_000, up=False)['read'], 'distribution')
        self.assertEqual(self.read(20, 200_000)['read'], 'churn')
        self.assertEqual(self.read(60, 120_000)['read'], 'normal')            # no volume behind it
        r = self.read(60, 200_000)
        self.assertEqual((r['ratio'], r['vol_ratio'], r['sessions']), (1.5, 2.0, 20))
        self.assertIsNone(smart.delivery_read(series([40.0] * 3, [1] * 3, [1.0] * 3)))   # too little history

    def test_deal_summary(self):
        s = smart.deal_summary(smart.parse_deals(BULK, 'bulk'))
        self.assertEqual(s['VEDL']['inst_buy_cr'], 26.14)                        # the mutual fund; the HUF isn't institutional
        self.assertEqual(s['ACEVECTOR']['inst_net_cr'], -9.62)                   # the foreign seller; the quant desk isn't
        self.assertEqual(s['ACEVECTOR']['top'][0]['client'], 'GOLDMAN SACHS BANK EUROPE SE')


class Pass(unittest.TestCase):
    def setUp(self):
        _reset()
        with store.connect() as c:
            for t in ('sm_deals', 'sm_flows', 'sm_poi', 'sm_delivery'):
                c.execute(f'DELETE FROM {t}')
            c.execute("DELETE FROM settings WHERE key = 'smart:absent'")
        self.client = app.app.test_client()

    def fake(self, holiday='2026-10-02'):
        f = mock.Mock()
        f.deals.return_value = smart.parse_deals(BULK, 'bulk')
        f.flows.return_value = smart.parse_flows(FLOWS)

        def bhav(d):
            if d.isoformat() == holiday or d.isoformat() == '2026-10-07':     # 7 Oct: not out yet
                return None
            return {'VEDL': {'close': 260.0, 'prev_close': 258.0, 'qty': 1_000_000, 'deliv_qty': 400_000, 'deliv_pct': 40.0}}
        f.bhav.side_effect = bhav
        f.participant_oi.side_effect = lambda d: None if d.isoformat() == holiday else smart.parse_participant_oi(POI)
        return f

    def run_pass(self, f, now=datetime(2026, 10, 7, 19, 0, tzinfo=data.IST)):
        with mock.patch.object(app.smart, 'fetcher', return_value=f), mock.patch.object(app.data, 'now_ist', return_value=now):
            return app.smart_pass()

    def test_backfill_holidays_and_late_files(self):
        f = self.fake()
        r = self.run_pass(f)
        self.assertEqual((r['deals'], r['flows'], r['delivery'], r['poi']), (4, 2, 23, 24))
        self.assertIn('sm_delivery:2026-10-02', store.get_json('smart:absent'))     # 3+ days old: a holiday
        self.assertNotIn('sm_delivery:2026-10-07', store.get_json('smart:absent'))  # today's: tried again
        f2 = self.fake()
        r = self.run_pass(f2)
        self.assertEqual((r['deals'], r['delivery'], r['poi']), (0, 0, 0))
        asked = [c.args[0].isoformat() for c in f2.bhav.call_args_list]
        self.assertEqual(asked, ['2026-10-07'])                                    # only the one still missing

    def test_context_breakdowns_and_api(self):
        self.run_pass(self.fake())
        ctx = app.smart_context('VEDL', '2026-10-08T10:15:00+05:30')
        self.assertEqual((ctx['sm'], ctx['inst_deals_cr'], ctx['fii_long_pct'], ctx['fii_cash_cr'], ctx['deliv_read']),
                         (1, 26.14, 9.2, -6121.37, 'normal'))
        dims = {k: fn for k, _, fn in perf.DIMENSIONS}
        self.assertEqual(dims['inst_deals']({'side': 'long', 'f': ctx}), 'with the trade')
        self.assertEqual(dims['inst_deals']({'side': 'short', 'f': ctx}), 'against the trade')
        self.assertEqual(dims['fii_futures']({'side': 'long', 'f': ctx}), 'FII short-heavy (40 % long or less)')
        self.assertEqual(dims['fii_cash']({'side': 'long', 'f': ctx}), 'FII net sellers')
        self.assertEqual(dims['delivery']({'side': 'long', 'f': ctx}), 'normal')
        self.assertIsNone(dims['inst_deals']({'side': 'long', 'f': {}}))          # trades from before smart money
        # Nothing from the entry day itself (or later) leaks into its context.
        self.assertNotIn('fii_cash_cr', app.smart_context('VEDL', '2026-10-07T10:15:00+05:30'))
        _zone('VEDL', status='tapped')
        with mock.patch.object(app, 'option_stocks', return_value=[]), \
                mock.patch.object(app.data, 'now_ist', return_value=datetime(2026, 10, 8, 11, 0, tzinfo=data.IST)):
            d = self.client.get('/api/smart').get_json()
        self.assertEqual(d['flows']['latest']['FII']['net'], -6121.37)
        self.assertEqual(d['positioning']['stance'], 'short-heavy')
        vedl = next(s for s in d['stocks'] if s['symbol'] == 'VEDL')
        self.assertEqual(vedl['why'], 'tapped')
        self.assertEqual(vedl['deals']['inst_net_cr'], 26.14)
        self.assertEqual(len([x for x in d['deals'] if x['followed']]), 2)
        s = self.client.get('/api/smart/stock/VEDL').get_json()
        self.assertEqual(s['delivery']['read'], 'normal')
        self.assertEqual(self.client.get('/api/smart/stock/%3Cx%3E').status_code, 400)


if __name__ == '__main__':
    unittest.main()
