# NiftyWhale

NiftyWhale scans NSE stocks for Smart Money Concepts (SMC) setups. It runs the 12-step
filtering protocol from `stockpulse/INSTRUCTIONS.md` mechanically, long and short, in two modes:

- **Swing:** daily candles every evening, then 15-minute candles during market hours, so you are
  told when a setup actually triggers. Trades last days to weeks.
- **Intraday:** the same protocol on 15-minute and 5-minute candles, inside one session: zones set
  every 15 minutes, entries until 14:30, everything out by 15:20 (section 5).

Around them: an **options** chain scanner with option-buying ideas (6), a **news desk** with FinBERT tone and the price
reaction (4.14), **smart money** from what NSE publishes after each session (4.15), a **demo funds** account that takes every
trade at a real size after charges (4.16), and **performance** with a backtesting, self-tuning lab (7).

- **Dashboard:** https://pandorasbox.local:5443 (sign in, 10.4; press **Ctrl K** / **⌘K** for the command palette, 4.8)
- **Runs as:** three Docker containers on the Pi: `niftywhale` (the dashboard and its schedulers), `niftywhale-lab`
  (backtests and the autopilot) and `niftywhale-nlp` (the FinBERT news scorer). It depends on no other app.
- **Places orders?** No. NiftyWhale is analysis only. Nothing in it can trade, and nothing it
  shows is investment advice.

---

## Contents

