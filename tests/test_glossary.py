"""
The glossary: the README's copy matches niftywhale/glossary.py, and every
pattern code and abbreviation the code emits has an entry.

Run:  python -m unittest discover -s tests
"""
import os
import tempfile
import re
import unittest

_tmp = tempfile.mkdtemp()
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.db'))
os.environ.setdefault('UNIVERSE_PATH', os.path.join(_tmp, 'universe.json'))

from niftywhale import store as _store  # noqa: E402
assert os.path.realpath(_store.DB_PATH).startswith(os.path.realpath(tempfile.gettempdir())), \
    f'tests would write to {_store.DB_PATH}; run them with DB_PATH set to a temporary file'

from niftywhale import glossary, patterns

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def terms():
    return {t for _, _, items in glossary.GROUPS for t, _, _ in items}


class Glossary(unittest.TestCase):
    def test_readme_matches_the_source(self):
        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            text = f.read()
        m = re.search(r'<!-- glossary:start[^>]*-->\n(.*?)<!-- glossary:end -->', text, re.S)
        self.assertIsNotNone(m, 'README has no glossary markers')
        self.assertEqual(m.group(1), glossary.markdown(),
                         'README glossary is stale: regenerate it from niftywhale/glossary.py')

    def test_every_pattern_code_is_explained(self):
        listed = terms()
        for code in patterns.SHORT.values():
            self.assertIn(code, listed, f'pattern code {code} missing from the glossary')

    def test_abbreviations_on_the_dashboard_are_explained(self):
        listed = ' '.join(terms())
        for abbr in ('SMC', 'OB', 'FVG', 'BSL', 'SSL', 'EQ', 'CHoCH', 'HH', 'LL', 'R:R', 'ATR', 'VWAP',
                     'ORB', 'PDH', 'PDL', 'BB', 'Δ', 'Cum Δ', 'F&O', 'MIS', 'NSE', 'IST', 'OHLC', 'TOTP'):
            self.assertIn(abbr, listed, f'{abbr} missing from the glossary')

    def test_no_duplicate_terms(self):
        all_terms = [t for _, _, items in glossary.GROUPS for t, _, _ in items]
        self.assertEqual(len(all_terms), len(set(all_terms)))


class InForce(unittest.TestCase):
    """The Legend says what the tunable terms are set to now; the README keeps the defaults."""
    def test_values_in_force(self):
        from niftywhale import intraday, smc
        now = glossary.in_force(smc.Rules(min_rr=2.0, max_discount=0.4).as_dict(),
                                intraday.IntradayRules(no_entry_after='14:00').as_dict())
        terms = {t['term']: t['meaning'] for g in glossary.as_json(now) for t in g['terms']}
        self.assertTrue(terms['1:3 / 1:2'].endswith('Now: swing at least 1:2, intraday at least 1:2.'))
        self.assertIn('Now: at most 40% back (swing), 50% (intraday).', terms['Leg position'])
        self.assertIn('Now: no intraday entries after 14:00.', terms['Entry cutoff'])
        self.assertNotIn('Now:', {t['term']: t['meaning'] for g in glossary.as_json() for t in g['terms']}['ATR%'])

if __name__ == '__main__':
    unittest.main()
