import json, os, re, sys
sys.path.insert(0, sys.argv[1])
from spec import E, GROUPS, STATIC
D = sys.argv[1]
G = json.load(open(f'{D}/out/get.json')); P = json.load(open(f'{D}/out/post.json'))
# Personal values to replace in the published answers (account and chat numbers, the bot's name), as
# [["real", "shown"], ...] in private_subs.json beside this file. Kept out of git (.gitignore).
_subs = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'private_subs.json')
SUBS = [tuple(x) for x in json.load(open(_subs))] if os.path.exists(_subs) else []

def clean(s):
    for a, b in SUBS:
        s = s.replace(a, b)
    return s

def trim(o, lists, depth_cap, depth=0):
    if isinstance(o, float):
        return round(o, 4)
    if isinstance(o, str):
        return o if len(o) <= 72 else o[:69] + '…'
    if isinstance(o, list):
        return [trim(x, lists, depth_cap, depth + 1) for x in o[:lists]]
    if isinstance(o, dict):
        if depth >= depth_cap:
            return '{…}' if o else {}
        return {k: trim(v, lists, depth_cap, depth + 1) for k, v in o.items()}
    return o

def pretty(raw, max_lines=34):
    try:
        o = json.loads(raw)
    except ValueError:
        return None
    for lists, cap in ((2, 5), (1, 4), (1, 3), (1, 2), (1, 1)):
        t = trim(o, lists, cap)
        s = json.dumps(t, indent=2, ensure_ascii=False)
        if s.count('\n') < max_lines:
            break
    lines = s.split('\n')
    if len(lines) > max_lines and isinstance(t, dict):
        # Still long: keep the first keys and say how many more there are.
        keep = {}
        for k in t:
            keep[k] = t[k]
            if json.dumps(keep, indent=2, ensure_ascii=False).count('\n') > max_lines - 3:
                del keep[k]
                break
        more = len(t) - len(keep)
        if more:
            keep['…'] = f'{more} more key{"s" if more > 1 else ""}'
        s = json.dumps(keep, indent=2, ensure_ascii=False)
    return clean(s).replace('"{…}"', '{…}')

def curl(m, url, body):
    q = f"'http://127.0.0.1:5058{url}'" if ('?' in url or '&' in url) else f'http://127.0.0.1:5058{url}'
    # The sign-in flow itself uses a cookie jar, like a browser; everything else an API token.
    auth = '-c jar -b jar' if url.startswith('/api/auth/') else '-H "Authorization: Bearer $NW_TOKEN"'
    if url in ('/healthz', '/api/auth/state'):
        auth = ''
    a = f'{auth} ' if auth else ''
    if m == 'GET':
        return f'curl {a}{q}'
    if not body:
        return f'curl -X POST {a}{q}'
    return f"curl -X POST {a}-H 'Content-Type: application/json' \\\n  -d '{clean(json.dumps(body, ensure_ascii=False))}' {q}"

uni = json.load(open(f'{D}/db/universe.json'))
out = ['## 12. API reference', '',
       'Every endpoint the dashboard uses, with a request you can run and the answer it gives. Send and receive JSON (a POST body is a',
       'JSON object); an error comes back as `{"error": "…"}` with a 4xx or 5xx status.', '',
       '**Authentication.** Everything except `/healthz`, `/login`, static files and the sign-in endpoints needs it. Scripts send an',
       'API token (10.4: create one in Account & security, or on the Pi with `docker exec niftywhale python -m niftywhale.auth token NAME`):',
       '', '```sh', 'export NW_TOKEN=nwt_...', 'curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/board', '```', '',
       'The examples run on the Pi itself (`http://127.0.0.1:5058`). From another device use `https://pandorasbox.local:5443`',
       '(with `--cacert caddy-root-ca.crt` unless the device trusts the Pi\'s certificate). Without a token or session: `401`.', '',
       'The answers shown are real ones from this app, shortened: long lists keep their first item or two, deep objects show `{…}`,',
       'long text ends in `…`, and personal values (account and chat numbers) are replaced.', '']
for n, (gk, gname) in enumerate(GROUPS, 1):
    out += [f'### 12.{n} {gname}', '']
    for g, m, path, ex, body, params, desc in E:
        if g != gk:
            continue
        cap = (G if m == 'GET' else P).get(path, {})
        if path in STATIC:
            st, js = STATIC[path]
            cap = {'status': st, 'raw': json.dumps(js, ensure_ascii=False), 'url': ex}
        url = cap.get('url', ex) if m == 'POST' else ex
        if m == 'POST' and cap.get('body') is not None:
            body = cap['body']
        out += [f'#### `{m} {path}`', '', desc, '']
        if params:
            out += [f'**{"Query" if m == "GET" and "?" in params else "Body" if m == "POST" else "Parameters"}:** {params}', '']
        out += ['```sh', curl(m, url, body), '```', '']
        status, raw = cap.get('status'), cap.get('raw', '')
        if path == '/api/universe/refresh':
            status, raw = 200, json.dumps({'built_at': uni.get('built_at'), 'failed': uni.get('failed') or [],
                                           'stocks': len(uni.get('stocks') or []), 'indices': 28})
        if path == '/healthz':
            out += ['```text', raw.strip(), '```', '']
            continue
        if path.endswith('.csv'):
            out += ['```text', clean('\n'.join(raw.strip().split('\n')[:5])), '```', '']
            continue
        if status == 204 or not raw.strip():
            out += [f'Answer: `{status}` with no body (nothing live right now).', '']
            continue
        js = pretty(raw)
        label = '' if status == 200 else f'Answer (`{status}`):'
        if label:
            out += [label, '']
        out += ['```json', js or clean(raw[:600]), '```', '']
out += ['---', '']
md = '\n'.join(out)
readme = open(sys.argv[2]).read()
s = readme.index('## 12. API reference'); e = readme.index('## 13. ')
open(sys.argv[2], 'w').write(readme[:s] + md + '\n' + readme[e:])
print('lines', md.count('\n'), 'endpoints', sum(1 for x in E))
