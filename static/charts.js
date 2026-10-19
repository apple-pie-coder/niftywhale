/* The Charts tab: as many live candle charts as you like, laid out to fill the screen.
 *
 * The app gives it a root element and its API under /api/charts:
 *   GET  config                       instruments, timeframes, the saved layout, the feed
 *   GET  candles?symbol=&tf=          [[t, o, h, l, c, v]] (t: seconds, already in IST) + levels
 *   GET  live?symbols=a,b&since=      ticks [[epoch, price]] since `since`, for every symbol
 *   POST layout                       [{symbol, tf, levels}]
 *   POST drawings                     {symbol, drawings: [{id, type: trend|hline|rect, p1: {t, p}, p2, color}]}
 *                                     (config carries them all as `drawings`; without the route they stay in this browser)
 *
 * A magnet (toolbar) snaps the crosshair and drawings to the candle's open, high, low or close.
 * One poll for all charts brings the new ticks; each chart folds them into its last candle or
 * starts the next one, so a 1-second chart moves every second and a weekly one updates its week.
 * When the page has a live socket (window.LiveSocket: up, sub(channel, params), unsub(channel),
 * listen(type, fn); NiftyWhale's /ws) the ticks are pushed on its 'ticks' channel instead, as they
 * trade; while it is down the poll takes over.
 * Candles are fetched again now and then to pick up volume and correct the forming candle.
 * Drawn with TradingView's Lightweight Charts (static/vendor, Apache 2.0).
 */
