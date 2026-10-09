"""
Indicator settings (indicators.SETTINGS / settings_from): every value clamped or checked,
junk falls back to the default, the saved settings reach the charts, and the settings
API stores them made safe.

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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import app  # noqa: E402
from niftywhale import indicators, store  # noqa: E402

from tests.test_regressions import _reset  # noqa: E402


def frame(n=80, minutes=15):
    idx = pd.date_range('2026-10-05 09:15', periods=n, freq=f'{minutes}min', tz='Asia/Kolkata')
    c = 100 + np.sin(np.arange(n) / 4) * 3
    return pd.DataFrame({'Open': c, 'High': c + 1, 'Low': c - 1, 'Close': c + 0.2, 'Volume': 1000.0}, index=idx)


class Settings(unittest.TestCase):
    def test_defaults_and_clamping(self):
        d = indicators.settings_from({})
        colors = {k: None for k in indicators.SETTINGS if k.startswith('color_')}
        self.assertEqual(d, {'bb_len': 20, 'bb_k': 2.0, 'orb_minutes': 15, 'delta_view': 'both', **colors})
        s = indicators.settings_from({'bb_len': 500, 'bb_k': 0.2, 'orb_minutes': 22, 'delta_view': 'sideways', 'evil': 1})
        self.assertEqual({k: s[k] for k in ('bb_len', 'bb_k', 'orb_minutes', 'delta_view')},
                         {'bb_len': 100, 'bb_k': 1.0, 'orb_minutes': 15, 'delta_view': 'both'})   # 22 -> nearest of 5/15/30/60
        self.assertNotIn('evil', s)
        self.assertEqual(indicators.settings_from({'bb_len': 'x', 'bb_k': float('nan')})['bb_len'], 20)
        self.assertEqual(indicators.settings_from('junk'), d)
        self.assertEqual(indicators.settings_from({'bb_len': 33.4})['bb_len'], 33)

    def test_colors(self):
        s = indicators.settings_from({'color_vwap': '#FF8800', 'color_bb': 'red', 'color_orb': '#12345', 'color_cum': None,
                                      'color_delta_up': 'javascript:alert(1)'})
        self.assertEqual((s['color_vwap'], s['color_bb'], s['color_orb'], s['color_cum'], s['color_delta_up']),
                         ('#ff8800', None, None, None, None))           # only #rrggbb; anything else: the theme's

    def test_settings_reach_the_indicators(self):
        f = frame()
        a = indicators.for_bars(f, 15, intraday=True)
        b = indicators.for_bars(f, 15, intraday=True, settings={'bb_len': 10, 'bb_k': 3, 'orb_minutes': 30})
        self.assertEqual((b['settings']['bb_len'], b['orb_minutes']), (10, 30))
        self.assertIsNotNone(b['bb_mid'][12])                   # 10-candle bands start sooner...
        self.assertIsNone(a['bb_mid'][12])                      # ...than the default 20
        k = next(i for i in range(25, 80) if a['bb_mid'][i] is not None)
        self.assertGreater(b['bb_upper'][k] - b['bb_mid'][k], 0)
        self.assertNotEqual(a['orb'], b['orb'])

    def test_api_stores_them_made_safe(self):
        _reset()
        c = app.app.test_client()
        r = c.post('/api/settings', json={'indicators': {'bb_len': 9999, 'delta_view': 'bars', 'x': 1}})
        self.assertEqual(r.status_code, 200)
        saved = json.loads(store.settings()['indicators'])
        self.assertEqual((saved['bb_len'], saved['delta_view']), (100, 'bars'))
        self.assertNotIn('x', saved)
        self.assertEqual(app.indicator_settings()['bb_len'], 100)
        self.assertEqual(c.post('/api/settings', json={'indicators': 'nope'}).status_code, 400)
        st = c.get('/api/state').get_json()
        self.assertEqual(st['indicator_settings']['delta_view'], 'bars')
        self.assertIn('bb_len', st['indicator_specs'])


if __name__ == '__main__':
    unittest.main()
