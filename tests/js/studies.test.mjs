// The Charts tab's indicator maths (static/studies.js), on candles where the right answer is known.
// Run:  node --test tests/js/   (Node 20; the catalogue is read from niftywhale/studies.py with python3)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const S = createRequire(import.meta.url)(path.join(root, 'static/studies.js'));
const { _: f } = S;
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);
const vals = (out) => out.map((p) => (p.value === undefined ? NaN : p.value));
const last = (out) => out[out.length - 1].value;
const DAY = 86400, T0 = 1_791_500_000 - (1_791_500_000 % DAY);       // a day's start on the chart's clock
// Candles from closes: open at the previous close, high / low a step either side, volume given or 1000.
function bars(closes, { tf = 900, vol = null, start = T0 + 9.25 * 3600 } = {}) {
    return closes.map((c, i) => {
        const o = i ? closes[i - 1] : c;
        return { time: start + i * tf, open: o, high: Math.max(o, c) + 1, low: Math.min(o, c) - 1, close: c, v: vol ? vol[i] : 1000 };
    });
}
const rising = (n, from = 100, step = 2) => Array.from({ length: n }, (_, i) => from + i * step);

test('averages: simple, seeded exponential, Wilder, weighted', () => {
    assert.deepEqual(f.sma([1, 2, 3, 4, 5], 3).slice(2), [2, 3, 4]);
    assert.ok(Number.isNaN(f.sma([1, 2, 3, 4, 5], 3)[1]));
    assert.deepEqual(f.ema([1, 2, 3, 4, 5], 3).slice(2), [2, 3, 4]);           // seed 2, then halfway each step
    assert.deepEqual(f.rma([1, 2, 3, 4, 5], 2).slice(1), [1.5, 2.25, 3.125, 4.0625]);
    near(f.wma([1, 2, 3], 3)[2], 14 / 6);
    assert.deepEqual(f.ema([NaN, NaN, 3, 3, 3, 9], 3).slice(4), [3, 6]);      // a later start seeds from its own first values
});

test('spread: population standard deviation, mean deviation, highest, lowest', () => {
    near(f.stdev([2, 4, 4, 4, 5, 5, 7, 9], 8)[7], 2);
    near(f.meanDev([1, 2, 3, 6], 4)[3], 1.5);
    assert.deepEqual(f.highest([1, 5, 2, 3], 2).slice(1), [5, 5, 3]);
    assert.deepEqual(f.lowest([4, 1, 2, 3], 2).slice(1), [1, 1, 2]);
});

test('RSI: 100 on gains only, 0 on losses only, Wilder-smoothed in between', () => {
    near(f.rsi(rising(30), 14)[29], 100);
    near(f.rsi(rising(30, 200, -1), 14)[29], 0);
    const zig = Array.from({ length: 40 }, (_, i) => (i % 2 ? 101 : 100));
    near(f.rsi(zig, 2)[39], 200 / 3, 1e-6);                                    // settles at gains 2/3, losses 1/3 after a gain
});

test('moving averages on a straight line lag it by the expected amount', () => {
    const b = bars(rising(60));
    near(last(S.compute('sma', b, { length: 20, source: 'close' }).ma), b[59].close - 19);     // the middle of 20 steps of 2
    near(last(S.compute('wma', b, { length: 3, source: 'close' }).ma), b[59].close - 4 / 3);       // (c-4 + 2(c-2) + 3c) / 6
    near(last(S.compute('hma', b, { length: 16, source: 'close' }).ma), b[59].close - 4 / 3, 1e-6);   // 2·WMA8 − WMA16 leads by a third of a step, WMA4 lags one
    near(last(S.compute('vwma', b, { length: 20, source: 'close' }).ma), b[59].close - 19);
    const out = S.compute('ema', b, { length: 9, source: 'close' }).ma;
    assert.equal(out.length, 60);
    assert.equal(out[7].value, undefined);                                      // warm-up: a gap, not a guess
});