(function () {
    'use strict';
    const API = '/api/charts';
    const css = `
.ch-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0 0 10px}
.ch-bar .ch-count{color:var(--ink-3);font-size:12.5px}
.ch-feed{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--ink-3);margin-left:auto;max-width:100%}
.ch-feed i{width:8px;height:8px;border-radius:50%;background:var(--ink-3);flex:none}
.ch-feed[data-live="1"] i{background:var(--good);box-shadow:0 0 0 0 var(--good);animation:chpulse 1.6s infinite}
.ch-feed[data-live="0"] i{background:var(--warn)}
@keyframes chpulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--good) 55%,transparent)}100%{box-shadow:0 0 0 7px transparent}}
.ch-add{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-pill);padding:3px 4px 3px 10px}
.ch-add input,.ch-add select,.ch-head input,.ch-head select{font:inherit;font-size:13px;color:var(--ink);background:transparent;border:0;outline:0;min-width:0}
.ch-add input{width:150px}
.ch-add button{border:0;background:var(--accent);color:var(--accent-ink);font:inherit;font-size:13px;font-weight:600;border-radius:var(--r-pill);padding:5px 12px;cursor:pointer;display:inline-flex;align-items:center;gap:5px}
.ch-add button:disabled{opacity:.5;cursor:default}
.ch-grid{display:grid;gap:8px;min-height:360px}
.ch-p{container-type:inline-size;position:relative;display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden}
.ch-p[hidden]{display:none!important}
.ch-head{display:flex;align-items:center;gap:6px;padding:5px 6px 5px 10px;border-bottom:1px solid var(--line);min-width:0}
.ch-head input{font-weight:700;flex:none;text-overflow:ellipsis;max-width:40%}
.ch-head select{flex:none}
.ch-head input:focus{background:var(--sunk);border-radius:5px}
.ch-head select{color:var(--ink-2);background:var(--sunk);border-radius:5px;padding:2px 4px;cursor:pointer}
.ch-px{font-family:var(--mono);font-size:13px;font-variant-numeric:tabular-nums;margin-left:4px;white-space:nowrap;transition:color .25s}
.ch-px[data-d="1"]{color:var(--up)} .ch-px[data-d="-1"]{color:var(--down)}
.ch-chg{font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
.ch-chg[data-d="1"]{color:var(--up)} .ch-chg[data-d="-1"]{color:var(--down)}
.ch-sp{flex:1}
.ch-ib{border:0;background:transparent;color:var(--ink-3);width:26px;height:26px;border-radius:6px;display:inline-grid;place-items:center;cursor:pointer;flex:none}
.ch-ib:hover{background:var(--sunk);color:var(--ink)}
.ch-ib[aria-pressed="true"]{color:var(--accent)}
.ch-body{position:relative;flex:1;min-height:0}
.ch-leg{position:absolute;left:8px;top:6px;z-index:3;pointer-events:none;font-family:var(--mono);font-size:11.5px;color:var(--ink-2);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;max-width:calc(100% - 80px)}
.ch-leg b{font-weight:500;color:var(--ink-3);margin-right:2px}
.ch-msg{position:absolute;inset:0;display:grid;place-items:center;text-align:center;padding:16px;color:var(--ink-3);font-size:13px;z-index:2;pointer-events:none}
.ch-msg[hidden]{display:none}
.ch-note{position:absolute;right:62px;bottom:30px;z-index:3;font-size:11px;color:var(--ink-3);background:color-mix(in srgb,var(--surface) 85%,transparent);padding:1px 6px;border-radius:4px;pointer-events:none}
.ch-note:empty{display:none}
.ch-empty[hidden]{display:none}
.ch-empty{border:1px dashed var(--line-strong);border-radius:var(--r-md);padding:40px 16px;text-align:center;color:var(--ink-3)}
@container (max-width:400px){.ch-chg{display:none}}
@container (max-width:330px){.ch-ib[data-a="fit"],.ch-ib[data-a="lv"]{display:none}}
@media (max-width:700px){.ch-feed{margin-left:0}.ch-add input{width:120px}}
.ch-tools{display:inline-flex;align-items:center;gap:2px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-pill);padding:3px}
.ch-tools button{border:0;background:none;color:var(--ink-3);width:30px;height:28px;border-radius:var(--r-pill);display:inline-grid;place-items:center;cursor:pointer}
.ch-tools button:hover:not(:disabled){background:var(--sunk);color:var(--ink)}
.ch-tools button[aria-pressed="true"]{background:var(--accent);color:var(--accent-ink)}
.ch-tools button:disabled{opacity:.35;cursor:default}
.ch-tools .sep{width:1px;height:18px;background:var(--line);margin:0 3px}
.ch-tools .sw{width:14px;height:14px;border-radius:50%;border:2px solid var(--surface);box-shadow:0 0 0 1px var(--line-strong)}
.ch-toolwrap{position:relative;display:inline-flex}
.ch-pal{position:absolute;z-index:20;top:calc(100% + 6px);left:0;background:var(--surface);border:1px solid var(--line-strong);border-radius:12px;
  box-shadow:0 10px 30px rgba(0,0,0,.18);padding:10px;display:grid;grid-template-columns:repeat(4,26px);gap:8px;width:max-content}
.ch-pal[hidden]{display:none}
.ch-pal .sw-b{width:26px;height:26px;border-radius:50%;border:2px solid var(--surface);box-shadow:0 0 0 1px var(--line-strong);cursor:pointer;padding:0}
.ch-pal .sw-b[aria-pressed="true"]{box-shadow:0 0 0 2px var(--ink)}
.ch-pal .pal-row{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12px;color:var(--ink-2)}
.ch-pal input[type=color]{width:34px;height:26px;border:1px solid var(--line-strong);border-radius:6px;padding:0;background:none;cursor:pointer}
.ch-pal .pal-def{border:0;background:none;color:var(--accent);font:inherit;font-size:12px;cursor:pointer;padding:0}
.ch-body[data-draw="1"]{cursor:crosshair;touch-action:none}
.ch-body[data-grab="1"]{cursor:move}
@media (pointer:coarse){.ch-tools button{width:36px;height:34px}}
.ch-leg .sts{display:flex;flex-direction:column;align-items:flex-start;gap:1px;margin-top:3px}
.ch-st{display:inline-flex;align-items:center;gap:6px;pointer-events:auto;max-width:100%;padding:0 4px;border-radius:4px;line-height:18px;
  background:color-mix(in srgb,var(--surface) 78%,transparent)}
.ch-st:hover{background:var(--surface)}
.ch-st .nm{color:var(--ink-2);font-family:var(--font);font-size:11.5px;cursor:pointer}
.ch-st .v{font-variant-numeric:tabular-nums}
.ch-st .note{color:var(--ink-3);font-family:var(--font);font-size:11px}
.ch-st[data-off="1"] .nm,.ch-st[data-off="1"] .v{opacity:.45}
.ch-st button{border:0;background:none;color:var(--ink-3);width:18px;height:18px;padding:0;border-radius:4px;display:inline-grid;place-items:center;cursor:pointer;opacity:0}
.ch-st:hover button,.ch-st:focus-within button{opacity:1}
.ch-st button:hover{background:var(--sunk);color:var(--ink)}
@media (pointer:coarse){.ch-st button{opacity:1;width:24px;height:24px}}
.ch-pleg{position:absolute;left:8px;z-index:3;pointer-events:none;font-family:var(--mono);font-size:11.5px;color:var(--ink-2);white-space:nowrap;max-width:calc(100% - 80px);overflow:hidden}
.ch-pop{position:absolute;z-index:30;top:40px;right:6px;width:min(330px,calc(100% - 12px));max-height:calc(100% - 48px);overflow:auto;background:var(--surface);
  border:1px solid var(--line-strong);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.18);padding:10px;font-size:13px;color:var(--ink)}
.ch-pop[hidden]{display:none}
.ch-pop h4{margin:0 0 8px;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:space-between;gap:8px}
.ch-pop input[type=search]{width:100%;box-sizing:border-box;font:inherit;font-size:13px;color:var(--ink);background:var(--sunk);border:1px solid var(--line);border-radius:8px;padding:6px 9px;outline:0}
.ch-pop .grp{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3);margin:10px 4px 3px}
.ch-pop .it{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;border:0;background:none;padding:6px 8px;border-radius:7px;cursor:pointer;text-align:left;font:inherit;font-size:13px;color:var(--ink)}
.ch-pop .it:hover:not(:disabled){background:var(--sunk)}
.ch-pop .it:disabled{opacity:.45;cursor:default}
.ch-pop .it small{color:var(--ink-3);font-size:11.5px;flex:none}
.ch-pop .row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:7px 0;font-size:12.5px;color:var(--ink-2)}
.ch-pop .row input[type=number],.ch-pop .row select{width:96px;font:inherit;font-size:12.5px;color:var(--ink);background:var(--sunk);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
.ch-pop .row input[type=color]{width:40px;height:24px;border:1px solid var(--line-strong);border-radius:6px;padding:0;background:none;cursor:pointer}
.ch-pop .sub{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3);margin:12px 0 2px}
.ch-pop .foot{display:flex;flex-wrap:wrap;gap:6px 14px;justify-content:space-between;align-items:center;margin-top:12px;padding-top:8px;border-top:1px solid var(--line)}
.ch-pop .lnk{border:0;background:none;color:var(--accent);font:inherit;font-size:12.5px;cursor:pointer;padding:0}
.ch-pop .lnk:disabled{color:var(--ink-3);cursor:default}
.ch-pop .empty{color:var(--ink-3);font-size:12.5px;padding:8px 4px}
.ch-pop .x{border:0;background:none;color:var(--ink-3);cursor:pointer;width:22px;height:22px;border-radius:6px;display:inline-grid;place-items:center;padding:0}
.ch-pop .x:hover{background:var(--sunk);color:var(--ink)}
`;
    const IC = {
        camera: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M2 5.2c0-.7.5-1.2 1.2-1.2h1.6l1-1.5h4.4l1 1.5h1.6c.7 0 1.2.5 1.2 1.2v6.6c0 .7-.5 1.2-1.2 1.2H3.2C2.5 13 2 12.5 2 11.8z"/><circle cx="8" cy="8.3" r="2.4"/></svg>',
        plus: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>',
        x: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
        max: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"/></svg>',
        min: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 2v4H2M10 2v4h4M6 14v-4H2M10 14v-4h4"/></svg>',
        lv: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M2 4h12M2 8h12" stroke-dasharray="2 2"/><path d="M2 12h12"/></svg>',
        fit: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 8h12M5 5L2 8l3 3M11 5l3 3-3 3"/></svg>',
        cursor: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 2.5l9 5.2-4 1-2 3.8z"/></svg>',
        trend: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3 13L13 3"/><circle cx="3" cy="13" r="1.6" fill="currentColor"/><circle cx="13" cy="3" r="1.6" fill="currentColor"/></svg>',
        hline: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M1.5 8h13"/><path d="M4 4.5h8M4 11.5h8" stroke-width="1" opacity=".45"/></svg>',
        rect: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="2.5" y="4" width="11" height="8" rx="1"/></svg>',
        magnet: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3.5 2.5v5.5a4.5 4.5 0 0 0 9 0V2.5"/><path d="M3.5 5.5h3M9.5 5.5h3"/></svg>',
        undo: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 3.5 2.5 6.5l3 3"/><path d="M2.5 6.5h7a4 4 0 0 1 0 8H7"/></svg>',
        redo: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.5 3.5l3 3-3 3"/><path d="M13.5 6.5h-7a4 4 0 0 0 0 8H9"/></svg>',
        ind: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1.5 12c1.6-4.5 3-6.5 4.5-3.2S9 12.5 10.5 7.5 13.2 3 14.5 4"/><path d="M1.5 14.5h13" opacity=".45"/></svg>',
        gear: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" stroke-linecap="round"/></svg>',
        eye: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>',
        eyeOff: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" opacity=".5"/><path d="M2.5 13.5l11-11"/></svg>',
        trash: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 4.2h11M6.2 4.2V2.8h3.6v1.4M3.9 4.2l.7 9h6.8l.7-9"/></svg>',
    };
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const getJSON = async (url, opt) => { const r = await fetch(url, opt); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText); return r.json(); };

    let gridTop = 0, cfg = null, root = null, grid = null, charts = [], since = 0, pollT = null, saveT = null, maxed = null, uid = 0, started = false;

    const dp = (p) => !cfg || cfg.price !== 'fx' ? 2 : (p < 10 ? 5 : p < 1000 ? 3 : 2);
    const fmt = (p, d) => p == null || !isFinite(p) ? '—' : (+p).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
    const tfLabel = (s) => ((cfg.tfs.find((t) => t.s === s) || {}).label) || s + 's';
    const instOf = (s) => cfg.instruments.find((i) => i.symbol === s);
    const labelOf = (s) => { const i = instOf(s); return i ? i.label : s; };
    // What was typed or picked: a symbol, a label ("EUR/USD", "NIFTY 50") or a name.
    const resolve = (v) => { v = String(v || '').trim().toUpperCase(); if (!v) return null;
        return cfg.instruments.find((i) => i.symbol.toUpperCase() === v || i.label.toUpperCase() === v)
            || cfg.instruments.find((i) => i.name.toUpperCase() === v)
            || cfg.instruments.find((i) => i.label.toUpperCase().replace(/[^A-Z0-9]/g, '') === v.replace(/[^A-Z0-9]/g, '')); };
    const visible = () => root && !root.closest('[hidden]') && !document.hidden;

    function theme() {
        const v = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
        return { bg: v('--surface'), ink: v('--ink-3'), line: v('--line'), strong: v('--line-strong'), up: v('--up'), down: v('--down'),
            accent: v('--accent'), warn: v('--warn'), bad: v('--bad'), good: v('--good'), font: v('--font'), ob: v('--ob-line') };
    }
    // Axis and crosshair labels read straight off the (already shifted) time, never through toLocaleString,
    // which a page may pin to a time zone and so shift them again on a device set to another one.
    const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const p2 = (x) => String(x).padStart(2, '0');
    const asDate = (t) => typeof t === 'number' ? new Date(t * 1000)
        : typeof t === 'string' ? new Date(t + 'T00:00:00Z') : new Date(Date.UTC(t.year, t.month - 1, t.day));
    const tickLabel = (t, type) => {
        const d = asDate(t);
        return type === 0 ? String(d.getUTCFullYear()) : type === 1 ? MON[d.getUTCMonth()] : type === 2 ? String(d.getUTCDate())
            : type === 4 ? `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}` : `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
    };
    function chartOptions(c, T) {
        const when = (t) => {
            const d = asDate(t), day = `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
            return c.tf >= 86400 ? day : `${day} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}${c.tf < 60 ? ':' + p2(d.getUTCSeconds()) : ''}`;
        };
        return {
            autoSize: true,
            grid: { vertLines: { color: T.line }, horzLines: { color: T.line } },
            rightPriceScale: { borderColor: T.line },
            layout: { background: { type: 'solid', color: T.bg }, textColor: T.ink, fontFamily: T.font, fontSize: 11,
                      panes: { separatorColor: T.line, separatorHoverColor: T.strong } },
            timeScale: { borderColor: T.line, timeVisible: c.tf < 86400, secondsVisible: c.tf < 60, rightOffset: 4, barSpacing: 7, tickMarkFormatter: tickLabel },
            crosshair: { mode: 0 },
            localization: { timeFormatter: when },                       // prices: each series' own format (priceFmt)
        };
    }
    const priceFmt = (c, d = c.dp) => ({ type: 'custom', formatter: (p) => fmt(p, d), minMove: Math.pow(10, -d) });
    function levelColor(kind, T) { return kind === 'stop' ? T.bad : kind === 'target' ? T.good : kind === 'entry' ? T.accent : T.ob; }

    // ------------------------------------------------------------ drawings
    // Trend lines, horizontal lines and rectangles on any chart, kept per instrument and anchored in time and price,
    // so they show on every timeframe of it. Pick a tool, then click: once for a horizontal line, twice for a trend
    // line or a rectangle (a preview follows the pointer in between). With no tool, click a drawing to select it, drag
    // it, or drag a handle to reshape it; Delete (or the bin) removes it, Esc lets go. Saved to the app (every device),
    // or in this browser where the app has no drawings route.
    const DRAW_COLOR = { trend: 'accent', hline: 'warn', rect: 'accent' };
    let drawings = {}, tool = '', sel = null, placing = null, dragging = null, drawLocal = false;
    // A drawing's colour: its own (chosen with the colour button while it is selected), else the theme's for its type.
    // With nothing selected the button sets the colour of the next drawings (remembered in this browser).
    const PALETTE = ['#2962ff', '#0b9ea0', '#22a06b', '#e5a50a', '#f97316', '#e5484d', '#9b5de5', '#787b86'];
    let drawColor = (() => { try { const v = localStorage.getItem('lc.drawColor'); return /^#[0-9a-f]{6}$/i.test(v || '') ? v : null; } catch (e) { return null; } })();
    const colorOf = (d, T) => d.color || T[DRAW_COLOR[d.type]] || T.accent;
    // The magnet (toolbar, remembered in this browser): the crosshair snaps to the hovered candle's open, high, low or
    // close, wicks included, and a drawing's point snaps to the nearest of them within MAGNET_PX.
    let magnet = (() => { try { return localStorage.getItem('lc.magnet') !== '0'; } catch (e) { return true; } })();
    // Each chart's zoom (candle width) and scroll (candles back from the latest), per instrument and timeframe on
    // this device: a reload, or going back to a chart, shows it as it was left. Kept relative to the latest candle,
    // so new candles still arrive at the right edge. The 200 most recent are kept.
    const VIEW_KEY = 'lc.views', VIEW_MAX = 200;
    let views = (() => { try { return JSON.parse(localStorage.getItem(VIEW_KEY) || '{}') || {}; } catch (e) { return {}; } })();
    const viewKey = (c) => `${c.symbol}|${c.tf}`;
    function saveView(c) {
        if (!c.bars.length || c.restoring) return;
        const ts = c.chart.timeScale(), s = ts.options().barSpacing, p = ts.scrollPosition();
        if (!isFinite(s) || !isFinite(p)) return;
        views[viewKey(c)] = { s: Math.round(s * 100) / 100, p: Math.round(p * 100) / 100, at: Date.now() };
        const keys = Object.keys(views);
        if (keys.length > VIEW_MAX) keys.sort((a, b) => views[a].at - views[b].at).slice(0, keys.length - VIEW_MAX).forEach((k) => delete views[k]);
        try { localStorage.setItem(VIEW_KEY, JSON.stringify(views)); } catch (e) { /* private mode: this page only */ }
    }
    function restoreView(c) {
        const v = views[viewKey(c)], ts = c.chart.timeScale();
        if (!v) { ts.scrollToRealTime(); return; }
        c.restoring = true;
        ts.applyOptions({ barSpacing: v.s });
        ts.scrollToPosition(v.p, false);
        setTimeout(() => { c.restoring = false; }, 0);
    }
    const MAGNET_PX = 24;
    // The candle under logical position l, and its price nearest to y (in pixels): [price, distance].
    function nearestOHLC(c, l, y) {
        const b = c.bars[Math.max(0, Math.min(c.bars.length - 1, Math.round(l)))];
        if (!b) return null;
        return [b.open, b.high, b.low, b.close].map((v) => [v, Math.abs((c.series.priceToCoordinate(v) ?? Infinity) - y)])
            .reduce((a, x) => (x[1] < a[1] ? x : a));
    }
    const drawSaveT = {};

    // A time as a (fractional) candle position on chart c, and back: between candles by interpolation, before the
    // first and after the last by the timeframe, so a drawing made on one timeframe lands in the same place on another.
    function tLogical(c, t) {
        const b = c.bars, n = b.length;
        if (!n) return null;
        if (t <= b[0].time) return (t - b[0].time) / c.tf;
        if (t >= b[n - 1].time) return n - 1 + (t - b[n - 1].time) / c.tf;
        let lo = 0, hi = n - 1;
        while (hi - lo > 1) { const m = (lo + hi) >> 1; if (b[m].time <= t) lo = m; else hi = m; }
        return lo + (t - b[lo].time) / (b[hi].time - b[lo].time);
    }
    function lTime(c, l) {
        const b = c.bars, n = b.length;
        if (!n) return null;
        if (l <= 0) return Math.round(b[0].time + l * c.tf);
        if (l >= n - 1) return Math.round(b[n - 1].time + (l - (n - 1)) * c.tf);
        const i = Math.floor(l);
        return Math.round(b[i].time + (l - i) * (b[i + 1].time - b[i].time));
    }
    // A (fractional) candle position on screen. The library places whole positions only (between two candles it
    // answers 0, the left edge), so a position between candles is placed between its two neighbours.
    function lx(ts, l) {
        const i = Math.floor(l), a = ts.logicalToCoordinate(i);
        if (a == null || l === i) return a;
        const b = ts.logicalToCoordinate(i + 1);
        return b == null ? a : a + (l - i) * (b - a);
    }
    const dX = (c, t) => { const l = tLogical(c, t); return l == null ? null : lx(c.chart.timeScale(), l); };
    const dY = (c, p) => c.series.priceToCoordinate(p);
    function pointAt(c, e) {
        const r = c.body.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
        // Only the candles' area: the price scale (right) and the time axis (below) belong to the chart, so a
        // drawing that runs on under them never takes a click, a drag or the grab cursor meant for them.
        const ts = c.chart.timeScale();
        if (x < 0 || y < 0 || x > ts.width() || y > r.height - ts.height()) return null;
        if (y > c.chart.panes()[0].getHeight()) return null;                 // a pane below: an indicator's, not the candles'
        const l = ts.coordinateToLogical(x);
        let p = c.series.coordinateToPrice(y);
        if (l == null || p == null) return null;
        if (magnet && l >= -0.5 && l <= c.bars.length - 0.5) {
            const near = nearestOHLC(c, l, y);
            if (near && near[1] <= MAGNET_PX) p = near[0];
        }
        return { t: lTime(c, l), l, p, x, y };
    }
    const segDist = (x, y, x1, y1, x2, y2) => {
        const dx = x2 - x1, dy = y2 - y1, len = dx * dx + dy * dy;
        const k = len ? Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / len)) : 0;
        return Math.hypot(x - (x1 + k * dx), y - (y1 + k * dy));
    };
    const isSel = (c, d) => sel && sel.symbol === c.symbol && sel.id === d.id;
    // The handles of a drawing on chart c: its two points, and a rectangle's other two corners.
    function handles(c, d) {
        if (d.type === 'hline') return [];
        const x1 = dX(c, d.p1.t), y1 = dY(c, d.p1.p), x2 = dX(c, d.p2.t), y2 = dY(c, d.p2.p);
        const hs = [['p1', x1, y1], ['p2', x2, y2]];
        if (d.type === 'rect') hs.push(['c12', x1, y2], ['c21', x2, y1]);
        return hs.filter((h) => h[1] != null && h[2] != null);
    }
    function hitTest(c, x, y) {
        const list = drawings[c.symbol] || [];
        if (sel && sel.symbol === c.symbol) {
            const d = list.find((q) => q.id === sel.id);
            const h = d && handles(c, d).find(([, hx, hy]) => Math.hypot(x - hx, y - hy) <= 9);
            if (h) return { d, part: h[0] };
        }
        for (let i = list.length - 1; i >= 0; i--) {
            const d = list[i], y1 = dY(c, d.p1.p);
            if (d.type === 'hline') { if (y1 != null && Math.abs(y - y1) <= 6) return { d, part: 'body' }; continue; }
            const x1 = dX(c, d.p1.t), x2 = dX(c, d.p2.t), y2 = dY(c, d.p2.p);
            if ([x1, y1, x2, y2].some((v) => v == null)) continue;
            if (d.type === 'trend' && segDist(x, y, x1, y1, x2, y2) <= 6) return { d, part: 'body' };
            if (d.type === 'rect' && x >= Math.min(x1, x2) - 4 && x <= Math.max(x1, x2) + 4 && y >= Math.min(y1, y2) - 4 && y <= Math.max(y1, y2) + 4)
                return { d, part: 'body' };
        }
        return null;
    }

    class DrawLayer {
        constructor(c) { this.c = c; this.views = [{ zOrder: () => 'top', renderer: () => ({ draw: (t) => this.draw(t) }) }]; }
        attached({ requestUpdate }) { this.req = requestUpdate; }
        detached() { this.req = null; }
        updateAllViews() {}
        paneViews() { return this.views; }
        update() { if (this.req) this.req(); }
        draw(target) {
            const c = this.c, list = (drawings[c.symbol] || []).slice();
            if (placing && placing.c === c) list.push({ ...placing, id: '_new', preview: true });
            if (!list.length || !c.bars.length) return;
            const T = theme();
            target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
                const W = mediaSize.width;
                list.forEach((d) => {
                    const col = colorOf(d, T), on = isSel(c, d);
                    ctx.save();
                    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = on ? 2 : 1.5;
                    if (d.preview) ctx.globalAlpha = 0.75;
                    const y1 = dY(c, d.p1.p);
                    if (d.type === 'hline') {
                        if (y1 == null) { ctx.restore(); return; }
                        ctx.beginPath(); ctx.moveTo(0, Math.round(y1) + 0.5); ctx.lineTo(W, Math.round(y1) + 0.5); ctx.stroke();
                        const txt = fmt(d.p1.p, c.dp);
                        ctx.font = `600 11px ${T.font || 'sans-serif'}`;
                        const tw = ctx.measureText(txt).width;
                        ctx.fillRect(W - tw - 12, y1 - 9, tw + 8, 17);
                        ctx.fillStyle = T.bg; ctx.fillText(txt, W - tw - 8, y1 + 4);
                    } else {
                        const x1 = dX(c, d.p1.t), x2 = dX(c, d.p2.t), y2 = dY(c, d.p2.p);
                        if ([x1, y1, x2, y2].some((v) => v == null)) { ctx.restore(); return; }
                        if (d.type === 'trend') { ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
                        else {
                            ctx.globalAlpha = (d.preview ? 0.75 : 1) * 0.13;
                            ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
                            ctx.globalAlpha = d.preview ? 0.75 : 1;
                            ctx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
                        }
                    }
                    if (on || d.preview) handles(c, d).forEach(([, hx, hy]) => {
                        ctx.beginPath(); ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
                        ctx.fillStyle = T.bg; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = col; ctx.stroke();
                    });
                    ctx.restore();
                });
            });
        }
    }

    const redrawSym = (sym) => charts.forEach((c) => { if (!sym || c.symbol === sym) c.draw.update(); });
    // While a tool is picked or a drawing is being dragged the chart must not pan under the pointer.
    function freeze(c, on) {
        c.chart.applyOptions({ handleScroll: !on, handleScale: !on });
        c.body.dataset.draw = tool ? '1' : '';
    }
    function setTool(t) {
        tool = t || '';
        placing = null;
        if (tool) sel = null;
        charts.forEach((c) => freeze(c, !!tool));
        toolState();
        redrawSym();
    }
    function toolState() {
        if (!root) return;
        root.querySelectorAll('[data-tool]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.tool === tool)));
        const del = root.querySelector('[data-tool-del]');
        if (del) del.disabled = !sel;
        historyState();
        // The colour button shows the selected drawing's colour, or the next drawings'.
        const d = selected(), cur = d ? d.color || null : drawColor;
        const sw = root.querySelector('[data-draw-color] .sw');
        if (sw) sw.style.background = cur || 'conic-gradient(#2962ff, #22a06b, #e5a50a, #e5484d, #9b5de5, #2962ff)';
        const btn = root.querySelector('[data-draw-color]');
        if (btn) btn.title = d ? 'Colour of the selected drawing' : 'Colour of the next drawings';
        root.querySelectorAll('.ch-pal [data-sw]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.sw === cur)));
    }
    // Undo / redo (Ctrl or Cmd + Z; Ctrl or Cmd + Shift + Z, or Ctrl + Y; the toolbar's arrows): each change to an
    // instrument's drawings (one placed, moved or reshaped, recoloured, deleted) first keeps a copy of its list as it
    // was. Undo puts that copy back (and saves it, as any change is); redo goes forward again. A new change clears redo.
    const HISTORY_MAX = 100;
    let undoStack = [], redoStack = [];
    const copyOf = (sym) => JSON.parse(JSON.stringify(drawings[sym] || []));
    function remember(sym, list) {
        undoStack.push({ sym, list: list || copyOf(sym) });
        if (undoStack.length > HISTORY_MAX) undoStack.shift();
        redoStack = [];
        historyState();
    }
    function stepHistory(from, to) {
        if (placing) { placing = null; redrawSym(); return; }     // a half-placed drawing: undo its first click only
        const h = from.pop();
        if (!h) return;
        to.push({ sym: h.sym, list: copyOf(h.sym) });
        drawings[h.sym] = h.list;
        if (sel && !h.list.some((d) => d.id === sel.id)) sel = null;
        toolState(); redrawSym(h.sym); saveDrawings(h.sym);
    }
    const undo = () => stepHistory(undoStack, redoStack);
    const redo = () => stepHistory(redoStack, undoStack);
    function historyState() {
        if (!root) return;
        const u = root.querySelector('[data-undo]'), r = root.querySelector('[data-redo]');
        if (u) u.disabled = !undoStack.length && !placing;
        if (r) r.disabled = !redoStack.length;
    }
    function saveDrawings(sym) {
        clearTimeout(drawSaveT[sym]);
        drawSaveT[sym] = setTimeout(async () => {
            const list = drawings[sym] || [];
            if (!drawLocal) {
                try {
                    const r = await fetch(`${API}/drawings`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ symbol: sym, drawings: list }) });
                    if (r.ok) return;
                    if (r.status !== 404 && r.status !== 405) return;
                } catch (e) { return; }
                drawLocal = true;                                       // an app without the route: this browser keeps them
            }
            try { localStorage.setItem('lc.drawings', JSON.stringify(drawings)); } catch (e) { /* private mode */ }
        }, 400);
    }
    function addDrawing(c, d) {
        remember(c.symbol);
        d.id = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        if (drawColor) d.color = drawColor;
        (drawings[c.symbol] = drawings[c.symbol] || []).push(d);
        sel = { symbol: c.symbol, id: d.id };
        setTool('');
        sel = { symbol: c.symbol, id: d.id };
        toolState(); redrawSym(c.symbol);
        saveDrawings(c.symbol);
    }
    function selected() {
        if (!sel) return null;
        return (drawings[sel.symbol] || []).find((d) => d.id === sel.id) || null;
    }
    // A colour from the palette or the picker (null: back to the theme's): the selected drawing's, or the next drawings'.
    function setColor(color) {
        const d = selected();
        if (d) {
            if ((d.color || null) === (color || null)) { toolState(); return; }
            remember(sel.symbol);
            if (color) d.color = color; else delete d.color;
            redrawSym(sel.symbol); saveDrawings(sel.symbol);
        } else {
            drawColor = color;
            try { if (color) localStorage.setItem('lc.drawColor', color); else localStorage.removeItem('lc.drawColor'); } catch (e) { /* private mode */ }
        }
        toolState();
    }
    function deleteSelected() {
        if (!sel) return;
        const sym = sel.symbol;
        remember(sym);
        drawings[sym] = (drawings[sym] || []).filter((d) => d.id !== sel.id);
        sel = null;
        toolState(); redrawSym(sym); saveDrawings(sym);
    }
    function onPointerDown(c, e) {
        if (e.button !== 0 || !c.bars.length || e.target.closest('.ch-st, .ch-pop')) return;
        const pt = pointAt(c, e);
        if (!pt) return;
        if (tool) {
            e.preventDefault(); e.stopPropagation();
            const at = { t: pt.t, p: pt.p };
            if (tool === 'hline') { addDrawing(c, { type: 'hline', p1: at }); return; }
            if (!placing || placing.c !== c) { placing = { c, type: tool, p1: at, p2: { ...at } }; redrawSym(c.symbol); historyState(); return; }
            placing.p2 = at;
            const d = { type: placing.type, p1: placing.p1, p2: placing.p2 };
            placing = null;
            addDrawing(c, d);
            return;
        }
        const h = hitTest(c, pt.x, pt.y);
        if (!h) { if (sel) { sel = null; toolState(); redrawSym(); } return; }
        e.preventDefault(); e.stopPropagation();
        sel = { symbol: c.symbol, id: h.d.id };
        toolState();
        dragging = { c, d: h.d, part: h.part, start: pt, orig: JSON.parse(JSON.stringify(h.d)), moved: false, id: e.pointerId,
                     before: copyOf(c.symbol) };
        freeze(c, true);
        redrawSym();
    }
    function onPointerMove(c, e) {
        if (dragging) return;
        if (placing && placing.c === c) {
            const pt = pointAt(c, e);
            if (pt) { placing.p2 = { t: pt.t, p: pt.p }; c.draw.update(); }
            return;
        }
        if (!tool) {
            const pt = pointAt(c, e), h = pt && hitTest(c, pt.x, pt.y);
            c.body.dataset.grab = h ? '1' : '';
        }
    }
    document.addEventListener('pointermove', (e) => {
        const g = dragging;
        if (!g || e.pointerId !== g.id) return;
        const pt = pointAt(g.c, e);
        if (!pt) return;
        g.moved = true;
        const d = g.d, o = g.orig, c = g.c;
        // A move goes by candles (not seconds), so a drawing keeps its shape across nights and weekends.
        const shift = (q) => ({ t: lTime(c, tLogical(c, q.t) + pt.l - g.start.l), p: q.p + pt.p - g.start.p });
        if (g.part === 'body') { d.p1 = shift(o.p1); if (o.p2) d.p2 = shift(o.p2); }
        else if (g.part === 'p1') d.p1 = { t: pt.t, p: pt.p };
        else if (g.part === 'p2') d.p2 = { t: pt.t, p: pt.p };
        else if (g.part === 'c12') { d.p1 = { ...d.p1, t: pt.t }; d.p2 = { ...d.p2, p: pt.p }; }
        else if (g.part === 'c21') { d.p2 = { ...d.p2, t: pt.t }; d.p1 = { ...d.p1, p: pt.p }; }
        redrawSym(c.symbol);
    });
    const endDrag = () => {
        const g = dragging;
        if (!g) return;
        dragging = null;
        freeze(g.c, !!tool);
        if (g.moved) { remember(g.c.symbol, g.before); saveDrawings(g.c.symbol); }    // a click without a move is no change
    };
    document.addEventListener('pointerup', endDrag);
    document.addEventListener('pointercancel', endDrag);
    document.addEventListener('keydown', (e) => {
        if (!root || !visible() || e.target.closest('input, select, textarea')) return;
        const k = (e.key || '').toLowerCase();
        if ((e.ctrlKey || e.metaKey) && !e.altKey && (k === 'z' || (k === 'y' && e.ctrlKey && !e.metaKey))) {
            e.preventDefault();
            if (k === 'y' || e.shiftKey) redo(); else undo();
            return;
        }
        if ((e.key === 'Delete' || e.key === 'Backspace') && sel) { e.preventDefault(); deleteSelected(); }
        else if (e.key === 'Escape' && (tool || sel || placing)) { sel = null; setTool(''); }
    });

    // ------------------------------------------------------------ layout
    function dims(n) {
        const W = root.clientWidth;
        if (W < 700) return { cols: 1, rowH: 340 };
        const cols = n <= 1 ? 1 : n === 2 ? 2 : n === 3 ? (W > 1100 ? 3 : 2) : n === 4 ? 2 : n <= 9 ? 3 : 4;
        const rows = Math.ceil(n / cols);
        const top = grid.getBoundingClientRect().top + window.scrollY;
        const H = Math.max(420, window.innerHeight - Math.max(0, top - window.scrollY) - 18);
        return { cols, rowH: Math.max(230, Math.floor((H - (rows - 1) * 8) / rows)) };
    }
    function relayout() {
        if (!grid) return;
        gridTop = grid.getBoundingClientRect().top + window.scrollY;
        const shown = maxed ? 1 : charts.length;
        const { cols, rowH } = dims(shown);
        grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
        grid.style.gridAutoRows = maxed ? `${Math.max(rowH, Math.floor(window.innerHeight * 0.72))}px` : `${rowH}px`;
        charts.forEach((c) => { c.el.hidden = !!maxed && maxed !== c; });
        root.querySelector('.ch-count').textContent = charts.length
            ? `${charts.length} chart${charts.length > 1 ? 's' : ''}${maxed ? ' · one enlarged (Esc)' : ''}` : '';
        root.querySelector('.ch-addbtn').disabled = charts.length >= cfg.max;
        const empty = root.querySelector('.ch-empty');
        empty.hidden = charts.length > 0;
    }
    function save() {
        clearTimeout(saveT);
        saveT = setTimeout(() => {
            fetch(`${API}/layout`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ layout: charts.map((c) => ({ symbol: c.symbol, tf: c.tf, levels: c.levels, studies: studyState(c) })) }) }).catch(() => {});
        }, 400);
    }

    // ------------------------------------------------------------ one chart
    function tfOptions(sel) { return cfg.tfs.map((t) => `<option value="${t.s}"${t.s === sel ? ' selected' : ''}>${esc(t.label)}</option>`).join(''); }
    function addChart(spec, persist = true) {
        const c = { id: ++uid, symbol: spec.symbol, tf: spec.tf, levels: spec.levels !== false, bars: [], lines: [], dp: 2, hasVol: false, last: null,
                    studies: [], hover: null };
        const el = document.createElement('div');
        el.className = 'ch-p';
        el.innerHTML = `<div class="ch-head">
                <input list="ch-syms" value="${esc(labelOf(spec.symbol))}" title="${esc((instOf(spec.symbol) || {}).name || '')}" aria-label="Instrument" spellcheck="false" autocomplete="off">
                <select aria-label="Timeframe">${tfOptions(spec.tf)}</select>
                <span class="ch-px">—</span><span class="ch-chg"></span><span class="ch-sp"></span>
                <button type="button" class="ch-ib" data-a="ind" title="Indicators: moving averages, VWAP, Bollinger, RSI, MACD…" aria-label="Indicators"${cfg.studies.kinds.length ? '' : ' hidden'}>${IC.ind}</button>
                <button type="button" class="ch-ib" data-a="shot" title="Save a picture of this chart as it shows" aria-label="Save a picture of this chart">${IC.camera}</button>
                <button type="button" class="ch-ib" data-a="fit" title="Back to the latest candles">${IC.fit}</button>
                <button type="button" class="ch-ib" data-a="lv" aria-pressed="${c.levels}" title="The app's zones, entries, stops and targets">${IC.lv}</button>
                <button type="button" class="ch-ib" data-a="max" title="Enlarge (Esc to return)">${IC.max}</button>
                <button type="button" class="ch-ib" data-a="x" title="Remove this chart">${IC.x}</button></div>
            <div class="ch-body"><div class="ch-leg"><span class="ohlc"></span><div class="sts"></div></div><div class="ch-note"></div><div class="ch-msg">Loading…</div></div>
            <div class="ch-pop" role="dialog" aria-label="Indicators" hidden></div>`;
        c.el = el;
        grid.appendChild(el);
        charts.push(c);
        const body = el.querySelector('.ch-body');
        const T = theme();
        c.chart = LightweightCharts.createChart(body, chartOptions(c, T));
        c.series = c.chart.addSeries(LightweightCharts.CandlestickSeries, { upColor: T.up, downColor: T.down, wickUpColor: T.up, wickDownColor: T.down, borderVisible: false,
            priceFormat: priceFmt(c),
            priceLineVisible: true, lastValueVisible: true });
        c.vol = c.chart.addSeries(LightweightCharts.HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
        c.chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
        c.chart.timeScale().subscribeVisibleLogicalRangeChange(() => { clearTimeout(c.viewT); c.viewT = setTimeout(() => saveView(c), 400); });
        c.chart.subscribeCrosshairMove((p) => {
            const d = p && p.time != null ? p.seriesData.get(c.series) : null;
            legend(c, d);
            c.hover = p && p.time != null ? p : null;
            if (c.studies.length) studyLegend(c, c.hover);
            // The magnet: the crosshair onto the nearest of the candle's open, high, low or close (set quietly; it raises
            // no move of its own, and the next pointer move moves it again).
            if (magnet && d && p.point && p.sourceEvent && d.open != null && !p.paneIndex) {
                const near = [d.open, d.high, d.low, d.close].reduce((a, v) =>
                    (Math.abs((c.series.priceToCoordinate(v) ?? Infinity) - p.point.y) < Math.abs((c.series.priceToCoordinate(a) ?? Infinity) - p.point.y) ? v : a));
                c.chart.setCrosshairPosition(near, p.time, c.series);
            }
        });
        c.body = body;
        c.draw = new DrawLayer(c);
        c.series.attachPrimitive(c.draw);
        body.addEventListener('pointerdown', (e) => onPointerDown(c, e), true);
        el.addEventListener('click', (e) => onStudyClick(c, e));
        // The panes take their new heights only once the chart has followed its box: place their legends after that.
        c.ro = new ResizeObserver(() => requestAnimationFrame(() => placePaneLegends(c)));
        c.ro.observe(body);
        body.addEventListener('pointermove', (e) => onPointerMove(c, e));
        if (tool) freeze(c, true);
        const inp = el.querySelector('input'), sel = el.querySelector('select');
        const fit = () => { inp.style.width = `${Math.min(18, inp.value.length * 1.2 + 1.5)}ch`; };
        fit();
        inp.addEventListener('input', fit);
        const pick = () => {
            const hit = resolve(inp.value);
            if (!hit) { inp.value = labelOf(c.symbol); fit(); return; }
            inp.value = hit.label;
            inp.title = hit.name;
            fit();
            if (hit.symbol !== c.symbol) { c.symbol = hit.symbol; placing = null; load(c, true); save(); }
        };
        inp.addEventListener('change', pick);
        inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { inp.blur(); } if (e.key === 'Escape') { inp.value = labelOf(c.symbol); fit(); inp.blur(); } });
        inp.addEventListener('focus', () => inp.select());
        sel.addEventListener('change', () => { c.tf = +sel.value; load(c, true); save(); });
        el.querySelector('.ch-head').addEventListener('click', (e) => {
            const b = e.target.closest('[data-a]');
            if (!b) return;
            const a = b.dataset.a;
            if (a === 'x') removeChart(c);
            else if (a === 'max') { maxed = maxed === c ? null : c; b.innerHTML = maxed ? IC.min : IC.max; relayout(); if (maxed) root.scrollIntoView({ block: 'start' }); }
            else if (a === 'fit') c.chart.timeScale().scrollToRealTime();
            else if (a === 'ind') openMenu(c);
            else if (a === 'shot') {
                const leg = c.el.querySelector('.ch-leg').textContent.trim() || c.el.querySelector('.ch-px').textContent;
                snapshot(c.chart, { title: `${labelOf(c.symbol)} · ${tfLabel(c.tf)}`, sub: leg, name: `${c.symbol}-${tfLabel(c.tf)}` }, b);
            }
            else if (a === 'lv') { c.levels = !c.levels; b.setAttribute('aria-pressed', String(c.levels)); drawLevels(c); save(); }
        });
        (spec.studies || []).forEach((st) => addStudy(c, st, false));
        relayout();
        load(c, true);
        if (persist) save();
        return c;
    }
    function removeChart(c) {
        clearTimeout(c.refT);
        clearTimeout(c.stT);
        c.ro.disconnect();
        if (placing && placing.c === c) placing = null;
        c.chart.remove();
        c.el.remove();
        charts = charts.filter((x) => x !== c);
        if (maxed === c) maxed = null;
        relayout();
        save();
    }

    function legend(c, d) {
        const b = d && d.open != null ? d : c.bars[c.bars.length - 1];
        const L = c.el.querySelector('.ch-leg .ohlc');
        if (!b) { L.textContent = ''; return; }
        const o = b.open, h = b.high, l = b.low, cl = b.close, ch = cl - o;
        L.innerHTML = `<b>O</b>${fmt(o, c.dp)} <b>H</b>${fmt(h, c.dp)} <b>L</b>${fmt(l, c.dp)} <b>C</b>${fmt(cl, c.dp)} `
            + `<span style="color:var(${ch >= 0 ? '--up' : '--down'})">${ch >= 0 ? '+' : ''}${fmt(ch, c.dp)}</span>`;
    }
    function setPrice(c, p) {
        const el = c.el.querySelector('.ch-px'), prev = c.last;
        el.textContent = fmt(p, c.dp);
        if (prev != null && p !== prev) {
            el.dataset.d = p > prev ? '1' : '-1';
            clearTimeout(c.flashT);
            c.flashT = setTimeout(() => { el.dataset.d = ''; }, 700);
        }
        c.last = p;
        // The change over the day: from the first candle of the latest date (intraday), else on the candle before.
        const chg = c.el.querySelector('.ch-chg');
        let ref = null;
        if (c.bars.length) {
            if (c.tf >= 86400) ref = c.bars.length > 1 ? c.bars[c.bars.length - 2].close : null;
            else {
                const day = Math.floor(c.bars[c.bars.length - 1].time / 86400);
                const first = c.bars.find((b) => Math.floor(b.time / 86400) === day);
                ref = first ? first.open : null;
            }
        }
        if (ref) {
            const pct = (p - ref) / ref * 100;
            chg.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
            chg.dataset.d = pct > 0 ? '1' : pct < 0 ? '-1' : '';
            chg.title = c.tf >= 86400 ? 'Since the previous close' : 'Since the day’s first candle';
        } else chg.textContent = '';
    }
    function drawLevels(c) {
        c.lines.forEach((l) => c.series.removePriceLine(l));
        c.lines = [];
        if (!c.levels) return;
        const T = theme();
        (c.levelData || []).forEach((x) => {
            c.lines.push(c.series.createPriceLine({ price: +x.price, color: levelColor(x.kind, T), lineWidth: 1,
                lineStyle: x.kind === 'zone' ? 2 : x.kind === 'entry' ? 0 : 1, axisLabelVisible: true, title: x.label || '' }));
        });
    }
    // ------------------------------------------------------------ indicators
    // Each chart's indicators (the catalogue: cfg.studies, from niftywhale/studies.py; the maths: static/studies.js),
    // saved with the layout. On the candles, or each in a pane of its own below them. Worked out again as the forming
    // candle ticks (at most four times a second: only its last point is redrawn) and in full on each new candle.
    const KIND = {};                                              // key -> catalogue entry (filled at start)
    const WIDE = new Set(['ma', 'vwap', 'up', 'down', 'rsi', 'macd', 'k', 'adx', 'cci', 'r', 'mfi', 'obv', 'roc', 'atr']);
    const LEVELS = { rsi: (p) => [p.upper, 50, p.lower], stoch: (p) => [p.upper, p.lower], stochrsi: (p) => [p.upper, p.lower],
        cci: (p) => [p.upper, 0, p.lower], willr: (p) => [p.upper, p.lower], mfi: (p) => [p.upper, p.lower], macd: () => [0],
        roc: () => [0], adx: (p) => (p.level > 0 ? [p.level] : []) };
    const BIG = new Set(['volume', 'obv']);                        // values in lakhs and crores, shortened
    const short = (v) => {
        const a = Math.abs(v);
        return a >= 1e7 ? (v / 1e7).toFixed(2) + ' Cr' : a >= 1e5 ? (v / 1e5).toFixed(2) + ' L' : a >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(Math.round(v));
    };
    const BIG_FMT = { type: 'custom', formatter: short, minMove: 1 };
    const NOT_IN_NAME = new Set(['upper', 'lower', 'level', 'band1', 'band2']);
    const newId = () => 's' + Date.now().toString(36).slice(-6) + Math.random().toString(36).slice(2, 6);
    const kindOf = (st) => KIND[st.type];
    const lineColor = (st, ln) => (st.colors && st.colors[ln.key]) || ln.color;
    const noVolume = (c, st) => kindOf(st).volume && !c.hasVol;
    const hasVolPanel = (c) => c.studies.some((st) => st.type === 'volume' && !noVolume(c, st));
    // "EMA 9", "BB 20 2", "VWAP week": the short name, the numbers, and any choice off its default.
    function stName(st) {
        const k = kindOf(st);
        return [k.short, ...k.params.filter((p) => !NOT_IN_NAME.has(p.key) && (!('values' in p) || st.params[p.key] !== p.default))
            .map((p) => st.params[p.key])].join(' ');
    }
    function stFmt(c, st, v) {
        if (v == null || !isFinite(v)) return '—';
        if (BIG.has(st.type)) return short(v);
        return fmt(v, kindOf(st).place === 'price' ? c.dp : 2);
    }

    // Shading between two of an indicator's lines (Bollinger, Keltner and Donchian bands, the Ichimoku cloud: green
    // where span A is above span B, red below), drawn under the candles.
    class FillLayer {
        constructor(c, st, a, b) {
            Object.assign(this, { c, st, a, b });
            this.views = [{ zOrder: () => 'bottom', renderer: () => ({ draw: (t) => this.draw(t) }) }];
        }
        attached({ requestUpdate }) { this.req = requestUpdate; }
        detached() { this.req = null; }
        updateAllViews() {}
        paneViews() { return this.views; }
        draw(target) {
            const { c, st } = this, A = st.out && st.out[this.a], B = st.out && st.out[this.b];
            if (st.hidden || !A || !B) return;
            const k = kindOf(st), la = k.lines.find((l) => l.key === this.a), lb = k.lines.find((l) => l.key === this.b);
            const s = st.series[this.a], ts = c.chart.timeScale(), cloud = st.type === 'ichimoku';
            const colA = lineColor(st, la) + (cloud ? '40' : '1c'), colB = cloud ? lineColor(st, lb) + '40' : colA;
            target.useMediaCoordinateSpace(({ context: ctx }) => {
                let prev = null;
                for (let i = 0; i < A.length && i < B.length; i++) {
                    const pa = A[i], pb = B[i];
                    const x = pa.value === undefined || pb.value === undefined ? null : ts.timeToCoordinate(pa.time);
                    const ya = x == null ? null : s.priceToCoordinate(pa.value), yb = x == null ? null : s.priceToCoordinate(pb.value);
                    if (ya == null || yb == null) { prev = null; continue; }
                    if (prev) {
                        ctx.fillStyle = prev.up ? colA : colB;
                        ctx.beginPath(); ctx.moveTo(prev.x, prev.ya); ctx.lineTo(x, ya); ctx.lineTo(x, yb); ctx.lineTo(prev.x, prev.yb); ctx.closePath(); ctx.fill();
                    }
                    prev = { x, ya, yb, up: pa.value >= pb.value };
                }
            });
        }
    }

    function buildStudy(c, st) {
        const k = kindOf(st), T = theme();
        st.series = {};
        st.pane = null;
        if (k.place === 'panel') {
            st.pane = c.chart.addPane();
            st.pane.setStretchFactor(1);
            c.chart.panes()[0].setStretchFactor(3);
        }
        k.lines.forEach((ln) => {
            const o = { color: lineColor(st, ln), priceLineVisible: false, visible: !st.hidden,
                lastValueVisible: k.place === 'panel' && ln.kind !== 'histogram' && ln === k.lines[0] };
            if (ln.kind === 'histogram') {
                o.priceFormat = BIG.has(st.type) ? BIG_FMT : priceFmt(c, 2);
            } else {
                Object.assign(o, { lineWidth: WIDE.has(ln.key) ? 2 : 1, crosshairMarkerVisible: ln.kind === 'line' });
                if (ln.kind === 'dots') Object.assign(o, { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.5 });
                o.priceFormat = BIG.has(st.type) ? BIG_FMT : priceFmt(c, k.place === 'price' ? c.dp : 2);
            }
            if (st.type === 'pivots') o.autoscaleInfoProvider = () => null;          // far levels never squash the candles
            const def = ln.kind === 'histogram' ? LightweightCharts.HistogramSeries : LightweightCharts.LineSeries;
            st.series[ln.key] = st.pane ? st.pane.addSeries(def, o) : c.chart.addSeries(def, o);
        });
        const main = st.series[(k.lines.find((l) => l.kind !== 'histogram') || k.lines[0]).key];
        if (st.pane) main.priceScale().applyOptions({ scaleMargins: { top: 0.12, bottom: 0.08 } });
        st.plines = (LEVELS[st.type] ? LEVELS[st.type](st.params) : []).map((v) =>
            main.createPriceLine({ price: v, color: T.ink, lineWidth: 1, lineStyle: 2, axisLabelVisible: false }));
        if (k.fill) { st.fill = new FillLayer(c, st, k.fill[0], k.fill[1]); st.series[k.fill[0]].attachPrimitive(st.fill); }
        st.n = -1;
    }
    function dropStudy(c, st) {
        if (st.fill) { try { st.series[kindOf(st).fill[0]].detachPrimitive(st.fill); } catch (e) { /* gone with the series */ } st.fill = null; }
        Object.values(st.series || {}).forEach((s) => { try { c.chart.removeSeries(s); } catch (e) { /* already gone */ } });
        if (st.pane) {
            const i = c.chart.panes().indexOf(st.pane);
            if (i > 0 && !st.pane.getSeries().length) { try { c.chart.removePane(i); } catch (e) { /* removed with its last series */ } }
        }
        st.series = {}; st.pane = null; st.out = null; st.plines = [];
    }
    // The indicators drawn on the candles price like them (c.dp, which a new instrument changes).
    function studyFormats(c) {
        c.studies.forEach((st) => {
            if (kindOf(st).place !== 'price' || BIG.has(st.type)) return;
            Object.values(st.series || {}).forEach((s) => s.applyOptions({ priceFormat: priceFmt(c) }));
        });
    }
    function computeStudy(c, st, full) {
        if (!st.series) return;
        const k = kindOf(st);
        // No candles (a new instrument with none yet), or none with volume: nothing drawn, not the last instrument's lines.
        if (!c.bars.length || noVolume(c, st)) { Object.values(st.series).forEach((s) => s.setData([])); st.out = null; st.n = -1; return; }
        const T = theme();
        const out = Studies.compute(st.type, c.bars, st.params, c.tf,
            { up: st.colors.vol || T.up + '80', down: st.colors.vol || T.down + '80', hist: st.colors.hist || null });
        const again = full || st.n !== c.bars.length || st.type === 'ichimoku';
        st.n = c.bars.length;
        st.out = out;
        k.lines.forEach((ln) => {
            const s = st.series[ln.key], d = out[ln.key] || [];
            if (again || !d.length) s.setData(d); else s.update(d[d.length - 1]);
        });
    }
    function computeAll(c, full) {
        c.studies.forEach((st) => computeStudy(c, st, full));
        volOverlay(c);
        studyLegend(c, null);
    }
    function studiesSoon(c) {
        if (!c.studies.length || c.stT) return;
        c.stT = setTimeout(() => { c.stT = null; if (charts.includes(c)) { c.studies.forEach((st) => computeStudy(c, st, false)); studyLegend(c, c.hover); } }, 250);
    }
    // The candles' own volume (an overlay at their foot) gives way to a Volume pane.
    function volOverlay(c) {
        const on = c.hasVol && !hasVolPanel(c);
        c.vol.applyOptions({ visible: on });
        c.series.priceScale().applyOptions({ scaleMargins: { top: 0.12, bottom: on ? 0.22 : 0.08 } });
    }
    function addStudy(c, raw, persist = true) {
        const k = KIND[raw.type];
        if (!k || c.studies.length >= cfg.studies.max) return null;
        if (k.place === 'panel' && c.studies.filter((s) => kindOf(s).place === 'panel').length >= cfg.studies.max_panels) return null;
        const params = Object.fromEntries(k.params.map((p) => [p.key, raw.params && raw.params[p.key] != null ? raw.params[p.key] : p.default]));
        const st = { id: raw.id || newId(), type: k.key, params, colors: { ...(raw.colors || {}) }, hidden: !!raw.hidden };
        c.studies.push(st);
        buildStudy(c, st);
        computeStudy(c, st, true);
        volOverlay(c);
        renderLegend(c);
        if (persist) save();
        return st;
    }
    function removeStudy(c, st) {
        dropStudy(c, st);
        c.studies = c.studies.filter((s) => s !== st);
        volOverlay(c);
        renderLegend(c);
        save();
    }
    function rebuildStudy(c, st) {
        dropStudy(c, st);
        // Back into its place in the list; a pane goes back below the others (panes keep the order they were added in).
        buildStudy(c, st);
        computeStudy(c, st, true);
        volOverlay(c);
        renderLegend(c);
    }
    function setHidden(c, st, on) {
        st.hidden = on;
        Object.values(st.series).forEach((s) => s.applyOptions({ visible: !on }));
        if (st.fill && st.fill.req) st.fill.req();
        renderLegend(c);
        save();
    }
    const studyState = (c) => c.studies.map(({ id, type, params, colors, hidden }) => ({ id, type, params: { ...params }, colors: { ...colors }, hidden }));
    function copyStudiesToAll(from) {
        const list = studyState(from);
        charts.forEach((c) => {
            if (c === from) return;
            c.studies.slice().forEach((st) => dropStudy(c, st));
            c.studies = [];
            list.forEach((s) => addStudy(c, { ...s, id: newId() }, false));
            volOverlay(c);
            renderLegend(c);
        });
        save();
    }

    // The legend: one row per indicator under the candles' O H L C (those on the candles) or at the top of its pane,
    // with its values under the crosshair (the latest without one), and buttons to hide, set up or remove it.
    function renderLegend(c) {
        const box = c.el.querySelector('.ch-leg .sts');
        c.body.querySelectorAll('.ch-pleg').forEach((e) => e.remove());
        const row = (st) => {
            const k = kindOf(st), off = noVolume(c, st);
            return `<span class="ch-st" data-id="${esc(st.id)}" data-off="${st.hidden || off ? 1 : 0}">`
                + `<span class="nm" data-st="cfg" title="${esc(k.label)}: settings">${esc(stName(st))}</span>`
                + (off ? '<span class="note">needs volume (none for an index)</span>'
                    : k.lines.map((ln) => `<span class="v" data-k="${ln.key}" style="color:${lineColor(st, ln)}"></span>`).join(''))
                + `<button type="button" data-st="eye" title="${st.hidden ? 'Show' : 'Hide'}" aria-label="${st.hidden ? 'Show' : 'Hide'} ${esc(k.label)}">${st.hidden ? IC.eyeOff : IC.eye}</button>`
                + `<button type="button" data-st="cfg" title="Settings" aria-label="Settings for ${esc(k.label)}">${IC.gear}</button>`
                + `<button type="button" data-st="del" title="Remove" aria-label="Remove ${esc(k.label)}">${IC.x}</button></span>`;
        };
        box.innerHTML = c.studies.filter((st) => kindOf(st).place === 'price').map(row).join('');
        c.studies.filter((st) => kindOf(st).place === 'panel').forEach((st) => {
            const el = document.createElement('div');
            el.className = 'ch-pleg';
            el.dataset.for = st.id;
            el.innerHTML = row(st);
            c.body.appendChild(el);
        });
        placePaneLegends(c);
        studyLegend(c, c.hover);
    }
    function placePaneLegends(c) {
        const panes = c.chart.panes();
        // The candles' legend stays inside their pane: many rows on a short pane are cut, not laid over the next one.
        // Runs with every crosshair move (a dragged pane divider moves no box), so the styles are written only when they change.
        const set = (el, k, v) => { if (el.style[k] !== v) el.style[k] = v; };
        set(c.el.querySelector('.ch-leg'), 'maxHeight', panes.length > 1 ? `${Math.max(20, panes[0].getHeight() - 8)}px` : '');
        c.studies.forEach((st) => {
            if (!st.pane) return;
            const el = c.body.querySelector(`.ch-pleg[data-for="${st.id}"]`), i = panes.indexOf(st.pane);
            if (!el || i < 0) return;
            let top = 0;
            for (let j = 0; j < i; j++) top += panes[j].getHeight() + 1;               // a 1 px separator between panes
            set(el, 'top', `${top + 4}px`);
        });
    }
    // The value of one line at the crosshair (p: the library's crosshair event), else its latest up to the last candle.
    function lineValue(c, st, key, p) {
        if (p && p.time != null) { const d = p.seriesData.get(st.series[key]); return d && d.value != null ? d.value : null; }
        const pts = st.out && st.out[key], lastT = c.bars.length ? c.bars[c.bars.length - 1].time : Infinity;
        for (let i = pts ? pts.length - 1 : -1; i >= 0; i--) if (pts[i].time <= lastT && pts[i].value !== undefined) return pts[i].value;
        return null;
    }
    function studyLegend(c, p) {
        c.studies.forEach((st) => {
            const rowEl = (kindOf(st).place === 'panel' ? c.body.querySelector(`.ch-pleg[data-for="${st.id}"]`) : c.el.querySelector('.ch-leg .sts'));
            if (!rowEl || st.hidden || noVolume(c, st)) return;
            const r = rowEl.querySelector(`.ch-st[data-id="${st.id}"]`);
            if (!r) return;
            kindOf(st).lines.forEach((ln) => {
                const v = lineValue(c, st, ln.key, p), el = r.querySelector(`.v[data-k="${ln.key}"]`);
                if (el) el.textContent = v == null ? '' : stFmt(c, st, v);
            });
        });
        placePaneLegends(c);
    }

    // The menu (Indicators, in each chart's header) and an indicator's settings, in one panel over the chart.
    function closePop(c) { const pop = c.el.querySelector('.ch-pop'); if (pop) { pop.hidden = true; pop.dataset.for = ''; } }
    function openMenu(c) {
        const pop = c.el.querySelector('.ch-pop');
        if (!pop.hidden && pop.dataset.for === 'menu') { closePop(c); return; }
        pop.dataset.for = 'menu';
        pop.hidden = false;
        pop.innerHTML = `<h4>Indicators <button type="button" class="x" data-pop="close" aria-label="Close">${IC.x}</button></h4>
            <input type="search" placeholder="Search: RSI, EMA, VWAP…" aria-label="Search indicators"><div class="lst"></div>
            <div class="foot"><button type="button" class="lnk" data-pop="copy"${c.studies.length ? '' : ' disabled'} title="The same indicators, with these settings, on every chart">Copy to all charts</button>
                <button type="button" class="lnk" data-pop="clear"${c.studies.length ? '' : ' disabled'}>Remove all</button></div>`;
        const list = pop.querySelector('.lst'), q = pop.querySelector('input');
        const panels = c.studies.filter((s) => kindOf(s).place === 'panel').length, full = c.studies.length >= cfg.studies.max;
        const draw = () => {
            const w = q.value.trim().toLowerCase();
            const hits = cfg.studies.kinds.filter((k) => !w || `${k.label} ${k.short} ${k.group}`.toLowerCase().includes(w));
            const groups = [...new Set(hits.map((k) => k.group))];
            list.innerHTML = groups.map((g) => `<div class="grp">${esc(g)}</div>` + hits.filter((k) => k.group === g).map((k) => {
                const why = full ? `At most ${cfg.studies.max} indicators a chart`
                    : k.place === 'panel' && panels >= cfg.studies.max_panels ? `At most ${cfg.studies.max_panels} panes a chart`
                        : k.volume && !c.hasVol ? 'Needs volume: an index has none' : '';
                return `<button type="button" class="it" data-add="${k.key}"${why ? ` disabled title="${esc(why)}"` : ''}>`
                    + `<span>${esc(k.label)}</span><small>${k.place === 'panel' ? 'own pane' : 'on the candles'}</small></button>`;
            }).join('')).join('') || '<div class="empty">No indicator by that name.</div>';
        };
        draw();
        q.addEventListener('input', draw);
        q.focus();
    }
    function openSettings(c, st) {
        const pop = c.el.querySelector('.ch-pop'), k = kindOf(st);
        pop.dataset.for = st.id;
        pop.hidden = false;
        const field = (p) => {
            const v = st.params[p.key];
            const input = 'values' in p
                ? `<select data-p="${p.key}">${p.values.map((x) => `<option value="${esc(x)}"${x === v ? ' selected' : ''}>${esc(x[0].toUpperCase() + x.slice(1))}</option>`).join('')}</select>`
                : `<input type="number" data-p="${p.key}" value="${v}" min="${p.min}" max="${p.max}" step="${p.step}">`;
            return `<label class="row"><span>${esc(p.label)}</span>${input}</label>`;
        };
        const lines = k.lines.filter((ln) => !(st.type === 'pivots' && ln.key.endsWith('4') && st.params.kind === 'fibonacci'));
        pop.innerHTML = `<h4>${esc(k.label)} <button type="button" class="x" data-pop="close" aria-label="Close">${IC.x}</button></h4>
            ${k.params.map(field).join('') || '<div class="empty">No settings.</div>'}
            <div class="sub">Colours</div>
            ${lines.map((ln) => `<label class="row"><span>${esc(ln.label)}</span><input type="color" data-c="${ln.key}" value="${lineColor(st, ln)}"></label>`).join('')}
            <div class="foot"><button type="button" class="lnk" data-pop="defaults">Defaults</button>
                <button type="button" class="lnk" data-pop="copy" title="This chart's indicators, with these settings, on every chart">Copy to all charts</button>
                <button type="button" class="lnk" data-pop="close">Done</button></div>`;
        pop.querySelectorAll('[data-p]').forEach((el) => el.addEventListener('change', () => {
            const p = k.params.find((x) => x.key === el.dataset.p);
            let v = el.value;
            if (!('values' in p)) {
                v = Number(v);
                if (!isFinite(v)) v = p.default;
                v = Math.min(p.max, Math.max(p.min, Math.round((v - p.min) / p.step) * p.step + p.min));
                v = +v.toFixed(6);
                el.value = v;
            }
            st.params[p.key] = v;
            rebuildStudy(c, st);
            save();
        }));
        pop.querySelectorAll('[data-c]').forEach((el) => el.addEventListener('input', () => {
            const ln = k.lines.find((x) => x.key === el.dataset.c), v = el.value.toLowerCase();
            if (v === ln.color) delete st.colors[ln.key]; else st.colors[ln.key] = v;
            if (ln.kind === 'histogram') computeStudy(c, st, true); else st.series[ln.key].applyOptions({ color: v });
            if (st.fill && st.fill.req) st.fill.req();
            renderLegend(c);
            save();
        }));
    }
    function onStudyClick(c, e) {
        const b = e.target.closest('[data-st], [data-pop], [data-add]');
        if (!b) return;
        e.preventDefault(); e.stopPropagation();
        if (b.dataset.add) { const st = addStudy(c, { type: b.dataset.add }); closePop(c); if (st && kindOf(st).params.length) openSettings(c, st); return; }
        const pop = b.dataset.pop;
        if (pop === 'close') { closePop(c); return; }
        if (pop === 'copy') { copyStudiesToAll(c); closePop(c); return; }
        if (pop === 'clear') { c.studies.slice().forEach((st) => dropStudy(c, st)); c.studies = []; volOverlay(c); renderLegend(c); save(); closePop(c); return; }
        if (pop === 'defaults') {
            const st = c.studies.find((s) => s.id === c.el.querySelector('.ch-pop').dataset.for);
            if (!st) return;
            st.params = Object.fromEntries(kindOf(st).params.map((p) => [p.key, p.default]));
            st.colors = {};
            rebuildStudy(c, st); save(); openSettings(c, st);
            return;
        }
        const id = b.closest('.ch-st') && b.closest('.ch-st').dataset.id, st = c.studies.find((s) => s.id === id);
        if (!st) return;
        if (b.dataset.st === 'eye') setHidden(c, st, !st.hidden);
        else if (b.dataset.st === 'del') { if (c.el.querySelector('.ch-pop').dataset.for === st.id) closePop(c); removeStudy(c, st); }
        else if (b.dataset.st === 'cfg') { const pop = c.el.querySelector('.ch-pop'); if (!pop.hidden && pop.dataset.for === st.id) closePop(c); else openSettings(c, st); }
    }

    function toBar(r) { return { time: r[0], open: r[1], high: r[2], low: r[3], close: r[4], v: r[5] }; }
    function volBar(b, T) { return { time: b.time, value: b.v || 0, color: (b.close >= b.open ? T.up : T.down) + '55' }; }

    async function load(c, fresh) {
        clearTimeout(c.refT);
        const msg = c.el.querySelector('.ch-msg');
        const want = `${c.symbol}|${c.tf}`;
        c.want = want;
        if (fresh) { msg.hidden = false; msg.textContent = 'Loading…'; c.el.querySelector('.ch-note').textContent = ''; }
        try {
            const d = await getJSON(`${API}/candles?symbol=${encodeURIComponent(c.symbol)}&tf=${c.tf}`);
            if (c.want !== want || !charts.includes(c)) return;
            const T = theme();
            const bars = (d.bars || []).map(toBar);
            c.levelData = d.levels || [];
            const lastP = bars.length ? bars[bars.length - 1].close : null;
            const hadVol = c.hasVol;
            if (fresh || !c.bars.length) {
                c.dp = dp(lastP || (c.levelData[0] && c.levelData[0].price) || 100);
                c.hasVol = bars.some((b) => b.v > 0);
                c.chart.applyOptions(chartOptions(c, T));
                c.series.applyOptions({ priceFormat: priceFmt(c) });
                studyFormats(c);                          // a new instrument's decimals for the indicators on its candles
                c.series.setData(bars);
                c.vol.setData(c.hasVol ? bars.map((b) => volBar(b, T)) : []);
                c.bars = bars;
                c.last = null;
                restoreView(c);                           // as it was left on this device, else the latest candles
            } else if (bars.length && c.bars.length && bars[bars.length - 1].time < c.bars[c.bars.length - 1].time) {
                // The chart started a candle the source has not (a day turned early): take the source's.
                const range = c.chart.timeScale().getVisibleLogicalRange();
                c.series.setData(bars);
                c.vol.setData(c.hasVol ? bars.map((b) => volBar(b, T)) : []);
                c.bars = bars;
                if (range) c.chart.timeScale().setVisibleLogicalRange(range);
            } else if (bars.length) {
                // Keep the view: fold in only what changed at the end.
                const from = c.bars.length ? c.bars[c.bars.length - 1].time : 0;
                bars.filter((b) => b.time >= from).forEach((b) => {
                    c.series.update(b);
                    if (c.hasVol) c.vol.update(volBar(b, T));
                    const i = c.bars.length - 1;
                    if (i >= 0 && c.bars[i].time === b.time) c.bars[i] = b; else c.bars.push(b);
                });
            }
            drawLevels(c);
            // The legend again for a new instrument, or once volume shows up (a volume indicator's row stops saying it has none).
            if (fresh || c.hasVol !== hadVol) renderLegend(c);
            computeAll(c, true);
            if (lastP != null && c.last == null) setPrice(c, lastP);
            legend(c, null);
            const live = cfg.market_open;
            msg.hidden = c.bars.length > 0;
            if (!c.bars.length) {
                msg.textContent = c.tf < 60
                    ? (live ? 'Waiting for live prices: candles under a minute are drawn from the ticks as they arrive.'
                        : 'The market is closed: candles under a minute are drawn from live prices while it trades.')
                    : 'No candles for this instrument and timeframe.';
            }
            const note = c.el.querySelector('.ch-note');
            note.textContent = d.source === 'ticks' && d.ticks_from
                ? `from live ticks since ${new Date((d.ticks_from - cfg.shift) * 1000).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })}`
                : '';
        } catch (e) {
            if (c.want !== want) return;
            msg.hidden = false;
            msg.textContent = 'Could not load: ' + e.message;
        }
        // Candles again now and then: volume, a corrected forming candle, a new day's levels.
        const every = c.tf < 60 ? 60000 : c.tf < 86400 ? 30000 : 120000;
        const again = () => { if (!charts.includes(c)) return; if (visible()) load(c, false); else c.refT = setTimeout(again, 5000); };
        c.refT = setTimeout(again, every);
    }

    // Fold one tick into the chart: the forming candle, or the next one.
    function tick(c, at, p) {
        const T = theme();
        const n = c.bars.length;
        if (!n) return;
        const lastB = c.bars[n - 1];
        let b;
        if (c.tf >= 86400) {
            // The tick's trading day (or the Monday of its week): a new one starts its own candle.
            let day = Math.floor((at + (cfg.day_shift != null ? cfg.day_shift : cfg.shift)) / 86400) * 86400;
            if (c.tf === 604800) day -= ((day / 86400 + 3) % 7) * 86400;
            if (day > lastB.time) {
                b = { time: day, open: p, high: p, low: p, close: p, v: 0 };
                c.bars.push(b);
            } else {
                b = { ...lastB, high: Math.max(lastB.high, p), low: Math.min(lastB.low, p), close: p };
                c.bars[n - 1] = b;
            }
        } else {
            const t = Math.floor(at) + cfg.shift;
            if (t < lastB.time) return;
            if (t < lastB.time + c.tf) {
                b = { ...lastB, high: Math.max(lastB.high, p), low: Math.min(lastB.low, p), close: p };
                c.bars[n - 1] = b;
            } else {
                b = { time: lastB.time + Math.floor((t - lastB.time) / c.tf) * c.tf, open: p, high: p, low: p, close: p, v: 0 };
                c.bars.push(b);
            }
        }
        c.series.update(b);
        if (c.hasVol && b.v === 0 && b.time !== lastB.time) c.vol.update(volBar(b, T));
        studiesSoon(c);
    }

    const sock = () => (window.LiveSocket && window.LiveSocket.up ? window.LiveSocket : null);
    let subSig = '';
    const seen = {};                        // symbol -> the newest tick folded in (a resent one is skipped)
    async function poll() {
        clearTimeout(pollT);
        let wait = (cfg && cfg.poll_ms) || 2000;
        // Something above the grid changed height (the ticker strip loading, a banner): fit again.
        if (grid && visible() && Math.abs(grid.getBoundingClientRect().top + window.scrollY - gridTop) > 2) relayout();
        const S = sock();
        if (charts.length && visible()) {
            const syms = [...new Set(charts.map((c) => c.symbol))].sort();
            if (S) {
                // Pushed: only say which symbols, when that changes (and check again in a second).
                const sig = syms.join(',');
                if (sig !== subSig) { subSig = sig; S.sub('ticks', { s: syms, since }); }
                wait = 1000;
            } else {
                subSig = '';
                try {
                    const d = await getJSON(`${API}/live?symbols=${encodeURIComponent(syms.join(','))}&since=${since}`);
                    took(d);
                    wait = d.poll_ms;
                } catch (e) {
                    feedLine({ error: e.message });
                }
            }
        } else if (subSig) {
            if (S) S.unsub('ticks');
            subSig = '';
        }
        pollT = setTimeout(poll, wait);
    }
    function took(d) {
        since = Math.max(since, d.now || 0);
        cfg.market_open = d.market_open;
        cfg.poll_ms = d.poll_ms;
        feedLine(d);
        const fresh = {};
        Object.keys(d.ticks || {}).forEach((s) => {
            fresh[s] = d.ticks[s].filter(([at]) => !(at <= (seen[s] || 0)));
            if (fresh[s].length) seen[s] = fresh[s][fresh[s].length - 1][0];
        });
        charts.forEach((c) => {
            const ts = fresh[c.symbol] || [];
            ts.forEach(([at, p]) => tick(c, at, p));
            if (ts.length) {
                if (!c.el.querySelector('.ch-msg').hidden && c.bars.length) c.el.querySelector('.ch-msg').hidden = true;
                if (!c.bars.length && c.tf < 60) load(c, true);
                setPrice(c, ts[ts.length - 1][1]);
                legend(c, null);
            }
        });
    }
    function feedLine(d) {
        const f = root.querySelector('.ch-feed');
        const live = d.realtime && d.market_open && !d.error;
        f.dataset.live = d.error || !d.market_open ? '' : live ? '1' : '0';
        f.querySelector('span').textContent = d.error ? `Live prices failed: ${d.error}`
            : !d.market_open ? 'Market closed · showing the last candles' : d.note || '';
    }

    // ------------------------------------------------------------ the tab
    function rethemeAll() {
        const T = theme();
        charts.forEach((c) => {
            c.chart.applyOptions(chartOptions(c, T));
            c.series.applyOptions({ upColor: T.up, downColor: T.down, wickUpColor: T.up, wickDownColor: T.down });
            if (c.hasVol) c.vol.setData(c.bars.map((b) => volBar(b, T)));
            drawLevels(c);
            c.studies.forEach((st) => (st.plines || []).forEach((l) => l.applyOptions({ color: T.ink })));
            computeAll(c, true);
        });
    }
    async function start(el) {
        root = el;
        if (started) { relayout(); poll(); return; }
        started = true;
        const style = document.createElement('style');
        style.textContent = css;
        document.head.appendChild(style);
        root.innerHTML = '<div class="ch-empty">Loading charts…</div>';
        try {
            cfg = await getJSON(`${API}/config`);
        } catch (e) {
            root.innerHTML = `<div class="ch-empty">Charts could not load: ${esc(e.message)}</div>`;
            started = false;
            return;
        }
        cfg.studies = cfg.studies && Array.isArray(cfg.studies.kinds) && window.Studies ? cfg.studies : { kinds: [], max: 0, max_panels: 0 };
        cfg.studies.kinds.forEach((k) => { KIND[k.key] = k; });
        const groups = [...new Set(cfg.instruments.map((i) => i.group))];
        root.innerHTML = `<div class="ch-bar">
                <form class="ch-add" autocomplete="off"><input list="ch-syms" placeholder="Instrument" aria-label="Instrument to add" spellcheck="false">
                    <select aria-label="Timeframe">${tfOptions(900)}</select>
                    <button type="submit" class="ch-addbtn">${IC.plus}Add chart</button></form>
                <div class="ch-tools" role="toolbar" aria-label="Drawing tools">
                    <button type="button" data-tool="" aria-pressed="true" title="Pointer: select, move and reshape drawings (Esc)" aria-label="Pointer">${IC.cursor}</button>
                    <button type="button" data-tool="trend" aria-pressed="false" title="Trend line: click its start, then its end" aria-label="Trend line">${IC.trend}</button>
                    <button type="button" data-tool="hline" aria-pressed="false" title="Horizontal line: click a price" aria-label="Horizontal line">${IC.hline}</button>
                    <button type="button" data-tool="rect" aria-pressed="false" title="Rectangle: click one corner, then the opposite one" aria-label="Rectangle">${IC.rect}</button>
                    <span class="sep" aria-hidden="true"></span>
                    <button type="button" data-magnet-lc aria-pressed="${magnet}" title="Magnet: the crosshair and drawings snap to the candle's open, high, low or close" aria-label="Magnet">${IC.magnet}</button>
                    <span class="ch-toolwrap"><button type="button" data-draw-color aria-haspopup="true" aria-expanded="false" title="Colour of the next drawings" aria-label="Colour"><i class="sw"></i></button>
                        <div class="ch-pal" role="dialog" aria-label="Drawing colour" hidden>${PALETTE.map((h) => `<button type="button" class="sw-b" data-sw="${h}" style="background:${h}" aria-label="${h}" aria-pressed="false"></button>`).join('')}
                            <div class="pal-row"><label>Custom <input type="color" data-sw-custom value="#2962ff"></label><button type="button" class="pal-def" data-sw-def>Default</button></div></div></span>
                    <button type="button" data-tool-del disabled title="Delete the selected drawing (Delete)" aria-label="Delete the selected drawing">${IC.trash}</button>
                    <span class="sep" aria-hidden="true"></span>
                    <button type="button" data-undo disabled title="Undo the last drawing change (Ctrl or Cmd + Z)" aria-label="Undo">${IC.undo}</button>
                    <button type="button" data-redo disabled title="Redo (Ctrl or Cmd + Shift + Z, or Ctrl + Y)" aria-label="Redo">${IC.redo}</button></div>
                <span class="ch-count"></span>
                <span class="ch-feed"><i></i><span>${esc(cfg.note || '')}</span></span></div>
            <datalist id="ch-syms">${groups.map((g) => cfg.instruments.filter((i) => i.group === g)
                .map((i) => `<option value="${esc(i.label)}">${esc(i.name)}</option>`).join('')).join('')}</datalist>
            <div class="ch-grid"></div>
            <div class="ch-empty" hidden>No charts open. Pick an instrument and a timeframe above and press <b>Add chart</b>: they share the screen as you add more.</div>`;
        grid = root.querySelector('.ch-grid');
        if (cfg.drawings && typeof cfg.drawings === 'object') drawings = cfg.drawings;
        else {
            drawLocal = true;
            try { drawings = JSON.parse(localStorage.getItem('lc.drawings') || '{}') || {}; } catch (e) { drawings = {}; }
        }
        setTimeout(toolState, 0);
        const pal = root.querySelector('.ch-pal'), palBtn = root.querySelector('[data-draw-color]');
        const palOpen = (on) => { pal.hidden = !on; palBtn.setAttribute('aria-expanded', String(on)); };
        // A pointer down on the palette must not count as a click on a chart (which would let go of the selection).
        pal.addEventListener('pointerdown', (e) => e.stopPropagation());
        pal.querySelector('[data-sw-custom]').addEventListener('input', (e) => setColor(e.target.value.toLowerCase()));
        document.addEventListener('pointerdown', (e) => { if (!pal.hidden && !e.target.closest('.ch-toolwrap')) palOpen(false); });
        root.querySelector('.ch-tools').addEventListener('click', (e) => {
            if (e.target.closest('[data-draw-color]')) { palOpen(pal.hidden); return; }
            const sw = e.target.closest('[data-sw]');
            if (sw) { setColor(sw.dataset.sw); palOpen(false); return; }
            if (e.target.closest('[data-sw-def]')) { setColor(null); palOpen(false); return; }
            const t = e.target.closest('[data-tool]');
            if (t) { setTool(t.dataset.tool === tool ? '' : t.dataset.tool); return; }
            if (e.target.closest('[data-tool-del]')) deleteSelected();
            if (e.target.closest('[data-undo]')) { undo(); return; }
            if (e.target.closest('[data-redo]')) { redo(); return; }
            const mg = e.target.closest('[data-magnet-lc]');
            if (mg) {
                magnet = !magnet;
                mg.setAttribute('aria-pressed', String(magnet));
                try { localStorage.setItem('lc.magnet', magnet ? '1' : '0'); } catch (er) { /* private mode */ }
            }
        });
        const form = root.querySelector('.ch-add');
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            const inp = form.querySelector('input'), v = inp.value.trim();
            const hit = resolve(v) || (!v && cfg.instruments.find((i) => !charts.some((c) => c.symbol === i.symbol)));
            if (!hit) { inp.focus(); inp.select(); return; }
            if (charts.length >= cfg.max) return;
            maxed = null;
            addChart({ symbol: hit.symbol, tf: +form.querySelector('select').value });
            inp.value = '';
        });
        feedLine({ ...cfg, market_open: cfg.market_open });
        (cfg.layout || []).forEach((s) => addChart(s, false));
        relayout();
        window.addEventListener('resize', () => { if (visible()) relayout(); });
        // An indicator panel closes on a click outside it, or Esc.
        document.addEventListener('pointerdown', (e) => {
            if (!e.target.closest('.ch-pop, [data-a="ind"], .ch-st')) charts.forEach((c) => closePop(c));
        });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape') charts.forEach((c) => closePop(c)); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && maxed && visible() && !e.target.closest('input')) { maxed = null; charts.forEach((c) => { c.el.querySelector('[data-a="max"]').innerHTML = IC.max; }); relayout(); } });
        document.addEventListener('visibilitychange', () => { if (!document.hidden && visible()) poll(); });
        if (window.LiveSocket) {
            window.LiveSocket.listen('ticks', (d) => { if (subSig && d && d.ticks) took(d); });
            // Up again (a new connection remembers nothing) or down (polling from now): start over.
            window.LiveSocket.listen('up', () => { subSig = ''; poll(); });
            window.LiveSocket.listen('down', () => { subSig = ''; poll(); });
        }
        new MutationObserver(rethemeAll).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', rethemeAll);
        poll();
    }
    // The tab came back into view: re-measure (the grid had no size while hidden) and catch up.
    function shown() {
        if (!started || !cfg) return;
        requestAnimationFrame(() => { relayout(); charts.forEach((c) => load(c, false)); poll(); });
    }
    // Open the tab: build it the first time, catch up after that.
    function open(el) { if (started) shown(); else start(el); }
    // A picture of a chart as it shows: its candles, lines, levels and drawings (the library draws them all on its own
    // canvas: chart.takeScreenshot), under a title line (`title`, the time it was taken, NiftyWhale) and `sub` (the
    // legend). A PNG, shared where the device can share files (a phone: Save image, AirDrop, a chat), else downloaded.
    // `btn` shows it is working. Used by the Charts tab and the stock chart (LiveCharts.snapshot).
    async function snapshot(chart, meta, btn) {
        if (btn) btn.disabled = true;
        try {
            const shot = chart.takeScreenshot(), css = getComputedStyle(document.documentElement);
            const v = (n, d) => css.getPropertyValue(n).trim() || d;
            const k = shot.width / Math.max(1, chart.timeScale().width() + (chart.priceScale('right').width() || 0)), pad = 14 * k;
            const head = (meta.sub ? 52 : 34) * k;
            const out = document.createElement('canvas');
            out.width = shot.width; out.height = shot.height + head;
            const g = out.getContext('2d'), font = v('--font', 'sans-serif');
            g.fillStyle = v('--surface', '#ffffff'); g.fillRect(0, 0, out.width, out.height);
            g.textBaseline = 'top';
            g.fillStyle = v('--ink', '#111'); g.font = `700 ${15 * k}px ${font}`;
            g.fillText(meta.title || '', pad, 10 * k);
            const stamp = new Date().toLocaleString('en-IN', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) + ' IST · NiftyWhale';
            g.fillStyle = v('--ink-3', '#667'); g.font = `500 ${11.5 * k}px ${font}`;
            g.textAlign = 'right'; g.fillText(stamp, out.width - pad, 12 * k); g.textAlign = 'left';
            if (meta.sub) { g.fillStyle = v('--ink-2', '#334'); g.font = `500 ${12 * k}px ${v('--mono', 'monospace')}`; g.fillText(meta.sub, pad, 31 * k); }
            g.drawImage(shot, 0, head);
            const blob = await new Promise((res) => out.toBlob(res, 'image/png'));
            if (!blob) throw new Error('the browser could not make the picture');
            const when = new Date(), z = (x) => String(x).padStart(2, '0');
            const file = `${(meta.name || 'chart').replace(/[^A-Za-z0-9_-]+/g, '-')}-${when.getFullYear()}${z(when.getMonth() + 1)}${z(when.getDate())}-${z(when.getHours())}${z(when.getMinutes())}.png`;
            const f = typeof File === 'function' ? new File([blob], file, { type: 'image/png' }) : null;
            if (f && matchMedia('(pointer: coarse)').matches && navigator.canShare && navigator.canShare({ files: [f] })) {
                try { await navigator.share({ files: [f], title: meta.title || 'Chart' }); return; }
                catch (e) { if (e && e.name === 'AbortError') return; }          // closed the share sheet: nothing more
            }
            const url = URL.createObjectURL(blob), a = document.createElement('a');
            a.href = url; a.download = file; document.body.appendChild(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (e) {
            alert('Could not save the chart picture: ' + (e && e.message || e));
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    window.LiveCharts = { start, shown, open, snapshot };
})();
