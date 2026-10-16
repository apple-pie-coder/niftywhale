// Screenshots of the NiftyWhale dashboard for the README, over the Chrome DevTools protocol.
// node --experimental-websocket scripts/readme-screenshots.mjs <base url> <out dir> <stock symbol>  (README, 15. Development)
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';

const [base, out, stock] = process.argv.slice(2);
const PORT = 9411, W = 1440, H = 900;
mkdirSync(out, { recursive: true });
const chrome = spawn('chromium', ['--headless=new', '--ozone-platform=headless', '--disable-gpu', '--no-sandbox',
    '--disable-dev-shm-usage', '--password-store=basic', '--disable-extensions',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${out}/../profile-${Date.now()}`,
    `--window-size=${W},${H}`, '--hide-scrollbars', '--no-first-run', '--no-default-browser-check', 'about:blank'],
    { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let targets;
for (let i = 0; i < 60 && !targets; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch { await sleep(500); }
}
const page = targets.find((t) => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let seq = 0;
const waiting = new Map();
ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq;
    waiting.set(id, (m) => (m.error ? reject(new Error(`${method}: ${m.error.message}`)) : resolve(m.result)));
    ws.send(JSON.stringify({ id, method, params }));
});
const js = async (expr) => (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result.value;

await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
await send('Page.enable');
console.log('connected'); await send('Page.navigate', { url: `${base}/#swing` }); console.log('loading');
await sleep(12000);

async function shot(name, rectExpr) {
    const r = await js(rectExpr);
    if (!r) { console.log(`skip ${name}: not found`); return; }
    const { data } = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true,
        clip: { x: r.x, y: r.y, width: r.w, height: r.h, scale: 1 } });
    writeFileSync(`${out}/${name}.png`, Buffer.from(data, 'base64'));
    console.log(`${name}.png ${Math.round(r.w)}x${Math.round(r.h)}`);
}
// An element's box on the page (scrolled into view first, so lazily drawn parts are drawn).
const box = (sel, pad = 8) => `(async () => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null;
    const s = e.closest('section') || e; s.scrollIntoView({ block: 'start' }); await new Promise((r) => setTimeout(r, 1500));
    const b = s.getBoundingClientRect(); return { x: Math.max(0, b.left - ${pad}), y: b.top + scrollY - ${pad},
    w: Math.min(innerWidth, b.width + ${2 * pad}), h: b.height + ${2 * pad} }; })()`;

await shot('dashboard', `(async () => { scrollTo(0, 0); await new Promise((r) => setTimeout(r, 1500)); return { x: 0, y: 0, w: innerWidth, h: innerHeight }; })()`);
await js(`document.head.insertAdjacentHTML('beforeend', '<style id="shot-css">.topbar{position:static!important}</style>')`);
await shot('funnel', box('#funnel'));
await shot('setups', box('#setups'));
await shot('paper-trades', box('#s-journal'));

await js(`document.getElementById('shot-css').remove()`);
await js(`(async () => { scrollTo(0, 0); openStock(${JSON.stringify(stock)}); })()`);
await sleep(15000);
await shot('stock-panel', `(async () => { const d = document.querySelector('#drawer'); const b = d.getBoundingClientRect();
    return { x: b.left, y: b.top + scrollY, w: b.width, h: Math.min(b.height, innerHeight) }; })()`);

ws.close();
chrome.kill();
process.exit(0);