test('Bollinger, Keltner, Donchian', () => {
    const flat = bars(Array(30).fill(100));
    const bb = S.compute('bb', flat, { length: 20, mult: 2, source: 'close' });
    assert.equal(last(bb.upper), 100); assert.equal(last(bb.lower), 100);
    const b = bars([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]);
    const bb2 = S.compute('bb', b, { length: 4, mult: 2, source: 'close' });
    near(last(bb2.upper) - last(bb2.basis), 2 * Math.sqrt(1.25));
    const dc = S.compute('dc', b, { length: 5 });
    assert.equal(last(dc.upper), 21); assert.equal(last(dc.lower), 14); assert.equal(last(dc.basis), 17.5);   // wicks: a point past open / close
    const kc = S.compute('kc', b, { length: 5, mult: 2, atr_length: 3 });
    near(last(kc.upper) - last(kc.basis), 2 * 3, 1e-2);                         // true ranges of 3 (the first candle's, 2, fading out)
});

test('VWAP: volume-weighted typical price, from each session (or week, month)', () => {
    const b = [
        { time: T0 + 33300, open: 10, high: 10, low: 10, close: 10, v: 1 },
        { time: T0 + 34200, open: 20, high: 20, low: 20, close: 20, v: 3 },
        { time: T0 + DAY + 33300, open: 50, high: 50, low: 50, close: 50, v: 2 },
    ];
    const v = S.compute('vwap', b, { anchor: 'session', band1: 1, band2: 0 }, 900);
    assert.deepEqual(vals(v.vwap), [10, 17.5, 50]);                             // (10 + 60) / 4, then a new session
    near(v.up1[1].value - 17.5, Math.sqrt((100 + 3 * 400) / 4 - 17.5 ** 2));
    assert.equal(v.up2[1].value, undefined);                                    // band 2 off
    assert.deepEqual(vals(S.compute('vwap', b, { anchor: 'week', band1: 0, band2: 0 }, 900).vwap), [10, 17.5, (10 + 60 + 100) / 6]);
});

test('Supertrend and Parabolic SAR follow a trend from the right side', () => {
    const up = bars(rising(60)), dn = bars(rising(60, 300, -2));
    const st = S.compute('supertrend', up, { atr_length: 10, factor: 3 });
    assert.ok(last(st.up) < up[59].low && st.down[59].value === undefined);
    const st2 = S.compute('supertrend', dn, { atr_length: 10, factor: 3 });
    assert.ok(last(st2.down) > dn[59].high && st2.up[59].value === undefined);
    const sar = S.compute('psar', up, { start: 0.02, increment: 0.02, max: 0.2 }).sar;
    assert.ok(last(sar) < up[59].low);
    assert.ok(last(S.compute('psar', dn, { start: 0.02, increment: 0.02, max: 0.2 }).sar) > dn[59].high);
    assert.equal(sar[0].value, undefined);
});

test('Ichimoku: leading spans drawn ahead of the last candle, the lagging span behind', () => {
    const b = bars(rising(80), { tf: 900 });
    const ic = S.compute('ichimoku', b, { conversion: 9, base: 26, span_b: 52, displacement: 26 }, 900);
    assert.equal(ic.span_a[ic.span_a.length - 1].time, b[79].time + 25 * 900);
    assert.equal(ic.lag.length, 80);
    assert.equal(ic.lag[54].value, b[79].close);                                // today's close, 25 candles back
    assert.equal(ic.lag[79].value, undefined);
    near(last(ic.conv), (b[79].high + b[71].low) / 2);
});

test('pivot points from the previous period', () => {
    // Day 1: high 110, low 90, close 100; day 2 reads its levels from day 1.
    const d1 = [[100, 110, 95, 105], [105, 106, 90, 100]], d2 = [[100, 101, 99, 100], [100, 102, 98, 101]];
    const b = [...d1.map((o, i) => ({ time: T0 + 33300 + i * 900, open: o[0], high: o[1], low: o[2], close: o[3], v: 1 })),
               ...d2.map((o, i) => ({ time: T0 + DAY + 33300 + i * 900, open: o[0], high: o[1], low: o[2], close: o[3], v: 1 }))];
    const tr = S.compute('pivots', b, { kind: 'traditional', period: 'auto' }, 900);
    assert.deepEqual([2, 3].map((i) => tr.p[i].value), [100, 100]);
    assert.deepEqual(['r1', 's1', 'r2', 's2', 'r3', 's3'].map((k) => tr[k][3].value), [110, 90, 120, 80, 130, 70]);
    assert.equal(tr.p[0].value, undefined);                                     // no day before the first
    const fib = S.compute('pivots', b, { kind: 'fibonacci', period: 'day' }, 900);
    near(fib.r1[3].value, 100 + 0.382 * 20); assert.equal(fib.r4[3].value, undefined);
    const cam = S.compute('pivots', b, { kind: 'camarilla', period: 'day' }, 900);
    near(cam.r4[3].value, 100 + 1.1 * 20 / 2); near(cam.s3[3].value, 100 - 1.1 * 20 / 4);
});

