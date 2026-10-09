/* The Charts tab: as many live candle charts as you like, laid out to fill the screen.
 *
 * Shared by NiftyWhale and MacroWhale (the same file in both). The app gives it a root element
 * and its API under /api/charts:
 *   GET  config                       instruments, timeframes, the saved layout, the feed
 *   GET  candles?symbol=&tf=          [[t, o, h, l, c, v]] (t: seconds, already in IST) + levels
 *   GET  live?symbols=a,b&since=      ticks [[epoch, price]] since `since`, for every symbol
 *   POST layout                       [{symbol, tf, levels}]
 *
 * One poll for all charts brings the new ticks; each chart folds them into its last candle or
 * starts the next one, so a 1-second chart moves every second and a weekly one updates its week.
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
`;
    const IC = {
        plus: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>',
        x: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
        max: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"/></svg>',
        min: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 2v4H2M10 2v4h4M6 14v-4H2M10 14v-4h4"/></svg>',
        lv: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M2 4h12M2 8h12" stroke-dasharray="2 2"/><path d="M2 12h12"/></svg>',
        fit: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 8h12M5 5L2 8l3 3M11 5l3 3-3 3"/></svg>',
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
    function chartOptions(c, T) {
        return {
            autoSize: true,
            layout: { background: { type: 'solid', color: T.bg }, textColor: T.ink, fontFamily: T.font, fontSize: 11 },
            grid: { vertLines: { color: T.line }, horzLines: { color: T.line } },
            rightPriceScale: { borderColor: T.line, scaleMargins: { top: 0.12, bottom: c.hasVol ? 0.22 : 0.08 } },
            timeScale: { borderColor: T.line, timeVisible: c.tf < 86400, secondsVisible: c.tf < 60, rightOffset: 4, barSpacing: 7 },
            crosshair: { mode: 0 },
            localization: { priceFormatter: (p) => fmt(p, c.dp) },
        };
    }
    function levelColor(kind, T) { return kind === 'stop' ? T.bad : kind === 'target' ? T.good : kind === 'entry' ? T.accent : T.ob; }

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
                body: JSON.stringify({ layout: charts.map((c) => ({ symbol: c.symbol, tf: c.tf, levels: c.levels })) }) }).catch(() => {});
        }, 400);
    }

    // ------------------------------------------------------------ one chart
    function tfOptions(sel) { return cfg.tfs.map((t) => `<option value="${t.s}"${t.s === sel ? ' selected' : ''}>${esc(t.label)}</option>`).join(''); }
    function addChart(spec, persist = true) {
        const c = { id: ++uid, symbol: spec.symbol, tf: spec.tf, levels: spec.levels !== false, bars: [], lines: [], dp: 2, hasVol: false, last: null };
        const el = document.createElement('div');
        el.className = 'ch-p';
        el.innerHTML = `<div class="ch-head">
                <input list="ch-syms" value="${esc(labelOf(spec.symbol))}" title="${esc((instOf(spec.symbol) || {}).name || '')}" aria-label="Instrument" spellcheck="false" autocomplete="off">
                <select aria-label="Timeframe">${tfOptions(spec.tf)}</select>
                <span class="ch-px">—</span><span class="ch-chg"></span><span class="ch-sp"></span>
                <button type="button" class="ch-ib" data-a="fit" title="Back to the latest candles">${IC.fit}</button>
                <button type="button" class="ch-ib" data-a="lv" aria-pressed="${c.levels}" title="The app's zones, entries, stops and targets">${IC.lv}</button>
                <button type="button" class="ch-ib" data-a="max" title="Enlarge (Esc to return)">${IC.max}</button>
                <button type="button" class="ch-ib" data-a="x" title="Remove this chart">${IC.x}</button></div>
            <div class="ch-body"><div class="ch-leg"></div><div class="ch-note"></div><div class="ch-msg">Loading…</div></div>`;
        c.el = el;
        grid.appendChild(el);
        charts.push(c);
        const body = el.querySelector('.ch-body');
        const T = theme();
        c.chart = LightweightCharts.createChart(body, chartOptions(c, T));
        c.series = c.chart.addCandlestickSeries({ upColor: T.up, downColor: T.down, wickUpColor: T.up, wickDownColor: T.down, borderVisible: false,
            priceLineVisible: true, lastValueVisible: true });
        c.vol = c.chart.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
        c.chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
        c.chart.subscribeCrosshairMove((p) => legend(c, p && p.time != null ? p.seriesData.get(c.series) : null));
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
            if (hit.symbol !== c.symbol) { c.symbol = hit.symbol; load(c, true); save(); }
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
            else if (a === 'lv') { c.levels = !c.levels; b.setAttribute('aria-pressed', String(c.levels)); drawLevels(c); save(); }
        });
        relayout();
        load(c, true);
        if (persist) save();
        return c;
    }
    function removeChart(c) {
        clearTimeout(c.refT);
        c.chart.remove();
        c.el.remove();
        charts = charts.filter((x) => x !== c);
        if (maxed === c) maxed = null;
        relayout();
        save();
    }

    function legend(c, d) {
        const b = d && d.open != null ? d : c.bars[c.bars.length - 1];
        const L = c.el.querySelector('.ch-leg');
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
            if (fresh || !c.bars.length) {
                c.dp = dp(lastP || (c.levelData[0] && c.levelData[0].price) || 100);
                c.hasVol = bars.some((b) => b.v > 0);
                c.chart.applyOptions(chartOptions(c, T));
                c.series.applyOptions({ priceFormat: { type: 'price', precision: c.dp, minMove: Math.pow(10, -c.dp) } });
                c.series.setData(bars);
                c.vol.setData(c.hasVol ? bars.map((b) => volBar(b, T)) : []);
                c.bars = bars;
                c.last = null;
                if (fresh) c.chart.timeScale().scrollToRealTime();
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
    }

    async function poll() {
        clearTimeout(pollT);
        let wait = (cfg && cfg.poll_ms) || 2000;
        // Something above the grid changed height (the ticker strip loading, a banner): fit again.
        if (grid && visible() && Math.abs(grid.getBoundingClientRect().top + window.scrollY - gridTop) > 2) relayout();
        if (charts.length && visible()) {
            const syms = [...new Set(charts.map((c) => c.symbol))];
            try {
                const d = await getJSON(`${API}/live?symbols=${encodeURIComponent(syms.join(','))}&since=${since}`);
                since = d.now;
                cfg.market_open = d.market_open;
                cfg.poll_ms = d.poll_ms;
                wait = d.poll_ms;
                feedLine(d);
                charts.forEach((c) => {
                    const ts = d.ticks[c.symbol] || [];
                    ts.forEach(([at, p]) => tick(c, at, p));
                    if (ts.length) {
                        if (!c.el.querySelector('.ch-msg').hidden && c.bars.length) c.el.querySelector('.ch-msg').hidden = true;
                        if (!c.bars.length && c.tf < 60) load(c, true);
                        setPrice(c, ts[ts.length - 1][1]);
                        legend(c, null);
                    }
                });
            } catch (e) {
                feedLine({ error: e.message });
            }
        }
        pollT = setTimeout(poll, wait);
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
        const groups = [...new Set(cfg.instruments.map((i) => i.group))];
        root.innerHTML = `<div class="ch-bar">
                <form class="ch-add" autocomplete="off"><input list="ch-syms" placeholder="Instrument" aria-label="Instrument to add" spellcheck="false">
                    <select aria-label="Timeframe">${tfOptions(900)}</select>
                    <button type="submit" class="ch-addbtn">${IC.plus}Add chart</button></form>
                <span class="ch-count"></span>
                <span class="ch-feed"><i></i><span>${esc(cfg.note || '')}</span></span></div>
            <datalist id="ch-syms">${groups.map((g) => cfg.instruments.filter((i) => i.group === g)
                .map((i) => `<option value="${esc(i.label)}">${esc(i.name)}</option>`).join('')).join('')}</datalist>
            <div class="ch-grid"></div>
            <div class="ch-empty" hidden>No charts open. Pick an instrument and a timeframe above and press <b>Add chart</b>: they share the screen as you add more.</div>`;
        grid = root.querySelector('.ch-grid');
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
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && maxed && visible() && !e.target.closest('input')) { maxed = null; charts.forEach((c) => { c.el.querySelector('[data-a="max"]').innerHTML = IC.max; }); relayout(); } });
        document.addEventListener('visibilitychange', () => { if (!document.hidden && visible()) poll(); });
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
    window.LiveCharts = { start, shown, open };
})();
