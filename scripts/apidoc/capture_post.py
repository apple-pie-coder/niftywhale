# POST examples in a throwaway copy (no .env, credentials removed, outside calls stubbed).
import json, os, sys
from unittest import mock
sys.path.insert(0, '/spec')
sys.path.insert(0, '/w')
os.environ['NIFTYWHALE_SCHEDULER'] = '0'
import app
from niftywhale import dhan, lab, notify, store
from spec import E

class NoThread:
    def __init__(self, *a, **k): pass
    def start(self): pass
    def is_alive(self): return False

def ids():
    with store.connect() as c:
        z = c.execute("SELECT id FROM zones WHERE status IN ('watching','tapped') AND COALESCE(mode,'swing')='swing' ORDER BY id DESC LIMIT 1").fetchone()
        iz = c.execute("SELECT id FROM zones WHERE status IN ('watching','tapped') AND mode='intraday' ORDER BY id DESC LIMIT 1").fetchone()
        n = c.execute("SELECT id FROM notices ORDER BY id DESC LIMIT 1").fetchone()
    return {'zone_id': z[0] if z else 1, 'izone_id': iz[0] if iz else 1, 'notice_id': n[0] if n else 1}

I = ids()
policy = {'mode': 'swing', **{k: v for k, v in (lab.policy().get('swing') or {}).items() if k in ('max_steps', 'shadow_days', 'cooldown_days', 'pause_r', 'rollback_r')}}
client = app.app.test_client()
out = {}
patches = [mock.patch.object(app.threading, 'Thread', NoThread),
           mock.patch.object(app, 'check_zones', return_value={'checked': 9, 'tapped': 1, 'triggered': 0, 'rejected': 0, 'patterns': 2, 'closed': 0}),
           mock.patch.object(app, 'check_intraday', return_value={'checked': 14, 'tapped': 2, 'triggered': 1, 'rejected': 0, 'failed': 1, 'closed': 0}),
           mock.patch.object(notify, 'send', return_value=True), mock.patch.object(notify, 'configured', return_value=True),
           mock.patch.object(notify, 'bot_name', return_value='your_bot'),
           mock.patch.object(dhan, 'profile', return_value={'dhanClientId': '1100012345', 'dataPlan': 'Active', 'dataValidity': '2026-11-08'})]
for p in patches:
    p.start()
for g, m, path, ex, body, params, desc in E:
    if m != 'POST':
        continue
    url = ex.format(**I)
    if body == '{policy}':
        body = policy
    if path == '/api/notices/read':
        body = {'ids': [I['notice_id']]}
    extra = mock.patch.object(dhan, 'available', return_value=True) if path == '/api/options/refresh' else mock.patch.object(app, 'logger')
    with extra:
        r = client.post(url, json=body)
    raw = r.get_data(as_text=True)
    out[path] = {'status': r.status_code, 'raw': raw[:400000], 'url': url, 'body': body}
    print(r.status_code, path, len(raw))
json.dump(out, open('/out/post.json', 'w'))