test('oscillators at their extremes', () => {
    const up = bars(rising(60));
    near(last(S.compute('rsi', up, { length: 14, source: 'close' }).rsi), 100);
    const st = S.compute('stoch', up, { k_length: 14, k_smooth: 1, d_smooth: 3 });
    near(last(st.k), 100 * (up[59].close - up[46].low) / (up[59].high - up[46].low));
    near(last(S.compute('willr', up, { length: 14 }).r), 100 * (up[59].close - up[59].high) / (up[59].high - up[46].low));
    near(last(S.compute('mfi', up, { length: 14 }).mfi), 100);
    near(last(S.compute('roc', up, { length: 10, source: 'close' }).roc), 100 * 20 / up[49].close);
    const adx = S.compute('adx', up, { length: 14, smoothing: 14 });
    assert.ok(last(adx.plus) > last(adx.minus) && last(adx.adx) > 50);
    const c = S.compute('cci', bars(Array(30).fill(100)), { length: 20, source: 'hlc3' }).cci;
    assert.equal(last(c), undefined);                                           // no spread: no reading, not a division by zero
});

test('MACD: the histogram is MACD less its signal', () => {
    const b = bars(Array.from({ length: 80 }, (_, i) => 100 + 10 * Math.sin(i / 6)));
    const m = S.compute('macd', b, { fast: 12, slow: 26, signal: 9, source: 'close' }, 900, {});
    near(m.hist[79].value, m.macd[79].value - m.signal[79].value);
    assert.ok(m.hist[79].color);
    assert.equal(S.compute('macd', b, { fast: 12, slow: 26, signal: 9, source: 'close' }, 900, { hist: '#123456' }).hist[79].color, '#123456');
});

test('volume and OBV', () => {
    const b = bars([10, 11, 10, 12], { vol: [100, 200, 50, 400] });
    assert.deepEqual(vals(S.compute('obv', b).obv), [0, 200, 150, 550]);
    const v = S.compute('volume', b, { ma_length: 2 }, 900, { up: 'u', down: 'd' });
    assert.deepEqual(v.vol.map((p) => [p.value, p.color]), [[100, 'u'], [200, 'u'], [50, 'd'], [400, 'u']]);
    assert.deepEqual(vals(v.ma).slice(1), [150, 125, 225]);
});

test('every kind in the catalogue draws every one of its lines', () => {
    const catalog = JSON.parse(execFileSync('python3', ['-c',
        'import json, sys; sys.path.insert(0, "."); from niftywhale import studies; print(json.dumps(studies.CATALOG))'], { cwd: root }));
    const b = bars(Array.from({ length: 400 }, (_, i) => 100 + 15 * Math.sin(i / 9) + i * 0.05), { vol: Array.from({ length: 400 }, (_, i) => 500 + (i * 37) % 900) });
    assert.deepEqual(catalog.map((k) => k.key).sort(), [...S.kinds].sort());
    for (const k of catalog) {
        const params = Object.fromEntries(k.params.map((p) => [p.key, p.default]));
        const out = S.compute(k.key, b, params, 900, {});
        for (const ln of k.lines) {
            assert.ok(Array.isArray(out[ln.key]), `${k.key}.${ln.key} missing`);
            const pts = out[ln.key];
            for (let i = 1; i < pts.length; i++) assert.ok(pts[i].time > pts[i - 1].time, `${k.key}.${ln.key} out of order`);
            assert.ok(pts.every((p) => p.value === undefined || Number.isFinite(p.value)), `${k.key}.${ln.key} not finite`);
            if (!(k.key === 'pivots' && ln.key.endsWith('4'))) assert.ok(pts.some((p) => p.value !== undefined), `${k.key}.${ln.key} empty`);
        }
    }
});
