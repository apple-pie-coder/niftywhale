# The API reference (README section 12): every endpoint, its group, what it does, its parameters,
# and the example request whose answer the README shows. GET examples are read from the live app;
# POST examples run against a throwaway copy of the app (capture_post.py) so nothing live changes.

GROUPS = [
    ('state', 'Dashboard, prices and search'),
    ('swing', 'Swing: scan, zones and rules'),
    ('intraday', 'Intraday'),
    ('options', 'Options'),
    ('trades', 'Trades and notifications'),
    ('demo', 'Demo funds'),
    ('news', 'News and smart money'),
    ('lab', 'Performance, lab and autopilot'),
    ('charts', 'Charts tab'),
    ('settings', 'Settings and connections'),
    ('auth', 'Signing in and the account'),
]

# (group, method, path, example, body, params, description)
E = [
    ('state', 'GET', '/healthz', '/healthz', None, '',
     'Liveness check, used by the Docker healthcheck. Plain text, not JSON.'),
    ('state', 'GET', '/api/state', '/api/state', None, '',
     'Everything the dashboard shows: market status, scan and watcher state, settings, rules, the latest scan with its funnel, '
     'setups, zones, alerts, `swing_trades` (the paper-trade journal and its totals) and `intraday` (session phase, its scan, '
     'setups, today\'s zones, journal, stats, rules).'),
    ('state', 'GET', '/api/board', '/api/board', None, '',
     'The live board: one card per watched stock with price, change, zone distance, today\'s 15m candles, patterns and today\'s news count.'),
    ('state', 'GET', '/api/ticker', '/api/ticker', None, '',
     'The index ticker: per index last, open, high, low, previous close, change, change %, place in the day\'s range (0–100), '
     'GIFT Nifty\'s gap to Nifty, and the option symbol for the five with chains. Empty without Dhan.'),
    ('state', 'GET', '/api/live', '/api/live?s=PAYTM,IDX:13', None,
     '`?s=` comma-separated feed symbols: a stock, `IDX:<id>` for an index, `OPT:<segment>:<id>` for an option contract (up to 250)',
     'The live prices behind every moving number on the page, for a page without its WebSocket (`/ws` pushes the same as they trade) '
     'and for scripts. Asking puts the symbols on the feed for 30 s; each price comes with the time it was read. Also carries the '
     'notification count (`notices`: `seq`, `unread`).'),
    ('state', 'GET', '/api/live/status', '/api/live/status', None, '',
     "The live plumbing: Dhan's market feed WebSocket (connected, since, instruments carried, packets, the last error), the feed "
     '(symbols streamed and still polled) and how many pages have their WebSocket open.'),
    ('state', 'GET', '/ws', '/ws', None, 'a WebSocket upgrade (`wss://` through Caddy); messages are JSON',
     "The page's live connection. Send `{\"t\": \"sub\", \"ch\": \"px\", \"p\": {\"s\": [\"IDX:13\"]}}` to subscribe (channels: "
     '`px` prices, `ticks` chart ticks `{s, since}`, `notices`, `demo`, `ticker`, `mood`, `chain` `{symbol, expiry}`), `{\"t\": \"unsub\", \"ch\": …}`, '
     '`{\"t\": \"vis\", \"hidden\": true}`, `{\"t\": \"ping\"}`. The server sends `hello` first, then `{\"t\": <channel>, \"d\": …}` as things '
     'change and `{\"t\": \"topics\", \"d\": {…}}` when a table (`db:<table>`) or the app\'s state (`run`, `market`, `news_run`, `smart_run`) '
     'changed. Signs in like a page (the cookie from the app\'s own origin) or with an API token; closes with 4401 when the sign-in ends, '
     '1013 when 12 pages are connected already.'),
    ('state', 'GET', '/api/glossary', '/api/glossary', None, '',
     'Every abbreviation and symbol the dashboard shows, by group (the Legend).'),
    ('state', 'GET', '/api/search', '/api/search?q=pay', None, '`?q=` text',
     'Up to 10 universe matches for the search box, plus an "analyse anyway" entry.'),
    ('state', 'GET', '/api/palette', '/api/palette', None, '',
     'What the command palette searches: every universe stock (symbol, name, tag, F&O, industry, weight) and the option indices.'),

    ('swing', 'POST', '/api/scan', '/api/scan', {'universe': 'fo'}, '`universe` (optional; also saved as the setting)',
     'Starts a scan. Universe keys: `nifty100`, `fo`, `both`, or an index such as `nifty500`, `niftybank`, `niftyit`. `409` if one is running.'),
    ('swing', 'POST', '/api/scan/stop', '/api/scan/stop', {}, '', 'Stops the running scan. The example shows the `409` answer when no scan is running.'),
    ('swing', 'POST', '/api/watch/check', '/api/watch/check', {}, '', 'Runs one watcher pass over the open swing zones now.'),
    ('swing', 'GET', '/api/funnel/<step>', '/api/funnel/structure', None,
     'step: `data`, `history`, `liquidity`, `volatility`, `structure`, `order_block`, `intact`, `discount`, `rr`; `?mode=intraday` for the intraday scan',
     'The stocks of the latest scan that stopped at that step, with the reason.'),
    ('swing', 'GET', '/api/stock/<SYMBOL>', '/api/stock/PAYTM?bars=30', None, '`?bars=30–260`, `?rules=<json>` (what-if rules)',
     'Daily candles with their indicators (`ind`), a fresh analysis (long, or short on a bearish F&O chart; `analysis.side`), and daily patterns.'),
    ('swing', 'GET', '/api/intraday/<SYMBOL>', '/api/intraday/PAYTM', None, '',
     'The last 3 sessions of 15m candles, the stock\'s zone, its patterns, and `ind`: volume, delta, cumulative delta, VWAP, Bollinger, opening range.'),
    ('swing', 'POST', '/api/whatif', '/api/whatif', {'rules': {'min_rr': 2.5}, 'universe': 'nifty500'}, '`rules`, `universe`',
     'The funnel, setups and per-step drop lists under those rules, from the last scan\'s data. Saves nothing.'),
    ('swing', 'POST', '/api/rules', '/api/rules', {'rules': {'min_rr': 3}}, '`rules` (`{}` resets)',
     'Saves rule-tuner overrides (they replace `SMC_*` in .env); returns the rules now in force.'),
    ('swing', 'POST', '/api/zones', '/api/zones', {'symbol': 'PAYTM', 'zone_low': 1619, 'zone_high': 1628.1, 'target': 1855.5},
     '`symbol`, `zone_low`, `zone_high`, `target`',
     'Watches (or updates) a zone you set: a target above the zone is a long, below it a short.'),
    ('swing', 'POST', '/api/zones/<id>/dismiss', '/api/zones/{zone_id}/dismiss', {}, '', 'Stops watching a swing zone (`409` once it has triggered or closed).'),

    ('intraday', 'POST', '/api/intraday/scan', '/api/intraday/scan', {}, '',
     'Starts a 15m scan of the intraday universe (a preview outside 09:30–14:30). `409` if one is running.'),
    ('intraday', 'POST', '/api/intraday/check', '/api/intraday/check', {}, '', 'Runs one 5m check of today\'s intraday zones now.'),
    ('intraday', 'GET', '/api/intraday/chart/<SYMBOL>', '/api/intraday/chart/PAYTM', None, '`?rules=<json>` (what-if rules)',
     '15m candles (3 sessions), 5m candles (latest session), a fresh 15m analysis with session levels, and today\'s zone.'),
    ('intraday', 'POST', '/api/intraday/whatif', '/api/intraday/whatif', {'rules': {'min_rr': 2.5}}, '`rules`',
     'Re-screens the last intraday scan\'s candles under those rules. Saves nothing. `409` before the first intraday scan since a restart.'),
    ('intraday', 'POST', '/api/intraday/rules', '/api/intraday/rules', {'rules': {'min_rr': 3}}, '`rules` (`{}` resets)',
     'Saves intraday tuner overrides (they replace `INTRADAY_*` in .env).'),
    ('intraday', 'POST', '/api/intraday/zones/<id>/dismiss', '/api/intraday/zones/{izone_id}/dismiss', {}, '', 'Stops watching an intraday zone.'),

    ('options', 'GET', '/api/options', '/api/options', None, '',
     'The Options tab: scanner state, one row per underlying (its nearest expiry\'s latest numbers, and why it has no idea), unusual activity, '
     'the idea journal and its totals, the idea rules.'),
    ('options', 'GET', '/api/options/chain/<SYMBOL>', '/api/options/chain/NIFTY', None, '`?expiry=YYYY-MM-DD`',
     'The latest chain for that expiry (default: the newest read): day and intraday OI changes and buildups, headline numbers, the day\'s '
     'spot / PCR / IV series, and the underlying\'s ideas. Each contract carries its feed symbol (`lp`). `404` before the first read.'),
    ('options', 'GET', '/api/options/chain/<SYMBOL>/live', '/api/options/chain/NIFTY/live', None, '`?expiry=YYYY-MM-DD`',
     'The same chain with live OI, volume, LTP and bid/ask for every stored strike (one quote request, cached 3 s) and the summary computed '
     'again; IV and max pain stay as read. `204` outside the session or without Dhan.'),
    ('options', 'POST', '/api/options/refresh', '/api/options/refresh', {'symbol': 'NIFTY'}, '`symbol` (optional)',
     'Reads that underlying\'s chains now, or runs a full pass. `409` without Dhan or while a pass runs.'),
    ('options', 'POST', '/api/options/rules', '/api/options/rules', {'stop_pct': 25, 'target_pct': 50}, 'rule: value pairs (`{}` resets)',
     'Saves idea-rule overrides (clamped); returns the rules in force.'),
    ('options', 'POST', '/api/options/stocks', '/api/options/stocks', {'stocks': 'RELIANCE TCS INFY'}, '`stocks` (empty = the default 30)',
     'Saves the stocks options mode reads; non-F&O symbols come back in `rejected`.'),

    ('trades', 'GET', '/api/trade/<ref>', '/api/trade/options:16', None,
     'ref: `swing:<zone id>`, `intraday:<zone id>` or `options:<idea id>`',
     'One trade in full, what a row in any trade table opens: levels, result, R, how long it was held, why it triggered, its alerts and '
     'notifications, the demo position with each charge, the context at entry, and news around it. `404` for anything that is not a trade.'),
    ('trades', 'GET', '/api/trade/<ref>/candles', '/api/trade/options:16/candles', None, '',
     'The trade\'s own chart: the option contract\'s premium for an idea, the stock otherwise; 5m for intraday and options, 15m / 60m / daily '
     'for swing by length; `entry_k` / `exit_k` mark the entry and exit candles.'),
    ('trades', 'GET', '/api/notices', '/api/notices', None, '`?after=<seq>` (0 = the latest 100)',
     'The bell: notifications changed since that seq, newest first, with the unread count. Each has a `kind`, a `level` '
     '(`info`, `good`, `bad`, `warn`) and a `link` (a stock, a chain, a tab, or a trade `ref`).'),
    ('trades', 'POST', '/api/notices/read', '/api/notices/read', {'ids': [1]}, '`ids` (omit to mark all read)',
     'Marks notifications read; returns the new counts.'),
    ('trades', 'POST', '/api/notices/clear', '/api/notices/clear', {}, '',
     'Clears every notification from the bell, on every device; returns how many and the new counts. They stay cleared: '
     'the same event (a trade near its stop, say) is not raised again.'),

    ('demo', 'GET', '/api/demo', '/api/demo', None, '',
     'The demo account: `account` (deposits, withdrawals, balance, blocked, free, unreal, equity, realised, charges, return), `by_mode`, '
     '`open`, `closed`, `skipped`, the `ledger` statement (newest first), its `statement` summary, `charges` by line, the balance `curve`, '
     '`first_trade`, `settings` and `live` (whether prices are live now).'),
    ('demo', 'GET', '/api/demo/live', '/api/demo/live', None, '',
     'The open positions\' latest prices and the totals they move, for the Demo tab\'s once-a-second refresh.'),
    ('demo', 'POST', '/api/demo/funds', '/api/demo/funds', {'kind': 'deposit', 'amount': 50000, 'note': 'top-up'},
     '`kind` (`deposit` or `withdraw`), `amount`, `note` (optional)',
     'Money in or out; the first deposit starts the account. A withdrawal above free funds is a `409`.'),
    ('demo', 'POST', '/api/demo/settings', '/api/demo/settings', {'risk_pct': 1, 'max_open': 20},
     '`risk_pct`, `max_alloc_pct`, `intraday_leverage`, `futures_margin_pct`, `max_open`, `max_per_day`, `swing`, `intraday`, `options`, `charges`',
     'Saves the demo settings (clamped; the two limits are whole numbers, 0 = no limit).'),
    ('demo', 'POST', '/api/demo/reset', '/api/demo/reset', {'confirm': True, 'amount': 1000000, 'since': 'first'},
     '`confirm: true`, `amount` (optional), `since`: `now`, `first` or `YYYY-MM-DD`, `note` (optional)',
     'Deletes the demo account; with an amount, opens a new one dated `since` and replays the trades since then at once. A future date is a `400`.'),
    ('demo', 'GET', '/api/demo/statement.csv', '/api/demo/statement.csv', None, '',
     'The whole statement as a spreadsheet (CSV): date, time, type, description, detail, amount, balance, and each charge line.'),
    ('demo', 'POST', '/api/demo/sync', '/api/demo/sync', {}, '', 'Takes and settles trades now (it also runs every minute in the session).'),

    ('news', 'GET', '/api/news', '/api/news?symbol=PAYTM&days=3', None, '`?symbol=X`, `?kind=filing|media`, `?days=1–45` (7)',
     'The news desk: filings and headlines, newest first, for every stock followed (or one stock), with `watch` and the desk\'s `state`.'),
    ('news', 'POST', '/api/news/refresh', '/api/news/refresh', {}, '`symbol` (optional)',
     'Fetches one stock\'s news now (at most every 5 minutes; answers when done), or every followed stock in the background.'),
    ('news', 'GET', '/api/smart', '/api/smart', None, '',
     'The Smart money tab: FII/DII `flows`, FII `positioning`, the followed `stocks` with their delivery read and 30-day deals, the last 10 '
     'days\' `deals`, what is `held`, and the pass `state`.'),
    ('news', 'GET', '/api/smart/stock/<SYMBOL>', '/api/smart/stock/PAYTM', None, '',
     'One stock: its delivery read and history, and its deals of the last 30 days.'),
    ('news', 'POST', '/api/smart/refresh', '/api/smart/refresh', {}, '', 'Fetches from NSE now, in the background.'),

    ('lab', 'GET', '/api/perf', '/api/perf?mode=swing', None, '`?mode=swing|intraday|options`, `?source=live|backtest`',
     'Statistics, the cumulative-R curve, highlights and breakdowns of the live paper journal (plus the last 25 trades, this week, and the '
     'open trades with their feed symbols in `open_live`), or of the latest backtest of the current rules.'),
    ('lab', 'GET', '/api/lab', '/api/lab', None, '',
     'History on disk, recent lab jobs, each mode\'s autopilot state, the policy, the change log and the lab `settings` '
     '(universes and history length per mode).'),
    ('lab', 'POST', '/api/lab/job', '/api/lab/job', {'kind': 'backtest'}, '`kind`: `backtest`, `tune`, `report` or `nightly`',
     'Queues a lab job; heavy work waits for the market to close.'),
    ('lab', 'POST', '/api/autopilot/policy', '/api/autopilot/policy', '{policy}',
     '`mode`, `max_steps`, `shadow_days`, `cooldown_days`, `pause_r`, `rollback_r`, `limits` (`{mode: {threshold: [min, max]}}`)',
     'Saves the autopilot settings.'),
    ('lab', 'POST', '/api/autopilot/<action>', '/api/autopilot/pause', {'mode': 'options'}, '`mode` (+ `"paused": false` to resume)',
     '`approve` a proposal now, `reject` it, `undo` the last change, or `pause` / resume a mode\'s entry alerts.'),

    ('charts', 'GET', '/api/charts/config', '/api/charts/config', None, '',
     'The Charts tab\'s instruments, timeframes, the saved layout and the live feed\'s state.'),
    ('charts', 'POST', '/api/charts/layout', '/api/charts/layout',
     {'layout': [{'symbol': 'IDX:13', 'tf': 300, 'levels': True}, {'symbol': 'PAYTM', 'tf': 900, 'levels': True}]},
     '`layout`: a list of `{symbol, tf, levels}`', 'Saves the Charts tab\'s layout.'),
    ('charts', 'POST', '/api/charts/drawings', '/api/charts/drawings', {'symbol': 'IDX:13', 'drawings': [{'id': 'a1', 'type': 'hline', 'p1': {'t': 1791545100, 'p': 22500}}]},
     '`symbol`, `drawings`: a list of `{id, type: "trend" | "hline" | "rect", p1, p2}`',
     "Replaces one instrument's drawings on the Charts tab (an empty list removes them). Points are `{t, p}`: the chart's time in "
     'seconds (IST clock time) and a price; a horizontal line has no `p2`. Up to 60 an instrument; `GET /api/charts/config` carries '
     'them all as `drawings`.'),
    ('charts', 'GET', '/api/charts/candles', '/api/charts/candles?symbol=IDX:13&tf=900', None,
     '`?symbol=`, `?tf=` seconds (1 to 604800, as in the config)', 'Candles `[t, o, h, l, c, v]` (t in seconds, IST) and the levels to draw.'),
    ('charts', 'GET', '/api/charts/live', '/api/charts/live?symbols=IDX:13&since=0', None, '`?symbols=a,b`, `?since=` epoch seconds',
     'Ticks `[epoch, price]` since `since` for each symbol, from the feed that polls every second.'),

    ('settings', 'POST', '/api/settings', '/api/settings', {'auto_scan': True, 'scan_time': '16:15'},
     '`universe`, `auto_scan`, `scan_time` (`HH:MM`), `watcher`, `telegram`, `pattern_alerts`, `shorts`, `intraday`, `intraday_universe`, '
     '`intraday_shorts`, `options`, `options_alerts`, `options_level_alerts`, `news`, `news_alerts`, `indicators` (an object), and the lab\'s '
     '`lab_swing_universe`, `lab_intraday_universe`, `lab_years_swing`, `lab_years_intraday`, `weekly_report`',
     'Saves dashboard settings; returns them all.'),
    ('settings', 'GET', '/api/config', '/api/config', None, '',
     'App settings that once needed .env (Telegram, Dhan automatic login, data source, candle delays, log level): each value and where it '
     'comes from (`app`, `env` or `default`). Secrets only say whether they are set; the Telegram token says which bot it is.'),
    ('settings', 'POST', '/api/config', '/api/config', {'watch_delay': '60', 'log_level': 'INFO'},
     '`telegram_token`, `telegram_chat_id`, `dhan_client_id`, `dhan_pin`, `dhan_totp`, `data_source` (`auto` or `yfinance`), '
     '`watch_delay`, `live_watch_delay`, `log_level`; `""` clears one back to .env',
     'Saves App settings (all or none: one bad value refuses the lot). New Dhan login details start a login at once.'),
    ('settings', 'POST', '/api/telegram/test', '/api/telegram/test', {}, '', 'Sends a test message to the configured chat.'),
    ('settings', 'POST', '/api/dhan/token', '/api/dhan/token', {'access_token': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJkaGFuQ2xpZW50SWQiOiIxMTAwMDEyMzQ1In0.signature', 'client_id': '1100012345'},
     '`access_token`, `client_id` (optional)', 'Connects Dhan with a token from web.dhan.co, after checking it with Dhan. The token is never sent back.'),
    ('settings', 'POST', '/api/dhan/disconnect', '/api/dhan/disconnect', {}, '', 'Forgets the Dhan token; back to yfinance.'),
    ('settings', 'POST', '/api/universe/refresh', '/api/universe/refresh', {}, '', 'Re-downloads every NSE list (28 indices + F&O) now; it answers when done (up to a minute). An index that fails keeps its previous members.'),
]


# Signing in (niftywhale/auth.py). These answers are written from the code, not captured: capturing them would sign in as
# the owner. The browser keeps the cookies; with curl, a cookie jar (-c / -b) does.
E += [
    ('auth', 'GET', '/api/auth/state', '/api/auth/state', None, '',
     'Public. Whether the account exists yet (`setup`), whether this browser is signed in, how many passkeys there are, whether '
     'passkeys work at this address, and whether sign-in is locked.'),
    ('auth', 'POST', '/api/auth/setup/begin', '/api/auth/setup/begin', {'setup_code': 'ABCD-EFGH-JKLM', 'username': 'me', 'password': 'a long passphrase'},
     '`setup_code` (from the Pi), `username`, `password` (10+ characters)',
     'First-time setup, step 1: checks the setup code and the password, and returns a new authenticator key with its QR code.'),
    ('auth', 'POST', '/api/auth/setup/finish', '/api/auth/setup/finish', {'code': '123456'}, '`code` from the authenticator',
     'Step 2: the code proves the authenticator is linked. Creates the account, signs this browser in, and returns the ten recovery codes (shown once).'),
    ('auth', 'POST', '/api/auth/login', '/api/auth/login', {'username': 'me', 'password': 'a long passphrase', 'remember': True},
     '`username`, `password`, `remember`', 'Sign-in, step 1. Right password: the second factor is due within 5 minutes. `423` while locked, `429` for an address with too many failures.'),
    ('auth', 'POST', '/api/auth/mfa', '/api/auth/mfa', {'code': '123456'}, '`code`, or `recovery_code`',
     'Sign-in, step 2: an authenticator code (each accepted once) or a recovery code (each works once). Sets the session cookie.'),
    ('auth', 'POST', '/api/auth/passkey/login/options', '/api/auth/passkey/login/options', {'remember': True}, '`remember`',
     'A passkey sign-in, step 1: WebAuthn request options (a challenge, the allowed passkeys, user verification required).'),
    ('auth', 'POST', '/api/auth/passkey/login/verify', '/api/auth/passkey/login/verify', {'credential': {'id': '…', 'response': {'…': '…'}}},
     '`credential`: the browser\'s answer to navigator.credentials.get, as JSON', 'Step 2: verifies the signature, the origin and the counter, and signs in.'),
    ('auth', 'POST', '/api/auth/logout', '/api/auth/logout', {}, '', 'Ends this session.'),
    ('auth', 'GET', '/api/auth/account', '/api/auth/account', None, '',
     'Signed-in browser only. The account: recovery codes left, passkeys, signed-in devices, API tokens (prefix only) and the last 40 sign-in events.'),
    ('auth', 'POST', '/api/auth/password', '/api/auth/password', {'current': 'old passphrase', 'new': 'a new long passphrase'},
     '`current`, `new`', 'Changes the password and signs every other device out.'),
    ('auth', 'POST', '/api/auth/totp/begin', '/api/auth/totp/begin', {}, '`password` when the sign-in is older than 10 minutes',
     'Moving the authenticator, step 1: a new key and QR code. The old app keeps working until step 2.'),
    ('auth', 'POST', '/api/auth/totp/finish', '/api/auth/totp/finish', {'code': '123456'}, '`code` from the new app', 'Step 2: links the new key.'),
    ('auth', 'POST', '/api/auth/recovery/new', '/api/auth/recovery/new', {}, '`password` when needed', 'Ten new recovery codes; the old ones stop working.'),
    ('auth', 'POST', '/api/auth/passkey/register/options', '/api/auth/passkey/register/options', {}, '`password` when needed',
     'Adding a passkey, step 1: WebAuthn creation options (a discoverable credential, user verification required). Only at https://pandorasbox.local:5443.'),
    ('auth', 'POST', '/api/auth/passkey/register/verify', '/api/auth/passkey/register/verify', {'credential': {'id': '…', 'response': {'…': '…'}}, 'name': 'iPhone'},
     '`credential` (navigator.credentials.create\'s answer), `name`', 'Step 2: verifies and keeps the passkey.'),
    ('auth', 'POST', '/api/auth/passkey/<id>/delete', '/api/auth/passkey/2/delete', {}, '`password` when needed', 'Removes a passkey.'),
    ('auth', 'POST', '/api/auth/sessions/<id>/revoke', '/api/auth/sessions/7/revoke', {}, '', 'Signs one device out.'),
    ('auth', 'POST', '/api/auth/sessions/revoke-others', '/api/auth/sessions/revoke-others', {}, '', 'Signs every other device out.'),
    ('auth', 'POST', '/api/auth/tokens', '/api/auth/tokens', {'name': 'laptop scripts'}, '`name`, `password` when needed',
     'Creates an API token. The token is in this answer only; afterwards only its first characters are shown.'),
    ('auth', 'POST', '/api/auth/tokens/<id>/revoke', '/api/auth/tokens/3/revoke', {}, '', 'Revokes an API token at once.'),
]

# The answers shown for the sign-in endpoints: (status, JSON).
STATIC = {
    '/api/live/status': (200, {'on': True, 'pages': 2, 'realtime': True, 'source': 'dhan', 'stream': {'connected': True, 'since': 1791560432.1,
                               'instruments': 61, 'packets': 48211, 'last_packet': 1791561620.4, 'error': None},
                               'feed': {'error': None, 'last_poll': 1791560431.0, 'streamed': 26, 'polled': 0},
                               'note': 'Dhan live feed, every trade', 'poll_ms': 1000}),
    '/ws': (101, [{'t': 'hello', 'on': True, 'live': {'source': 'dhan', 'stream': True, 'note': 'Dhan live feed, every trade'},
                   'notices': {'seq': 20, 'unread': 3}, 'topics': {'db:zones': 412, 'db:notices': 57, 'run': '9f3a0c1b2d4e'},
                   'channels': ['chain', 'demo', 'mood', 'notices', 'px', 'ticker', 'ticks']},
                  {'t': 'px', 'd': {'on': True, 'stream': True, 'px': {'IDX:13': [22525.15, 1791533853.72]}}},
                  {'t': 'topics', 'd': {'db:zones': 413}}]),
    '/api/auth/state': (200, {'setup': False, 'signed_in': False, 'username': None, 'passkeys': 1, 'passkey_origin': True, 'rp_id': 'pandorasbox.local',
                              'origins': ['https://pandorasbox.local:5443', 'https://192.168.1.50:5443'], 'locked_for': 0}),
    '/api/auth/setup/begin': (200, {'secret': 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP', 'uri': 'otpauth://totp/NiftyWhale:me?secret=JBSW…&issuer=NiftyWhale&digits=6&period=30',
                                    'qr': '<svg …>'}),
    '/api/auth/setup/finish': (200, {'ok': True, 'next': '/', 'recovery_codes': ['abcde-fghjk', 'mnpqr-stuvw', '…']}),
    '/api/auth/login': (200, {'mfa': True, 'methods': ['totp', 'recovery', 'passkey']}),
    '/api/auth/mfa': (200, {'ok': True, 'next': '/'}),
    '/api/auth/passkey/login/options': (200, {'challenge': 'Nh0…', 'timeout': 60000, 'rpId': 'pandorasbox.local',
                                              'allowCredentials': [{'id': 'xQ…', 'type': 'public-key'}], 'userVerification': 'required'}),
    '/api/auth/passkey/login/verify': (200, {'ok': True, 'next': '/'}),
    '/api/auth/logout': (200, {'ok': True}),
    '/api/auth/account': (200, {'username': 'me', 'created': '2026-10-09T18:20:11', 'recovery_left': 10,
                                'passkeys': [{'id': 2, 'name': 'Safari on iPhone', 'created': '2026-10-09T18:22:40', 'last_used': None, 'backed_up': 1}],
                                'sessions': [{'id': 7, 'device': 'Safari on iPhone', 'method': 'passkey (Safari on iPhone)', 'ip': '192.168.1.23', 'current': True}],
                                'tokens': [{'id': 3, 'name': 'laptop scripts', 'prefix': 'nwt_Q2xA9pZk', 'created': '2026-10-09T18:30:02', 'last_used': None}],
                                'events': [{'ts': '2026-10-09T18:22:41', 'kind': 'signed_in', 'ok': 1, 'device': 'Safari on iPhone', 'detail': 'passkey (Safari on iPhone)'}]}),
    '/api/auth/password': (200, {'ok': True}),
    '/api/auth/totp/begin': (200, {'secret': 'KRSXG5CTMVRXEZLU…', 'uri': 'otpauth://totp/…', 'qr': '<svg …>'}),
    '/api/auth/totp/finish': (200, {'ok': True}),
    '/api/auth/recovery/new': (200, {'recovery_codes': ['pqrst-uvwxy', 'bcdef-ghjkm', '…']}),
    '/api/auth/passkey/register/options': (200, {'rp': {'id': 'pandorasbox.local', 'name': 'NiftyWhale'}, 'user': {'id': 'r2…', 'name': 'me', 'displayName': 'me'},
                                                 'challenge': 'yW…', 'pubKeyCredParams': [{'type': 'public-key', 'alg': -7}, '…'],
                                                 'authenticatorSelection': {'residentKey': 'required', 'userVerification': 'required'}}),
    '/api/auth/passkey/register/verify': (200, {'ok': True, 'passkeys': [{'id': 2, 'name': 'iPhone'}]}),
    '/api/auth/passkey/<id>/delete': (200, {'ok': True, 'passkeys': []}),
    '/api/auth/sessions/<id>/revoke': (200, {'ok': True}),
    '/api/auth/sessions/revoke-others': (200, {'ok': True, 'revoked': 2}),
    '/api/auth/tokens': (200, {'id': 3, 'name': 'laptop scripts', 'token': 'nwt_Q2xA9pZk…', 'prefix': 'nwt_Q2xA9pZk'}),
    '/api/auth/tokens/<id>/revoke': (200, {'ok': True}),
}
