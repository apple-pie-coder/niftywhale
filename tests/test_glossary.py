"""
The glossary: the README's copy matches niftywhale/glossary.py, and every
pattern code and abbreviation the code emits has an entry.

Run:  python -m unittest discover -s tests
"""
import os
import re
import unittest

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


if __name__ == '__main__':
    unittest.main()
