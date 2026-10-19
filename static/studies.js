/* The Charts tab's indicators, worked out in the page so they move with every live tick.
 *
 * Studies.compute(type, bars, params, tf) -> { line key: [{ time, value } | { time } (a gap) | { time, value, color }] }
 * for the kinds in niftywhale/studies.py (the catalogue: settings, defaults, lines). `bars` are the chart's candles
 * ({ time, open, high, low, close, v }, time in seconds on the IST clock), `tf` their length in seconds.
 *
 * Each follows TradingView's own definition (its Pine reference implementations), so a chart reads the same in both:
 * averages seeded with a simple average of their first `length` values, Wilder's smoothing (rma) for RSI, ATR and
 * DMI, a population standard deviation for Bollinger and VWAP bands, and the same warm-up (no value until enough
 * candles). Pure functions; runs in the page (window.Studies) and under Node for the tests (tests/js).
 */
(function (root, factory) {
    const S = factory();
    if (typeof module === 'object' && module.exports) module.exports = S;
    else root.Studies = S;
}(typeof self !== 'undefined' ? self : this, function () {
    'use strict';
    const N = NaN, ok = Number.isFinite;

    // ---------------------------------------------------------------- building blocks (arrays in, arrays out; NaN = none)
    function source(bars, name) {
        return bars.map((b) => {
            switch (name) {
                case 'open': return b.open;
                case 'high': return b.high;
                case 'low': return b.low;
                case 'hl2': return (b.high + b.low) / 2;
                case 'hlc3': return (b.high + b.low + b.close) / 3;
                case 'ohlc4': return (b.open + b.high + b.low + b.close) / 4;
                default: return b.close;
            }
        });
    }
    // Over the `n` values up to i, only when all of them are there.
    function windowed(x, n, fn) {
        const out = new Array(x.length).fill(N);
        let run = 0;                                     // finite values in a row up to i
        for (let i = 0; i < x.length; i++) {
            run = ok(x[i]) ? run + 1 : 0;
            if (run >= n) out[i] = fn(i - n + 1, i);
        }
        return out;
    }
    // The rolling ones below carry their window along (each candle O(1), not O(length): they run on every tick, over
    // every candle of up to 16 charts) and add it up again from scratch every `n` candles, so rounding never builds up.
    const exact = (x, a, b, fn) => { for (let k = a; k <= b; k++) fn(x[k], k - a + 1); };
    function sum(x, n) {
        const out = new Array(x.length).fill(N);
        let run = 0, s = 0, nz = 0;                      // nz: non-zero values in the window (all zero: exactly 0)
        for (let i = 0; i < x.length; i++) {
            if (!ok(x[i])) { run = 0; s = 0; nz = 0; continue; }
            run++;
            s += x[i]; if (x[i] !== 0) nz++;
            if (run > n) { s -= x[i - n]; if (x[i - n] !== 0) nz--; }
            if (run > n && run % n === 0) { s = 0; exact(x, i - n + 1, i, (v) => { s += v; }); }
            if (run >= n) out[i] = nz ? s : 0;
        }
        return out;
    }
    function sma(x, n) { return sum(x, n).map((s) => s / n); }
    // An average that carries itself forward with weight `alpha`, seeded with the simple average of its first n values.
    function smoothed(x, n, alpha) {
        const out = new Array(x.length).fill(N), seed = sma(x, n);
        let prev = N;
        for (let i = 0; i < x.length; i++) {
            if (ok(prev)) prev = ok(x[i]) ? alpha * x[i] + (1 - alpha) * prev : N;
            else if (ok(seed[i])) prev = seed[i];
            out[i] = prev;
        }
        return out;
    }
    const ema = (x, n) => smoothed(x, n, 2 / (n + 1));
    const rma = (x, n) => smoothed(x, n, 1 / n);
    function wma(x, n) {
        const norm = n * (n + 1) / 2, out = new Array(x.length).fill(N);
        let run = 0, s = 0, num = 0;                     // the window's sum, and its sum weighted 1 … n
        for (let i = 0; i < x.length; i++) {
            if (!ok(x[i])) { run = 0; s = 0; num = 0; continue; }
            run++;
            if (run <= n) { num += x[i] * run; s += x[i]; }
            else { num += n * x[i] - s; s += x[i] - x[i - n]; }   // every weight one down, the oldest out, the newest at n
            if (run > n && run % n === 0) { s = 0; num = 0; exact(x, i - n + 1, i, (v, w) => { s += v; num += v * w; }); }
            if (run >= n) out[i] = num / norm;
        }
        return out;
    }
    function stdev(x, n) {                              // population (TradingView's ta.stdev)
        const out = new Array(x.length).fill(N);
        // Sums of x − k and its square, k a recent value: small numbers, so the difference of squares keeps its digits.
        let run = 0, k = 0, s = 0, sq = 0;
        const resum = (i) => { k = x[i]; s = 0; sq = 0; exact(x, i - Math.min(run, n) + 1, i, (v) => { s += v - k; sq += (v - k) * (v - k); }); };
        for (let i = 0; i < x.length; i++) {
            if (!ok(x[i])) { run = 0; s = 0; sq = 0; continue; }
            run++;
            if (run === 1) k = x[i];
            const d = x[i] - k;
            s += d; sq += d * d;
            if (run > n) { const o = x[i - n] - k; s -= o; sq -= o * o; }
            if (run > n && run % n === 0) resum(i);
            if (run >= n) out[i] = Math.sqrt(Math.max(0, sq / n - (s / n) * (s / n)));
        }
        return out;
    }
    function meanDev(x, n) {                            // mean absolute deviation from the average (ta.dev)
        return windowed(x, n, (a, b) => {
            let m = 0; for (let k = a; k <= b; k++) m += x[k]; m /= n;
            let d = 0; for (let k = a; k <= b; k++) d += Math.abs(x[k] - m);
            return d / n;
        });
    }
    // The window's highest (or lowest): a queue of the candles that can still be it, best first.
    function extreme(x, n, beats) {
        const out = new Array(x.length).fill(N), q = [];
        let run = 0, head = 0;
        for (let i = 0; i < x.length; i++) {
            if (!ok(x[i])) { run = 0; q.length = 0; head = 0; continue; }
            run++;
            while (q.length > head && !beats(x[q[q.length - 1]], x[i])) q.pop();
            q.push(i);
            if (q[head] <= i - n) head++;
            if (run >= n) out[i] = x[q[head]];
        }
        return out;
    }
    const highest = (x, n) => extreme(x, n, (a, b) => a > b);
    const lowest = (x, n) => extreme(x, n, (a, b) => a < b);
    function change(x, n = 1) { return x.map((v, i) => (i >= n ? v - x[i - n] : N)); }
    // True range; the first candle has none to compare with: its own range when `first`, else none (ta.tr(true / false)).
    function trueRange(bars, first) {
        return bars.map((b, i) => (i === 0 ? (first ? b.high - b.low : N)
            : Math.max(b.high - b.low, Math.abs(b.high - bars[i - 1].close), Math.abs(b.low - bars[i - 1].close))));
    }
    const atr = (bars, n) => rma(trueRange(bars, true), n);
    function stoch(close, high, low, n) {
        const hh = highest(high, n), ll = lowest(low, n);
        return close.map((c, i) => (hh[i] > ll[i] ? 100 * (c - ll[i]) / (hh[i] - ll[i]) : N));
    }
    function rsi(x, n) {
        const d = change(x);
        const up = rma(d.map((v) => (ok(v) ? Math.max(v, 0) : N)), n), dn = rma(d.map((v) => (ok(v) ? -Math.min(v, 0) : N)), n);
        return up.map((u, i) => (!ok(u) || !ok(dn[i]) ? N : dn[i] === 0 ? 100 : u === 0 ? 0 : 100 - 100 / (1 + u / dn[i])));
    }

    // ---------------------------------------------------------------- periods (VWAP anchors, pivots)
    const DAY = 86400;
    function periodKey(t, period) {
        const day = Math.floor(t / DAY);
        if (period === 'week') return Math.floor((day + 3) / 7);           // weeks from Monday (1 Jan 1970 was a Thursday)
        if (period === 'month') { const d = new Date(t * 1000); return d.getUTCFullYear() * 12 + d.getUTCMonth(); }
        return day;
    }

    // ---------------------------------------------------------------- output
    const pts = (bars, x) => bars.map((b, i) => (ok(x[i]) ? { time: b.time, value: x[i] } : { time: b.time }));
    // A line `shift` candles to the right (into the future past the last candle, at its spacing) or left (negative).
    function shifted(bars, x, shift, tf) {
        const out = [], n = bars.length;
        for (let i = 0; i < n; i++) {
            const j = i + shift;
            if (j < 0) continue;
            const time = j < n ? bars[j].time : bars[n - 1].time + (j - n + 1) * tf;
            out.push(ok(x[i]) ? { time, value: x[i] } : { time });
        }
        // The candles a left shift leaves without a value, as gaps, so the line ends where its data does.
        for (let j = Math.max(0, n + shift); j < n && shift < 0; j++) out.push({ time: bars[j].time });
        return out;
    }

    // ---------------------------------------------------------------- the indicators
    const K = {};
    const maOf = (fn) => (bars, p) => ({ ma: pts(bars, fn(source(bars, p.source), p.length, bars)) });
    K.sma = maOf(sma);
    K.ema = maOf(ema);
    K.wma = maOf(wma);
    K.hma = maOf((x, n) => {
        const half = wma(x, Math.max(1, Math.floor(n / 2))), full = wma(x, n);
        return wma(half.map((h, i) => 2 * h - full[i]), Math.max(1, Math.floor(Math.sqrt(n))));
    });
    K.vwma = maOf((x, n, bars) => {
        const v = bars.map((b) => b.v || 0), num = sma(x.map((s, i) => s * v[i]), n), den = sma(v, n);
        return num.map((a, i) => (den[i] > 0 ? a / den[i] : N));
    });

    K.vwap = (bars, p, tf) => {
        const out = { vwap: [], up1: [], lo1: [], up2: [], lo2: [] };
        let key = null, sv = 0, spv = 0, sp2v = 0;
        bars.forEach((b) => {
            const k = periodKey(b.time, p.anchor === 'session' ? 'day' : p.anchor);   // a daily chart's session is each candle
            if (k !== key) { key = k; sv = 0; spv = 0; sp2v = 0; }
            const tp = (b.high + b.low + b.close) / 3, v = b.v || 0;
            sv += v; spv += tp * v; sp2v += tp * tp * v;
            const vw = sv > 0 ? spv / sv : N, sd = sv > 0 ? Math.sqrt(Math.max(sp2v / sv - vw * vw, 0)) : N;
            const put = (name, val) => out[name].push(ok(val) ? { time: b.time, value: val } : { time: b.time });
            put('vwap', vw);
            put('up1', p.band1 > 0 ? vw + p.band1 * sd : N); put('lo1', p.band1 > 0 ? vw - p.band1 * sd : N);
            put('up2', p.band2 > 0 ? vw + p.band2 * sd : N); put('lo2', p.band2 > 0 ? vw - p.band2 * sd : N);
        });
        return out;
    };

    function bands(bars, basis, width, mult) {
        return { basis: pts(bars, basis), upper: pts(bars, basis.map((m, i) => m + mult * width[i])),
                 lower: pts(bars, basis.map((m, i) => m - mult * width[i])) };
    }
    K.bb = (bars, p) => { const x = source(bars, p.source); return bands(bars, sma(x, p.length), stdev(x, p.length), p.mult); };
    K.kc = (bars, p) => bands(bars, ema(source(bars, 'close'), p.length), atr(bars, p.atr_length), p.mult);
    K.dc = (bars, p) => {
        const hi = highest(bars.map((b) => b.high), p.length), lo = lowest(bars.map((b) => b.low), p.length);
        return { basis: pts(bars, hi.map((h, i) => (h + lo[i]) / 2)), upper: pts(bars, hi), lower: pts(bars, lo) };
    };

    K.supertrend = (bars, p) => {
        const a = atr(bars, p.atr_length), n = bars.length;
        const upB = new Array(n).fill(N), loB = new Array(n).fill(N), st = new Array(n).fill(N), dir = new Array(n).fill(N);
        for (let i = 0; i < n; i++) {
            const b = bars[i], src = (b.high + b.low) / 2;
            let up = src + p.factor * a[i], lo = src - p.factor * a[i];
            const pLo = i && ok(loB[i - 1]) ? loB[i - 1] : 0, pUp = i && ok(upB[i - 1]) ? upB[i - 1] : 0;
            const pc = i ? bars[i - 1].close : N;
            lo = lo > pLo || pc < pLo ? lo : pLo;
            up = up < pUp || pc > pUp ? up : pUp;
            loB[i] = lo; upB[i] = up;
            if (!ok(a[i])) continue;
            if (!i || !ok(a[i - 1])) dir[i] = 1;
            else if (st[i - 1] === upB[i - 1]) dir[i] = b.close > up ? -1 : 1;
            else dir[i] = b.close < lo ? 1 : -1;
            st[i] = dir[i] === -1 ? lo : up;
        }
        return { up: pts(bars, st.map((v, i) => (dir[i] === -1 ? v : N))), down: pts(bars, st.map((v, i) => (dir[i] === 1 ? v : N))) };
    };

    K.psar = (bars, p) => {
        const n = bars.length, out = new Array(n).fill(N);
        let result = N, maxMin = N, acc = N, below = false;
        for (let i = 1; i < n; i++) {
            const b = bars[i];
            let first = false;
            if (i === 1) {
                if (b.close > bars[0].close) { below = true; maxMin = b.high; result = bars[0].low; }
                else { below = false; maxMin = b.low; result = bars[0].high; }
                first = true;
                acc = p.start;
            }
            result += acc * (maxMin - result);
            if (below) {
                if (result > b.low) { first = true; below = false; result = Math.max(b.high, maxMin); maxMin = b.low; acc = p.start; }
            } else if (result < b.high) { first = true; below = true; result = Math.min(b.low, maxMin); maxMin = b.high; acc = p.start; }
            if (!first) {
                if (below) { if (b.high > maxMin) { maxMin = b.high; acc = Math.min(acc + p.increment, p.max); } }
                else if (b.low < maxMin) { maxMin = b.low; acc = Math.min(acc + p.increment, p.max); }
            }
            if (below) { result = Math.min(result, bars[i - 1].low); if (i > 1) result = Math.min(result, bars[i - 2].low); }
            else { result = Math.max(result, bars[i - 1].high); if (i > 1) result = Math.max(result, bars[i - 2].high); }
            out[i] = result;
        }
        return { sar: pts(bars, out) };
    };

    K.ichimoku = (bars, p, tf) => {
        const hi = bars.map((b) => b.high), lo = bars.map((b) => b.low);
        const mid = (n) => { const h = highest(hi, n), l = lowest(lo, n); return h.map((v, i) => (v + l[i]) / 2); };
        const conv = mid(p.conversion), base = mid(p.base), d = p.displacement - 1;
        return {
            conv: pts(bars, conv), base: pts(bars, base),
            lag: shifted(bars, bars.map((b) => b.close), -d, tf),
            span_a: shifted(bars, conv.map((c, i) => (c + base[i]) / 2), d, tf),
            span_b: shifted(bars, mid(p.span_b), d, tf),
        };
    };

    // Each period's levels from the one before it; the last candle of a period is left as a gap, so each period's
    // levels show as their own segment.
    K.pivots = (bars, p, tf) => {
        const period = p.period !== 'auto' ? p.period : tf < DAY ? 'day' : tf < 604800 ? 'week' : 'month';
        const keys = bars.map((b) => periodKey(b.time, period));
        const hlc = new Map();
        bars.forEach((b, i) => {
            const s = hlc.get(keys[i]);
            if (!s) hlc.set(keys[i], { h: b.high, l: b.low, c: b.close });
            else { s.h = Math.max(s.h, b.high); s.l = Math.min(s.l, b.low); s.c = b.close; }
        });
        const order = [...hlc.keys()], index = new Map(order.map((k, j) => [k, j]));
        const names = ['p', 'r1', 'r2', 'r3', 'r4', 's1', 's2', 's3', 's4'];
        const out = Object.fromEntries(names.map((k) => [k, []]));
        const levels = (H, L, C) => {
            const P = (H + L + C) / 3, R = H - L;
            if (p.kind === 'fibonacci') return { p: P, r1: P + 0.382 * R, r2: P + 0.618 * R, r3: P + R, s1: P - 0.382 * R, s2: P - 0.618 * R, s3: P - R };
            if (p.kind === 'camarilla') {
                const k = 1.1 * R;
                return { p: P, r1: C + k / 12, r2: C + k / 6, r3: C + k / 4, r4: C + k / 2, s1: C - k / 12, s2: C - k / 6, s3: C - k / 4, s4: C - k / 2 };
            }
            return { p: P, r1: 2 * P - L, s1: 2 * P - H, r2: P + R, s2: P - R, r3: 2 * P + H - 2 * L, s3: 2 * P - 2 * H + L,
                     r4: 3 * P + H - 3 * L, s4: 3 * P - 3 * H + L };
        };
        const memo = new Map();
        bars.forEach((b, i) => {
            const at = index.get(keys[i]);
            const lastOfPeriod = i + 1 < bars.length && keys[i + 1] !== keys[i];
            let lv = null;
            if (at > 0 && !lastOfPeriod) {
                if (!memo.has(at)) { const s = hlc.get(order[at - 1]); memo.set(at, levels(s.h, s.l, s.c)); }
                lv = memo.get(at);
            }
            names.forEach((k) => out[k].push(lv && ok(lv[k]) ? { time: b.time, value: lv[k] } : { time: b.time }));
        });
        return out;
    };

    K.volume = (bars, p, tf, colors) => {
        const v = bars.map((b) => b.v || 0);
        return {
            vol: bars.map((b) => ({ time: b.time, value: b.v || 0, color: (b.close >= b.open ? colors.up : colors.down) })),
            ma: p.ma_length > 0 ? pts(bars, sma(v, p.ma_length)) : bars.map((b) => ({ time: b.time })),
        };
    };
    K.rsi = (bars, p) => ({ rsi: pts(bars, rsi(source(bars, p.source), p.length)) });
    K.macd = (bars, p, tf, colors) => {
        const x = source(bars, p.source), f = ema(x, p.fast), s = ema(x, p.slow);
        const m = f.map((v, i) => v - s[i]), sig = ema(m, p.signal), h = m.map((v, i) => v - sig[i]);
        return {
            macd: pts(bars, m), signal: pts(bars, sig),
            // TradingView's four shades: above or below zero, growing or shrinking (one colour when it is set).
            hist: bars.map((b, i) => (!ok(h[i]) ? { time: b.time } : { time: b.time, value: h[i], color: colors.hist || (h[i] >= 0
                ? (i && h[i] < h[i - 1] ? '#b2dfdb' : '#26a69a') : (i && h[i] > h[i - 1] ? '#ffcdd2' : '#ff5252')) })),
        };
    };
    K.stoch = (bars, p) => {
        const k = sma(stoch(bars.map((b) => b.close), bars.map((b) => b.high), bars.map((b) => b.low), p.k_length), p.k_smooth);
        return { k: pts(bars, k), d: pts(bars, sma(k, p.d_smooth)) };
    };
    K.stochrsi = (bars, p) => {
        const r = rsi(source(bars, p.source), p.rsi_length), k = sma(stoch(r, r, r, p.stoch_length), p.k_smooth);
        return { k: pts(bars, k), d: pts(bars, sma(k, p.d_smooth)) };
    };
    K.atr = (bars, p) => ({ atr: pts(bars, atr(bars, p.length)) });
    K.adx = (bars, p) => {
        const hi = bars.map((b) => b.high), lo = bars.map((b) => b.low);
        const up = change(hi), down = change(lo).map((v) => -v);
        const pdm = up.map((u, i) => (!ok(u) ? N : u > down[i] && u > 0 ? u : 0));
        const mdm = down.map((d, i) => (!ok(d) ? N : d > up[i] && d > 0 ? d : 0));
        const tr = rma(trueRange(bars, false), p.length);
        const fix = (x) => { let last = N; return x.map((v) => (ok(v) ? (last = v) : last)); };      // fixnan
        const plus = fix(rma(pdm, p.length).map((v, i) => 100 * v / tr[i]));
        const minus = fix(rma(mdm, p.length).map((v, i) => 100 * v / tr[i]));
        const adx = rma(plus.map((v, i) => { const s = v + minus[i]; return Math.abs(v - minus[i]) / (s === 0 ? 1 : s); }), p.smoothing)
            .map((v) => 100 * v);
        return { adx: pts(bars, adx), plus: pts(bars, plus), minus: pts(bars, minus) };
    };
    K.cci = (bars, p) => {
        const x = source(bars, p.source), m = sma(x, p.length), d = meanDev(x, p.length);
        return { cci: pts(bars, x.map((v, i) => (d[i] > 0 ? (v - m[i]) / (0.015 * d[i]) : N))) };
    };
    K.willr = (bars, p) => {
        const hh = highest(bars.map((b) => b.high), p.length), ll = lowest(bars.map((b) => b.low), p.length);
        return { r: pts(bars, bars.map((b, i) => (hh[i] > ll[i] ? 100 * (b.close - hh[i]) / (hh[i] - ll[i]) : N))) };
    };
    K.mfi = (bars, p) => {
        const tp = source(bars, 'hlc3'), d = change(tp), v = bars.map((b) => b.v || 0);
        const up = sum(tp.map((t, i) => (!ok(d[i]) ? N : d[i] <= 0 ? 0 : t * v[i])), p.length);
        const dn = sum(tp.map((t, i) => (!ok(d[i]) ? N : d[i] >= 0 ? 0 : t * v[i])), p.length);
        return { mfi: pts(bars, up.map((u, i) => (!ok(u) ? N : dn[i] === 0 ? (u === 0 ? N : 100) : 100 - 100 / (1 + u / dn[i])))) };
    };
    K.obv = (bars) => {
        let run = 0;
        return { obv: bars.map((b, i) => {
            if (i) run += Math.sign(b.close - bars[i - 1].close) * (b.v || 0);
            return { time: b.time, value: run };
        }) };
    };
    K.roc = (bars, p) => {
        const x = source(bars, p.source);
        return { roc: pts(bars, x.map((v, i) => (i >= p.length && x[i - p.length] ? 100 * (v - x[i - p.length]) / x[i - p.length] : N))) };
    };

    function compute(type, bars, params, tf, colors) {
        const fn = K[type];
        if (!fn || !bars || !bars.length) return {};
        return fn(bars, params || {}, tf || 60, colors || {});
    }
    return { compute, kinds: Object.keys(K), _: { source, sma, ema, rma, wma, stdev, meanDev, highest, lowest, change, trueRange, atr, rsi, stoch, periodKey, shifted } };
}));
