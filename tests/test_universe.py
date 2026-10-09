"""Universe selection: the protocol's three options, NSE indices, labels."""
import json
import os
import tempfile
import unittest

from niftywhale import universe

STOCKS = [
    {'symbol': 'RELIANCE', 'name': 'Reliance', 'nifty100': True, 'fo': True, 'indices': ['nifty50', 'nifty100', 'nifty500', 'niftyenergy']},
    {'symbol': 'IRCTC', 'name': 'IRCTC', 'nifty100': False, 'fo': True, 'indices': ['niftymidcap150', 'nifty500']},
    {'symbol': 'TINYCO', 'name': 'Tiny', 'nifty100': False, 'fo': False, 'indices': ['niftysmallcap250', 'nifty500']},
    {'symbol': 'FOONLY', 'name': 'F&O only', 'nifty100': False, 'fo': True, 'indices': []},
]


class Universe(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), 'u.json')
        with open(self.path, 'w') as f:
            json.dump({'built_at': '2026-10-05T10:00:00', 'stocks': STOCKS}, f)

    def syms(self, which):
        return sorted(m['symbol'] for m in universe.members(which, self.path))

    def test_protocol_options_unchanged(self):
        self.assertEqual(self.syms('nifty100'), ['RELIANCE'])
        self.assertEqual(self.syms('fo'), ['FOONLY', 'IRCTC', 'RELIANCE'])
        self.assertEqual(self.syms('both'), ['FOONLY', 'IRCTC', 'RELIANCE'])

    def test_index_membership(self):
        self.assertEqual(self.syms('nifty500'), ['IRCTC', 'RELIANCE', 'TINYCO'])
        self.assertEqual(self.syms('niftyenergy'), ['RELIANCE'])
        self.assertEqual(self.syms('niftyit'), [])

    def test_tickers_attached(self):
        self.assertEqual(universe.members('nifty100', self.path)[0]['ticker'], 'RELIANCE.NS')

    def test_valid_and_labels(self):
        self.assertTrue(universe.valid('niftysmallcap250'))
        self.assertTrue(universe.valid('both'))
        self.assertFalse(universe.valid('nifty9000'))
        self.assertEqual(universe.label('fo'), 'F&O stocks')
        self.assertEqual(universe.label('niftyoilgas'), 'Nifty Oil & Gas')

    def test_options_grouped_with_counts(self):
        with open(self.path) as f:
            opts = universe.options(json.load(f))
        by = {o['key']: o for o in opts}
        self.assertEqual([o['key'] for o in opts[:3]], ['nifty100', 'fo', 'both'])   # protocol first
        self.assertEqual(sum(1 for o in opts if o['key'] == 'nifty100'), 1)           # listed once
        self.assertEqual(by['nifty500']['count'], 3)
        self.assertEqual(by['niftybank']['group'], 'Sectors')

    def test_search_tag(self):
        self.assertEqual(universe.tag(STOCKS[0]), 'Nifty 100')
        self.assertEqual(universe.tag(STOCKS[1]), 'F&O')
        self.assertEqual(universe.tag(STOCKS[2]), 'Smallcap 250')


if __name__ == '__main__':
    unittest.main()


class Placeholders(unittest.TestCase):
    def test_dummy_rows_are_skipped_on_load(self):
        import json, tempfile, os
        from niftywhale import universe as u
        d = tempfile.mkdtemp()
        path = os.path.join(d, 'u.json')
        with open(path, 'w') as f:
            json.dump({'stocks': [{'symbol': 'DUMMYHEG', 'indices': ['niftysmallcap250']},
                                  {'symbol': 'RELIANCE', 'indices': ['nifty50']}]}, f)
        self.assertEqual([x['symbol'] for x in u.load(path)['stocks']], ['RELIANCE'])
        self.assertTrue(u.is_placeholder('dummyabc'))
        self.assertFalse(u.is_placeholder('DUMMIES'[:0] + 'DLF'))