1. [Quick start](#1-quick-start)
2. [The idea in one page](#2-the-idea-in-one-page)
3. [How it works](#3-how-it-works)
4. [Using the dashboard](#4-using-the-dashboard)
5. [Intraday mode](#5-intraday-mode)
6. [Options mode](#6-options-mode)
7. [Performance and the autopilot](#7-performance-and-the-autopilot)
8. [A typical day](#8-a-typical-day)
9. [Telegram alerts](#9-telegram-alerts)
10. [Configuration](#10-configuration)
11. [The rules in detail](#11-the-rules-in-detail)
12. [API reference](#12-api-reference)
13. [Running, updating and backing up](#13-running-updating-and-backing-up)
14. [Troubleshooting](#14-troubleshooting)
15. [Development](#15-development)
16. [Limits](#16-limits)
17. [Glossary](#17-glossary)

---

## 1. Quick start

```sh
cd /home/flypi/dockers/niftywhale
cp .env.example .env          # optional: add Telegram credentials (see section 9)

# The news desk's tone model (FinBERT, ~107 MB) is not in git: fetch it once into nlp/model/.
mkdir -p nlp/model && cd nlp/model
for f in config.json tokenizer.json tokenizer_config.json special_tokens_map.json; do
  curl -fLO https://huggingface.co/Xenova/finbert/resolve/main/$f
done
curl -fL -o model_quantized.onnx https://huggingface.co/Xenova/finbert/resolve/main/onnx/model_quantized.onnx
cd ../..

docker compose up -d --build
```

Open **https://pandorasbox.local:5443** (on a phone, first trust the Pi's certificate: 10.4). The first visit sets up your
account: it asks for a one-time **setup code**, which you read on the Pi with

```sh
docker exec niftywhale python -m niftywhale.auth setup-code
```

then a username and password, your authenticator app (scan the QR code), your recovery codes and, if you like, Face ID.
Then pick a universe in the sidebar (**Nifty 100** is the default) and press **Run scan**. A Nifty 100 scan takes under a
minute.

After that you don't need to do anything. The app:

- scans again every weekday at **16:15 IST**;
- watches every setup it finds on **15-minute candles** during market hours;
- alerts you (on the dashboard, and on Telegram if configured) when a setup triggers with a
  reward of at least **3× the risk**.

---

## 2. The idea in one page

The protocol looks for stocks where large institutions have left footprints, waits for price to
come back to those footprints at a discount, and only acts when price confirms the turn on a
lower timeframe.

| Term | Meaning in NiftyWhale |
|---|---|
| **Swing high / low** | A daily candle whose high (low) is beyond the 3 candles either side of it. |
| **Structure** | *Bullish* when the last two swing highs rise (HH) **and** the last two swing lows rise (HL). |
| **BSL / SSL** | Buy-side / sell-side liquidity: swing highs above price and swing lows below it that nothing has traded through yet. That's where stop-losses cluster. |
| **Break of structure** | The first daily close above a previous swing high. |
| **Leg** | The rally from its lowest point (the *origin*) up to its highest point after the break. |
| **Order block (OB)** | The body of the last down-close candle at the start of that rally. |
| **Fair value gap (FVG)** | A three-candle gap inside the rally: candle 1's high is below candle 3's low, so price skipped that range. |
| **Equilibrium / discount** | The midpoint of the leg. Below it is the *discount* half, where the protocol buys. |
| **Zone** | Where NiftyWhale waits for price: the OB body, extended up to any open FVG in the discount half. |
| **Sweep** | On 15-minute candles, price dips below a recent swing low (taking out the stops there). |
| **CHoCH** | Change of character: after the sweep, a 15-minute candle *closes* above the last minor swing high. That is the entry signal. |
| **R:R** | Reward-to-risk: (target − entry) ÷ (entry − stop). The protocol requires at least 1:3. |

The table describes a **long** (buy). NiftyWhale also finds **shorts** (sells): the same protocol
upside down. A bearish structure (LH + LL) replaces the bullish one, the order block sits at the
start of a *decline*, the zone is in the **premium** half, the sweep takes out a swing **high**,
the CHoCH closes **below** a minor swing low, and the target is the **SSL** below (3.6).

In **intraday mode** every term moves one timeframe down: swings, structure, the leg and the order
block are read on 15-minute candles, and the sweep and CHoCH on 5-minute candles (section 5).

---

## 3. How it works

### 3.1 The two jobs

```
 EVENING (16:15 IST, weekdays)                MARKET HOURS (09:15–15:30 IST)
 ┌─────────────────────────────┐              ┌──────────────────────────────────┐
 │ SCAN  — daily candles       │   zones      │ WATCH — 15-minute candles        │
 │  Steps 1–8 + provisional R:R├────────────► │  Steps 9–12, after every candle  │
 │  over the whole universe    │              │  for the watched zones only      │
 └─────────────────────────────┘              └───────────────┬──────────────────┘
                                                              │ CHoCH with R:R ≥ 1:3
                                                              ▼
                                                   Alert (dashboard + Telegram)
```

Both jobs run inside the one container, driven by a scheduler thread that wakes every 20 seconds.
Intraday mode (section 5) has its own scheduler thread: a 15-minute scan and a 5-minute check
during the session, independent of these two jobs.

### 3.2 The scan (Steps 1–8)

For each stock in the chosen universe, NiftyWhale downloads one year of daily candles from
yfinance (in batches of 50) and runs the steps below in order. A stock stops at the first step
it fails; the dashboard's funnel counts how many stocks passed each one.

| # | Step | Passes when |
|---|---|---|
| 1 | Universe | The stock is in the chosen universe: the protocol's Nifty 100 / F&O lists, or any of 28 NSE indices (see 4.2). |
| 2 | Liquidity | Its average daily volume over the last 63 sessions (≈ 3 months) is at least 10 lakh shares. |
| 3 | Volatility | Its 14-day ATR is more than 1.5% of price. |
| 4 | Structure | The last two swing highs and lows are HH + HL (a long), and the latest close is still above that HL. Or LH + LL (a short), with the close still below that LH. |
| 5 | Liquidity pools | (Always mapped, never fails.) BSL and SSL levels are recorded for the chart and the target. |
| 6 | Order block | Within the last 120 sessions there is a break of structure whose move from origin to the break covers at least 1.5 ATR, with an opposite-colour candle at its origin (a down candle before a rally, an up candle before a decline). |
| 6b | Intact | No daily close since then through the order block. |
| 7 | FVGs | (Never fails.) Unfilled gaps in the move are recorded and can widen the zone. |
| 8 | Discount / premium | Price has come back at least halfway: at or below 50% of a rally (discount), or at or above 50% of a decline (premium). |
| — | Provisional R:R | With the stop just beyond the leg's origin and the target at the leg's far end, R:R is at least 1:1.5. |

The provisional R:R filters out setups that can never work. The doc's 1:3 rule is applied
later, to the much tighter stop from the 15-minute sweep.

Every stock that passes becomes a **setup**. Setups are split into:

- **In the zone now:** today's candle traded into the zone and price hasn't closed through it.
- **Waiting for the pullback:** price is still beyond the zone (above it for a long, below it
  for a short).

Each setup gets a **score** from 0 to 100, used only for ordering. It weighs closeness to the
zone (30%), provisional R:R (25%), depth into the discount (20%), whether an FVG overlaps the
order block (15%) and volatility (10%).

### 3.3 Zones and their life cycle

When a scan finishes, every setup becomes a **watched zone**:

```
 watching ──(price trades into the zone)──► tapped ──(sweep + CHoCH)──┬─► triggered ──┬─► won   (paper trade hit its target)
     │                                                                │  (R:R ≥ 1:3,  └─► lost  (paper trade hit its stop)
     │                                                                │   alert sent: an open paper trade)
     │                                                                └─► rejected   (R:R < 1:3, or found too late; no alert)
     ├──(next scan no longer passes it)──► expired
     ├──(10 days old)───────────────────► expired
     └──(you press Dismiss)─────────────► dismissed
```

- A later scan **refreshes** a scan zone's levels if the stock still passes, and **expires** it
  if not. Only completed scans do this; a scan you stop partway leaves zones alone, and so
  does a scan that got daily candles for less than half the universe (the data source was
  down; see section 14). A stock the scan got no candles for keeps its zone.
- **A setup is watched once.** After its zone has triggered, been rejected, been dismissed or
  turned 10 days old, later scans don't arm the same setup again (same stock, same side, same
  order block). The protocol's Step 12 says it: "delete the stock from your watchlist ... wait
  for the next setup". A new setup on that stock (another order block, or the other side) is
  armed as usual, once the stock has no paper trade open: **one trade per stock** (3.7). A
  setup that only dropped off the screen (say, price left the discount half) comes back if it
  passes again.
- **Zones you set yourself** (see 4.6) are never refreshed or expired by a scan. They stay until
  they trigger or you dismiss them.

### 3.4 The watcher (Steps 9–12)

During market hours, about 90 seconds after each 15-minute candle closes (so the candle has
reached yfinance), the watcher downloads 15-minute candles for every open zone in one request.
It runs one extra pass in the 20 minutes after the close, to catch the 15:15 candle. For each
zone, it looks at the last two sessions, completed candles only, and only at price action from
the candle the zone was set in onwards. A tap, sweep or CHoCH from before the zone existed
is old news, not an entry (a zone set by a 13:48 scan does not fire on that morning's 09:30 CHoCH).

1. **Tap:** a candle's low reaches the top of the zone. The zone becomes `tapped`.
2. **Sweep:** from the tap on, price takes out a confirmed 15-minute swing low (2 candles each side).
3. **CHoCH:** a candle then **closes** above the most recent minor 15-minute swing high that
   formed before the sweep low.
4. **Plan:**
   - **Entry:** the CHoCH candle's close.
   - **Stop:** 0.1% below the sweep low.
   - **Target:** the daily leg high (BSL).
5. **Verdict:**
   - **R:R ≥ 1:3:** the zone becomes `triggered` and an entry alert is recorded and sent.
   - **Below 1:3:** the zone becomes `rejected` and the alert log shows why. Nothing is sent.
   - **Found late:** a CHoCH whose candle closed more than 45 minutes before the check found
     it (the watcher was off, the app was down) is history, not an entry: its entry price is
     long gone, and price may already be through the stop. The zone becomes `rejected` with
     "too late to enter", and nothing is sent.

For a **short** zone, every step is mirrored (3.6): the tap is price rallying up into the zone,
the sweep takes out a 15-minute swing high, the CHoCH is a close below a minor swing low, the
stop sits 0.1% above the sweep high, and the target is the daily leg low.

Every entry alert then becomes a **paper trade**, which the same passes follow to its target or
stop (3.7).

### 3.5 Where things live

| Path | What |
|---|---|
| `app.py` | Flask app: scan runner, watcher, both schedulers, the intraday scan and check, API routes. |
| `niftywhale/intraday.py` | Intraday mode as pure functions: the 15m screen (wrapping `smc.evaluate()`), session levels and target pools, the 5m trigger, trade outcomes. Its thresholds live in `IntradayRules`. |
| `niftywhale/lab.py` | The lab service (`python -m niftywhale.lab`, container `niftywhale-lab`): job queue, schedule, history and record building, baseline backtests, the autopilot's tuning / shadow / rollback / pause, options candidate outcomes, the weekly report. |
| `niftywhale/news.py` | The news desk: NSE filings and Google News headlines per stock (fetch, parse, which headlines name the company, tags), the FinBERT client and the price reaction after each item. |
| `niftywhale/smart.py` | Smart money: NSE's bulk / block deals, FII/DII flows, participant-wise OI and delivery (fetch and parse), client types, the delivery read and positioning. |
| `nlp/` | The FinBERT scorer (`server.py`, its Dockerfile and the model files), container `niftywhale-nlp`. |
| `niftywhale/backtest.py` | The backtester: screen records with loose filters, then the live zone life cycle replayed per rule set (memoised triggers and outcomes). |
| `niftywhale/autopilot.py` | The autopilot's decisions as pure functions: search grid, walk-forward, shadow, rollback, pause, policy. |
| `niftywhale/history.py` | Candle history for the lab: yfinance dailies, Dhan 15m / 5m in 90-day pieces, Nifty / VIX. |
| `niftywhale/perf.py` | Statistics, equity curve and breakdowns for any list of trades. |
| `niftywhale/features.py` | The context recorded with every trade, live or backtested. |
| `niftywhale/ticker.py` | The index ticker: the list of indices with their Dhan IDs, previous closes from daily candles, day position. |
| `niftywhale/options.py` | Options mode as pure functions on parsed chains: PCR, max pain, OI walls, buildups, OI bias, ATM IV, skew, IV percentile, unusual activity, and the idea and paper-trade rules. |
| `niftywhale/patterns.py` | The four candlestick patterns, as pure functions like `smc.py`. |
| `niftywhale/smc.py` | The protocol as pure functions on candle data: swings, structure, liquidity, order block, FVGs, the daily `evaluate()` and the 15-minute `choch_trigger()`, both with a `side` (shorts run on the mirrored chart). All the thresholds live in `Rules`. |
| `niftywhale/data.py` | Daily candles from yfinance, 5m/15m candles from Dhan (yfinance as fallback), the in-memory caches, the IST market clock. |
| `niftywhale/universe.py` | Downloads the Nifty 100, F&O and 27 other index lists from NSE; `INDICES` is the registry. |
| `niftywhale/store.py` | SQLite: scans, per-stock verdicts, setups, zones, alerts, settings, and options mode's chain snapshots, daily IV history and ideas. Scans, zones and alerts carry a `mode` (`swing` / `intraday`) that keeps the two modes apart. |
| `niftywhale/dhanws.py` | Dhan's live market feed over a WebSocket: the binary packets, subscriptions (ticker / quote / full), reconnects. `charts.DhanSource` maps it to the feed's symbols. |
| `niftywhale/livefeed.py` | The live feed: who wants which instrument priced, ticks for the Charts tab, the stream with a REST poll for whatever it does not carry. |
| `niftywhale/hub.py` | The page's WebSocket (`/ws`): channels, "this changed" from the database's change triggers and the app's state. |
| `niftywhale/dhan.py` | Dhan real-time data: tokens (paste, TOTP login, renewal), symbol IDs, option lot sizes, quotes (stocks and indices), index daily candles, 5m and 15m candles (one shared 5-requests-a-second limit), option chains and expiries (their own one-every-3-seconds limit). |
| `niftywhale/indicators.py` | Chart indicators as pure functions: estimated delta (close-location split, from 1-minute candles where available), cumulative delta, session VWAP, the opening range and its breakout, Bollinger Bands. |
| `niftywhale/notify.py` | Telegram. |
| `templates/index.html` | The whole dashboard (HTML, CSS and JS in one file). |
| `var/` (mounted as `/data`) | `niftywhale.db` and `universe.json`: everything that persists. |

Daily candles are cached in memory for **10 minutes** during market hours and **3 hours**
otherwise, so opening charts doesn't re-download data the scan just fetched. After the close,
only candles fetched once it has settled (15:45) count: a frame cached by a 14:35 scan holds a
14:35 candle, not the close, so the evening scan downloads again. A scan never falls back to an
older cached frame when a download fails; a chart does.


### 3.6 Short setups

Every step has a mirror image, and NiftyWhale runs shorts exactly that way. It turns the chart
upside down (each price *p* becomes *K − p*), runs the long protocol unchanged, and turns every
price back. So a rule means the same thing in both directions, and the tests check that a short
on a flipped chart is the exact mirror of the long on the original.

| | Long | Short |
|---|---|---|
| Structure | HH + HL | LH + LL |
| Order block | last down candle before the rally | last up candle before the decline |
| Wait for price | to pull back into the **discount** half | to rally back into the **premium** half |
| Sweep | below a swing low | above a swing high |
| CHoCH | a close **above** a minor swing high | a close **below** a minor swing low |
| Stop | under the sweep | above the sweep |
| Target | BSL above (leg high) | SSL below (leg low) |
| Patterns | bullish engulfing, hammer, inside-bar breakout, morning star | bearish engulfing, shooting star, inside-bar breakdown, evening star |

- **One side per stock.** A chart can't be HH/HL and LH/LL at once. A stock that fails the long
  on a bearish structure is evaluated as a short instead, so each stock still has one verdict
  and the funnel counts it once.
- **Swing shorts are F&O stocks only.** In India a short in the cash market must be closed the
  same day, so a short held for days needs futures or options. A bearish non-F&O stock stops at
  the structure step with "shorting overnight needs F&O". Intraday shorts (MIS) work for any
  stock.
- **Switches:** **Short setups** in the sidebar's Scan section (swing) and Intraday section. Both
  are on by default. Turn one off for long-only.
- **Everywhere it shows:** a **Long** / **Short** tag (an up or down triangle) next to the symbol in every table,
  card and panel; *below* / *above* in "To zone"; *Sell* in the trade planner; **SHORT** in
  Telegram alerts.

### 3.7 Paper trades

Every swing entry alert is followed as a **paper trade**: what the alert would have done if you
had taken it exactly as given. Nothing is ever ordered. It works like intraday mode's journal
(5.5) without the square-off: a swing trade runs until its target or its stop, however many
days that takes.

- **In:** at the CHoCH candle's close, the alert's entry.
- **Out at the target** when a candle's high reaches it (its low, for a short). Filled at the target.
- **Out at the stop** when a candle's low reaches it (its high, for a short). Filled at the stop,
  or at the candle's open when price gapped through it overnight, so a gap can cost more than 1R.
- **Both in one candle** counts as stopped. A candle can't say which came first, so the worse case
  is assumed.
- **How it's followed:** on the watcher's passes, on completed 15-minute candles, each examined
  once. If the app was off for longer than 15-minute candles reach back (about a week), daily
  candles fill the gap and the result is marked **≈**. The entry day's own daily candle is never
  used: it also holds the price action from before the entry.
- **One trade per stock.** While a stock has a paper trade open, scans don't arm another zone on
  it. Once the trade closes its setup is spent (3.3), and a new setup on the stock is armed as usual.
- **Where results show:** the Paper trades panel (4.12), the alert log, the live board while the
  trade is open, and Telegram (section 9).

Alerts from before paper trading started (6 Oct 2026) stay as they were: triggered, not followed.

---

## 4. Using the dashboard

### 4.1 Layout

- **Top bar:** the search box (it opens the **command palette**, 4.8; **Ctrl K** / **⌘K** or **/** from anywhere), the
  **Controls** button (folds or opens the sidebar), **Legend**, **Guide**, and the **market status**: open or closed, the IST time, and the data
  feed (*Real-time* from Dhan or *Delayed* from yfinance). It is the only place the app shows these.
  It turns amber when Dhan is set up but not delivering, or when the market is open on delayed prices.
  Click it for the details (session hours, the token's expiry, when the ticker last read prices) and
  **Connect / Reconnect / Disconnect Dhan** (hidden with automatic login). On a phone it shortens
  to the dot and "Market open/closed".
- **Left sidebar:** everything you control, in six sections: Scan, Watcher, Intraday, Options,
  Telegram and Rules (4.2). On a computer it folds to a slim rail of section icons (the **‹** at its top, or **Controls**);
  a rail icon opens it again at that section, and the choice is remembered. On a tablet or phone it is a panel that slides in
  from the left over the page (**Controls**), closed by its **×**, a tap outside it, or **Esc**.
- **Mode switch** (top of the main column): **Swing**, **Intraday**, **Options**, **News**, **Smart money**, **Demo funds** or **Performance**.
  A filled pill slides to the tab you pick; your choice is remembered in this browser. Small count chips: Swing and Intraday,
  open trades (or, with none, watched zones; Intraday also "live"); Options, open ideas; News, today's NSE filings on the stocks
  you follow (4.14); Demo funds, open positions; Performance, a pause mark or a dot when the autopilot is paused or waiting.
- **Main column, Swing:** the live board, the protocol funnel, setups, paper trades, zone watcher,
  price action signals and the alert log.
- **Main column, Intraday:** see 5.6.
- **Stock panel:** slides in from the right when you click any stock (4.6). From the Smart money tab it opens on that stock's
  smart-money record instead (4.15).
- **Long tables page:** every long table (setups, zones, signals, alerts; the intraday zones, setups and journal; the options
  tables; Your stocks and the deals in Smart money; every Demo funds table) shows 10 rows at a time, with **« ‹ 1 2 3 › »** and a
  **Rows** choice (10 / 25 / 50 / 100, remembered per table) underneath once it has more than 10. The News list pages its cards
  in twelves (12 / 24 / 48 / 96, 24 to start). The page you are on survives the dashboard's refresh; changing a filter or sort
  goes back to page 1. The zone watcher keeps closed zones for 14 days, and the alert and signal logs their last 300.

### 4.2 Sidebar

Six sections. Each has a small header, sometimes with one text action on the right, and rows
with the switch on the right.

| Section | Control | What it does |
|---|---|---|
| **Scan** | **Universe** dropdown | Which list the scan screens, with its size. Also used by scheduled scans. See *Universes* below. |
| | **Refresh** (next to "NSE lists from …") | Re-downloads every list from NSE (about 15 s). Do this after index rebalances (March and September) or F&O changes. A list that fails to download keeps its previous members. |
| | **Run scan / Stop scan** | Starts a scan now, with a progress bar while candles download. Stopping keeps whatever was analysed. |
| | **Auto-scan** + time | Runs the scan every weekday at this IST time. 16:15 lets the closing candle settle. If it fails (no data from yfinance, say), it is tried again every 15 minutes, up to four times. The time can only be edited while the switch is on. |
| | **Short setups** | Also finds shorts in bearish F&O stocks (3.6). On by default. |
| **Watcher** | **15m zone watch** | Turns the market-hours watcher on or off. Its subtitle shows when the next check is due. |
| | **News desk** | Turns the news desk on or off (4.14): NSE filings and media headlines for the stocks you follow, every 15 minutes in the session. |
| | **Check now** (header) | Runs one watcher pass immediately, at any time. Outside market hours it reads the last two sessions. |
| **Intraday** | **Universe** dropdown | The intraday universe, set separately from the swing one. Nifty 50 by default; F&O gives more zones. |
| | **Intraday mode** | Turns the intraday scans and checks on or off. Its subtitle shows the session phase or the next scan. |
| | **Short setups** | Also finds intraday shorts (any stock). On by default. |
| | **Intraday defaults / Tune…** | Whether the intraday rules are changed from the defaults, and the intraday tuner (5.8). |
| | **Scan now** (header) | A 15m scan now. Outside 09:30–14:30 it is a preview that sets no zones. |
| **Options** | **Option chain scanner** | Reads the option chains (5 indices every 3 minutes, the options stocks in rotation) during market hours (6). Needs Dhan. |
| | **Idea alerts** | Telegram for each option-buying idea and its result. |
| | **Level alerts** | Telegram when an index's OI support or resistance moves and holds for two reads. Off by default. |
| | **Stocks / Edit…** | Which F&O stocks have their chains read (6). |
| | **Idea defaults / Tune…** | Whether the idea rules are changed from the defaults, and their tuner. |
| | **Refresh now** (header) | Fetches every chain at once (about a minute). |
| **Telegram** | **Entry alerts** | Entry alerts (swing and intraday), plus the evening summary after scheduled scans. |
| | **Pattern alerts** | 15-minute candlestick patterns that form inside a zone (4.9). Off by default. |
| | **Filing alerts** | A message when a new NSE filing (not housekeeping) lands for a stock with an open trade or a tapped zone (4.14). On by default. |
| | **Send test** (header) | Sends a test message. Until credentials are set (section 9), the section shows "Not set up yet · how to" instead, and both switches are disabled. |
| **Rules** | Summary line | *Protocol defaults*, or *Custom · N changed from the protocol* (in amber) once you've saved tuner changes. |
| | **Show all** | Expands the full list of thresholds, with changed values highlighted, and a link to section 11. |
| | **Tune…** (header) | Opens the rule tuner (4.7). |

**Universes.** The dropdown is grouped:

| Group | Universes |
|---|---|
| **Protocol (Step 1)** | Nifty 100 · F&O stocks · Nifty 100 + F&O. These are what the doc prescribes. |
| **Broad market** | Nifty 50 · Next 50 · 200 · 500 · Midcap 100 · Midcap 150 · Smallcap 250 · MidSmallcap 400 |
| **Sectors** | Bank · PSU Bank · Financial Services · IT · Pharma · Healthcare · Auto · FMCG · Metal · Energy · Oil & Gas · Realty · Media · Consumer Durables |
| **Themes** | CPSE · PSE · Infrastructure · India Consumption · MNC |

- **Source:** every list is the official constituents file from niftyindices.com.
- **Wider universes are safe to scan.** The doc limits Step 1 to Nifty 100 / F&O because SMC
  footprints are unreliable in illiquid stocks. Step 2 (10 lakh shares a day) still applies, so
  a small-cap universe screens down to its liquid names: Nifty 500 → about 260 liquid stocks.
- **Scan time** grows with the list: a sector takes a few seconds, Nifty 100 under a minute, and
  Nifty 500 about a minute and a half.
- **Zones follow the last completed scan.** Scanning a different universe expires zones for
  stocks that aren't in its results (zones you set yourself are kept). Switch back to your usual
  universe before the evening scan if you want its zones watched.

### 4.3 Protocol funnel

One card per step, showing how many stocks of the last scan **passed** that step, with a red
**−N here** for how many stopped there.

**Click any card** to see exactly which stocks stopped at that step and why, for example:

- `AXISBANK — lower high and lower low — shorting overnight needs F&O, and this is not an F&O stock`
- `BHARTIARTL — closed below the last higher low`
- `ASIANPAINT — avg volume 753,898 < 1,000,000`

Click a stock in that list to open its chart. This is the quickest answer to "why isn't X on
my list?".

### 4.4 Setups table

| Column | Meaning |
|---|---|
| Stock | Symbol, **Long** or **Short**, and company name. |
| Close | Last daily close. |
| Zone (OB / FVG) | The price range the watcher waits for. |
| To zone | How far price is from the zone (%): *above* it for a long, *below* it for a short. Or **in zone**. |
| Leg position | How far price has come back from the leg's origin: 0% is the origin, 50% is equilibrium; it must be under 50%. |
| Target | The leg's far end: its high (BSL) for a long, its low (SSL) for a short. |
| R:R\* | Provisional R:R, with the stop beyond the leg origin. The real R:R is usually better. |
| Score | 0–100, for ordering only. |
| Watcher | The zone's status: watching, tapped, triggered or rejected. |

- **Filters:** **All / In zone / Waiting / Long / Short**.
- **Sorting:** click a column header to sort descending; click again for ascending, and a third
  time to return to the default grouping.
- **Details:** click a row to open the stock panel.

The filter and sort are remembered in your browser. On a long list, a page shows the group
heading (*In the zone now* / *Waiting*) again wherever that group appears on it.

### 4.5 Zone watcher and alerts

- **Zone watcher:** every open zone, plus recently closed ones. *Latest* shows the watcher's last
  finding (e.g. "in the zone, waiting for a sweep and a 15m CHoCH") and when it checked.
  **Dismiss** stops watching a zone, and later scans don't bring the same setup back (3.3).
- **Alerts:** every CHoCH found. An entry whose CHoCH candle is from an earlier day (the
  watcher was off when it happened) names that day in the alert, e.g. "at 11:45 IST on 05 Oct".
  - **Entries** (R:R ≥ 1:3) show entry, stop, target and R:R in bold, and say whether Telegram
    received them.
  - **Rejected** ones say why, e.g. "CHoCH, but R:R 1:2.9 is under 1:3 — skipped".
  - **Results** of paper trades (3.7) say "Paper trade: Target hit …" or "Stopped out …", with
    the result in R.

### 4.6 The stock panel

Opens from any table row, funnel list, tuner result or search result. From top to bottom:

**Chart controls**

- **3M / 6M / 1Y:** the visible range.
- **Overlay toggles:**
  - Order block (blue box)
  - FVG (amber boxes)
  - Liquidity (dotted BSL / SSL lines)
  - Target / EQ / stop: on the daily chart the leg's target, provisional stop and 50 % line (or, when today's screen has
    no leg, the watched zone's target, entry and stop); on the 15m chart the target, an open trade's entry, stop and exit
    (or the provisional stop), and the daily leg's 50 % line. A level within 6 % of price widens the chart to show it; one
    further away is marked at the chart's top or bottom edge with its price and distance, rather than left off.
  - Watch zone (dashed box)
- **Magnet** (or press **M**): snaps the crosshair to the hovered candle's open, high, low or
  close, whichever is nearest the pointer. The price tag then shows that exact level with its
  letter (e.g. **H 337.40**), and the tooltip underlines it. Turn it off for a free crosshair
  that reads any price. On by default.
- **Indicators** (gear): the indicator settings (4.11).
- **Zoom, move and stretch** (every candle chart):
  - **Drag the chart** to move it: sideways through time, up and down through price. A mostly sideways drag moves time only.
  - **Drag the price scale** (the prices on the right) up to stretch the candles taller, down to squeeze them.
  - **Drag the time axis** (the dates along the bottom) to show fewer or more candles.
  - **Mouse wheel:** zooms time around the pointer, or the price over the price scale. In the stock panel it takes Ctrl (or a
    trackpad pinch), so the wheel still scrolls the panel; in the expanded chart it always zooms.
  - **− / + / Fit** buttons, or the **−**, **+** and **0** keys. **Double-click** the chart (or **Fit**) to fit everything again.
  - The price scale fits the visible candles until you move or stretch it. Overlays, markers and indicators follow the zoom;
    cumulative delta is summed over the whole series, so a zoomed-in window doesn't restart it. A zoom pinned to the latest
    candle stays pinned as new candles arrive, and resets when you open another stock, timeframe or range.
  - On a phone, sideways swipes move the chart in the stock panel and up-and-down swipes scroll the panel; in the expanded
    chart every gesture moves the chart.
- **Price scale:** round prices on the right with faint gridlines, and the current price in a tag (green if the last candle closed
  up, red if down) with a dashed line across the chart. Scale labels make way for the level labels (Target, Stop and the rest).
- **Expand** (or press **F**): the chart in a large window over the page, redrawn at its size (not stretched) and again whenever
  the window resizes or a phone rotates. All the chart controls come along. The **×**, **F**, **Esc** or a click outside closes it.

Your choices are remembered.

**Chart**

- Daily candles with the overlays above.
- **Hover** (or tap on a phone) for a crosshair showing the date, open/high/low/close, the day's
  % change, and the price at the cursor.
- Labels that would overlap are hidden; their lines stay.

**Key levels:** close, zone, equilibrium, target, provisional stop and provisional R:R.

**Trade planner:** works out a position size from your risk.

- **Inputs:** capital (₹), risk per trade (%), entry, stop, target. A stop above the entry makes
  it a short: the quantity reads "Sell".
- **Prefilled:**
  - from the **15-minute trigger** if the zone has triggered;
  - otherwise from the **provisional plan**;
  - otherwise with the last close as entry.
- **Outputs:**
  - quantity (capped by your capital, with a note if so);
  - position size and its % of capital;
  - ₹ at risk if stopped;
  - ₹ at target;
  - R:R, marked with a tick or a cross against the 1:3 rule.
- Capital and risk % are remembered in your browser.

**Watch this zone:** Step 9 by hand.

- **Inputs:** zone low, zone high and target. Prefilled from the order block / FVG zone, or
  from the open zone if the stock is already watched.
- **Watch zone** (or **Update zone**) adds it to the watcher. Updating a zone restarts its
  watch: only price action after the new levels were set counts.
- Use it to:
  - watch a stock the scan didn't pick;
  - adjust a zone you read differently;
  - keep watching a stock after a scan would have dropped it.
- Zones set here are marked "your levels" and are never expired by a scan.
- The rule is `0 < zone low < zone high`, with the target **above** the zone for a long or
  **below** it for a short. The side follows from the target.

**The 12 steps:** a checklist mirroring the doc.

**News and smart money** close the panel: the stock's filings and headlines of the last 30 days (4.14), and its last session's
delivery read with its bulk and block deals (4.15).

- a **tick**: passed; a **cross**: failed (with the reason); **…** waiting (e.g. for the 15-minute CHoCH),
  **–** not applicable.
- Steps 10–12 fill in from the watcher once the zone triggers.

### 4.7 Rule tuner

Opened with **Tune…** in the sidebar's Rules section. It answers "what if I relaxed or tightened a rule?"
without committing to anything. Intraday mode has its own tuner, opened from **Tune…** in the
sidebar's Intraday section (5.8); it works the same way.

- **Left side:** one slider per threshold, each with the doc step it controls. A **●** marks
  values that differ from the rules currently in force.
- **Right side, live:**
  - a status line: setup count, stocks screened, universe;
  - a bar per funnel step;
  - **setups under these rules**, with **+ added** / ~~removed~~ chips compared with your last
    scan.
- **Click a setup** to open its chart analysed under the what-if rules. The panel's subtitle
  says "what-if rules".

A re-screen takes about a second. It uses the candles already downloaded and never changes the
saved scan.

| Button | Effect |
|---|---|
| **Save as my rules** | Makes these the rules for every future scan and watcher check. |
| **Save & rescan** | Saves, then runs a scan straight away. |
| **Back to saved** | Resets the sliders to the rules currently in force. |
| **Protocol defaults** | Resets the sliders to the doc's values (the `.env` / built-in defaults). |

Saved values are stored in the database and sit on top of any `SMC_*` values in `.env` (10.3).
Saving the protocol defaults clears the overrides.

### 4.8 Search and the command palette

**Ctrl K** (**⌘K** on a Mac), **/**, or a click in the top bar's search box opens the command palette: one search over every
universe stock, the option indices, the tabs and the app's actions. Results are ranked by how well they match (symbol, then
name, then industry; better-known names first on a tie), what you opened recently, and what is live (an open trade, a
watched zone, a demo position). Each stock shows its list, F&O, its status and, if it is on the board, its price and change.

With nothing typed it shows what is open now, your recent picks and the other tabs. It also reads a few phrases:

| Type | Does |
|---|---|
| `VEDL` | the stock, and everything you can open for it: daily and 15m chart, intraday view, option chain (F&O), smart money, news |
| `VEDL 15m` · `VEDL d` | that chart straight away (`15`, `daily`, `chart`) |
| `VEDL options` · `nifty oc` | the option chain (`oc`, `chain`) |
| `VEDL deals` | its smart money (`smart`, `delivery`, `bulk`, `block`) |
| `VEDL news` · `VEDL intraday` | its filings and headlines; its intraday setup |
| `add 50k` · `withdraw 1L` | demo funds in or out (`5L`, `2.5 lakh`, `1cr`, `1,00,000`) |
| `gainers` · `losers` · `in zone` · `board` · `open` | lists from the live board and what is open |
| `pharma` | stocks by industry |
| `2500*1.2` | a calculation; Enter copies the result |
| `scan`, `settings`, `legend`, … | any action or tab by name |
| `?` | this list, in the palette |

A symbol outside the scanned lists is offered as "Analyse … anyway". **Keys:** **↑ / ↓** move, **Enter** runs, **Esc**
closes. **Esc** also closes the expanded chart, then the stock panel, then any open dialog. With a stock panel open, **F**
opens or closes the expanded chart, **M** toggles the magnet, and **+**, **−** and **0** zoom.

### 4.9 Live board and price action

**Live board** (top of the dashboard): one card per watched stock and per open paper trade (3.7),
in this order: open trades, then tapped, then nearest to its zone. An open trade's card adds its
entry, stop and running R at the current price. (Alerts from before paper trading stay on the
board for 14 days after they triggered.)

Each card reads top to bottom:

- the symbol with a triangle for its side (green up: long, red down: short; hover for the word) and its state (watching,
  tapped, triggered, open trade), with the company name under it;
- the current price and today's change, and the news badge (4.14) when the stock has news today;
- a small chart of today's 15-minute candles, with the zone shaded and yesterday's close dashed;
- for an open paper trade, its entry, stop and running R in a shaded strip;
- the zone, how far price is from it, the target and today's range, each labelled;
- today's 15-minute patterns as small chips (**●** = formed inside the zone).

A card is outlined amber when price is in the zone, and green once the zone has triggered.
Clicking a card opens the stock on its 15-minute chart.

The board refreshes every minute while the market is open; when it's closed, it shows the last
session. Prices come from yfinance 15-minute candles, so they can lag a few minutes. The last
candle may still be forming; that is what makes its close the current price.

**Candlestick patterns.** NiftyWhale looks for four bullish price-action patterns on long zones,
and their bearish twins on short zones:

| Pattern | Short | What it looks like |
|---|---|---|
| Bullish engulfing | BE | A down candle, then an up candle whose body covers it completely. |
| Hammer / pin bar | H | A long lower wick (≥ 2× the body and > half the range) and little above the body: sellers pushed down, buyers took it all back. |
| Inside-bar breakout | IB | A candle inside the one before it, then an up close above its high. |
| Morning star | MS | A strong down candle, a small-bodied pause, then an up candle closing back above the middle of the first. |
| Bearish engulfing | BrE | An up candle, then a down candle whose body covers it (after a short rise). |
| Shooting star | SS | A long upper wick and little below the body: buyers pushed up, sellers took it all back. |
| Inside-bar breakdown | IBd | A candle inside the one before it, then a down close below its low. |
| Evening star | ES | A strong up candle, a small pause, then a down candle closing back below the middle of the first. |

The bearish four are the bullish detectors run on the mirrored chart, with the same rules.

- **After a decline:** engulfing, hammer and morning star count only after a short decline (the
  close before the pattern is lower than 3 closes earlier). A hammer in the middle of a rally
  isn't a reversal of anything.
- **Completed candles only:** a candle still forming is never judged.
- **At zone:** a pattern is flagged when its candles traded inside the stock's zone. That is the
  one that matters.

Where patterns show up:

| Where | Timeframe | What |
|---|---|---|
| **Price action signals** table | 15m | Every pattern the watcher sees on a watched stock, once per candle, newest first. |
| Live board chips | 15m | Today's patterns per stock. |
| Setups table, *Price action* column | Daily | Patterns on the last 3 daily candles, recorded by the scan. |
| Stock panel chart | both | ▲ markers under the candle that completed a bullish pattern, ▼ over one that completed a bearish pattern (amber = in the zone). Toggle with **Patterns**. Switch timeframe with **Daily / 15m**; hover a marked candle to see the pattern name. |
| Checklist, step 10 | 15m | "Price action in the zone: …" next to the CHoCH status. |

**Patterns are confirmation, not the entry.** The protocol's entry is still the 15-minute CHoCH
with R:R ≥ 1:3. A hammer or engulfing candle (or, for a short, a shooting star or bearish
engulfing) inside the zone just before the CHoCH makes a
setup stronger; a pattern outside the zone means little.

**Pattern alerts** (sidebar switch, **off** by default): sends a Telegram message for each new
15-minute pattern that forms *inside a zone*. It needs Telegram alerts on. Patterns outside the
zone are only logged, never sent.

### 4.10 Real-time data with Dhan

By default, live prices come from yfinance, which lags a few minutes. With a
[Dhan](https://dhan.co) account, the two places where minutes matter switch to Dhan's real-time
data:

| | yfinance (default) | Dhan connected |
|---|---|---|
| Live board price | Last 15m candle's close, a few minutes old | Real-time quote, refreshed every **10 s** |
| Live prices on the page (open trades, demo, ticker, charts, chain) | none | **Every trade**, pushed over Dhan's live market feed (WebSocket); every second over REST while the feed is down |
| Watcher checks | 90 s after each 15m candle closes | **10 s** after each 15m candle closes |
| 15m candles (watcher, charts) | yfinance | Dhan |
| Evening scan (daily candles) | yfinance | yfinance (completed candles; the delay doesn't matter) |

It's read-only market data. NiftyWhale never calls an order API, so Dhan's static-IP rule for
order placement doesn't apply.

**What you need:** Dhan's **Data API** subscription. It's free if you made 25+ trades in the last
30 days, otherwise ₹499 + tax a month. The market status in the top bar shows if it isn't active.

**Connecting.** Dhan access tokens last 24 hours. Choose one way:

1. **Paste a token** (simplest):
   - On web.dhan.co, go to **My Profile → Access DhanHQ APIs**, and generate a token.
   - In NiftyWhale's top bar, click the market status, then **Connect Dhan**, and paste it.
   - NiftyWhale checks it with Dhan before keeping it. It's stored on the Pi and never sent back
     to the browser.
   - It then **renews the token automatically** before it expires, so you only paste again if
     the app was off when the token lapsed.
2. **Automatic login** (hands-off):
   - Enable TOTP on your Dhan account and keep the secret key (the text behind the QR code).
   - Put these in `.env`, then run `docker compose up -d`:
     ```sh
     DHAN_CLIENT_ID=1100xxxxxx
     DHAN_PIN=123456
     DHAN_TOTP_SECRET=ABCDEFGHIJKLMNOP
     ```
   - NiftyWhale generates a fresh token whenever it needs one.
   - These three values together can log in to your Dhan account, so keep `.env` private (it
     is git-ignored and not copied into the image).
   - After a refused login, it waits 15 minutes before trying again, so a wrong PIN can't lock
     the account.
   - If Dhan refuses a token before it expires (revoked, say because a new one was generated
     elsewhere), it logs in again straight away rather than waiting for the expiry.

**What the sidebar shows:**
- **yfinance · delayed:** Dhan isn't connected.
- **Dhan · real-time:** connected, with the token's expiry.
- **Dhan: …:** connected but not usable, with the reason (subscription inactive, token expired).

In every case except "Dhan · real-time", NiftyWhale falls back to yfinance on its own, and it
also falls back per stock if a Dhan request fails.

Set `NIFTYWHALE_DATA=yfinance` in `.env` to ignore Dhan entirely.

#### Live updates (WebSockets)

Two WebSockets carry everything that moves, so nothing on the page waits for a timer:

1. **Dhan → NiftyWhale.** One connection to Dhan's live market feed (`wss://api-feed.dhan.co`,
   `niftywhale/dhanws.py`) carries every instrument something wants priced: the open positions,
   whatever is on screen, the Charts tab's symbols, the ticker's indices (with the day's open, high
   and low) and, while an option chain is open, its contracts with OI and the best bid and ask.
   Prices arrive as they trade instead of once a second. With nothing wanted, or the market closed,
   the connection is closed after a minute. If it drops, it reconnects with a growing pause; Dhan's
   own reasons for closing it (too many connections on the account, an expired token, no Data API)
   are shown and wait 5 minutes. While it is down, the feed polls Dhan's REST quote once a second as
   before, so nothing stops.
2. **The page → NiftyWhale** (`/ws`, `niftywhale/hub.py`). Each open page keeps one connection, over
   which the server pushes:
   - the live prices of what the page shows, each as it changes;
   - the Charts tab's ticks;
   - the bell's count;
   - the Demo tab's positions and totals, the open option chain and the ticker strip, while shown;
   - **"this changed"**: a database trigger counts every write to the tables the page shows (by
     the app or by the lab's container), and the app's own state (a scan's progress, the market
     opening) is watched too, so the panel that shows it loads again within a second or two. The
     panels' own timers stay only as a safety net (about two minutes).

   A page in the background gets only the bell and "this changed" until it is shown again. The
   connection signs in like the page (the cookie, from the app's own address only, or an API
   token), is checked again every minute and closes when the sign-in ends. At most 12 pages are
   connected at once; a page turned away, or one whose connection fails, polls over HTTP as before
   and tries the socket again later.

`GET /api/live/status` shows both: whether Dhan's feed is connected, how many instruments it
carries, how many the feed is still polling, and how many pages are connected.

### 4.11 Order flow and indicators

Every chart can show four indicators. Each has a toggle next to the overlay toggles (in the
intraday panel, next to 5m / 15m), and your choices are remembered.

| Indicator | Charts | What it shows |
|---|---|---|
| **Delta** (on) | all | A panel under the price. **Bars:** estimated delta per candle (buying minus selling volume), green above zero and red below. **Line:** cumulative delta, restarting at each session's open on intraday charts and running across the visible range on daily ones. Each has its own scale; the latest cumulative value is labelled on the right. |
| **VWAP** (on) | 5m, 15m | Session VWAP: the volume-weighted average of (high + low + close) / 3, anchored at 09:15 each day. |
| **ORB** (on) | 5m, 15m | The opening range, the high-low of the first 15 minutes (adjustable), shaded across the session. **ORB▲ / ORB▼** marks the first candle that *closes* above or below it (the breakout or breakdown). On a 15m chart that's the first candle after the range; a 5m chart can show it sooner. |
| **Bollinger** (off) | all | 20-candle simple average (dashed) ± 2 standard deviations, with the band shaded (both adjustable: see below). |

The crosshair tooltip adds, for that candle, its volume, delta and cumulative delta, VWAP, and
the three Bollinger values.

**Indicator settings** (the **Indicators** button with the gear, next to the chart's other buttons): saved with the app, so
they apply to every chart on every device.

| Setting | Default | Range |
|---|---|---|
| Bollinger length | 20 candles | 5–100 |
| Bollinger width | 2 standard deviations | 1–4 |
| Opening range | 15 minutes | 5, 15, 30 or 60 |
| Delta panel | bars and cumulative line | both, bars only, cumulative only |
| Colours: VWAP, Bollinger bands, opening range, delta up, delta down, cumulative delta | the theme's own (follows light and dark) | any colour (`#rrggbb`) |

The defaults are the values these indicators were built around and are read with everywhere (John Bollinger's own 20 / 2; the
first 15 minutes for the opening range). They draw the chart; no rule trades on them, so there is no trade record to rank one
setting against another. A colour you set is used in light and dark alike; **Theme** next to it hands it back to the theme.
**Reset to defaults** puts them back. VWAP runs from each session's open and has nothing to set.

**Delta is an estimate, and it says so ("Δ est.").** True order-flow delta is volume traded at
the ask minus volume traded at the bid, which needs every trade tagged with the side that
started it. Dhan's and yfinance's candle data has only open, high, low, close and volume, so
NiftyWhale estimates it:

- **On intraday charts** it fetches **1-minute** candles and splits each minute's volume by
  where that minute closed within its range: at the high, all buying; at the low, all selling;
  in the middle, balanced. A minute with no range counts as buying or selling by whether it
  closed above or below the minute before. The minutes are then summed into each 5m or 15m
  candle. This tracks real delta's direction and turning points well; its size is softer.
- **On daily charts** each day is split the same way from its own candle, which is only a
  coarse hint. The panel then says "Δ est. (from bars)".

Because 5m and 15m delta come from the same minutes, the two charts always agree on a session's
cumulative delta, and so does VWAP.

How these sit with the protocol: they are context, not rules. Nothing in the 12 steps uses
them, and no alert depends on them. Typical reads alongside a setup:

- a zone tap where cumulative delta stops falling (sellers drying up);
- a CHoCH candle with strong positive delta;
- a long that triggers above VWAP rather than below it.

### 4.12 Paper trades

Below the setups, four figures and the journal of every swing paper trade (3.7):

| Figure | What it shows |
|---|---|
| **Open** | Trades still running, and their running R together. |
| **Closed** | Winners out of closed trades, and how many were stopped out. |
| **Win rate** | Winners as a share of closed trades. |
| **Total R** | The closed trades' results added up, and the average per trade. |

The journal lists them newest first: entry time, stock and side, entry, stop, target, the
result (*open*, *target* or *stop*, with **≈** when daily candles decided part of it), the exit
and its time (or the price now, while open) and R. Click a row to open the stock. After a few
weeks it shows how the swing alerts actually perform on your universe.

### 4.13 Index ticker

A strip under the top bar scrolls through 35 indices:

- **Headline:** Nifty 50, Sensex, Bank Nifty, Fin Nifty and Midcap Select.
- **Volatility and pre-market:** India VIX and GIFT Nifty.
- **Broad market:** Next 50, 100, 500, Midcap 100 and 150, Smallcap 100 and 250.
- **Sectors:** every Nifty sector, plus Bankex.
- **Themes:** Capital Markets, Defence, CPSE, PSE, Infra, Consumption and MNC.

Each index shows:

- its last price;
- its change on the previous session's close: green up, red down. India VIX is coloured the other way, since a rising VIX is bad news;
- a **day-position bar**: a dot placed between the day's low (left) and high (right).

Hover an index for open, high, low, previous close and its exact position in the range. Hovering also pauses the strip.

The buttons at the strip's right end:

- **Pause / play** (the first button) pauses or plays it. While it's paused you can scroll or swipe it by hand.
- **Settings** (the gear button) controls:
  - **Speed:** slow, normal or fast.
  - **Change:** shown as %, points or both.
  - **Day-range bar:** shown or hidden.
  - **Pause on hover:** on or off.
  - **Indices:** which to show, with All / None per group (Headline, Volatility, Broad market, Sectors, Themes). **Reset to defaults** turns everything back on.
- **Collapse** (the last button) folds it away. A **Ticker** button then appears in the top bar to bring it back, and nothing is fetched while it's collapsed.

All of these are remembered in your browser, so a phone and a laptop can each have their own.

- **GIFT Nifty** trades almost round the clock, so it shows its premium or discount to Nifty 50 instead of a range. It's a hint of the next open.
- **Option chains:** click Nifty, Bank Nifty, Fin Nifty, Midcap Select or Sensex to open its option chain.
- **LIVE / CLOSED:** the label on the left says whether the market is open.

Prices come from Dhan, every 5 seconds during the session, with one request for all 35 indices however
many browsers are open. Outside the session they refresh every 5 minutes. Dhan's quote doesn't carry
the previous close, so it comes from daily candles, read once a session. Without Dhan the strip is hidden.
With *reduce motion* switched on in your system settings, the strip stays still and scrolls by hand.

---

#### Market mood

The page's background follows the market's mood, worked out from the ticker's own rows (`ticker.mood`):

| Input | Weight |
|---|---|
| Nifty 50's change today | half: ±0.8 % is already a strong day |
| Breadth: indices up minus down, out of all of them (not GIFT Nifty or VIX) | about a third |
| India VIX's change | the rest: fear rising pulls the mood down |

The score runs from −1 to +1 and reads as **Fearful**, **Nervous**, **Calm** (or **Uneasy** when the VIX is high or
jumping), **Upbeat** or **Euphoric**. It tints the page from rose (fearful) through slate blue (calm) to green
(euphoric), with amber creeping in as the VIX rises. The tint shows as a soft wash at both ends of the top bar,
a thin line under it and glows at the page's edges. It's stronger the stronger the mood, dimmer after the close
(the last session's mood), and fades over a few seconds as the mood moves. The market pill's tooltip and
**Market status** (click the pill) say what the mood is made of; the switch there turns the tint off. It needs Dhan,
like the ticker.

### 4.14 News desk

The **News** tab (beside Swing, Intraday, Options and Performance) is the news desk: what has been said about the stocks you are
following. Its tab shows how many NSE filings came in today. It covers:

- every stock on the board (open paper trades, tapped and watched zones);
- today's intraday zones;
- the setups of the latest swing and intraday scans.

**Sources:**

| Source | What | How it is fetched |
|---|---|---|
| **NSE filings** | The company's own announcements to the exchange: results and board meetings, orders, credit ratings, fund raising, pledges, management changes, dividends and record dates, and the exchange's own "spurt in volume" queries | NSE's corporate-announcements feed, the last 30 days per stock |
| **Media** | Headlines from Indian business media (Economic Times, Moneycontrol, Business Standard, Mint, …) | Google News, searched by company name over the last 7 days |

- In the every-stock view each stock shows at most its 15 newest headlines (filings are never folded away);
  a link opens the rest.
- A media headline is kept only if it names the company: its name without "Ltd." (*Tata Chemicals*, not just *Tata*), or its NSE
  symbol as a word of its own. The same story syndicated across several sites is kept once.
- Housekeeping filings (trading-window closures, depository certificates, newspaper copies, ESOP allotments, …) are hidden by
  default; tick **Housekeeping filings** to see them.
- Each item carries keyword **tags** (results, board meeting, order, dividend, fundraise, rating, broker call, stake change,
  pledge, regulatory, management, M&amp;A, volume query, business update). They say what an item is about.
- Each item also carries a **tone**: FinBERT's read of the words (below). Housekeeping filings are never read.
- Headlines quoting an overseas listing's price ("… to EUR 24.80", ADR, NYSE, Frankfurt) are dropped; a deal size in dollars
  ("raises $2 billion") stays.
- BSE refuses requests from the Pi, and yfinance's news is thin for NSE stocks, so neither is used.

**When it updates:** every 15 minutes during the session for the stocks on the board, today's intraday zones and open trades
(option ideas included); every hour for all of them, setups and the options mode's stocks included (the only pass outside market
hours). Quiet from midnight to 06:00. **Refresh now** fetches at once.
Requests are spaced 2 seconds apart per site. Items are kept for 45 days.

**The list** is a grid of cards, three across on a wide screen, two on a medium one and one on a phone, newest first, paged
(12, 24, 48 or 96 a page; 24 to start). A filing has a teal edge; a long headline is cut at three lines (the link opens it
whole). Changing the stock, the source, the tone or the housekeeping switch goes back to page 1; a refresh keeps the page.

**Elsewhere on the dashboard:**

- **Board cards** show a small newspaper badge with today's count, amber when an NSE filing (not housekeeping) came in today.
- **The stock panel** (4.6) lists the stock's filings and headlines from the last 30 days. For a stock the desk doesn't follow,
  opening the panel fetches them once.
- **Paper trades** record the news count and the filing count of the 24 hours before the entry. The Performance tab's
  **News in the 24 h before entry** breakdown (7.2) then shows whether trades with fresh news did better or worse. It covers live
  trades only: the backtester has no news history.

**Tone (FinBERT).** A finance-trained language model (ProsusAI's FinBERT, the 8-bit ONNX export) runs on the Pi in its own
container, `niftywhale-nlp` (one core at most, low priority, ~310 MB of memory, no port outside Docker). It reads each headline,
or a filing's category with the exchange's summary of it, and labels it **positive**, **neutral** or **negative**, with a score
from −1 to +1 (P(positive) − P(negative)). It reads the words, not the market's expectations: "profit falls 5%" is negative even
when the street feared worse. Up to 150 items are read per pass, about a third of a second each; if the scorer is down, items
wait for the next pass.

**Reaction.** For each item, how the stock moved afterwards **against Nifty**, from the last price before the item:

| Figure | Measured to |
|---|---|
| **1 h** | an hour after the item could first be traded on (out of hours: from the next open) |
| **close** | that session's close |
| **next day** | the next session's close |

15-minute candles are used while they reach back to the item (about a week); older items get the close and next-day figures from
daily closes (marked *daily closes*; the base is then the previous close even for news that came mid-session). Items are measured
oldest first, up to 500 a pass.

**On the News tab:**

- **Tone by stock · 24 h:** each followed stock's average tone over the last day, with its item count; click one to see its news.
- **Does the tone move prices?** The stocks' average move against Nifty after positive, neutral and negative items over the last
  30 days, at each horizon. Grey cells have under 10 items. It takes a few weeks of items before these mean much.
- Filters for the tone, next to the source filter.

**In Performance:** every swing and intraday trade, and every option idea on a stock, records the news of the 24 hours before its
entry: how many items, how many filings, how many read positive and negative, and the average tone. Two breakdowns use them:
**News in the 24 h before entry** and **News tone vs the trade** (good news on a long or bad news on a short is *with the trade*;
average tone within ±0.15 is *neutral news*). Both cover live trades only: the backtester has no news history.

**Telegram filing alerts** (sidebar → Telegram → **Filing alerts**, on by default): a message when a new NSE filing lands for a
stock with an **open paper trade** or a **tapped zone**, published in the last 3 hours, and not housekeeping. The message
says how FinBERT reads it. For example, a board
meeting to approve results while a swing trade is open. Media headlines are never sent: there are too many.

Turn the whole desk off under sidebar → Watcher → **News desk**.

### 4.15 Smart money

The **Smart money** tab shows what institutions did, from the files NSE publishes after each session. Nothing in it is real time,
and nothing names who is buying today: India publishes no trader identities during the session. It is the record after the
close, context for the next session.

| Source | What it tells you | History |
|---|---|---|
| **FII/DII cash flows** | What FIIs/FPIs and DIIs bought, sold and net in the cash market (₹ cr) | NSE publishes the latest day only: recorded each evening from the first run |
| **Participant-wise open interest** | Long and short contracts in index and stock futures and options for Client (retail), DII, FII and Pro (brokers' own books) | Dated files: the last 25 sessions are fetched on the first run |
| **Bulk deals** | A named client trading 0.5 % or more of a company's shares in a session | Latest session only: recorded from the first run |
| **Block deals** | Trades of ₹10 cr or more in the block-deal window | Latest session only, likewise |
| **Delivery** | Every stock's traded and delivered quantity (NSE's full bhavcopy) | Dated files: the last 25 sessions are fetched on the first run |

**On the tab:**

- **Institutional cash flows:** a card each for FII/FPI and DII (the latest day's net, bought against sold, the 5- and
  20-session totals) beside a two-lane chart of daily nets (buying up in green, selling down in red, the last 20 session
  slots; hover a session for its figures). NSE publishes only the latest day, so the history fills in one evening at a time.
- **Positioning in index derivatives:** the share of FIIs' index-futures positions that are long (*long-heavy* at 60 % or more,
  *short-heavy* at 40 % or less), their net futures contracts and the day's change, their net index calls and puts (bought minus
  written); a chart of every participant's long share by session (FII, DII, Pro, retail; chips switch lines on and off; hover
  for everyone's numbers and the change), and a table of the same with a sparkline and long/short bar each.
- **Your stocks:** every stock the news desk follows (4.14), with the last session's **delivery read** and its institutional
  bulk / block deals of the last 30 days.
- **Bulk & block deals:** the last 10 days: your stocks, institutions only, or all.

**Clicking a row** opens the stock's smart-money panel, led by what was clicked. From a deal: that deal (who, bulk or block,
bought or sold, quantity, price, value), its share of the day's volume, the price since, the day's delivery, and the client's
other deals in the stock. From a stock: its delivery read. Then, for both, a delivery chart (delivery % by session against the
stock's average, the close on its own scale, deal days marked; hover a session), and the stock's deals of 30 days with the
institutions' buys and sells (the deal you opened first and marked; all or institutions only). **Open the trading chart** at
the bottom switches to the usual stock panel.

**Delivery read** (the last session against the stock's own previous 20):

| Read | When |
|---|---|
| **accumulation** | volume 1.5× its average or more, delivery % 1.3× its average or more, stock up: positions taken home |
| **distribution** | the same on a down day: holders delivering out |
| **churn** | volume 1.5× its average or more, delivery % 0.7× its average or less: traded and squared off the same day |
| **normal** | none of these |

**Client types** come from the client's name: *mutual fund*, *insurance*, *fund* (FPIs and other pooled money: offshore vehicles
such as PCC, SE or Pte Ltd, the global houses by name, anything calling itself a fund), *bank*, *prop / quant* (trading and
market-making firms), *company* and *individual*. The first four count as **institutions** in the deal sums. A fund whose name says
nothing about it is filed as a company, so treat the types as a good guess.

**When it updates:** hourly from 17:30 to midnight on weekdays (NSE puts the day's files out between about 17:30 and 20:30),
once at 08:00, once shortly after the app starts, and on **Refresh now**. A dated file that isn't there three days later is taken
for a holiday and not asked for again.

**In the stock panel (4.6):** the last session's delivery read and the stock's deals of the last 30 days.

**In Performance:** every swing and intraday trade, and every option idea on a stock, records what NSE had published before the
entry's day: the stock's delivery read, its institutions' net bulk / block deals of the week before, FII index-futures positioning
and the FII cash flow of the session before. Four breakdowns use them: **Institutional deals in the week before** (*with the
trade* when institutions were buying a long or selling a short), **Delivery the session before**, **FII index futures positioning**
and **FII cash flow the session before**. Live trades only: the backtester has no history of these.

### 4.16 Demo funds

The **Demo funds** tab is a pretend trading account that takes every trade the app takes, at a size you could actually trade,
and keeps the money: what each trade made or lost after charges, what the account is worth, and what is tied up in open
positions. No order is ever placed.

**Starting:** pick an opening balance (₹1 lakh to ₹25 lakh, or any amount under **Add funds**). Every trade the app takes from
that moment is mirrored; trades from before it are left alone. **Add funds** and **Withdraw** move money in and out at any time
(a withdrawal only from free funds, not money in open positions). **Start over** deletes the demo account and opens a new one;
the app's own records (paper trades, journals, Performance) are never touched.

**How each trade is sized** (from the account as it stood at the trade's entry):

| Trade | Instrument | Quantity | Money tied up |
|---|---|---|---|
| Swing long | the shares, delivery (CNC) | whole shares | the full value |
| Swing short | the stock's futures (NRML): a cash short can't be held overnight | whole lots | margin: 20 % of the contract value (an approximation of NSE's SPAN + exposure margin) |
| Intraday, long or short | the shares, intraday (MIS) | whole shares | value ÷ 5 (intraday leverage) |
| Option idea | the option, bought | whole lots | the premium, in full |

- **Lot sizes** are NSE's, from Dhan's instrument list (refreshed weekly).
- **The quantity** risks 1 % of the balance between entry and stop, and no position ties up more than 25 % of it (or more than
  is free).
- **A trade that can't be funded** is listed under **Skipped** with the reason, for example "one lot needs ₹1,07,211 margin,
  more than the ₹44 available".
- **Prices:** entries and exits are the app's own paper prices. A swing short's futures are priced at the stock's price (the
  futures' small premium over the stock is ignored).

**Charges** are worked out line by line, as a contract note shows them, from the published rates (`demo.RATES`, checked
2026-10-08; rates in force from 2026-04-01):

| Charge | Delivery (CNC) | Intraday (MIS) | Futures | Options (on premium) | Source |
|---|---|---|---|---|---|
| Brokerage, an executed order | ₹0 | ₹20 or 0.03 %, the lower | ₹20 | ₹20 | Dhan pricing |
| STT | 0.1 % buy and sell | 0.025 % sell | 0.05 % sell | 0.15 % sell | Finance Act 2026, from 1 Apr 2026 |
| Exchange transaction, a side | 0.0030699 % | 0.0030699 % | 0.0018299 % | 0.0355299 % | NSE circular NSE/FA/73061, from 1 Mar 2026 |
| IPFT, a side | ₹0.01 a crore | ₹0.01 a crore | ₹0.01 a crore | ₹0.01 a crore | the same circular |
| SEBI fee, a side | ₹10 a crore | ₹10 a crore | ₹10 a crore | ₹10 a crore | SEBI |
| Stamp duty, buy only | 0.015 % | 0.003 % | 0.002 % | 0.003 % | Indian Stamp Act |
| DP charge, a scrip sold | ₹12.50 + GST | — | — | — | Dhan |
| GST | 18 % of brokerage, exchange, IPFT, SEBI and DP charges | | | | |

A swing short held across a monthly expiry (the last Tuesday of the month) also pays for the roll: closing the expiring
month and opening the next is one more round trip, costed at the entry price, once per expiry crossed. Option ideas square off
the same day, so options exercise STT never applies. Not modelled: an expiry moved by a holiday, orders split at NSE's freeze
quantity (one order's brokerage is charged per position), Dhan's ₹20 auto square-off fee, and the stock futures' premium. When a
rate changes, update `RATES` and `SCHEDULE_AS_OF`. Charges can be turned off in Settings.

**On the tab:** equity (balance plus open positions at their latest price), free funds, realised P&L after charges, return on
the money put in; the **Balance** chart beside **By mode** (the balance after every closed trade against the money put in,
green above it and red below, a dot per trade coloured won or lost, the peak and the deepest fall; hover a point for the trade,
its net, the balance and how far it is from the money put in; click a trade's point to open its stock); open positions marked to their latest price, every closed
trade with its gross, charges and net (hover the charges for the lines), the skipped trades, **Charges paid** (each charge
summed over the closed trades, with its share of the total and how it is levied) and the **Statement**: a summary bar (opening
balance, funds added and withdrawn, trading P&L, charges, closing balance, and the period it covers), then money in and out,
and for each closed trade its P&L and its charges as separate lines, with the balance after each line, as a broker's ledger books
them. A P&L line says what was traded and how it ended ("bought 2,932 shares @ 215.83 → 218.80 · target hit"; *stop hit*,
*squared off*); a charges line lists the charge it is made of. Every table on the tab pages (10, 25, 50 or 100 rows, remembered per table), newest first; **Download CSV** gives
the whole statement. **Start over** can count trades from now, from the app's first trade, or from a chosen day's open
(09:15), with one opening-balance line; a backdated account is replayed at once. The tab's count is the open positions.

**Settings** (the tab's **Settings** button): risk a trade (0.1–10 %), most in one position (1–100 %), intraday leverage (1–5×),
futures margin (5–50 %), the limits (most positions open at once, all modes together, and most new trades a day;
default 20 each, 0 = no limit: once one is reached the account stops taking trades, listed under Skipped with the reason,
until a position closes or the next day starts), which modes are taken, and charges on or off. Changes apply to trades taken after them.

The account checks for new and closed trades every minute during the session and every ten minutes otherwise. From the command
palette (4.8), `add 50k` or `withdraw 1L` moves demo money directly.

## 5. Intraday mode

Intraday mode runs the same protocol **one timeframe down, inside one session**. The 15-minute
chart takes the place of the daily chart, and 5-minute candles take the place of the 15-minute
trigger. Every trade is over by the end of the day.

|  | Swing mode | Intraday mode |
|---|---|---|
| Directions | Long; short in F&O stocks | Long and short, any stock |
| Structure, order block, discount / premium | Daily candles | 15-minute candles (last 5 sessions) |
| Entry trigger | 15m sweep + CHoCH | 5m sweep + CHoCH |
| Target | The daily leg's far end | The **nearest liquidity pool**: above for a long (previous day's high, today's high, opening-range high, the 15m leg high, an untaken 15m swing high), below for a short (the matching lows) |
| Minimum R:R | 1:3 | 1:2 |
| Scans | Once, in the evening | Every 15 minutes, 09:30–14:15 |
| Trigger checks | Every 15 minutes | Every 5 minutes |
| Trade ends at | Stop or target, days later | Stop, target, or the **15:20 square-off** |

Both modes run side by side. Each has its own zones and alerts, so an intraday scan never
expires a swing zone, or the reverse. Choose which one you're looking at with the
**Swing / Intraday** switch above the main column.

### 5.1 Getting started

1. **Connect Dhan** (4.10). Intraday needs real-time candles: yfinance's delay of a few minutes
   is most of a 5-minute candle. Without Dhan the mode still runs, and its *Data* card warns you.
2. In the sidebar's **Intraday** section, pick a universe (Nifty 50 by default) and make sure
   **Intraday mode** is on (it is by default).
3. That's all. From 09:30 the scans and checks run by themselves. Open the **Intraday** tab to
   follow them.

**Scan now** runs a 15m scan at any time. Outside 09:30–14:30 it is a **preview**: you see the
funnel and setups, but no zones are set.

### 5.2 The session

| Time (IST) | What happens |
|---|---|
| 09:15–09:30 | The first 15-minute candle forms. It is the **opening range**. |
| **09:30** | First 15m scan. Its setups become today's zones. |
| Every 15 min | A new 15m scan, 10 seconds after the candle closes (90 s on yfinance). New setups are added, and zones that are still only *watching* get fresh levels or expire. A watching zone whose levels move starts its watch again from then. The scan runs in the background, so it never delays the 5m check. |
| Every 5 min | A 5m check: zones move along (tap, trigger, fail), and open trades are followed. |
| **14:30** | **Entry cutoff.** A CHoCH whose candle closes after 14:30 is rejected, and zones still waiting expire. |
| **15:20** | **Square-off.** Any trade still open is closed at the 15:15 candle's close, as soon as that candle arrives (on delayed data, a check or two later). Only if it never arrives is the trade closed at the last price seen, at 15:30. |
| 15:30 | Anything left is settled and expired. Intraday scans older than a week are pruned. |

### 5.3 The 15-minute scan (Steps 1–8)

| # | Step | Passes when |
|---|---|---|
| 1 | Universe | In the intraday universe (any list from 4.2; set separately from the swing one). |
| 2 | Liquidity | Average **daily** volume of at least 10 lakh shares over ~3 months. |
| 3 | Range | **Daily** ATR at least 1% of price: room to move within a session. |
| 4 | Structure | The last two **15m** swing highs and lows are HH + HL (a long) or LH + LL (a short); a swing is 2 candles each side. |
| 6 | Order block | Within the last 50 15m candles (~2 sessions), a break of structure whose move covers at least **2 × the 15m ATR**, with an opposite-colour candle at its origin. |
| 6b | Intact | No 15m close through the order block since. |
| 8 | Discount / premium | Price has come back at least halfway into the 15m leg. |
| — | Target + R:R | A liquidity pool at least **0.3%** beyond the entry (closer ones aren't worth the costs), and a provisional R:R of at least 1:1 with the stop beyond the leg's origin. |

Steps 2–3 use daily candles (cached), so only liquid, moving stocks cost a 15m request. The
**target** is the nearest pool, not the farthest. Intraday moves usually stall at the first
pool of resting orders in the way, and those pools are named on every setup:

- **Prev day high / low:** yesterday's high (for a long) or low (for a short).
- **Today's high / low:** the session's extreme so far.
- **Opening range high / low:** the 09:15 candle's high or low.
- **15m leg high / low:** the far end of the move that made the order block.
- **15m swing high / low:** any untaken 15m swing point in between.

### 5.4 The 5-minute trigger (Steps 9–12)

On today's 5-minute candles, completed ones only:

1. **Tap:** a candle trades into the zone **after the zone was set**. Price that was there earlier
   in the day doesn't count.
2. **Sweep:** price takes out a 5m swing low.
3. **CHoCH:** a 5m candle **closes** above the last minor 5m swing high that formed before the sweep.
4. **Plan:**
   - **Entry:** the CHoCH close.
   - **Stop:** 0.05% under the sweep low.
   - **Target:** the zone's pool.
5. **Verdict:**
   - **R:R ≥ 1:2 and the candle closed by 14:30:** an entry alert. The zone becomes an
     **open trade**.
   - **Otherwise:** `rejected`, with the reason. That includes a CHoCH found more than 20
     minutes after its candle closed (say, after a restart): too late to enter.

A sweep **beyond the 15m leg's origin** (below it for a long, above it for a short) is not a
sweep of the zone; it is the structure failing. The zone expires with that reason, and any
CHoCH after it is ignored.

A **short** runs the same steps mirrored: price rallies into the zone, sweeps a 5m swing
**high**, and a 5m candle closes **below** the last minor swing low. The stop sits 0.05% above
the sweep high.

### 5.5 Zones and trades

```
 watching ──► tapped ──► open trade ──┬─► target   (won:  +R:R)
    │            │                    ├─► stop     (lost: −1R)
    │            │                    └─► squared off at 15:20 (whatever it was worth)
    │            └──► rejected  (R:R < 1:2, or after 14:30)
    ├──► expired  (no longer a 15m setup · broke below the leg origin · no CHoCH by 14:30)
    └──► dismissed (by you)
```

- **One trade per stock per day.** Once a stock has triggered, later scans don't open a
  second zone for it.
- **A setup is watched once a day.** A setup you dismissed, whose CHoCH was rejected, or whose
  leg origin broke is not armed again that day (same stock, side and 15m order block). A new
  setup on the stock is.
- **Tapped zones stay.** A later scan doesn't expire a zone that price is already in,
  because the sweep that comes before a CHoCH often breaks the 15m picture for a moment.
- **How the outcome is judged.** Every open trade is followed on 5-minute candles. A candle that
  touches both the stop and the target counts as **stopped**, because 5-minute candles can't
  show which came first, so the worse case is assumed. **R** is the result in units of risk:
  +2.4R means the trade made 2.4 times what it risked.

### 5.6 The Intraday tab

- **Stats strip:**
  - **Session:** the session phase, and when the next check and scan are due.
  - **Data:** real-time or delayed (amber when delayed).
  - **Today:** wins/trades and total R.
  - **Journal:** total R and win rate across every closed intraday trade.
- **Today's zones:** status, zone, target and which pool it is, entry/stop and running R once
  triggered, and the latest note from the check. **Check now** runs a 5m check immediately;
  **Dismiss** stops watching a zone.
- **15m funnel:** like the swing funnel (4.3). Click a step to see who stopped there and why.
- **15m setups:** the latest scan's setups with their target pool and provisional R:R. Click one
  to open it.
- **Trade journal:** every intraday entry alert with its result (target, stop or squared off),
  exit price and time, and R. It fills in by itself as alerts play out, so after a few weeks it
  shows how the mode actually performs on your universe.
- **Stock panel:** a **5m / 15m** chart showing:
  - the 15m zone, the target, and entry, stop and exit once there's a trade;
  - the previous day's high and low, and the opening range.

  **Magnet**, **Expand** and the zoom controls work here as on the swing chart (4.6), and so do the
  **VWAP, ORB, Bollinger and Delta** toggles (4.11). The opening range is drawn as the ORB box
  when that toggle is on.

  Below the chart are the session's levels, and the **Trade planner** prefilled from the trigger
  (or the provisional plan) and checked against 1:2.

The tab shows a count next to its name: the number of open trades ("2 live"), or else the number
of zones being watched.

### 5.7 What to expect

The protocol is strict, and intraday makes it stricter: the zone, the tap, the sweep and the
CHoCH all have to happen in the same session, before 14:30. Replays of real sessions (early
October 2026, Dhan candles) gave:

| Session | Universe | Zones set | Entry alerts |
|---|---|---|---|
| 30 Sep | Nifty 50 | 8 | 0 (3 CHoCHs rejected for R:R under 1:2) |
| 1 Oct | Nifty 50 | 3 | 0 (all three broke below their leg origin) |
| 5 Oct | Nifty 50 | 3 | 1 (HINDALCO, squared off at about +1.6R) |
| 5 Oct | F&O (213 stocks) | 11 | 1 (the same HINDALCO trade) |

With shorts on (on by default), the same replays set more zones on down days. On 1 Oct, for
example, the F&O list set 27 zones, nearly all shorts. One short triggered: HDFCBANK, sold at
717.75 at 12:10 and stopped out at 719.86 (−1R). Most others were rejected because the CHoCH
came too close to (or past) the nearest pool below.

So expect **zero to a couple of alerts a day**, and many days with none. A wider universe (F&O,
Nifty 200) gives more zones. A 15m scan takes about 25 seconds for Nifty 50 and a little over a
minute for F&O with Dhan.

### 5.8 Intraday settings

**Intraday tuner.** **Tune…** in the sidebar's Intraday section opens a rule tuner like the swing
one (4.7), with nine sliders:

- daily volume and daily ATR;
- 15m swing size, "massive" move, look-back for the break, and the discount / premium threshold;
- nearest pool distance;
- provisional and final R:R.

It re-screens the **15m candles of the last intraday scan** in about a second (run **Scan now**
first if there hasn't been one since the app started). The status line says what time those
candles are from. Click a what-if setup to open its chart under those rules. **Save as my
rules** applies them from the next 15m scan and 5m check. **Save & rescan** runs a 15m scan
straight away. Saved values sit on top of `.env`, as in swing mode. The clock (start, cutoff,
square-off) stays in `.env`.

Every threshold can also be set in `.env` as `INTRADAY_<NAME>`:

| Variable | Default | Meaning |
|---|---|---|
| `INTRADAY_MIN_AVG_VOLUME` | 1000000 | Step 2, average daily volume in shares. |
| `INTRADAY_MIN_ATR_PCT` | 1.0 | Step 3, daily ATR as % of price. |
| `INTRADAY_SESSIONS` | 5 | Sessions of 15m candles analysed. |
| `INTRADAY_SWING_LEN` | 2 | Candles each side of a 15m swing point. |
| `INTRADAY_ATR_LEN` | 14 | 15m ATR period. |
| `INTRADAY_BOS_LOOKBACK` | 50 | 15m candles searched for the break of structure. |
| `INTRADAY_DISPLACEMENT_ATR` | 2.0 | "Massive" rally, in 15m ATRs. |
| `INTRADAY_OB_SEARCH` | 5 | Candles back from the origin searched for the order block. |
| `INTRADAY_MAX_DISCOUNT` | 0.5 | Price must be at or below this fraction of the 15m leg. |
| `INTRADAY_PRE_MIN_RR` | 1.0 | Provisional R:R to become a setup. |
| `INTRADAY_MIN_TARGET_PCT` | 0.3 | A pool must be at least this % above the entry to be the target. |
| `INTRADAY_TRIGGER_SWING_LEN` | 2 | Candles each side of a 5m swing point. |
| `INTRADAY_STOP_BUFFER_PCT` | 0.05 | The stop sits this % under the 5m sweep low. |
| `INTRADAY_MIN_RR` | 2.0 | Final R:R for an entry alert. |
| `INTRADAY_START` | 09:30 | First scan (after the opening range). |
| `INTRADAY_NO_ENTRY_AFTER` | 14:30 | Entry cutoff. |
| `INTRADAY_SQUARE_OFF` | 15:20 | Square-off time. |

Restart the container after changing them (`docker compose up -d`). A typo falls back to the
default rather than stopping the app.

---

## 6. Options mode

Options mode reads **option chains** rather than candles: the open interest (OI), volume and implied
volatility (IV) at every strike of an expiry. From them it works out where option writers are building
floors and ceilings, which way the balance is tipping during the day, and whether options are cheap or
dear. When those agree with the underlying's own move, it suggests a simple **option buy** and paper-trades
it like the other modes' alerts.

It needs **Dhan** (section 4.10): option chains come only from Dhan's API. Without it the tab says
"Needs Dhan" and nothing is fetched. Open it with the **Options** switch above the main column.

### 6.1 What it reads, and how often

| | |
|---|---|
| Indices | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and SENSEX: every pass |
| Stocks | 30 liquid F&O stocks by default (edit the list in the sidebar, up to 60). Ten are read each pass, so each is seen about every 9 minutes; a stock with an open idea is read on every pass |
| Passes | Every 3 minutes from 09:15 to 15:30, plus one after the close that reads every chain (the day's closing numbers) |
| Expiries | The nearest expiry. On an expiry day also the next one, because ideas never use an option that expires today |
| Strikes kept | Within 8 % of spot for indices, 15 % for stocks (at most 60) |

Dhan allows one option-chain request every 3 seconds. Options mode has its own queue for that limit, so
it never slows the swing watcher or the intraday checks. A pass takes about 45 seconds.

### 6.2 Reading the chain

Option **writers** (sellers) hold most open interest, and they write where they expect price *not* to go.

- **Support** is the strike with the most **put** OI below spot. **Resistance** is the strike with the most **call** OI above it.
- **PCR** (put-call ratio) is total put OI ÷ total call OI. It's shown as *open → now*. A rising PCR means puts are being written through the day, which is usually bullish.
- **Max pain** is the expiry price at which all option buyers together would collect the least. Price often drifts toward it in the last days before expiry.
- **Buildup** describes what price and OI did together at a strike. The colour shows what that means for the underlying: green is bullish, red is bearish.

| Price | OI | Name | On a call | On a put |
|---|---|---|---|---|
| ↑ | ↑ | Long buildup | bullish (buyers) | bearish |
| ↓ | ↑ | Short buildup | bearish (writers cap it) | bullish (writers floor it) |
| ↑ | ↓ | Short covering | bullish | bearish |
| ↓ | ↓ | Long unwinding | bearish | bullish |

- **OI bias** runs from −1 to +1. It's put OI added minus call OI added, over the 5 strikes either side of the money.
  - Before the session's first read it's measured against yesterday. After that it's measured *since the open*, which is what ideas use.
  - +0.35 or more reads as **bullish**; −0.35 or less as **bearish**.
- **ATM IV** is the average of call and put IV at the strike nearest spot.
  - Its **percentile** compares today with past sessions. It needs 20 sessions of history, which the scanner records from its first day.
  - **Skew** is 25-delta put IV minus call IV. It's normally positive; a jump means traders are paying up for protection.
- **Unusual activity** lists strikes whose OI rose 50 % or more since yesterday, or whose volume is 3× yesterday's.
  - The change must also be at least a tenth of the chain's largest, so tiny far strikes don't flood the list.

### 6.3 Ideas

An idea is an **at-the-money option buy** on the nearest expiry that isn't today. Every condition below must hold:

| Check | Long (buy the call) | Short (buy the put) |
|---|---|---|
| Time | 09:45–14:30 | 09:45–14:30 |
| OI bias since the open | ≥ +0.35 (put writing) | ≤ −0.35 (call writing) |
| Underlying over the last 30 min | up ≥ 0.15 % | down ≥ 0.15 % |
| PCR since the open | not falling | not rising |
| ATM IV percentile | ≤ 80 (once known) | ≤ 80 (once known) |
| Option's bid-ask spread | ≤ 3 % of its price | ≤ 3 % of its price |
| Per underlying | one open idea, at most 2 a day | one open idea, at most 2 a day |

- **Entry** is the option's ask, the price you would pay.
- **Stop** is 25 % below the entry premium and **target** 50 % above it (1:2).
- The idea is also closed early if the underlying crosses the OI level it leaned on: support for a call, resistance for a put.
- Everything is squared off by 15:15.

All thresholds are tunable from **Options → Tune…** in the sidebar.

Ideas are followed on every pass with the chain's own prices:

- **Target:** fills at the target.
- **Stop:** fills at the price seen, which can be worse than the stop, as a real stop-market order would be.
- **Level break and square-off:** exit at the price seen.

The journal shows each idea's result in R and in ₹ for one lot. Lot sizes come from Dhan's instrument list.

Ideas are paper trades: no order is ever placed. They are a starting point for your own reading of the
chain, not signals to follow blindly. Option buying loses to time decay when the move doesn't come.

### 6.4 The Options tab

- **Stats:**
  - **Scanner:** the next pass, or why nothing runs.
  - **Nifty:** its PCR, max pain and bias.
  - **Today:** the day's ideas.
  - **Journal:** every closed idea.
- **Market overview:** one row per underlying for its nearest expiry. It shows spot and the change since the open, OI bias, PCR, max pain, support · resistance, ATM IV with its percentile, skew and expiry. Filter by indices, stocks, bullish or bearish. Hover a row to see why it has no idea right now; click it to open the chain.
- **Unusual activity:** the biggest OI and volume jumps across all underlyings.
- **Ideas & paper trades:** every idea, open or closed, with its result.

The **chain panel** (click any row) shows:

- Headline numbers.
- An OI-by-strike chart: calls red, puts green, a dark tick at yesterday's OI, with spot and max pain marked.
- The day's spot, PCR and ATM IV lines.
- The chain table: calls left, puts right. The at-the-money row is highlighted, and support, resistance and max pain are labelled.
  - ΔOI and buildup are measured since the session's first read once there is one.
- Switch expiries with the buttons at the top. **Refresh** reads that underlying again now.

### 6.5 Options settings

Sidebar, **Options** section:

| Setting | Default | Meaning |
|---|---|---|
| Option chain scanner | on | Read chains during market hours |
| Idea alerts | on | Telegram for each idea and its result (needs Telegram, section 9) |
| Level alerts | off | Telegram when an index's OI support or resistance moves, and holds for two reads |
| Stocks | 30 | Which F&O stocks to read besides the indices |
| Tune… | | Minimum OI bias, 30-minute move, IV percentile cap, spread, stop %, target %, ideas per day |

Storage:

- Full chains are kept for 3 days.
- Each read's headline numbers are kept for 30 days.
- One row per underlying per session (closing spot, ATM IV, PCR, max pain) is kept indefinitely; it's the IV history.

---

## 7. Performance and the autopilot

The **Performance** tab shows three things:

- how the alerts are actually doing;
- how the current rules would have done over years of history;
- the **autopilot**, which tunes the rules from those results.

The autopilot works inside limits you set, and every change it makes can be undone.

### 7.1 What is recorded

Every live entry (swing, intraday and options) is saved with its context:

- the entry time and weekday;
- whether Nifty is above or below its 50-day average;
- India VIX at the previous close;
- the stock's gap that day;
- the R:R and the stop distance;
- the setup's pullback depth, daily ATR % and score;
- how long the zone waited (swing) or which liquidity pool is the target (intraday).

The backtester records the same fields for every replayed trade, so live and backtest results are compared on the same terms.

Options mode also keeps every **candidate**: each read of a chain that came close to an idea, with its numbers. Each night the lab works
out what each candidate would have done for a grid of stops and targets, using that day's later option prices.
Without the candidates, the autopilot could only ever make the idea rules stricter, never learn that looser ones would have done better.

### 7.2 The Performance tab

Pick a mode (Swing, Intraday or Options) and a source:

- **Live paper trades:** the journal, every alert taken exactly as given, with no costs.
- **Backtest:** the latest replay of the current rules over the history.

The tab then shows:

- **Stats:** trades, win rate, total R and average R a trade, profit factor and maximum drawdown.
- **Equity curve:** cumulative R, trade by trade.
- **What stands out:** conditions whose trades did clearly better or worse than the rest.
  - A condition is only called out with 15 or more trades and a difference of more than two standard errors.
  - That's a rough t-test, but it stops a handful of lucky trades from looking like a pattern.
- **Breakdowns:** one card per condition (direction, entry time, Nifty trend, VIX, gap, R:R, pullback depth, ATR, score, stop distance, zone age, target pool, stock trend).
  - Each card shows trades, win rate, average R (the bar) and total R.
  - Buckets under 15 trades are grey.
- **By stock** (backtest): the best and worst stocks. **Recent trades** (live): the last 25 with their context.

### 7.3 The lab and the backtester

Backtests run in a second container, `niftywhale-lab`, built from the same image and sharing the database:

- It has a lower CPU priority than the live app, and one core is always left free.
- Heavy work waits for the market to close.
- It never renews the Dhan token; the live app owns it.

**History** (kept in `var/lab/history`, topped up nightly):

| What | Source | Default |
|---|---|---|
| Daily candles | yfinance (as the live scan) | the live universes, 4+ years |
| 15-minute candles | Dhan, 90 days per request | the swing universe 3 years, the intraday one 1 |
| 5-minute candles | Dhan | the intraday universe, 1 year |
| Nifty 50 and India VIX daily | Dhan | 8 years |

**How a backtest runs.** It calls the live rule code directly, with no reimplementation:

1. **Records** (slow, once, then a day at a time).
   - Swing: the screen on every historical evening.
   - Intraday: the screen at every 15-minute scan from 09:30 to 14:15.
   - The filter thresholds (volume, ATR %, discount depth, provisional R:R) are set to their loosest, and each passing setup is stored with those numbers.
   - Those thresholds never change a zone, stop or target, so filtering the records later gives exactly what stricter rules would have seen.
2. **Simulation** (seconds per rule set). The live zone life cycle is replayed over the records:
   - **Swing:** setups that already played out aren't re-armed; one trade per stock; 10-day zone lifetime; a structure flip; a CHoCH found too late is rejected.
   - **Intraday:** zones refreshed each scan; tapped zones frozen; rejected or broken setups not re-armed that day; no entries after 14:30; square-off.
   - Triggers and trade outcomes come from the live functions and are cached, which is what makes trying hundreds of rule sets possible on a Raspberry Pi.

**What a backtest can't see:**

- the live data's delays and gaps;
- costs and slippage (fills are at the alert's prices, as in the paper journal);
- stocks that left the index since (it uses today's members, which flatters the results).

Use backtests to compare rules, not as a forecast.

**Schedule (IST):**

| When | What |
|---|---|
| First start | Bootstrap: download the history and build the records. About 30 minutes of downloads and 1–2 hours of replay, outside market hours. |
| Weekdays 20:30 | Nightly: top up history and records, backtest the current rules, run the shadow, rollback and pause checks, work out options candidate outcomes. |
| Saturday 06:00 | Walk-forward tuning for each mode. |
| Saturday 09:00 | Weekly Telegram report. |

The **Lab** panel shows what history is on disk and the latest jobs, with buttons for **Run backtest now**, **Tune now** and **Send weekly report**.

Its **Coming up** list shows what the lab runs next, soonest first, with the day, the time (IST) and a countdown:
jobs already queued, then the nightly update, Saturday's tuning and the weekly report (when it is on). Each says
what may change it:
- a job still running then (one job of a kind is open at a time, so a nightly still running at the next 20:30
  means that night's run is skipped);
- for tuning, a mode cooling down after a change (and from which day it is tuned again), a proposal in shadow
  or awaiting approval, or the autopilot off;
- for the nightly update, the modes whose proposal it replays in shadow.

`GET /api/lab` carries the same list as `upcoming`.

### 7.4 The autopilot

**What it can change:** only the filter thresholds the rule tuners already expose. It never touches the strategy or the code, and it never places an order.

| Mode | Thresholds it may move |
|---|---|
| Swing | Min volume, min ATR %, discount ≤, provisional R:R, final R:R, and the three context gates: max Nifty 20-day run, min zone age, max R:R (§11) |
| Intraday | The same five |
| Options | Min OI bias, min 30-minute move, max IV percentile, max spread %, stop %, target % |

**How a change happens:**

1. **Tune (weekly, walk-forward).** The history is cut into 3-month folds (1-month for options).
   - For each fold, rules are tuned on everything before it and judged on the fold itself, data the tuning never saw.
   - The tuner maximises *average R minus one standard error*, which rewards rules that are both profitable and consistent over many trades.
   - A change is proposed only if, out of sample, the tuned rules beat the current ones on all of these:
     - at least +0.05R a trade;
     - more total R;
     - a drawdown not much deeper;
     - enough trades (swing 40, intraday 60, options 40); a rule set also needs 60 swing trades in training to count,
       so the gates can't win by keeping a lucky handful.
   - The proposal is tuned on all the history, and each threshold moves at most 2 tuner steps. The gates move along
     fixed ladders, off first (Nifty run: off, 6, 4, 3, 2, 1, 0, −1, −2 %; zone age: off, 2–6 days; max R:R: off, 15,
     12, 10, 8, 6), so a first change takes a gate at most two rungs in.
2. **Shadow.**
   - The proposal is replayed nightly on new data next to the current rules for 20 sessions (up to 60 if trades are scarce).
   - It's promoted if it does at least as well, and dropped if it's clearly worse.
3. **Promote.** With the autopilot **On — promote by itself**, the new thresholds are saved and the next scan uses them. With **On — wait for my approval**, it asks on Telegram and shows **Apply now** on the tab.
4. **Rollback.** The previous rules come back when both of these hold:
   - live paper trades since a change lose more than the rollback limit (swing 4R, intraday 6R, options 6R);
   - the previous rules would have done better over the same days.
5. **Pause.**
   - When a mode's last 30 closed paper trades lose more than the pause limit (swing 6R, intraday 10R, options 8R), its Telegram entry alerts stop.
   - Alerts still appear on the dashboard and are followed as paper trades.
   - They resume when trades during the pause add up to +2R, when new rules are promoted, or when you press **Resume alerts**.

Every proposal, promotion, drop, rollback, pause and resume is announced on Telegram and listed in the **Change log**. **Undo this change** puts the previous rules back.
Options learning stays dormant until about three months of candidates are recorded; Dhan has no historical option chains to backtest on.

**Why swing has gates (October 2026 study).** All 711 swing trades from three years of nifty100 history, replayed with the
loosest filters, averaged −0.04R (±0.08), sliding from +0.84R in late 2023 to −0.20R in 2026. Exits weren't the problem:
fixed 1–4R targets, breakeven stops, trailing stops and 3–15 day time stops all landed between −0.08R and −0.01R, so the
entries themselves had no edge to capture. The five filter thresholds can't fix that. Walk-forward over the three gates,
on data the tuning never saw, made about +0.07R a trade against −0.13R for the rules of the time, mostly by skipping
pullback longs after Nifty had already run up (buying a stock's dip in an extended market) and by skipping 1:10+ R:R
entries, whose stops sit in the noise. That's a reason to let the autopilot try them, not proof of an edge: the gated
history is small, and costs aren't in the backtest.

### 7.5 The weekly report

Saturday at 09:00, Telegram gets one message per mode:

- the week's closed paper trades and the all-time record;
- the latest backtest with its strongest highlights;
- what the autopilot is doing (testing a proposal, waiting for approval, or why it changed nothing);
- any pause, plus the week's changes.

### 7.6 Autopilot settings

**Performance → Autopilot → Settings…**

| Setting | Default | Meaning |
|---|---|---|
| Autopilot | On — promote by itself | Or *wait for my approval*, or *Off* (no tuning, shadows, rollbacks or pauses) |
| Most a threshold may move per change | 2 | In tuner steps (e.g. final R:R moves at most 1.0 at a time) |
| Shadow run before a change | 20 sessions | |
| Days between changes | 14 | |
| Pause after losing | per mode | Over the last 30 closed paper trades |
| Roll back after losing | per mode | Since the change |
| Limits | the tuner ranges | Per threshold, the lowest and highest value the autopilot may ever use |

By default each mode is backtested and tuned on the universe it trades live, so rules are never tuned on
large caps and then applied to smallcaps. To change what the lab uses, open **Performance → Lab settings**: a
universe and a history length per mode (with an estimate of how long a full intraday replay takes on the Pi) and
the weekly report switch. The same settings can also be sent to `POST /api/settings`:

| Setting | Default |
|---|---|
| `lab_swing_universe` | empty: the live swing universe |
| `lab_intraday_universe` | empty: the live intraday universe |
| `lab_years_swing` | `3` |
| `lab_years_intraday` | `1` (intraday replays are the slow ones: about 0.5 s per stock and session) |
| `weekly_report` | on |

---

## 8. A typical day

| When (IST) | What happens | What you do |
|---|---|---|
| **16:15** (weekdays) | The scan runs. Setups become zones. If Telegram is on, you get a summary: setups in the zone, and setups waiting for a pullback. | Open the dashboard and review the setups on their charts. Dismiss any you disagree with. Add your own with **Watch this zone**. |
| **Evening** | — | Optional: check the funnel for stocks you expected; try the rule tuner. |
| **17:30–24:00**, hourly | The smart-money pass reads what NSE published for the day: bulk and block deals, FII/DII cash, participant positioning, delivery (4.15). | Optional: the Smart money tab. |
| **09:15–15:30** | Every 15 minutes (+90 s) the watcher checks the open zones. A zone traded into turns *tapped*. | Nothing. |
| **On a CHoCH ≥ 1:3** | An entry alert on the dashboard and on Telegram: entry, stop, risk %, target, R:R, the CHoCH level and time, and the low that was swept. | Open the stock, size the trade in the **Trade planner**, and decide yourself. Place the order in your broker. |
| **On a CHoCH < 1:3** | Logged as rejected, nothing sent. | Nothing. This is the doc's "walk away". |
| **On target or stop**, days later | The paper trade closes: a short Telegram message with the exit and the result in R, and a journal entry (4.12). | Close it in your broker if your own order didn't. |
| **Every minute** in the session | The demo account (4.16) takes each new trade at a real size and settles closed ones after charges. | Nothing: watch the Demo funds tab. |

**With intraday mode on**, the session also runs on its own:

| When (IST) | What happens | What you do |
|---|---|---|
| **09:30**, then every 15 min until 14:15 | A 15m scan sets and refreshes today's intraday zones. | Glance at the Intraday tab's zones if you like. |
| Every 5 min | A 5m check: taps, CHoCHs, open trades. | Nothing. |
| **On a 5m CHoCH ≥ 1:2** | An **INTRADAY ENTRY** alert, with the target pool named and the square-off time. | Decide, size it in the planner, place it yourself, and set your stop. |
| **On target or stop** | A short Telegram message with the result in R. The journal records it. | Close it in your broker if your order didn't. |
| **15:20** | Open trades are squared off in the journal, with a "square off now" message. | Exit anything still open. |

---

## 9. Telegram alerts

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the **token** it gives you.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` from the reply.
   (If stockpulse already has a bot, you can reuse its token and chat ID.)
3. Put both in `niftywhale/.env`:
   ```sh
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   ```
4. `docker compose up -d` (a restart is enough; no rebuild needed).
5. In the dashboard's Telegram section, turn on **Entry alerts** and press **Send test**.

**What gets sent:**

- **Evening summary:** after *scheduled* scans with at least one setup. Manual scans don't send one.
- **Entry alert:** for each CHoCH with R:R ≥ 1:3.
- **Pattern alert:** only if **Pattern alerts** is on; for each new 15-minute pattern inside a zone.
- **Intraday entry alert:** for each 5m CHoCH with R:R ≥ 1:2 before 14:30, with entry, stop,
  risk %, the target and which pool it is, and the square-off time.

Every entry alert names its direction: **SMC ENTRY · LONG** / **SHORT**, and **INTRADAY LONG** /
**SHORT**. A short alert says *Sell* instead of *Buy*, and a swing short adds "Short via F&O".
- **Intraday result:** when an intraday trade hits its target or stop, or is squared off at 15:20,
  with the exit and the result in R.
- **Swing result:** when a swing paper trade (3.7) hits its target or stop, with the entry, the
  exit, when it happened and the result in R, and a note when price gapped through the stop.

Rejected triggers are never sent. The **Entry alerts** switch controls both modes.

---

## 10. Configuration

### 10.1 Environment (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Telegram delivery (section 9). |
| `WATCH_DELAY_SECONDS` | `90` | Wait after each 15-minute candle closes before reading it from yfinance. |
| `SMC_*` | see 11 | Any protocol threshold, e.g. `SMC_MIN_RR=2.5`. |
| `INTRADAY_*` | see 5.8 | Any intraday threshold or time, e.g. `INTRADAY_MIN_RR=2.5`, `INTRADAY_NO_ENTRY_AFTER=14:00`. |
| `DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET` | empty | Automatic Dhan login for real-time data (4.10). |
| `DHAN_ACCESS_TOKEN` | empty | A Dhan token to start with, instead of pasting one in the dashboard. |
| `LIVE_WATCH_DELAY_SECONDS` | `10` | Delay after each candle (swing watcher, intraday scan and check) when Dhan is live. Without Dhan, `WATCH_DELAY_SECONDS` applies. |
| `NIFTYWHALE_DATA` | `auto` | `yfinance` ignores Dhan even if connected. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `NIFTYWHALE_SCHEDULER` | `1` | `0` disables the scheduler thread (for tests or a second copy of the app). |
| `NIFTYWHALE_ORIGINS` | `https://pandorasbox.local:5443` | The addresses the app is served on (comma-separated). Passkeys are bound to the first one's host name; sign-in cookies are accepted only from these pages. Set in `docker-compose.yml`. |
| `NIFTYWHALE_AUTH` | `1` | `0` switches sign-in off. **Tests only**: the app logs a warning, and anyone who can reach it can use it. |

Most of these can also be set in the app (sidebar → **App → Settings…**): a value saved there wins over `.env`.

These are set by `docker-compose.yml` and rarely need changing: `PORT=5058` (published on the Pi's loopback only, 10.4),
`DB_PATH=/data/niftywhale.db`, `UNIVERSE_PATH=/data/universe.json`, `TZ=Asia/Kolkata`, `NIFTYWHALE_ORIGINS`.

### 10.2 Dashboard settings (stored in the database)

| Group | Settings | Changed from |
|---|---|---|
| Swing | `universe`, `auto_scan`, `scan_time`, `watcher`, `shorts`, `rules` | sidebar: Scan, Watcher, Rules (tuner) |
| Intraday | `intraday`, `intraday_universe`, `intraday_shorts`, `intraday_rules` | sidebar: Intraday |
| Options | `options`, `options_stocks`, `options_alerts`, `options_level_alerts`, `options_rules` | sidebar: Options |
| Telegram | `telegram`, `pattern_alerts`, `news_alerts`, `weekly_report` | sidebar: Telegram; Performance (weekly report) |
| News | `news` | sidebar: Watcher |
| Charts | `indicators` (settings and colours) | the chart's **Indicators** button (4.11) |
| Demo funds | `demo` (sizing, limits, modes, charges), `demo_since` | the Demo funds tab (4.16) |
| Lab and autopilot | `autopilot_policy`, `paused_swing`, `paused_intraday`, `paused_options`, `lab_swing_universe`, `lab_intraday_universe`, `lab_years_swing`, `lab_years_intraday` | Performance (7.6) |

All are kept across restarts. The Dhan login (`dhan_client_id`, `secret:dhan_token`, `dhan_token_expiry`) is stored in the same
table but is not one of these settings: it is never sent to the browser (`/api/state` leaves it out).

### 10.4 Signing in and security

NiftyWhale has one account, yours, and nothing is reachable without signing in except the sign-in page itself, the
health check (`/healthz`) and static files (icons, styles).

**The address.** Other devices reach the app only over HTTPS at **https://pandorasbox.local:5443**, through the Caddy server
of Pi Observatory (`~/dockers/pi-observatory`, its `Caddyfile`). The app's own port 5058 listens on the Pi's loopback only,
so a password never crosses the network unencrypted. `https://192.168.1.50:5443` works too, but passkeys need the name.

**Trusting the certificate (once per device).** The certificate comes from Caddy's own local authority, so each device has
to trust that authority once:

- **iPhone / iPad:** in Safari open `https://pandorasbox.local:5443/niftywhale-root-ca.crt` (accept the warning this once),
  allow the profile download, then **Settings → General → VPN & Device Management** → install it, and
  **Settings → General → About → Certificate Trust Settings** → turn on full trust for it.
- **Mac:** open the same file, add it to the **System** keychain, and set it to **Always Trust**.
- The same certificate is `~/dockers/pi-observatory/caddy-root-ca.crt`; a device that already trusts Pi Observatory is done.

**First-time setup.** With no account yet, the page asks for a one-time setup code (only someone who can run commands on
the Pi can read it: `docker exec niftywhale python -m niftywhale.auth setup-code`, also in the log), a username and a
password (10 characters or more), then links an authenticator app (Google Authenticator, 1Password, Authy, the iPhone
Passwords app: scan the QR code, type the 6 digits), shows **ten recovery codes** once, and offers to add a passkey.

**Signing in.** Either the password and then the 6-digit code (or one recovery code instead), or a **passkey** alone: Face
ID, Touch ID or a security key, which is two factors in itself and cannot be phished. On an iPhone, Safari offers the passkey
right in the username field. "Keep me signed in" keeps the device signed in for 30 days of use; otherwise a session ends
after 12 hours idle.

**Account & security** (sidebar → App): change the password (every other device is signed out), move the authenticator to a
new phone, make new recovery codes, add or remove passkeys, see and sign out signed-in devices, create and revoke API tokens,
and the sign-in history. Changes that matter ask for the password again when the sign-in is more than 10 minutes old.

**API tokens** are for scripts and curl: create one in Account & security, then send `Authorization: Bearer <token>` (or
`X-API-Key: <token>`). A token can do everything you can, so keep it private; revoking it stops it at once. On the Pi itself,
`http://127.0.0.1:5058` with a token works too.

**Guards.** Passwords are stored as scrypt hashes; sessions and tokens only as SHA-256 hashes of random values (the database
holds nothing that signs anyone in). A code from the authenticator is accepted once. After 8 failures in a row the account
locks for 15 minutes; more than 20 failures from one address in 15 minutes and that address waits. A browser can only make
changes from the app's own pages (cross-site requests are refused). A sign-in raises a notification in the bell.

**Locked out?** On the Pi:

```sh
docker exec -it niftywhale python -m niftywhale.auth reset-password   # new password, every device signs out
docker exec niftywhale python -m niftywhale.auth reset-mfa            # start the account over (data and settings are kept)
docker exec niftywhale python -m niftywhale.auth token "pi scripts"  # an API token, printed once
docker exec niftywhale python -m niftywhale.auth sign-out-all
docker exec niftywhale python -m niftywhale.auth status
```

### 10.3 Where the rules come from

```
built-in defaults (smc.Rules)  →  SMC_* in .env  →  values saved from the rule tuner
                                                     (highest priority)
```

The tuner can change the eight thresholds in its sliders. The rest (ATR length, OB search window,
stop buffers, 15-minute swing size, intraday sessions) are `.env`-only. Out-of-range or
unparsable values are clamped or ignored rather than breaking a scan: a tuner value is clamped
to its slider's range, and a `.env` value that is not a number, infinite, negative, or a window
of zero candles (say `SMC_ATR_LEN=0`) is ignored in favour of the default.

---

## 11. The rules in detail

| Rule (`SMC_` + name) | Default | Tuner range | Doc step | Meaning |
|---|---|---|---|---|
| `MIN_AVG_VOLUME` | 1,000,000 | 0 – 50 L | 2 | Minimum average daily volume, in shares. |
| `AVG_VOLUME_DAYS` | 63 | — | 2 | Sessions averaged (≈ 3 months). |
| `ATR_LEN` | 14 | — | 3 | ATR period (Wilder smoothing). |
| `MIN_ATR_PCT` | 1.5 | 0 – 5 | 3 | ATR must exceed this % of price. |
| `SWING_LEN` | 3 | 2 – 8 | 4–5 | Candles each side that define a daily swing point. |
| `BOS_LOOKBACK` | 120 | 20 – 240 | 6 | How many sessions back a break of structure may be. |
| `DISPLACEMENT_ATR` | 1.5 | 0.5 – 5 | 6 | "Massive rally": origin-to-break distance in ATRs. |
| `OB_SEARCH` | 5 | — | 6 | Candles back from the origin searched for the down-close candle. |
| `MAX_DISCOUNT` | 0.5 | 0.2 – 0.8 | 8 | Price must be at or below this fraction of the leg. |
| `PRE_MIN_RR` | 1.5 | 0.5 – 5 | 11–12 | Provisional R:R needed to become a setup. |
| `MIN_RR` | 3.0 | 1 – 6 | 12 | Final R:R needed for an entry alert. |
| `STOP_BUFFER_ATR` | 0.1 | — | 11 | Provisional stop sits this many ATRs under the leg origin. |
| `STOP_BUFFER_PCT` | 0.1 | — | 11 | The 15-minute stop sits this % under the sweep low. |
| `INTRADAY_SWING_LEN` | 2 | — | 10 | Candles each side that define a 15-minute swing point. |
| `INTRADAY_SESSIONS` | 2 | — | 10 | How many sessions of 15-minute candles are searched. |
| `MAX_MARKET_RUN` | 10 (off) | −3 – 10 | gate | Skip a swing CHoCH when Nifty has moved more than this % the trade's way over 20 days. |
| `MIN_ZONE_AGE` | 0 (off) | 0 – 8 | gate | Skip a swing CHoCH that comes sooner than this many days after its zone was set. |
| `MAX_RR` | 30 (off) | 4 – 30 | gate | Skip a swing CHoCH whose 15-minute R:R is above this. |

The last three are **context gates**, checked when a swing CHoCH fires: they never change the screen, the zones or
the backtest records. A gated CHoCH is logged as *rejected* with the reason ("CHoCH, but skipped: …"), spends its
setup like an R:R rejection, and never becomes a paper trade. Zones you add yourself are never gated. Each is off at
its default; the autopilot turns them on only when walk-forward and a shadow run say so (§7.4), and you can set them
in the rule tuner.

**Tuning tips**

- **Too few setups:** check which funnel step removes most stocks. Usually it's *structure*,
  which is the market, not a setting. The most effective dials after that are
  `DISPLACEMENT_ATR` (lower) and `MAX_DISCOUNT` (higher).
- **Too many weak setups:** raise `SWING_LEN` (only major swings), `DISPLACEMENT_ATR`, or
  `PRE_MIN_RR`.
- **Keep `MIN_RR` at 3** unless you've deliberately decided otherwise. It is the doc's hard rule.
- Always look at a few charts in the tuner before saving.

---

## 12. API reference

Every endpoint the dashboard uses, with a request you can run and the answer it gives. Send and receive JSON (a POST body is a
JSON object); an error comes back as `{"error": "…"}` with a 4xx or 5xx status.

**Authentication.** Everything except `/healthz`, `/login`, static files and the sign-in endpoints needs it. Scripts send an
API token (10.4: create one in Account & security, or on the Pi with `docker exec niftywhale python -m niftywhale.auth token NAME`):

```sh
export NW_TOKEN=nwt_...
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/board
```

The examples run on the Pi itself (`http://127.0.0.1:5058`). From another device use `https://pandorasbox.local:5443`
(with `--cacert caddy-root-ca.crt` unless the device trusts the Pi's certificate). Without a token or session: `401`.

The answers shown are real ones from this app, shortened: long lists keep their first item or two, deep objects show `{…}`,
long text ends in `…`, and personal values (account and chat numbers) are replaced.

### 12.1 Dashboard, prices and search

#### `GET /healthz`

Liveness check, used by the Docker healthcheck. Plain text, not JSON.

```sh
curl http://127.0.0.1:5058/healthz
```

```text
ok
```

#### `GET /api/state`

Everything the dashboard shows: market status, scan and watcher state, settings, rules, the latest scan with its funnel, setups, zones, alerts, `swing_trades` (the paper-trade journal and its totals) and `intraday` (session phase, its scan, setups, today's zones, journal, stats, rules).

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/state
```

```json
{
  "alerts": [
    {…}
  ],
  "candidates": [
    {…}
  ],
  "env_rules": {…},
  "indicator_settings": {…},
  "indicator_specs": {…},
  "intraday": {…},
  "market_data": {…},
  "market_open": true,
  "now": "2026-10-09T13:47:29+05:30",
  "options": {…},
  "patterns": {…},
  "rule_overrides": {},
  "rules": {…},
  "scan": {…},
  "scan_state": {…},
  "settings": {…},
  "signals": [
    {…}
  ],
  "steps": [
    {…}
  ],
  "swing_trades": {…},
  "telegram_configured": true,
  "tunable": {…},
  "…": "4 more keys"
}
```

#### `GET /api/board`

The live board: one card per watched stock with price, change, zone distance, today's 15m candles, patterns and today's news count.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/board
```

```json
{
  "cards": [
    {…}
  ],
  "market_open": true,
  "now": "2026-10-09T13:47:31+05:30",
  "source": "dhan"
}
```

#### `GET /api/ticker`

The index ticker: per index last, open, high, low, previous close, change, change %, place in the day's range (0–100), GIFT Nifty's gap to Nifty, and the option symbol for the five with chains. Empty without Dhan.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/ticker
```

```json
{
  "error": null,
  "live": true,
  "refresh_s": 5,
  "rows": [
    {
      "change": 295.1,
      "change_pct": 1.33,
      "group": "headline",
      "high": 22534.4,
      "id": 13,
      "label": "NIFTY 50",
      "last": 22526.9,
      "low": 22294.75,
      "name": "Nifty 50",
      "open": 22314.95,
      "option": "NIFTY",
      "position": 96.9,
      "prev_close": 22231.8
    }
  ],
  "session": "2026-10-09",
  "source": "dhan",
  "updated": "2026-10-09T13:47:30+05:30"
}
```

#### `GET /api/live`

The live prices behind every moving number on the page, for a page without its WebSocket (`/ws` pushes the same as they trade) and for scripts. Asking puts the symbols on the feed for 30 s; each price comes with the time it was read. Also carries the notification count (`notices`: `seq`, `unread`).

**Query:** `?s=` comma-separated feed symbols: a stock, `IDX:<id>` for an index, `OPT:<segment>:<id>` for an option contract (up to 250)

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/live?s=PAYTM,IDX:13'
```

```json
{
  "error": null,
  "every_ms": 1000,
  "notices": {
    "seq": 20,
    "unread": 13
  },
  "now": 1791533856.8404,
  "on": true,
  "px": {
    "IDX:13": [
      22525.15,
      1791533853.72
    ],
    "PAYTM": [
      1646.9,
      1791533853.72
    ]
  }
}
```

#### `GET /api/live/status`

The live plumbing: Dhan's market feed WebSocket (connected, since, instruments carried, packets, the last error), the feed (symbols streamed and still polled) and how many pages have their WebSocket open.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/live/status
```

```json
{
  "feed": {
    "error": null,
    "last_poll": 1791560431.0,
    "polled": 0,
    "streamed": 26
  },
  "note": "Dhan live feed, every trade",
  "on": true,
  "pages": 2,
  "poll_ms": 1000,
  "realtime": true,
  "source": "dhan",
  "stream": {
    "connected": true,
    "error": null,
    "instruments": 61,
    "last_packet": 1791561620.4,
    "packets": 48211,
    "since": 1791560432.1
  }
}
```

#### `GET /ws`

The page's live connection. Send `{"t": "sub", "ch": "px", "p": {"s": ["IDX:13"]}}` to subscribe (channels: `px` prices, `ticks` chart ticks `{s, since}`, `notices`, `demo`, `ticker`, `mood`, `chain` `{symbol, expiry}`), `{"t": "unsub", "ch": …}`, `{"t": "vis", "hidden": true}`, `{"t": "ping"}`. The server sends `hello` first, then `{"t": <channel>, "d": …}` as things change and `{"t": "topics", "d": {…}}` when a table (`db:<table>`) or the app's state (`run`, `market`, `news_run`, `smart_run`) changed. Signs in like a page (the cookie from the app's own origin) or with an API token; closes with 4401 when the sign-in ends, 1013 when 12 pages are connected already.

**Parameters:** a WebSocket upgrade (`wss://` through Caddy); messages are JSON

```sh
python3 - <<'PY'
import asyncio, json, os, websockets
async def main():
    url, auth = 'ws://127.0.0.1:5058/ws', {'Authorization': 'Bearer ' + os.environ['NW_TOKEN']}
    async with websockets.connect(url, additional_headers=auth) as ws:
        await ws.send(json.dumps({'t': 'sub', 'ch': 'px', 'p': {'s': ['IDX:13', 'PAYTM']}}))
        async for msg in ws:
            print(msg)
asyncio.run(main())
PY
```

Messages, as they come:

```json
{"t": "hello", "on": true, "live": {"source": "dhan", "stream": true, "note": "Dhan live feed, every trade"}, "notices": {"seq": 20, "unread": 3}, "topics": {"db:zones": 412, "db:notices": 57, "run": "9f3a0c1b2d4e"}, "channels": ["chain", "demo", "mood", "notices", "px", "ticker", "ticks"]}
{"t": "px", "d": {"on": true, "stream": true, "px": {"IDX:13": [22525.15, 1791533853.72]}}}
{"t": "topics", "d": {"db:zones": 413}}
```

#### `GET /api/glossary`

Every abbreviation and symbol the dashboard shows, by group (the Legend).

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/glossary
```

```json
[
  {
    "key": "smc",
    "terms": [
      {
        "meaning": "Reading a chart for the footprints large institutions leave: where th…",
        "name": "Smart Money Concepts",
        "term": "SMC"
      },
      {
        "meaning": "Each swing high and swing low above the last one: a bullish structure…",
        "name": "Higher high / higher low",
        "term": "HH / HL"
      }
    ],
    "title": "Smart Money Concepts (the protocol)"
  },
  {
    "key": "trade",
    "terms": [
      {
        "meaning": "(target − entry) ÷ (entry − stop). 1:3 means the target is three time…",
        "name": "Reward-to-risk",
        "term": "R:R"
      },
      {
        "meaning": "Swing alerts need at least 1:3, intraday alerts at least 1:2 (both tu…",
        "name": "Minimum R:R",
        "term": "1:3 / 1:2"
      }
    ],
    "title": "Trades and risk"
  }
]
```

#### `GET /api/search`

Up to 10 universe matches for the search box, plus an "analyse anyway" entry.

**Query:** `?q=` text

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/search?q=pay'
```

```json
[
  {
    "name": "One 97 Communications Ltd.",
    "symbol": "PAYTM",
    "tag": "F&O"
  },
  {
    "name": "SBI Cards and Payment Services Ltd.",
    "symbol": "SBICARD",
    "tag": "F&O"
  }
]
```

#### `GET /api/palette`

What the command palette searches: every universe stock (symbol, name, tag, F&O, industry, weight) and the option indices.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/palette
```

```json
{
  "indices": [
    [
      "NIFTY",
      "Nifty 50"
    ],
    [
      "BANKNIFTY",
      "Nifty Bank"
    ]
  ],
  "stocks": [
    [
      "360ONE",
      "360 ONE WAM Ltd."
    ],
    [
      "AADHARHFC",
      "Aadhar Housing Finance Ltd."
    ]
  ]
}
```

### 12.2 Swing: scan, zones and rules

#### `POST /api/scan`

Starts a scan. Universe keys: `nifty100`, `fo`, `both`, or an index such as `nifty500`, `niftybank`, `niftyit`. `409` if one is running.

**Body:** `universe` (optional; also saved as the setting)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"universe": "fo"}' http://127.0.0.1:5058/api/scan
```

```json
{
  "status": "started"
}
```

#### `POST /api/scan/stop`

Stops the running scan. The example shows the `409` answer when no scan is running.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/scan/stop
```

Answer (`409`):

```json
{
  "status": "idle"
}
```

#### `POST /api/watch/check`

Runs one watcher pass over the open swing zones now.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/watch/check
```

```json
{
  "checked": 9,
  "closed": 0,
  "patterns": 2,
  "rejected": 0,
  "tapped": 1,
  "triggered": 0
}
```

#### `GET /api/funnel/<step>`

The stocks of the latest scan that stopped at that step, with the reason.

**Query:** step: `data`, `history`, `liquidity`, `volatility`, `structure`, `order_block`, `intact`, `discount`, `rr`; `?mode=intraday` for the intraday scan

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/funnel/structure
```

```json
{
  "scan_id": 6,
  "step": "structure",
  "stocks": [
    {
      "name": "Aarti Industries Ltd.",
      "reason": "mixed swings",
      "symbol": "AARTIIND"
    },
    {
      "name": "Aditya Birla Capital Ltd.",
      "reason": "mixed swings",
      "symbol": "ABCAPITAL"
    }
  ]
}
```

#### `GET /api/stock/<SYMBOL>`

Daily candles with their indicators (`ind`), a fresh analysis (long, or short on a bearish F&O chart; `analysis.side`), and daily patterns.

**Query:** `?bars=30–260`, `?rules=<json>` (what-if rules)

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/stock/PAYTM?bars=30'
```

```json
{
  "analysis": {…},
  "bars": [
    {…}
  ],
  "in_universe": true,
  "ind": {…},
  "name": "One 97 Communications Ltd.",
  "offset": 218,
  "patterns": [
    {…}
  ],
  "symbol": "PAYTM"
}
```

#### `GET /api/intraday/<SYMBOL>`

The last 3 sessions of 15m candles, the stock's zone, its patterns, and `ind`: volume, delta, cumulative delta, VWAP, Bollinger, opening range.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/intraday/PAYTM
```

```json
{
  "bars": [
    {…}
  ],
  "ind": {…},
  "patterns": [
    {…}
  ],
  "symbol": "PAYTM",
  "zone": {…}
}
```

#### `POST /api/whatif`

The funnel, setups and per-step drop lists under those rules, from the last scan's data. Saves nothing.

**Body:** `rules`, `universe`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"rules": {"min_rr": 2.5}, "universe": "nifty500"}' http://127.0.0.1:5058/api/whatif
```

```json
{
  "drops": {…},
  "funnel": {…},
  "rules": {…},
  "setups": [],
  "universe": "nifty500",
  "universe_label": "Nifty 500"
}
```

#### `POST /api/rules`

Saves rule-tuner overrides (they replace `SMC_*` in .env); returns the rules now in force.

**Body:** `rules` (`{}` resets)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"rules": {"min_rr": 3}}' http://127.0.0.1:5058/api/rules
```

```json
{
  "overrides": {},
  "rules": {
    "atr_len": 14,
    "avg_volume_days": 63,
    "bos_lookback": 120,
    "displacement_atr": 1.5,
    "intraday_sessions": 2,
    "intraday_swing_len": 2,
    "max_discount": 0.5,
    "max_market_run": 10.0,
    "max_rr": 30.0,
    "min_atr_pct": 1.5,
    "min_avg_volume": 1000000,
    "min_rr": 3.0,
    "min_zone_age": 0,
    "ob_search": 5,
    "pre_min_rr": 1.5,
    "stop_buffer_atr": 0.1,
    "stop_buffer_pct": 0.1,
    "swing_len": 3
  }
}
```

#### `POST /api/zones`

Watches (or updates) a zone you set: a target above the zone is a long, below it a short.

**Body:** `symbol`, `zone_low`, `zone_high`, `target`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"symbol": "PAYTM", "zone_low": 1619, "zone_high": 1628.1, "target": 1855.5}' http://127.0.0.1:5058/api/zones
```

```json
{
  "created_at": "2026-10-09T08:19:51",
  "ctx": null,
  "expires_at": null,
  "id": 79,
  "last_checked": null,
  "meta": null,
  "mode": "swing",
  "name": "One 97 Communications Ltd.",
  "note": "levels set by you",
  "scan_id": null,
  "side": "long",
  "source": "manual",
  "status": "watching",
  "symbol": "PAYTM",
  "target": 1855.5,
  "trigger": null,
  "zone_high": 1628.1,
  "zone_low": 1619.0
}
```

#### `POST /api/zones/<id>/dismiss`

Stops watching a swing zone (`409` once it has triggered or closed).

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/zones/79/dismiss
```

```json
{
  "status": "dismissed"
}
```

### 12.3 Intraday

#### `POST /api/intraday/scan`

Starts a 15m scan of the intraday universe (a preview outside 09:30–14:30). `409` if one is running.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/intraday/scan
```

```json
{
  "status": "started"
}
```

#### `POST /api/intraday/check`

Runs one 5m check of today's intraday zones now.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/intraday/check
```

```json
{
  "checked": 14,
  "closed": 0,
  "failed": 1,
  "rejected": 0,
  "tapped": 2,
  "triggered": 1
}
```

#### `GET /api/intraday/chart/<SYMBOL>`

15m candles (3 sessions), 5m candles (latest session), a fresh 15m analysis with session levels, and today's zone.

**Query:** `?rules=<json>` (what-if rules)

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/intraday/chart/PAYTM
```

```json
{
  "analysis": {…},
  "ind": {…},
  "m15": [
    {…}
  ],
  "m5": [
    {…}
  ],
  "name": "One 97 Communications Ltd.",
  "rules": {…},
  "symbol": "PAYTM",
  "zone": null
}
```

#### `POST /api/intraday/whatif`

Re-screens the last intraday scan's candles under those rules. Saves nothing. `409` before the first intraday scan since a restart.

**Body:** `rules`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"rules": {"min_rr": 2.5}}' http://127.0.0.1:5058/api/intraday/whatif
```

Answer (`409`):

```json
{
  "error": "no intraday candles yet — run an intraday scan first (Scan now)"
}
```

#### `POST /api/intraday/rules`

Saves intraday tuner overrides (they replace `INTRADAY_*` in .env).

**Body:** `rules` (`{}` resets)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"rules": {"min_rr": 3}}' http://127.0.0.1:5058/api/intraday/rules
```

```json
{
  "overrides": {
    "min_rr": 3.0
  },
  "rules": {
    "atr_len": 14,
    "bos_lookback": 50,
    "displacement_atr": 2.0,
    "max_discount": 0.5,
    "min_atr_pct": 1.0,
    "min_avg_volume": 1000000,
    "min_rr": 3.0,
    "min_target_pct": 0.3,
    "no_entry_after": "14:30",
    "ob_search": 5,
    "pre_min_rr": 1.0,
    "sessions": 5,
    "square_off": "15:20",
    "start": "09:30",
    "stop_buffer_pct": 0.05,
    "swing_len": 2,
    "trigger_swing_len": 2
  }
}
```

#### `POST /api/intraday/zones/<id>/dismiss`

Stops watching an intraday zone.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/intraday/zones/77/dismiss
```

```json
{
  "status": "dismissed"
}
```

### 12.4 Options

#### `GET /api/options`

The Options tab: scanner state, one row per underlying (its nearest expiry's latest numbers, and why it has no idea), unusual activity, the idea journal and its totals, the idea rules.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/options
```

```json
{
  "default_stocks": [
    "RELIANCE"
  ],
  "journal": [
    {…}
  ],
  "market_data": {…},
  "market_open": true,
  "now": "2026-10-09T13:47:43+05:30",
  "rows": [
    {…}
  ],
  "rule_overrides": {},
  "rules": {…},
  "session": "2026-10-09",
  "settings": {…},
  "state": {…},
  "stats": {…},
  "stocks": [
    "RELIANCE"
  ],
  "tunable": {…},
  "tunable_order": [
    "min_bias"
  ],
  "unusual": [
    {…}
  ]
}
```

#### `GET /api/options/chain/<SYMBOL>`

The latest chain for that expiry (default: the newest read): day and intraday OI changes and buildups, headline numbers, the day's spot / PCR / IV series, and the underlying's ideas. Each contract carries its feed symbol (`lp`). `404` before the first read.

**Query:** `?expiry=YYYY-MM-DD`

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/options/chain/NIFTY
```

```json
{
  "chain": [
    {…}
  ],
  "expiries": [
    "2026-10-13"
  ],
  "expiry": "2026-10-13",
  "ideas": [
    {…}
  ],
  "lp": "IDX:13",
  "name": "Nifty 50",
  "ref_ts": "2026-10-09T09:15:00+05:30",
  "series": {…},
  "session": "2026-10-09",
  "summary": {…},
  "symbol": "NIFTY",
  "ts": "2026-10-09T13:43:55+05:30",
  "why": "an idea is already open"
}
```

#### `GET /api/options/chain/<SYMBOL>/live`

The same chain with live OI, volume, LTP and bid/ask for every stored strike (one quote request, cached 3 s) and the summary computed again; IV and max pain stay as read. `204` outside the session or without Dhan.

**Query:** `?expiry=YYYY-MM-DD`

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/options/chain/NIFTY/live
```

```json
{
  "chain": [
    {…}
  ],
  "expiry": "2026-10-13",
  "quoted": 120,
  "snap": 2624,
  "summary": {…},
  "ts": "2026-10-09T13:47:43+05:30"
}
```

#### `POST /api/options/refresh`

Reads that underlying's chains now, or runs a full pass. `409` without Dhan or while a pass runs.

**Body:** `symbol` (optional)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"symbol": "NIFTY"}' http://127.0.0.1:5058/api/options/refresh
```

```json
{
  "started": true,
  "symbol": "NIFTY"
}
```

#### `POST /api/options/rules`

Saves idea-rule overrides (clamped); returns the rules in force.

**Body:** rule: value pairs (`{}` resets)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"stop_pct": 25, "target_pct": 50}' http://127.0.0.1:5058/api/options/rules
```

```json
{
  "rule_overrides": {},
  "rules": {
    "max_iv_pct": 80.0,
    "max_per_day": 2,
    "max_spread_pct": 3.0,
    "min_bias": 0.35,
    "min_move_pct": 0.15,
    "no_entry_after": "14:30",
    "square_off": "15:15",
    "start": "09:45",
    "stop_pct": 25.0,
    "target_pct": 50.0
  }
}
```

#### `POST /api/options/stocks`

Saves the stocks options mode reads; non-F&O symbols come back in `rejected`.

**Body:** `stocks` (empty = the default 30)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"stocks": "RELIANCE TCS INFY"}' http://127.0.0.1:5058/api/options/stocks
```

```json
{
  "rejected": [],
  "stocks": [
    "RELIANCE",
    "TCS"
  ]
}
```

### 12.5 Trades and notifications

#### `GET /api/trade/<ref>`

One trade in full, what a row in any trade table opens: levels, result, R, how long it was held, why it triggered, its alerts and notifications, the demo position with each charge, the context at entry, and news around it. `404` for anything that is not a trade.

**Parameters:** ref: `swing:<zone id>`, `intraday:<zone id>` or `options:<idea id>`

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/trade/options:16
```

```json
{
  "alerts": [],
  "demo": {…},
  "direction": "long",
  "entry": 48.45,
  "entry_time": "2026-10-09T09:52:38+05:30",
  "exit": 36.0,
  "exit_time": "2026-10-09T11:58:16+05:30",
  "features": {…},
  "last": 36.0,
  "lp": "",
  "minutes": 126,
  "mode": "options",
  "name": null,
  "news": [
    {…}
  ],
  "note": "stop",
  "notices": [],
  "option": {…},
  "r": -1.03,
  "r_open": -1.03,
  "ref": "options:16",
  "reward_pts": 24.23,
  "risk_pts": 12.11,
  "rr": 2.0,
  "side": "long",
  "status": "lost",
  "stop": 36.34,
  "symbol": "TCS",
  "target": 72.68,
  "why": "put writing near the money (bias +1.00), underlying +0.77% in 30 min,…",
  "word": "Stopped out"
}
```

#### `GET /api/trade/<ref>/candles`

The trade's own chart: the option contract's premium for an idea, the stock otherwise; 5m for intraday and options, 15m / 60m / daily for swing by length; `entry_k` / `exit_k` mark the entry and exit candles.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/trade/options:16/candles
```

```json
{
  "bars": [
    {
      "c": 34.8,
      "d": "10-09 09:15",
      "day": "2026-10-09",
      "h": 40.25,
      "l": 20.35,
      "o": 21.4,
      "t": "2026-10-09T09:15:00+05:30"
    },
    {
      "c": 41.1,
      "d": "10-09 09:20",
      "day": "2026-10-09",
      "h": 44.85,
      "l": 34.9,
      "o": 37.4,
      "t": "2026-10-09T09:20:00+05:30"
    }
  ],
  "entry_k": 7,
  "exit_k": 32,
  "label": "TCS 2180 CE premium",
  "lp": "",
  "tf": 5,
  "ttl": 3600
}
```

#### `GET /api/notices`

The bell: notifications changed since that seq, newest first, with the unread count. Each has a `kind`, a `level` (`info`, `good`, `bad`, `warn`) and a `link` (a stock, a chain, a tab, or a trade `ref`).

**Query:** `?after=<seq>` (0 = the latest 100)

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/notices
```

```json
{
  "items": [
    {
      "body": "20 trades already taken that day: the day's limit is 20",
      "id": 1211,
      "key": "skip:intraday:49",
      "kind": "skip",
      "level": "warn",
      "link": {
        "ref": "intraday:49",
        "tab": "demo"
      },
      "mode": "intraday",
      "read": 0,
      "seq": 20,
      "symbol": "ITCHOTELS",
      "title": "Demo account skipped ITCHOTELS",
      "ts": "2026-10-09T13:25:20"
    }
  ],
  "seq": 20,
  "unread": 13
}
```

#### `POST /api/notices/read`

Marks notifications read; returns the new counts.

**Body:** `ids` (omit to mark all read)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"ids": [1211]}' http://127.0.0.1:5058/api/notices/read
```

```json
{
  "seq": 21,
  "unread": 12
}
```

#### `POST /api/notices/clear`

Clears every notification from the bell, on every device; returns how many and the new counts. They stay cleared: the same event (a trade near its stop, say) is not raised again.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:5058/api/notices/clear
```

```json
{
  "cleared": 81,
  "seq": 134,
  "unread": 0
}
```

### 12.6 Demo funds

#### `GET /api/demo`

The demo account: `account` (deposits, withdrawals, balance, blocked, free, unreal, equity, realised, charges, return), `by_mode`, `open`, `closed`, `skipped`, the `ledger` statement (newest first), its `statement` summary, `charges` by line, the balance `curve`, `first_trade`, `settings` and `live` (whether prices are live now).

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/demo
```

```json
{
  "account": {…},
  "by_mode": {…},
  "charges": {…},
  "closed": [
    {…}
  ],
  "curve": [
    {…}
  ],
  "first_trade": "2026-10-09T09:30:00+05:30",
  "ledger": [
    {…}
  ],
  "live": {…},
  "open": [
    {…}
  ],
  "products": {…},
  "settings": {…},
  "since": "2026-10-09T09:15:00+05:30",
  "skipped": [
    {…}
  ],
  "statement": {…}
}
```

#### `GET /api/demo/live`

The open positions' latest prices and the totals they move, for the Demo tab's once-a-second refresh.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/demo/live
```

```json
{
  "account": {
    "balance": 991487.65,
    "blocked": 819235.8,
    "charges": 108.6,
    "closed": 1,
    "deposits": 1000000.0,
    "equity": 1006213.7,
    "free": 172251.85,
    "gross": -8403.75,
    "open": 19,
    "realized": -8512.35,
    "return_pct": 0.62,
    "unreal": 14726.05,
    "withdrawals": 0
  },
  "by_mode": {
    "intraday": {…},
    "options": {…},
    "swing": {…}
  },
  "live": {
    "error": null,
    "every_ms": 1000,
    "on": true
  },
  "now": 1791533871.1109,
  "open": [
    {…}
  ]
}
```

#### `POST /api/demo/funds`

Money in or out; the first deposit starts the account. A withdrawal above free funds is a `409`.

**Body:** `kind` (`deposit` or `withdraw`), `amount`, `note` (optional)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind": "deposit", "amount": 50000, "note": "top-up"}' http://127.0.0.1:5058/api/demo/funds
```

```json
{
  "account": {…},
  "by_mode": {…},
  "charges": {…},
  "closed": [
    {…}
  ],
  "curve": [
    {…}
  ],
  "first_trade": "2026-10-09T09:30:00+05:30",
  "ledger": [
    {…}
  ],
  "live": {…},
  "open": [
    {…}
  ],
  "products": {…},
  "settings": {…},
  "since": "2026-10-09T09:15:00+05:30",
  "skipped": [
    {…}
  ],
  "statement": {…}
}
```

#### `POST /api/demo/settings`

Saves the demo settings (clamped; the two limits are whole numbers, 0 = no limit).

**Body:** `risk_pct`, `max_alloc_pct`, `intraday_leverage`, `futures_margin_pct`, `max_open`, `max_per_day`, `swing`, `intraday`, `options`, `charges`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"risk_pct": 1, "max_open": 20}' http://127.0.0.1:5058/api/demo/settings
```

```json
{
  "account": {…},
  "by_mode": {…},
  "charges": {…},
  "closed": [
    {…}
  ],
  "curve": [
    {…}
  ],
  "first_trade": "2026-10-09T09:30:00+05:30",
  "ledger": [
    {…}
  ],
  "live": {…},
  "open": [
    {…}
  ],
  "products": {…},
  "settings": {…},
  "since": "2026-10-09T09:15:00+05:30",
  "skipped": [
    {…}
  ],
  "statement": {…}
}
```

#### `POST /api/demo/reset`

Deletes the demo account; with an amount, opens a new one dated `since` and replays the trades since then at once. A future date is a `400`.

**Body:** `confirm: true`, `amount` (optional), `since`: `now`, `first` or `YYYY-MM-DD`, `note` (optional)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"confirm": true, "amount": 1000000, "since": "first"}' http://127.0.0.1:5058/api/demo/reset
```

```json
{
  "account": {…},
  "by_mode": {…},
  "charges": {…},
  "closed": [
    {…}
  ],
  "curve": [
    {…}
  ],
  "first_trade": "2026-10-09T09:30:00+05:30",
  "ledger": [
    {…}
  ],
  "live": {…},
  "open": [
    {…}
  ],
  "products": {…},
  "settings": {…},
  "since": "2026-10-09T09:15:00+05:30",
  "skipped": [
    {…}
  ],
  "statement": {…}
}
```

#### `GET /api/demo/statement.csv`

The whole statement as a spreadsheet (CSV): date, time, type, description, detail, amount, balance, and each charge line.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/demo/statement.csv
```

```text
date,time,type,description,detail,amount,balance,brokerage,stt,exchange,ipft,sebi,stamp,dp,gst
2026-10-09,09:15,Opening balance,Opening balance,opening balance,1000000.00,1000000.00,,,,,,,,
2026-10-09,11:58,Trade P&L,Options · TCS 2180 CE 27 Oct,bought 3 lots × 225 (bullish) @ 48.45 → 36.00 · stop hit,-8403.75,991596.25,,,,,,,,
2026-10-09,11:58,Charges,Charges · TCS 2180 CE 27 Oct,Option round trip,-108.60,991487.65,40.00,36.45,20.25,0.00,0.06,0.98,0.00,10.86
```

#### `POST /api/demo/sync`

Takes and settles trades now (it also runs every minute in the session).

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/demo/sync
```

```json
{
  "account": {…},
  "by_mode": {…},
  "charges": {…},
  "closed": [
    {…}
  ],
  "curve": [
    {…}
  ],
  "first_trade": "2026-10-09T09:30:00+05:30",
  "ledger": [
    {…}
  ],
  "live": {…},
  "open": [
    {…}
  ],
  "products": {…},
  "settings": {…},
  "since": "2026-10-09T09:15:00+05:30",
  "skipped": [
    {…}
  ],
  "statement": {…},
  "sync": {…}
}
```

### 12.7 News and smart money

#### `GET /api/news`

The news desk: filings and headlines, newest first, for every stock followed (or one stock), with `watch` and the desk's `state`.

**Query:** `?symbol=X`, `?kind=filing|media`, `?days=1–45` (7)

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/news?symbol=PAYTM&days=3'
```

```json
{
  "days": 3,
  "enabled": true,
  "fetched_at": "2026-10-09T13:46:06",
  "hidden": {},
  "impact": {
    "by_kind": {…},
    "by_label": {…},
    "days": 30,
    "items": 3322
  },
  "items": [],
  "state": {
    "errors": [],
    "last_full": null,
    "last_pass": null,
    "message": "",
    "running": true
  },
  "today": {
    "filings": 2,
    "n": 149
  },
  "tones": {},
  "watch": [
    {…}
  ]
}
```

#### `POST /api/news/refresh`

Fetches one stock's news now (at most every 5 minutes; answers when done), or every followed stock in the background.

**Body:** `symbol` (optional)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/news/refresh
```

```json
{
  "status": "started"
}
```

#### `GET /api/smart`

The Smart money tab: FII/DII `flows`, FII `positioning`, the followed `stocks` with their delivery read and 30-day deals, the last 10 days' `deals`, what is `held`, and the pass `state`.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/smart
```

```json
{
  "deals": [
    {…}
  ],
  "flows": {…},
  "held": {…},
  "positioning": {…},
  "state": {…},
  "stocks": [
    {…}
  ]
}
```

#### `GET /api/smart/stock/<SYMBOL>`

One stock: its delivery read and history, and its deals of the last 30 days.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/smart/stock/PAYTM
```

```json
{
  "deal_list": [],
  "deals": null,
  "delivery": {
    "avg_pct": 42.37,
    "change_pct": -5.23,
    "day": "2026-10-08",
    "deliv_pct": 32.58,
    "deliv_value_cr": 930.5,
    "ratio": 0.77,
    "read": "normal",
    "sessions": 20,
    "vol_ratio": 3.94
  },
  "history": [
    {
      "close": 1739.0,
      "day": "2026-09-10",
      "deliv_pct": 32.63,
      "deliv_qty": 482576,
      "prev_close": 1751.5,
      "qty": 1479094,
      "symbol": "PAYTM",
      "turnover_cr": 257.34
    }
  ],
  "symbol": "PAYTM"
}
```

#### `POST /api/smart/refresh`

Fetches from NSE now, in the background.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/smart/refresh
```

```json
{
  "status": "started"
}
```

### 12.8 Performance, lab and autopilot

#### `GET /api/perf`

Statistics, the cumulative-R curve, highlights and breakdowns of the live paper journal (plus the last 25 trades, this week, and the open trades with their feed symbols in `open_live`), or of the latest backtest of the current rules.

**Query:** `?mode=swing|intraday|options`, `?source=live|backtest`

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/perf?mode=swing'
```

```json
{
  "breakdown": [],
  "equity": [],
  "highlights": [],
  "mode": "swing",
  "open_live": [
    {…}
  ],
  "recent": [
    {…}
  ],
  "source": "live",
  "summary": {
    "open": 1,
    "trades": 0
  },
  "week": {
    "open": 0,
    "trades": 0
  }
}
```

#### `GET /api/lab`

History on disk, recent lab jobs, each mode's autopilot state, the policy, the change log and the lab `settings` (universes and history length per mode).

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/lab
```

```json
{
  "candidates": 100,
  "coverage": {…},
  "jobs": [
    {…}
  ],
  "last_report": null,
  "log": [],
  "modes": {…},
  "policy": {…},
  "settings": {…}
}
```

#### `POST /api/lab/job`

Queues a lab job; heavy work waits for the market to close.

**Body:** `kind`: `backtest`, `tune`, `report` or `nightly`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind": "backtest"}' http://127.0.0.1:5058/api/lab/job
```

```json
{
  "job": 3
}
```

#### `POST /api/autopilot/policy`

Saves the autopilot settings.

**Body:** `mode`, `max_steps`, `shadow_days`, `cooldown_days`, `pause_r`, `rollback_r`, `limits` (`{mode: {threshold: [min, max]}}`)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode": "swing"}' http://127.0.0.1:5058/api/autopilot/policy
```

```json
{
  "policy": {
    "cooldown_days": 14,
    "fold_months": 3,
    "limits": {},
    "max_steps": 2,
    "min_gain_r": 0.05,
    "min_oos_trades": {…},
    "min_train_folds": 2,
    "min_train_trades": {…},
    "mode": "auto",
    "pause_r": {…},
    "pause_window": 30,
    "rollback_r": {…},
    "search_steps": 4,
    "shadow_days": 20,
    "shadow_max_days": 60,
    "shadow_min_trades": {…}
  }
}
```

#### `POST /api/autopilot/<action>`

`approve` a proposal now, `reject` it, `undo` the last change, or `pause` / resume a mode's entry alerts.

**Body:** `mode` (+ `"paused": false` to resume)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode": "options"}' http://127.0.0.1:5058/api/autopilot/pause
```

```json
{
  "ok": true,
  "paused": true,
  "state": {
    "paused_at": "2026-10-09T13:49:52+05:30"
  }
}
```

### 12.9 Charts tab

#### `GET /api/charts/config`

The Charts tab's instruments, timeframes, the saved layout and the live feed's state.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/charts/config
```

```json
{
  "day_shift": 19800,
  "instruments": [
    {
      "group": "Indices",
      "label": "NIFTY 50",
      "name": "Nifty 50",
      "symbol": "IDX:13"
    }
  ],
  "layout": [],
  "market_open": true,
  "max": 16,
  "note": "Dhan prices, every second",
  "poll_ms": 1000,
  "price": "inr",
  "realtime": true,
  "shift": 19800,
  "source": "dhan",
  "tfs": [
    {
      "label": "1s",
      "s": 1
    }
  ],
  "tz": "IST"
}
```

#### `POST /api/charts/layout`

Saves the Charts tab's layout.

**Body:** `layout`: a list of `{symbol, tf, levels}`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"layout": [{"symbol": "IDX:13", "tf": 300, "levels": true}, {"symbol": "PAYTM", "tf": 900, "levels": true}]}' http://127.0.0.1:5058/api/charts/layout
```

```json
{
  "layout": [
    {
      "levels": true,
      "symbol": "IDX:13",
      "tf": 300
    },
    {
      "levels": true,
      "symbol": "PAYTM",
      "tf": 900
    }
  ],
  "ok": true
}
```

#### `GET /api/charts/candles`

Candles `[t, o, h, l, c, v]` (t in seconds, IST) and the levels to draw.

**Query:** `?symbol=`, `?tf=` seconds (1 to 604800, as in the config)

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/charts/candles?symbol=IDX:13&tf=900'
```

```json
{
  "bars": [
    [
      1786353300,
      24581.25
    ],
    [
      1786354200,
      24557.9
    ]
  ],
  "daily": false,
  "levels": [],
  "source": "dhan",
  "ticks_from": null
}
```

#### `GET /api/charts/live`

Ticks `[epoch, price]` since `since` for each symbol, from the feed that polls every second.

**Query:** `?symbols=a,b`, `?since=` epoch seconds

```sh
curl -H "Authorization: Bearer $NW_TOKEN" 'http://127.0.0.1:5058/api/charts/live?symbols=IDX:13&since=0'
```

```json
{
  "error": null,
  "market_open": true,
  "note": "Dhan prices, every second",
  "now": 1791533876.6746,
  "poll_ms": 1000,
  "realtime": true,
  "source": "dhan",
  "ticks": {
    "IDX:13": []
  }
}
```

### 12.10 Settings and connections

#### `POST /api/settings`

Saves dashboard settings; returns them all.

**Body:** `universe`, `auto_scan`, `scan_time` (`HH:MM`), `watcher`, `telegram`, `pattern_alerts`, `shorts`, `intraday`, `intraday_universe`, `intraday_shorts`, `options`, `options_alerts`, `options_level_alerts`, `news`, `news_alerts`, `indicators` (an object), and the lab's `lab_swing_universe`, `lab_intraday_universe`, `lab_years_swing`, `lab_years_intraday`, `weekly_report`

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"auto_scan": true, "scan_time": "16:15"}' http://127.0.0.1:5058/api/settings
```

```json
{
  "auto_scan": "1",
  "autopilot_policy": "{}",
  "demo": "{\"risk_pct\": 1.0, \"max_alloc_pct\": 25.0, \"intraday_leverage\": 5.0, \"f…",
  "demo_since": "2026-10-09T09:15:00+05:30",
  "indicators": "{}",
  "intraday": "1",
  "intraday_rules": "{\"min_rr\": 3.0}",
  "intraday_shorts": "1",
  "intraday_universe": "nifty500",
  "lab_intraday_universe": "niftysmallcap250",
  "lab_swing_universe": "niftysmallcap250",
  "lab_years_intraday": "1",
  "lab_years_swing": "3",
  "news": "1",
  "news_alerts": "1",
  "options": "1",
  "options_alerts": "1",
  "options_level_alerts": "0",
  "options_rules": "{}",
  "options_stocks": "RELIANCE TCS INFY",
  "pattern_alerts": "0",
  "paused_intraday": "0",
  "paused_options": "1",
  "paused_swing": "0",
  "rules": "{}",
  "scan_time": "16:15",
  "shorts": "1",
  "telegram": "1",
  "universe": "fo",
  "watcher": "1",
  "weekly_report": "1"
}
```

#### `GET /api/config`

App settings that once needed .env (Telegram, Dhan automatic login, data source, candle delays, log level): each value and where it comes from (`app`, `env` or `default`). Secrets only say whether they are set; the Telegram token says which bot it is.

```sh
curl -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/config
```

```json
{
  "dhan": {
    "client_id": "1100012345",
    "data_plan": "Active",
    "data_validity": "2026-11-04 13:26:34.0",
    "error": null,
    "live": true,
    "message": "Live",
    "mode": "auto",
    "provider": "dhan",
    "token_expiry": "2026-10-10T05:24+05:30"
  },
  "fields": {
    "data_source": {…},
    "dhan_client_id": {…},
    "dhan_pin": {…},
    "dhan_totp": {…},
    "live_watch_delay": {…},
    "log_level": {…},
    "telegram_chat_id": {…},
    "telegram_token": {…},
    "watch_delay": {…}
  },
  "telegram_configured": true
}
```

#### `POST /api/config`

Saves App settings (all or none: one bad value refuses the lot). New Dhan login details start a login at once.

**Body:** `telegram_token`, `telegram_chat_id`, `dhan_client_id`, `dhan_pin`, `dhan_totp`, `data_source` (`auto` or `yfinance`), `watch_delay`, `live_watch_delay`, `log_level`; `""` clears one back to .env

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"watch_delay": "60", "log_level": "INFO"}' http://127.0.0.1:5058/api/config
```

```json
{
  "dhan": {
    "client_id": null,
    "data_plan": null,
    "data_validity": null,
    "error": null,
    "live": false,
    "message": "Not connected",
    "mode": "off",
    "provider": "yfinance",
    "token_expiry": null
  },
  "fields": {
    "data_source": {…},
    "dhan_client_id": {…},
    "dhan_pin": {…},
    "dhan_totp": {…},
    "live_watch_delay": {…},
    "log_level": {…},
    "telegram_chat_id": {…},
    "telegram_token": {…},
    "watch_delay": {…}
  },
  "telegram_configured": true
}
```

#### `POST /api/telegram/test`

Sends a test message to the configured chat.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/telegram/test
```

```json
{
  "status": "sent"
}
```

#### `POST /api/dhan/token`

Connects Dhan with a token from web.dhan.co, after checking it with Dhan. The token is never sent back.

**Body:** `access_token`, `client_id` (optional)

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" -H 'Content-Type: application/json' \
  -d '{"access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJkaGFuQ2xpZW50SWQiOiIxMTAwMDEyMzQ1In0.signature", "client_id": "1100012345"}' http://127.0.0.1:5058/api/dhan/token
```

```json
{
  "client_id": "1100012345",
  "data_plan": "Active",
  "data_validity": "2026-11-08",
  "error": null,
  "live": true,
  "message": "Live",
  "mode": "token",
  "provider": "dhan",
  "token_expiry": "2026-10-10T12:49+05:30"
}
```

#### `POST /api/dhan/disconnect`

Forgets the Dhan token; back to yfinance.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/dhan/disconnect
```

```json
{
  "client_id": "1100012345",
  "data_plan": null,
  "data_validity": null,
  "error": null,
  "live": false,
  "message": "Not connected",
  "mode": "off",
  "provider": "yfinance",
  "token_expiry": null
}
```

#### `POST /api/universe/refresh`

Re-downloads every NSE list (28 indices + F&O) now; it answers when done (up to a minute). An index that fails keeps its previous members.

```sh
curl -X POST -H "Authorization: Bearer $NW_TOKEN" http://127.0.0.1:5058/api/universe/refresh
```

```json
{
  "built_at": "2026-10-08T17:55:59",
  "failed": [],
  "stocks": 508,
  "indices": 28
}
```

### 12.11 Signing in and the account

#### `GET /api/auth/state`

Public. Whether the account exists yet (`setup`), whether this browser is signed in, how many passkeys there are, whether passkeys work at this address, and whether sign-in is locked.

```sh
curl http://127.0.0.1:5058/api/auth/state
```

```json
{
  "setup": false,
  "signed_in": false,
  "username": null,
  "passkeys": 1,
  "passkey_origin": true,
  "rp_id": "pandorasbox.local",
  "origins": [
    "https://pandorasbox.local:5443",
    "https://192.168.1.50:5443"
  ],
  "locked_for": 0
}
```

#### `POST /api/auth/setup/begin`

First-time setup, step 1: checks the setup code and the password, and returns a new authenticator key with its QR code.

**Body:** `setup_code` (from the Pi), `username`, `password` (10+ characters)

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"setup_code": "ABCD-EFGH-JKLM", "username": "me", "password": "a long passphrase"}' http://127.0.0.1:5058/api/auth/setup/begin
```

```json
{
  "secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
  "uri": "otpauth://totp/NiftyWhale:me?secret=JBSW…&issuer=NiftyWhale&digits=6&…",
  "qr": "<svg …>"
}
```

#### `POST /api/auth/setup/finish`

Step 2: the code proves the authenticator is linked. Creates the account, signs this browser in, and returns the ten recovery codes (shown once).

**Body:** `code` from the authenticator

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"code": "123456"}' http://127.0.0.1:5058/api/auth/setup/finish
```

```json
{
  "ok": true,
  "next": "/",
  "recovery_codes": [
    "abcde-fghjk",
    "mnpqr-stuvw"
  ]
}
```

#### `POST /api/auth/login`

Sign-in, step 1. Right password: the second factor is due within 5 minutes. `423` while locked, `429` for an address with too many failures.

**Body:** `username`, `password`, `remember`

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"username": "me", "password": "a long passphrase", "remember": true}' http://127.0.0.1:5058/api/auth/login
```

```json
{
  "mfa": true,
  "methods": [
    "totp",
    "recovery"
  ]
}
```

#### `POST /api/auth/mfa`

Sign-in, step 2: an authenticator code (each accepted once) or a recovery code (each works once). Sets the session cookie.

**Body:** `code`, or `recovery_code`

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"code": "123456"}' http://127.0.0.1:5058/api/auth/mfa
```

```json
{
  "ok": true,
  "next": "/"
}
```

#### `POST /api/auth/passkey/login/options`

A passkey sign-in, step 1: WebAuthn request options (a challenge, the allowed passkeys, user verification required).

**Body:** `remember`

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"remember": true}' http://127.0.0.1:5058/api/auth/passkey/login/options
```

```json
{
  "challenge": "Nh0…",
  "timeout": 60000,
  "rpId": "pandorasbox.local",
  "allowCredentials": [
    {
      "id": "xQ…",
      "type": "public-key"
    }
  ],
  "userVerification": "required"
}
```

#### `POST /api/auth/passkey/login/verify`

Step 2: verifies the signature, the origin and the counter, and signs in.

**Body:** `credential`: the browser's answer to navigator.credentials.get, as JSON

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"credential": {"id": "…", "response": {"…": "…"}}}' http://127.0.0.1:5058/api/auth/passkey/login/verify
```

```json
{
  "ok": true,
  "next": "/"
}
```

#### `POST /api/auth/logout`

Ends this session.

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/logout
```

```json
{
  "ok": true
}
```

#### `GET /api/auth/account`

Signed-in browser only. The account: recovery codes left, passkeys, signed-in devices, API tokens (prefix only) and the last 40 sign-in events.

```sh
curl -c jar -b jar http://127.0.0.1:5058/api/auth/account
```

```json
{
  "username": "me",
  "created": "2026-10-09T18:20:11",
  "recovery_left": 10,
  "passkeys": [
    {…}
  ],
  "sessions": [
    {…}
  ],
  "tokens": [
    {…}
  ],
  "events": [
    {…}
  ]
}
```

#### `POST /api/auth/password`

Changes the password and signs every other device out.

**Body:** `current`, `new`

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"current": "old passphrase", "new": "a new long passphrase"}' http://127.0.0.1:5058/api/auth/password
```

```json
{
  "ok": true
}
```

#### `POST /api/auth/totp/begin`

Moving the authenticator, step 1: a new key and QR code. The old app keeps working until step 2.

**Body:** `password` when the sign-in is older than 10 minutes

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/totp/begin
```

```json
{
  "secret": "KRSXG5CTMVRXEZLU…",
  "uri": "otpauth://totp/…",
  "qr": "<svg …>"
}
```

#### `POST /api/auth/totp/finish`

Step 2: links the new key.

**Body:** `code` from the new app

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"code": "123456"}' http://127.0.0.1:5058/api/auth/totp/finish
```

```json
{
  "ok": true
}
```

#### `POST /api/auth/recovery/new`

Ten new recovery codes; the old ones stop working.

**Body:** `password` when needed

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/recovery/new
```

```json
{
  "recovery_codes": [
    "pqrst-uvwxy",
    "bcdef-ghjkm"
  ]
}
```

#### `POST /api/auth/passkey/register/options`

Adding a passkey, step 1: WebAuthn creation options (a discoverable credential, user verification required). Only at https://pandorasbox.local:5443.

**Body:** `password` when needed

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/passkey/register/options
```

```json
{
  "rp": {
    "id": "pandorasbox.local",
    "name": "NiftyWhale"
  },
  "user": {
    "id": "r2…",
    "name": "me",
    "displayName": "me"
  },
  "challenge": "yW…",
  "pubKeyCredParams": [
    {
      "type": "public-key",
      "alg": -7
    },
    "…"
  ],
  "authenticatorSelection": {
    "residentKey": "required",
    "userVerification": "required"
  }
}
```

#### `POST /api/auth/passkey/register/verify`

Step 2: verifies and keeps the passkey.

**Body:** `credential` (navigator.credentials.create's answer), `name`

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"credential": {"id": "…", "response": {"…": "…"}}, "name": "iPhone"}' http://127.0.0.1:5058/api/auth/passkey/register/verify
```

```json
{
  "ok": true,
  "passkeys": [
    {
      "id": 2,
      "name": "iPhone"
    }
  ]
}
```

#### `POST /api/auth/passkey/<id>/delete`

Removes a passkey.

**Body:** `password` when needed

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/passkey/2/delete
```

```json
{
  "ok": true,
  "passkeys": []
}
```

#### `POST /api/auth/sessions/<id>/revoke`

Signs one device out.

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/sessions/7/revoke
```

```json
{
  "ok": true
}
```

#### `POST /api/auth/sessions/revoke-others`

Signs every other device out.

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/sessions/revoke-others
```

```json
{
  "ok": true,
  "revoked": 2
}
```

#### `POST /api/auth/tokens`

Creates an API token. The token is in this answer only; afterwards only its first characters are shown.

**Body:** `name`, `password` when needed

```sh
curl -X POST -c jar -b jar -H 'Content-Type: application/json' \
  -d '{"name": "laptop scripts"}' http://127.0.0.1:5058/api/auth/tokens
```

```json
{
  "id": 3,
  "name": "laptop scripts",
  "token": "nwt_Q2xA9pZk…",
  "prefix": "nwt_Q2xA9pZk"
}
```

#### `POST /api/auth/tokens/<id>/revoke`

Revokes an API token at once.

```sh
curl -X POST -c jar -b jar http://127.0.0.1:5058/api/auth/tokens/3/revoke
```

```json
{
  "ok": true
}
```

---

## 13. Running, updating and backing up

```sh
cd /home/flypi/dockers/niftywhale

docker compose up -d --build     # after changing code or the template
docker compose up -d             # after changing only .env
docker compose logs -f           # follow the logs (scans, watcher, alerts)
docker compose restart           # restart without rebuilding
docker compose down              # stop (data in ./var is kept)
```

- **Backups:** copy `var/niftywhale.db` and `var/universe.json`. Back up while the app is
  stopped, or use `sqlite3 var/niftywhale.db ".backup copy.db"`. The database holds the Dhan login (client ID and token), so
  keep copies private. `var/lab/` (the lab's history and records, about 200 MB) can always be rebuilt and needn't be backed up.
- **Fresh start:** stop the app and the lab (`docker compose stop niftywhale niftywhale-lab`), move `var/niftywhale.db` and
  `var/lab/` aside, and start them again: the app creates an empty database, the lab bootstraps again (about 2.5 hours on the
  Pi) and the autopilot learns from scratch. Every setting goes back to its default. To keep the Dhan login, copy the
  settings rows `dhan_client_id`, `dhan_token_expiry` and `secret:dhan_token` into the new database before starting it (with
  automatic login in `.env` a token is made anyway). The universe is reseeded from the image if `var/universe.json` is missing.
- **Web app only:** `docker compose up -d --build --no-deps niftywhale` rebuilds and restarts the dashboard without touching a
  lab job in progress.
- **Gunicorn:** runs **one worker** on purpose. The scheduler and scan are threads in that
  process; a second worker would start a second scheduler.
- **HTTPS:** served by Pi Observatory's Caddy on `pandorasbox.local:5443` (its `Caddyfile`, mounted into that container, and
  its compose file joins NiftyWhale's network). After changing that Caddyfile: `cd ~/dockers/pi-observatory && docker compose
  up -d --no-deps pi-observatory`. NiftyWhale itself publishes port 5058 on `127.0.0.1` only.
- **Backups** hold the account too (password hash, authenticator key, passkeys): keep them as private as the Pi.

---

## 14. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Dashboard says **Server unreachable** | `docker compose ps` and `docker compose logs`. The container should be `healthy`. |
| The browser warns **the connection is not private** | The device does not trust the Pi's certificate yet: 10.4, "Trusting the certificate". |
| `pandorasbox.local` does not open | The device is not on the home network, or its network blocks mDNS names: try `https://192.168.1.50:5443` (password and code work there; passkeys need the name). |
| **No "Sign in with Face ID" button** | No passkey added yet (Account & security → Passkeys), the page was opened by IP instead of `pandorasbox.local`, or the certificate is not trusted (passkeys need a fully trusted HTTPS page). |
| **Sign-in is locked** | 8 failures in a row: wait 15 minutes, or reset it on the Pi (10.4, "Locked out?"). |
| Lost the phone with the authenticator | Sign in with a recovery code (or a passkey), then Account & security → **Move to a new phone or app**. No codes left: `reset-mfa` on the Pi (10.4). |
| A script gets **401 sign in first** | Send an API token: `-H "Authorization: Bearer <token>"` (10.4). |
| A scan finds **0 setups** | Often correct: the protocol is strict. Click the funnel steps to see where stocks stopped; *structure* removing most stocks means few bullish charts right now. |
| A scan ends with "only N of M stocks had daily candles" | yfinance (or the Pi's network) was down. The scan changed no zones. A scheduled scan tries again every 15 minutes, up to four times; run one by hand once the network is back. |
| A stock passes the screen but has **no zone** | Its setup has already been watched: it triggered, was rejected, you dismissed it, or it turned 10 days old (3.3). Or the stock has a paper trade open (one trade per stock, 3.7). A new order block on the stock arms a new zone once no trade is open. **Watch this zone** (4.6) watches it again if you want to. |
| **Daily data** count below the universe size | yfinance had no candles for some symbols (renamed or new listings). Click the card to see which. A universe **Refresh** usually fixes renamed ones. |
| Universe **Refresh** fails | niftyindices.com or NSE refused the request (they rate-limit). Try again later; the old list stays in place. |
| The watcher never checks | It only runs during market hours with the **15m zone watch** switch on. **Check now** works any time. |
| A zone is *tapped* but never triggers | It needs a sweep **and** a closed candle above a minor swing high within the last two sessions. "Waiting for a sweep" is normal; many tapped zones never trigger. |
| Telegram switch is greyed out | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` missing from `.env`, or the container wasn't restarted. |
| Test message fails | Wrong chat ID, or you haven't messaged the bot first. |
| Alerts arrive a few minutes after the candle | Expected with yfinance: its intraday data is delayed and the watcher waits `WATCH_DELAY_SECONDS`. Connect Dhan (4.10) to cut this to seconds. |
| The market status details say "Dhan: Connected, but the Data API subscription is not active" | Activate Dhan's Data API (Dhan web → My Profile → DhanHQ APIs). Until then NiftyWhale uses yfinance. |
| The market status details say "Dhan: token expired" | Paste a fresh token (**Reconnect**). With automatic login this fixes itself; check the logs for "token generation refused" if it doesn't. |
| A bearish stock has **no short setup** | Swing shorts need an F&O stock (the funnel reason says so), and **Short setups** must be on. |
| Intraday **tuner** says "no intraday candles yet" | It re-screens the last intraday scan's candles; press **Scan now** once after a restart. |
| Intraday: **no zones today** | Normal on many days (5.7). Check the 15m funnel; *structure* removing most stocks means a weak or choppy session. Make sure **Intraday mode** is on. |
| Intraday: **Scan now** sets no zones | Outside 09:30–14:30, or with the market closed, a scan is only a preview. |
| Intraday: a zone **expired** right after it was tapped | Price swept below the 15m leg's origin: the structure failed, so no CHoCH there can be an entry. The note says so. |
| Intraday: the *Data* card is amber | Dhan isn't connected or its token lapsed. 5m entries on delayed yfinance candles arrive minutes late. |

| Demo funds: a trade the app took is **not in the account** | It came before the account's start, its mode is off in Settings, or it was skipped: the **Skipped** table says why (no funds free, one lot over the risk allowed, a limit reached). |
| Smart money: **only one or two bars** of FII/DII flows | NSE publishes only the latest day, so the history builds one evening at a time from when the app first ran. |
| The command palette finds **no stocks** | Its stock list loads once per page load from `/api/palette`; reload the page. A symbol outside the universe is still offered as "Analyse … anyway". |

Logs say what each scan did, for example:
`Scan #7: 2 setups (1 in zone); zones +1 ~1 -0; funnel {...}`.

---

## 15. Development

### Tests

```sh
# Once: a test image with the software authenticator the passkey tests use (requirements-dev.txt).
printf 'FROM niftywhale:latest\nRUN pip install fido2 && pip install --no-deps soft-webauthn==0.1.4\n' | docker build -t niftywhale-test -

docker run --rm --entrypoint python -v "$PWD:/w" -w /w -e DB_PATH=/tmp/test.db -e UNIVERSE_PATH=/tmp/u.json \
  -e NIFTYWHALE_AUTH=0 -e NIFTYWHALE_SCHEDULER=0 -e DHAN_CLIENT_ID= -e DHAN_PIN= -e DHAN_TOTP_SECRET= \
  -e DHAN_ACCESS_TOKEN= -e TELEGRAM_BOT_TOKEN= -e TELEGRAM_CHAT_ID= niftywhale-test -m unittest discover -s tests
```

`NIFTYWHALE_AUTH=0` lets the feature tests reach the API without signing in; `tests/test_auth.py` switches sign-in back on
for itself. The empty Dhan and Telegram variables keep the tests from reading the real ones in `.env` (a Dhan login from a
test would replace the live app's token).

The tests use a throwaway database (`DB_PATH` above); the test files that write refuse to run
against any database outside the temporary directory, so they can never touch `var/`.

The tests build charts by hand, so the right answer is known:

- a textbook bullish leg, and each way it should fail (thin volume, mirrored bearish chart,
  shallow pullback, broken order block, too little history);
- a 15-minute sweep-and-CHoCH sequence, an R:R rejection, an unfinished candle that must be
  ignored, and a stock still falling;
- the rule overrides' clamping and their effect on the screen;
- shorts: a flipped chart's short is the exact mirror of the original long (zone, target, R:R,
  FVGs), each side fails the other's structure, F&O-only blocking, the short trigger and its
  real-price stop, short outcomes, and the bearish pattern twins;
- the intraday tuner's clamping, and a looser discount admitting a shallow pullback;
- the indicators against hand-worked numbers: delta's volume split and tick rule, 1-minute
  deltas summed into their bar, cumulative delta restarting each session, VWAP and its daily
  anchor, Bollinger Bands, the opening range and its first breakout;
- the funnel: every card's "−N here" equals its "who stopped here" list, in both modes;
- intraday mode: a 15m setup and its target pool, forming candles ignored, the daily filters,
  the 5m trigger with the 14:30 cutoff, taps before the zone existed, a sweep below the leg
  origin, and trade outcomes (target, stop, both in one candle, open then squared off);
- regressions (`tests/test_regressions.py`), each named after the bug it pins: the swing
  watcher ignoring a CHoCH from before its zone existed, spent setups not re-armed in either
  mode, a dismissal mid-check getting no alert, scans surviving a data outage and a database
  error, the evening scan's retry, daily candles cached before the close not passing for the
  close, NaN and junk rule values, M&M-style symbols, secrets kept out of error text, the
  square-off waiting for its candle, old triggers leaving the live board, the 15m scan
  not holding up the 5m check, and the command palette's index (`/api/palette`);
- swing paper trades (`tests/test_paper.py`): target, stop, a gap through the stop filled at the
  open, both levels in one candle, shorts, each candle examined once, forming candles ignored,
  daily candles filling a gap after downtime (but never the entry day's), an alert becoming a
  trade that closes on its target with one message, older triggers left alone, and one trade per
  stock.
- the news desk (`tests/test_news.py`): parsing NSE filings and Google News, which headlines name the company (and which are
  about an overseas listing), housekeeping filings, tags, filing alerts, storage, FinBERT's client and the backlog, the price
  reaction on 15m candles and daily closes, the tone breakdown and the API.
- demo funds (`tests/test_demo.py`): sizing on each instrument (delivery shares, futures lots, intraday shares, option lots) and
  every skip reason, the charges line by line at the 2026 rates (STT, NSE transaction + IPFT, SEBI, stamp, GST, DP), monthly
  expiries and futures rolls, settling longs and shorts, the statement tying out to the balance, and the account mirroring trades: only those after it started,
  sized from the account at their entry, marked while open, settled with margin released, a trade opened and closed between two
  checks, withdrawals limited to free funds, modes switched off, the open-at-once and per-day limits, the statement's lines,
  summary and CSV, and starting over from now, from the app's first trade or from a date (and refusing a future one).
- indicator settings (`tests/test_indicator_settings.py`): defaults, clamping and junk, the settings reaching the indicators, and the
  settings API storing them made safe.
- smart money (`tests/test_smart.py`): parsing bulk / block deals, FII/DII flows, participant OI and delivery, client types, the
  delivery read, positioning (every participant's series), the backfill (holidays and files not out yet), the trade context (nothing from the entry's own
  day), the breakdowns and the API.
- swing context gates (`tests/test_gates.py`): off never blocks, each gate and its edge, the market run
  read the trade's way, missing context never blocks, records keep their key, a gated CHoCH spends
  its setup in the backtester, the autopilot's ladders, and the live watcher rejecting a gated
  CHoCH with its reason but never a zone you set yourself.
- learning (`tests/test_learn.py`): statistics, drawdown and breakdown flags (never on small
  buckets); the backtester's swing and intraday life cycles rule by rule (zones from the screen,
  refresh, filters, one trade per stock, setups not re-armed, lifetime, late CHoCHs, tapped zones
  frozen, the cutoff); the autopilot finding a real edge out of sample and refusing noise, step
  limits, shadow / rollback / pause / resume; options candidates, their outcomes and limits; the
  lab's job queue, promotions with approval and undo, and the API's validation.
- the index ticker (`tests/test_ticker.py`): which session the quotes belong to, the previous
  close skipping today's own candle, day position, GIFT Nifty without a day range, and one Dhan call
  per interval however many browsers poll.
- options mode (`tests/test_options.py`): chain parsing and trimming, PCR, max pain worked by hand,
  OI walls, the four buildups, OI bias (against yesterday and against the open), ATM IV and 25-delta
  skew, IV percentile, unusual activity ignoring tiny strikes, the idea rules and each reason for no
  idea, following an idea to target, stop, a broken OI level and the square-off, settling after the
  close, pruning old chains, the API's validation, and a whole morning through `fetch_options` with
  Dhan and Telegram mocked: one idea, one entry message, one result message.

### Rebuilding the universe by hand

```sh
docker run --rm --entrypoint python -v "$PWD:/w" -w /w -e UNIVERSE_PATH=/w/var/universe.json \
  niftywhale:latest -m niftywhale.universe
```

### Changing the protocol

All the logic is in `niftywhale/smc.py`, with no network or database access, so a change can be
tested against hand-built charts first. If you add a step:

1. add it to `STEPS`, in order (the funnel and the "why not?" lists follow automatically);
2. add the reason string in `evaluate()`;
3. add a test.

If you add a tunable rule, give it an entry in `TUNABLE` and it appears in the tuner.

Intraday mode reuses `smc.evaluate()` and `smc.choch_trigger()`, and shorts are those same
functions on the mirrored chart, so a change there changes both modes and both sides. Write
changes for the long; the short follows. Intraday-only logic (target pools, the clock, outcomes) is in `niftywhale/intraday.py`.

---

## 16. Limits

- **Swing shorts are F&O stocks only,** and NiftyWhale doesn't choose the contract. Its levels are
  the stock's (cash) prices. Futures trade at a small premium and in lots; options behave
  differently again. Size and place the F&O trade yourself.
- **Paper trades are the alerts' results, not your fills.** Both journals assume you got the
  CHoCH close and your target exactly, your stop exactly (swing: or the open, when price gapped
  through it), and paid no costs. They assume the worse case when one candle touches both levels.
- **Demo funds are an estimate.** Charges come from the published rates, not a contract note; futures margin is a flat
  percentage, not NSE's SPAN; fills are the app's paper prices with no slippage (4.16).
- **Smart money is end-of-day and public.** It shows what NSE publishes after each session; no one's live orders are visible.
- **Intraday needs Dhan in practice.** On yfinance its 5-minute entries arrive minutes late.
- **Swing mode reads daily structure only.** The doc also allows 4-hour charts; not implemented.
- **Delayed intraday data without Dhan.** yfinance 15-minute candles lag, and the watcher checks
  once per candle. Connect Dhan (4.10) for real-time prices. Even then the watcher works on
  closed 15-minute candles, by design.
- **No exchange holiday calendar.** On a holiday the watcher just finds no new candles.
- **The doc's judgement calls are fixed numbers.** "Massive rally", "minor swing high" and
  similar phrases are the thresholds in section 11. Tune them against charts you trust.
- **Analysis only.** NiftyWhale never places, modifies or cancels an order, and nothing it shows
  is investment advice.

---

## 17. Glossary

Every abbreviation, code and symbol the dashboard shows. The same list is in the app: press
**Legend** in the top bar (or **?** anywhere, or **?** next to a chart's controls).

<!-- glossary:start (generated from niftywhale/glossary.py; edit it there) -->
### Smart Money Concepts (the protocol)

| Term | Stands for | Meaning |
|---|---|---|
| **SMC** | Smart Money Concepts | Reading a chart for the footprints large institutions leave: where they bought or sold, and where other traders' stops sit. |
| **HH / HL** | Higher high / higher low | Each swing high and swing low above the last one: a bullish structure, the basis for a long. |
| **LH / LL** | Lower high / lower low | Each swing high and swing low below the last one: a bearish structure, the basis for a short. |
| **Swing high / low** | Swing point | A candle whose high (low) is beyond the candles either side of it: 3 each side on daily charts, 2 on 15m and 5m. |
| **BOS** | Break of structure | The first close beyond a previous swing high (or low, for a short): the trend continuing. |
| **CHoCH** | Change of character | After a sweep, a candle closing back beyond the last minor swing point: the turn that is the entry signal. |
| **OB** | Order block | The body of the last opposite-colour candle before a strong move: where the move was launched. Drawn as a blue box. |
| **FVG** | Fair value gap | A three-candle gap inside a strong move (candle 1 and candle 3 don't overlap), so price skipped that range. Drawn as amber boxes. |
| **BSL** | Buy-side liquidity | Untaken swing highs above price, where buy stops cluster. A long's target. |
| **SSL** | Sell-side liquidity | Untaken swing lows below price, where sell stops cluster. A short's target. |
| **Liquidity pool** | Resting orders | A level where stops and orders gather (BSL, SSL, yesterday's high or low, the opening range). Price is often drawn to it. |
| **Leg** | The impulse move | The move from its origin to its far end after the break of structure. |
| **Origin** | Where the leg started | The leg's first extreme: a low for a long, a high for a short. The provisional stop sits just beyond it. |
| **Displacement** | "Massive" move | A leg at least 1.5 ATR (daily) or 2 × the 15m ATR (intraday) from origin to break. |
| **EQ** | Equilibrium | The 50% midpoint of the leg. Drawn as the dashed "EQ 50%" line. |
| **Discount / premium** | The cheap / expensive half | Below EQ is discount, where longs are taken; above EQ is premium, where shorts are taken. |
| **Leg position** | How far price has come back | Distance back from the leg's origin: 0% at the origin, 50% at EQ. Must be under 50%. |
| **Zone** | The alert zone | The order block, widened to any open FVG in the discount (or premium) half. Where the watcher waits for price. |
| **Sweep** | Liquidity grab | Price briefly taking out a recent swing low (high, for a short), triggering the stops there, before turning. |
| **Tap** | Into the zone | The first candle that trades into the zone after it was set. |

### Trades and risk

| Term | Stands for | Meaning |
|---|---|---|
| **R:R** | Reward-to-risk | (target − entry) ÷ (entry − stop). 1:3 means the target is three times as far as the stop. |
| **1:3 / 1:2** | Minimum R:R | Swing alerts need at least 1:3, intraday alerts at least 1:2 (both tunable). |
| **R** | Result in risk units | A trade's result as a multiple of what it risked: +2.4R made 2.4× the risk, −1R hit the stop. |
| ***** | Provisional | Before the trigger: the stop is beyond the whole leg's origin. The real stop, beyond the sweep, is tighter. |
| **Stop*** | Provisional stop | The chart line for that provisional stop. |
| **Long (up triangle)** | Buy setup | Bullish structure; profit if price rises. |
| **Short (down triangle)** | Sell setup | Bearish structure; profit if price falls. |
| **F&O** | Futures & options | NSE stocks with derivatives. A swing short (held overnight) needs them. |
| **MIS** | Margin intraday square-off | An intraday product: positions, shorts included, must close the same day. Any stock can be shorted this way. |
| **Square-off** | Closing out | Exiting an intraday position. NiftyWhale squares off open intraday trades at 15:20. |
| **Entry cutoff** | No new entries | Intraday CHoCHs after 14:30 are rejected. |
| **Paper trade** | An alert taken on paper | Every entry alert, followed as if taken exactly: in at the CHoCH close, out at the target or the stop (or the 15:20 square-off, intraday), no costs. Nothing is ever ordered. |

### Chart indicators and levels

| Term | Stands for | Meaning |
|---|---|---|
| **O / H / L / C** | Open, high, low, close | A candle's four prices. The magnet snaps to these; the tooltip underlines the one it snapped to. |
| **OHLC** | Open-high-low-close | A candle's four prices together. |
| **Vol** | Volume | Shares traded in the candle. |
| **ATR** | Average true range | Average candle range over 14 candles (Wilder smoothing): how much a stock typically moves. |
| **ATR%** | ATR as % of price | Daily ATR divided by price. The swing screen needs over 1.5%, intraday 1% or more. |
| **Δ est.** | Estimated delta | Buying minus selling volume, estimated from where each 1-minute candle closed in its range. True delta needs tick data. |
| **Cum Δ** | Cumulative delta | Running total of delta: since 09:15 on intraday charts, across the visible range on daily charts. |
| **VWAP** | Volume-weighted average price | The average price weighted by volume, from 09:15 each session. Purple line. |
| **ORB** | Opening range (breakout) | The high-low of the first 15 minutes, shaded across the session. |
| **ORB▲ / ORB▼** | Opening range breakout / breakdown | The first candle closing above (below) the opening range. |
| **OR H / OR L** | Opening range high / low | The first 15-minute candle's high and low, as lines when the ORB box is off. |
| **PDH / PDL** | Previous day high / low | Yesterday's high and low: common intraday liquidity pools and targets. |
| **BB** | Bollinger Bands | 20-candle average (dashed) ± 2 standard deviations, shaded. |
| **EQ 50%** | Equilibrium line | The leg's midpoint, dashed grey. |
| **Target** | Target line | The liquidity the setup aims for, green. |
| **watch** | Watch-zone box | Dashed amber box: the zone the watcher is waiting on. |

### Candlestick pattern codes

| Term | Stands for | Meaning |
|---|---|---|
| **BE** | Bullish engulfing | A down candle, then an up candle whose body covers it. |
| **H** | Hammer / pin bar | Long lower wick, little above the body: sellers pushed down, buyers took it back. |
| **IB** | Inside-bar breakout | A candle inside the one before it, then an up close above its high. |
| **MS** | Morning star | Strong down candle, small pause, then an up candle closing above the first one's middle. |
| **BrE** | Bearish engulfing | An up candle, then a down candle whose body covers it. |
| **SS** | Shooting star | Long upper wick, little below the body: buyers pushed up, sellers took it back. |
| **IBd** | Inside-bar breakdown | A candle inside the one before it, then a down close below its low. |
| **ES** | Evening star | Strong up candle, small pause, then a down candle closing below the first one's middle. |
| **▲ / ▼ marker** | Where a pattern completed | ▲ under the candle for bullish patterns, ▼ above it for bearish ones. |
| **●  / amber** | Inside the zone | The pattern formed inside the stock's zone: the ones that matter. |

### Statuses and checklist marks

| Term | Stands for | Meaning |
|---|---|---|
| **watching** | Zone set | Waiting for price to reach the zone. |
| **tapped** | Price in the zone | Waiting for a sweep and a CHoCH. |
| **triggered / open trade** | Entry signalled | A CHoCH passed the R:R rule: an entry alert went out, and its paper trade is running. |
| **rejected** | Signal skipped | A CHoCH came, but R:R was too low (or, intraday, after 14:30). |
| **expired** | No longer watched | The scan stopped passing it, it was too old, the structure failed, or the session ended. |
| **dismissed** | Removed by you | You stopped watching it. |
| **target / won** | Hit the target | A paper trade (swing or intraday) that reached its target. |
| **stop / lost** | Hit the stop | A paper trade that reached its stop: −1R, or worse when price gapped through it overnight. |
| **squared off / closed** | Closed at 15:20 | An intraday trade still open at square-off, closed at that price. |
| **≈** | Approximate result | Part of a swing paper trade was judged on daily candles: the app was off for longer than 15m candles go back. |
| **tick · cross · … · –** | Checklist marks | A tick: passed. A cross: failed. …: waiting. –: not applicable. |
| **−N here** | Funnel drop | How many stocks stopped at that funnel step. Click the card to see which. |
| **forming** | Live candle | The current candle, not closed yet: shown faded and never used for signals. |

### Options mode

| Term | Stands for | Meaning |
|---|---|---|
| **OI** | Open interest | Contracts open at a strike, shown in shares (contracts × lot). Writers hold most of it, so big OI is where writers expect price not to go. |
| **ΔOI** | Change in OI | OI added (+) or closed (−) since yesterday's close, or since the session's first read where marked. |
| **CE / PE** | Call / put | Call option (European, "CE") and put option ("PE"). |
| **ATM** | At the money | The strike nearest the underlying's price. Ideas buy this strike. |
| **ITM / OTM** | In / out of the money | A call below spot (a put above it) is in the money; the other side is out of the money. |
| **PCR** | Put-call ratio | Total put OI ÷ total call OI in the chain. Rising PCR in the session means puts are being written: usually read as bullish. |
| **Max pain** | Least-payout strike | The expiry price at which option buyers together would collect the least. Price often drifts toward it near expiry. |
| **Support / resistance (OI)** | OI walls | The strike with the most put OI below spot (support) and the most call OI above it (resistance). |
| **Long buildup** | Price ↑ OI ↑ | Buyers opening positions. On a call: bullish; on a put: bearish. |
| **Short buildup** | Price ↓ OI ↑ | Writers opening positions. On a call: bearish (a cap); on a put: bullish (a floor). |
| **Short covering** | Price ↑ OI ↓ | Writers closing at a loss. On a call: bullish; on a put: bearish. |
| **Long unwinding** | Price ↓ OI ↓ | Buyers closing. On a call: bearish; on a put: bullish. |
| **OI bias** | Writing balance | From −1 to +1: put OI added minus call OI added, over the 5 strikes either side of the money. + is bullish, − bearish. |
| **IV** | Implied volatility | The yearly volatility an option's price implies. High IV makes options dear to buy. |
| **IV pct** | IV percentile | Share of past sessions whose ATM IV was below today's. Shown after 20 sessions of history. |
| **Skew** | 25-delta skew | Put IV minus call IV at about 25 delta. Positive is normal (insurance costs more); a jump means fear. |
| **DTE** | Days to expiry | Calendar days until the expiry; "expires today" on expiry day. |
| **Lot** | Lot size | Shares per contract (Nifty 65, Bank Nifty 30, …). ₹ / lot in the journal is the result for one lot. |

### Performance and the autopilot

| Term | Stands for | Meaning |
|---|---|---|
| **Total R** | Sum of results | All closed trades' results added up, in R. +12R made twelve times the usual risk. |
| **Avg R** | Expectancy | Total R ÷ trades: what an average trade made. Above zero means the rules make money before costs. |
| **PF** | Profit factor | R won by winners ÷ R lost by losers. Above 1 is profitable; 1.5+ is comfortable. |
| **Max drawdown** | Deepest dip | The largest fall of the cumulative R from a previous high: the worst run you would have sat through. |
| **Backtest** | Replay of history | Years of candles run through the same rule code as the live alerts, in the lab container. |
| **Out of sample** | Unseen data | Results on a period the tuning never looked at: the only honest test of tuned rules. |
| **Walk-forward** | Tune, then test ahead | Tune on the past, judge on the next 3 months, roll forward; repeated across the history. |
| **Shadow** | Trial run | Proposed rules replayed on new data next to the live rules before anything changes. |
| **Rollback** | Change undone | The autopilot puts the previous rules back when live results after a change go badly. |
| **Paused** | Alerts muted | A mode's Telegram entry alerts stop after a losing streak; trades are still recorded. |
| **Candidate** | Near-idea | An options read that came close to an idea, kept so the autopilot can test looser or tighter rules. |

### Market, data and units

| Term | Stands for | Meaning |
|---|---|---|
| **NSE** | National Stock Exchange of India | Where every stock here trades. |
| **IST** | India Standard Time | UTC+5:30. Every time in the app is IST. |
| **Nifty 50 / 100 / 200 / 500** | NSE indices | The largest 50, 100, 200 or 500 NSE companies, from niftyindices.com. |
| **PSU** | Public sector undertaking | A government-owned company (as in Nifty PSU Bank). |
| **CPSE / PSE** | Central public sector enterprise / public sector enterprise | Government-owned company indices. |
| **FMCG** | Fast-moving consumer goods | A sector index. |
| **IT** | Information technology | A sector index. |
| **MNC** | Multinational company | Indian-listed subsidiaries of foreign groups. |
| **D / 5m / 15m / 1m** | Timeframes | Daily, 5-minute, 15-minute and 1-minute candles. |
| **3M / 6M / 1Y** | Chart range | The last 3 months, 6 months or 1 year of daily candles. |
| **L** | Lakh | 100,000. 10 L = 1,000,000 shares. |
| **Cr** | Crore | 10,000,000 (100 lakh). |
| **K** | Thousand | 1,000. |
| **Dhan** | Broker data feed | Real-time quotes and candles from Dhan's Data API, when connected. |
| **yfinance** | Yahoo Finance data | Free data, delayed a few minutes. Used when Dhan isn't connected. |
| **TOTP** | Time-based one-time password | The 6-digit authenticator code used for Dhan's automatic daily login. |
<!-- glossary:end -->
