# GET examples from the live app (read-only calls).
import json, os, sys, urllib.request, urllib.error
sys.path.insert(0, sys.argv[1])
from spec import E
out = {}
for g, m, path, ex, body, params, desc in E:
    if m != 'GET':
        continue
    url = 'http://127.0.0.1:5058' + ex.replace(':', '%3A', 1) if ex.startswith('/api/trade/') else 'http://127.0.0.1:5058' + ex
    try:
        # The API needs a token now (README 10.4): NW_TOKEN=$(docker exec niftywhale python -m niftywhale.auth token apidoc)
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['NW_TOKEN']} if os.getenv('NW_TOKEN') else {})
        r = urllib.request.urlopen(req, timeout=60)
        code, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read().decode()
    out[path] = {'status': code, 'raw': raw[:400000]}
    print(code, path, len(raw))
json.dump(out, open(sys.argv[2], 'w'))
