/* NiftyWhale's theme engine: Light, Dark or System, the same on every page.
 *
 * Loaded first in <head> (a plain blocking script, before any CSS paints) so a page never flashes the
 * wrong theme. It resolves the choice to 'light' or 'dark' and sets it on <html data-theme>, which is all
 * the CSS keys on (static/theme.css holds both palettes). With System it follows the device and switches
 * live when the device does.
 *
 * The choice is kept per device (localStorage 'nw.theme', JSON like the app's other settings) and open
 * tabs follow each other. Each change:
 *   - sets <html data-theme> ('light' | 'dark') and data-theme-pref ('light' | 'system' | 'dark');
 *   - sets the browser's colour for its own bars (<meta name="theme-color" data-light data-dark>);
 *   - eases the colours over a quarter second (<html class="theme-fading">), unless reduced motion is asked for;
 *   - fires a 'themechange' event on window ({detail: {theme, pref}}) for what draws its own colours.
 *
 * The style is a second choice, kept the same way (localStorage 'nw.style'): Glass (the default) or Classic,
 * set on <html data-style> ('glass' | 'classic'); static/theme.css holds both.
 *
 * The palette is the third (localStorage 'nw.palette', <html data-palette>): NiftyWhale's own (static/theme.css)
 * or one of PALETTES below, popular editor themes and Terminal (phosphor green, for trading). Their colours are
 * data here, written once as CSS (<style id="nw-palettes">) before anything paints, every derived token a
 * concrete colour (the charts read them and cannot parse color-mix). A palette with one variant (Dracula, Nord,
 * Monokai, Terminal: dark only) holds the page to it whatever the Light / System / Dark choice.
 * Gain / loss pairs are the palettes' own hues where they stay apart under colour blindness (checked with the
 * dataviz validator: OKLab ΔE ≥ 8 under protan / deutan); GitHub's are its colour-blind blue / orange.
 * Every change fires 'themechange' ({detail: {theme, pref, style, palette}}).
 *
 * window.NWTheme: pref, theme, style, palette, PALETTES, set(pref), setStyle(style), setPalette(id), cycle(),
 *   control(opts) -> a Light / System / Dark switch, styleControl(opts) -> a Glass / Classic switch,
 *   manager() -> the theme manager (mode, style and a card for each palette).
 */
(function () {
    'use strict';
    var KEY = 'nw.theme', MODES = ['light', 'system', 'dark'];
    var LABEL = { light: 'Light', system: 'System', dark: 'Dark' };
    var ICON = {
        light: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"/></svg>',
        system: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1.8" y="2.5" width="12.4" height="8.6" rx="1.4"/><path d="M5.5 13.8h5M8 11.1v2.7"/></svg>',
        dark: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.6 9.6A5.8 5.8 0 0 1 6.4 2.4a5.8 5.8 0 1 0 7.2 7.2z"/></svg>'
    };
    var STYLE_KEY = 'nw.style', STYLES = ['glass', 'classic'];
    var STYLE_LABEL = { glass: 'Glass', classic: 'Classic' };
    var STYLE_ICON = {
        glass: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.8S3.6 6.5 3.6 9.6a4.4 4.4 0 0 0 8.8 0C12.4 6.5 8 1.8 8 1.8z"/><path d="M6 10.2a2 2 0 0 0 1.6 1.8"/></svg>',
        classic: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="0.8"/><path d="M2 5.6h12"/></svg>'
    };
    // Each variant: plane (behind everything), surface (panels), ink / ink2 / ink3 (text, strong to muted), accent
    // and the ink on it, good / warn / bad (states), up / down (candles), gain / loss (results in charts), vwap.
    var PALETTES = [
        { id: 'niftywhale', name: 'NiftyWhale', note: 'Deep sea teal', base: true, modes: {
            light: { plane: '#eaf0f6', surface: '#f8fafc', ink: '#0f1d2b', ink3: '#6b7b8c', accent: '#0b6e8a', gain: '#0b8a74', loss: '#d0532a' },
            dark:  { plane: '#09131e', surface: '#13202e', ink: '#e6edf3', ink3: '#7d8e9e', accent: '#3fb5d4', gain: '#2ea597', loss: '#dd6b3d' } } },
        { id: 'terminal', name: 'Terminal', note: 'Phosphor green, monospace: for trading', font: 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace', modes: {
            dark: { plane: '#010603', surface: '#03100a', ink: '#4dff88', ink2: '#33d96b', ink3: '#1fa04e', accent: '#39ff6a', accentInk: '#001a08',
                    good: '#39ff6a', warn: '#d6ff3d', bad: '#ff4f7b', up: '#39ff6a', down: '#ff4f7b', gain: '#39ff6a', loss: '#ff4f7b', vwap: '#00e5c0' } } },
        { id: 'solarized', name: 'Solarized', note: 'Ethan Schoonover', modes: {
            light: { plane: '#eee8d5', surface: '#fdf6e3', ink: '#073642', ink2: '#586e75', ink3: '#657b83', accent: '#268bd2', accentInk: '#fdf6e3',
                     good: '#859900', warn: '#b58900', bad: '#dc322f', up: '#859900', down: '#dc322f', gain: '#2aa198', loss: '#cb4b16', vwap: '#6c71c4' },
            dark:  { plane: '#002b36', surface: '#073642', ink: '#eee8d5', ink2: '#93a1a1', ink3: '#839496', accent: '#268bd2', accentInk: '#fdf6e3',
                     good: '#859900', warn: '#b58900', bad: '#dc322f', up: '#859900', down: '#dc322f', gain: '#2aa198', loss: '#cb4b16', vwap: '#6c71c4' } } },
        { id: 'github', name: 'GitHub', note: 'Primer', modes: {
            light: { plane: '#f6f8fa', surface: '#ffffff', ink: '#1f2328', ink2: '#424a53', ink3: '#656d76', accent: '#0969da', accentInk: '#ffffff',
                     good: '#1a7f37', warn: '#9a6700', bad: '#d1242f', up: '#1a7f37', down: '#d1242f', gain: '#0969da', loss: '#bc4c00', vwap: '#8250df' },
            dark:  { plane: '#010409', surface: '#0d1117', ink: '#e6edf3', ink2: '#c9d1d9', ink3: '#7d8590', accent: '#4493f8', accentInk: '#0d1117',
                     good: '#3fb950', warn: '#d29922', bad: '#f85149', up: '#3fb950', down: '#f85149', gain: '#4493f8', loss: '#db6d28', vwap: '#ab7df8' } } },
        { id: 'catppuccin', name: 'Catppuccin', note: 'Latte and Mocha', modes: {
            light: { plane: '#e6e9ef', surface: '#eff1f5', ink: '#4c4f69', ink2: '#5c5f77', ink3: '#6c6f85', accent: '#8839ef', accentInk: '#eff1f5',
                     good: '#40a02b', warn: '#df8e1d', bad: '#d20f39', up: '#40a02b', down: '#d20f39', gain: '#179299', loss: '#fe640b', vwap: '#7287fd' },
            dark:  { plane: '#181825', surface: '#1e1e2e', ink: '#cdd6f4', ink2: '#bac2de', ink3: '#9399b2', accent: '#cba6f7', accentInk: '#1e1e2e',
                     good: '#a6e3a1', warn: '#f9e2af', bad: '#f38ba8', up: '#a6e3a1', down: '#f38ba8', gain: '#94e2d5', loss: '#fab387', vwap: '#b4befe' } } },
        { id: 'gruvbox', name: 'Gruvbox', note: 'Retro groove', modes: {
            light: { plane: '#f2e5bc', surface: '#fbf1c7', ink: '#3c3836', ink2: '#504945', ink3: '#7c6f64', accent: '#076678', accentInk: '#fbf1c7',
                     good: '#79740e', warn: '#b57614', bad: '#9d0006', up: '#79740e', down: '#9d0006', gain: '#427b58', loss: '#af3a03', vwap: '#8f3f71' },
            dark:  { plane: '#1d2021', surface: '#282828', ink: '#ebdbb2', ink2: '#d5c4a1', ink3: '#a89984', accent: '#83a598', accentInk: '#1d2021',
                     good: '#b8bb26', warn: '#fabd2f', bad: '#fb4934', up: '#b8bb26', down: '#fb4934', gain: '#8ec07c', loss: '#fe8019', vwap: '#d3869b' } } },
        { id: 'one', name: 'One', note: 'Atom One Light and Dark', modes: {
            light: { plane: '#f0f0f1', surface: '#fafafa', ink: '#383a42', ink2: '#4f525e', ink3: '#696c77', accent: '#4078f2', accentInk: '#ffffff',
                     good: '#50a14f', warn: '#c18401', bad: '#e45649', up: '#50a14f', down: '#e45649', gain: '#0184bc', loss: '#e45649', vwap: '#a626a4' },
            dark:  { plane: '#21252b', surface: '#282c34', ink: '#dcdfe4', ink2: '#abb2bf', ink3: '#7f848e', accent: '#61afef', accentInk: '#21252b',
                     good: '#98c379', warn: '#e5c07b', bad: '#e06c75', up: '#98c379', down: '#e06c75', gain: '#56b6c2', loss: '#d19a66', vwap: '#c678dd' } } },
        { id: 'tokyonight', name: 'Tokyo Night', note: 'Day and Night', modes: {
            light: { plane: '#d0d5e3', surface: '#e1e2e7', ink: '#3760bf', ink2: '#6172b0', ink3: '#6c76a8', accent: '#2e7de9', accentInk: '#ffffff',
                     good: '#587539', warn: '#8c6c3e', bad: '#f52a65', up: '#587539', down: '#f52a65', gain: '#118c74', loss: '#b15c00', vwap: '#9854f1' },
            dark:  { plane: '#16161e', surface: '#1a1b26', ink: '#c0caf5', ink2: '#a9b1d6', ink3: '#737aa2', accent: '#7aa2f7', accentInk: '#1a1b26',
                     good: '#9ece6a', warn: '#e0af68', bad: '#f7768e', up: '#9ece6a', down: '#f7768e', gain: '#73daca', loss: '#ff9e64', vwap: '#bb9af7' } } },
        { id: 'nord', name: 'Nord', note: 'Arctic, north-bluish', modes: {
            dark: { plane: '#2e3440', surface: '#3b4252', ink: '#eceff4', ink2: '#d8dee9', ink3: '#9aa3b5', accent: '#88c0d0', accentInk: '#2e3440',
                    good: '#a3be8c', warn: '#ebcb8b', bad: '#bf616a', up: '#a3be8c', down: '#bf616a', gain: '#8fbcbb', loss: '#d08770', vwap: '#b48ead' } } },
        { id: 'dracula', name: 'Dracula', note: 'Dark, vivid', modes: {
            dark: { plane: '#21222c', surface: '#282a36', ink: '#f8f8f2', ink2: '#e0e0da', ink3: '#7b88c0', accent: '#bd93f9', accentInk: '#282a36',
                    good: '#50fa7b', warn: '#f1fa8c', bad: '#ff5555', up: '#50fa7b', down: '#ff5555', gain: '#8be9fd', loss: '#ffb86c', vwap: '#ff79c6' } } },
        { id: 'monokai', name: 'Monokai', note: 'The classic editor dark', modes: {
            dark: { plane: '#1e1f1c', surface: '#272822', ink: '#f8f8f2', ink2: '#cfcfc2', ink3: '#8f8b74', accent: '#66d9ef', accentInk: '#272822',
                    good: '#a6e22e', warn: '#e6db74', bad: '#f92672', up: '#a6e22e', down: '#f92672', gain: '#a6e22e', loss: '#f92672', vwap: '#ae81ff' } } }
    ];
    var PALETTE_KEY = 'nw.palette';
    var paletteOf = function (id) { for (var i = 0; i < PALETTES.length; i++) if (PALETTES[i].id === id) return PALETTES[i]; return PALETTES[0]; };

    // Colours as numbers: a mix of two hex colours (t of the second), or one with an alpha.
    function rgb(h) { h = h.replace('#', ''); return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]; }
    function mix(a, b, t) {
        var x = rgb(a), y = rgb(b), out = '#';
        for (var i = 0; i < 3; i++) out += ('0' + Math.round(x[i] + (y[i] - x[i]) * t).toString(16)).slice(-2);
        return out;
    }
    function alpha(h, a) { var c = rgb(h); return 'rgba(' + c[0] + ', ' + c[1] + ', ' + c[2] + ', ' + a + ')'; }

    // One palette variant as the tokens static/theme.css defines for NiftyWhale's own.
    function tokens(v, mode) {
        var dark = mode === 'dark', ink2 = v.ink2 || mix(v.ink, v.surface, 0.25);
        var t = {
            'color-scheme': mode,
            '--plane': v.plane, '--surface': v.surface, '--glass': alpha(v.surface, dark ? 0.74 : 0.66),
            '--sunk': mix(v.surface, v.ink, 0.05), '--line': mix(v.surface, v.ink, 0.13), '--line-strong': mix(v.surface, v.ink, 0.26),
            '--ink': v.ink, '--ink-2': ink2, '--ink-3': v.ink3,
            '--accent': v.accent, '--accent-ink': v.accentInk, '--accent-soft': mix(v.surface, v.accent, dark ? 0.22 : 0.14),
            '--good': v.good, '--good-soft': mix(v.surface, v.good, 0.16),
            '--warn': v.warn, '--warn-soft': mix(v.surface, v.warn, 0.16),
            '--bad': v.bad, '--bad-soft': mix(v.surface, v.bad, 0.16),
            '--ob': alpha(v.accent, 0.20), '--ob-line': alpha(v.accent, 0.72),
            '--fvg': alpha(v.warn, 0.22), '--fvg-line': alpha(v.warn, 0.72),
            '--up': v.up, '--down': v.down, '--gain': v.gain, '--loss': v.loss,
            '--vwap': v.vwap, '--bb': v.ink3, '--bb-fill': alpha(v.ink3, 0.09), '--orb': alpha(v.accent, 0.08), '--orb-line': v.accent,
            '--amb-1': alpha(v.accent, dark ? 0.24 : 0.26), '--amb-2': alpha(v.vwap, 0.20), '--amb-3': alpha(v.warn, 0.08), '--amb-4': alpha(v.gain, 0.14)
        };
        if (dark) t['--glass-edge'] = alpha(v.ink, 0.14);
        return t;
    }
    (function writePalettes() {
        var css = '';
        PALETTES.forEach(function (p) {
            if (p.base) return;
            Object.keys(p.modes).forEach(function (m) {
                var t = tokens(p.modes[m], m), body = '';
                Object.keys(t).forEach(function (k) { body += k + ': ' + t[k] + '; '; });
                if (p.font) body += '--font: ' + p.font + '; ';
                css += ':root[data-palette="' + p.id + '"][data-theme="' + m + '"] { ' + body + '}\n';
            });
        });
        var el = document.createElement('style');
        el.id = 'nw-palettes';
        el.textContent = css;
        // Ahead of theme.css in the page, but more specific (html, palette and mode) than its own palettes: these win.
        document.head.appendChild(el);
    })();

    var root = document.documentElement;
    var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

    function read() {
        try {
            var v = JSON.parse(localStorage.getItem(KEY));
            return MODES.indexOf(v) >= 0 ? v : 'system';
        } catch (e) { return 'system'; }
    }
    function readStyle() {
        try {
            var v = JSON.parse(localStorage.getItem(STYLE_KEY));
            return STYLES.indexOf(v) >= 0 ? v : 'glass';
        } catch (e) { return 'glass'; }
    }
    function readPalette() {
        try { var v = JSON.parse(localStorage.getItem(PALETTE_KEY)); return paletteOf(v).id; } catch (e) { return PALETTES[0].id; }
    }
    var pref = read(), style = readStyle(), palette = readPalette(), fadeTimer = null;
    // The mode asked for, unless the palette has only the other one.
    var resolve = function () {
        var t = pref === 'system' ? (mq && mq.matches ? 'dark' : 'light') : pref, modes = paletteOf(palette).modes;
        return modes[t] ? t : Object.keys(modes)[0];
    };

    function paint() {
        var t = resolve();
        root.setAttribute('data-theme', t);
        root.setAttribute('data-theme-pref', pref);
        root.setAttribute('data-style', style);
        root.setAttribute('data-palette', palette);
        var metas = document.querySelectorAll('meta[name="theme-color"][data-' + t + ']');
        var own = paletteOf(palette), plane = own.base ? null : own.modes[t].plane;      // a chosen palette: its own plane
        for (var i = 0; i < metas.length; i++) metas[i].setAttribute('content', plane || metas[i].getAttribute('data-' + t));
        return t;
    }

    function apply(animate) {
        var before = root.getAttribute('data-theme'), beforePref = root.getAttribute('data-theme-pref');
        var after = resolve();
        if (before === after && beforePref === pref) return;
        var done = false;
        var commit = function () {
            if (done) return;
            done = true;
            paint();
            var ctl = document.querySelectorAll('[data-theme-set]');
            for (var i = 0; i < ctl.length; i++) ctl[i].setAttribute('aria-checked', String(ctl[i].getAttribute('data-theme-set') === pref));
            var icons = document.querySelectorAll('[data-theme-icon]');
            for (var j = 0; j < icons.length; j++) icons[j].innerHTML = ICON[pref];
            // After the colours changed, so what reads them (charts, the mood tint) reads the new ones.
            try { window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: after, pref: pref, style: style, palette: palette } })); } catch (e) { /* very old browser */ }
        };
        // The colours ease into the new theme (plain CSS transitions for a moment: nothing can get stuck).
        if (animate && before && before !== after) fade();
        commit();
    }

    function fade() {
        if (reduce && reduce.matches) return;
        root.classList.add('theme-fading');
        clearTimeout(fadeTimer);
        fadeTimer = setTimeout(function () { root.classList.remove('theme-fading'); }, 320);
    }

    function applyStyle() {
        if (root.getAttribute('data-style') === style) return;
        fade();
        paint();
        changed();
    }

    // A palette or style changed: what reads the colours reads them again.
    function changed() {
        var marks = [['[data-theme-set]', 'data-theme-set', pref], ['[data-style-set]', 'data-style-set', style], ['[data-palette-set]', 'data-palette-set', palette]];
        marks.forEach(function (m) {
            var ctl = document.querySelectorAll(m[0]);
            for (var i = 0; i < ctl.length; i++) ctl[i].setAttribute('aria-checked', String(ctl[i].getAttribute(m[1]) === m[2]));
        });
        try { window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: resolve(), pref: pref, style: style, palette: palette } })); } catch (e) { /* very old browser */ }
    }

    function applyPalette() {
        if (root.getAttribute('data-palette') === palette) return;
        fade();
        paint();
        changed();
    }

    function setPalette(id) {
        palette = paletteOf(id).id;
        try { localStorage.setItem(PALETTE_KEY, JSON.stringify(palette)); } catch (e) { /* private mode: this page only */ }
        applyPalette();
    }

    function setStyle(s) {
        if (STYLES.indexOf(s) < 0) return;
        style = s;
        try { localStorage.setItem(STYLE_KEY, JSON.stringify(s)); } catch (e) { /* private mode: this page only */ }
        applyStyle();
    }

    function set(p) {
        if (MODES.indexOf(p) < 0) return;
        pref = p;
        try { localStorage.setItem(KEY, JSON.stringify(p)); } catch (e) { /* private mode: this page only */ }
        apply(true);
    }

    // The device's own setting changed (System follows it); another tab chose something else.
    if (mq) {
        var onSystem = function () { if (pref === 'system') apply(true); };
        if (mq.addEventListener) mq.addEventListener('change', onSystem); else if (mq.addListener) mq.addListener(onSystem);
    }
    window.addEventListener('storage', function (e) {
        if (e.key === KEY) { pref = read(); apply(true); }
        if (e.key === STYLE_KEY) { style = readStyle(); applyStyle(); }
        if (e.key === PALETTE_KEY) { palette = readPalette(); applyPalette(); }
    });

    // A radio group of buttons (icons, with labels unless `compact`): Light / System / Dark, or Glass / Classic.
    function group(opts, name, values, label, icon, attr, current, choose, title) {
        opts = opts || {};
        var el = document.createElement('div');
        el.className = 'theme-seg' + (opts.compact ? ' compact' : '') + (opts.vertical ? ' vertical' : '');
        el.setAttribute('role', 'radiogroup');
        el.setAttribute('aria-label', name);
        el.innerHTML = values.map(function (m) {
            return '<button type="button" role="radio" ' + attr + '="' + m + '" aria-checked="' + (m === current()) + '"'
                + ' title="' + title(m) + '" aria-label="' + label[m] + '">'
                + icon[m] + (opts.compact ? '' : '<span>' + label[m] + '</span>') + '</button>';
        }).join('');
        el.addEventListener('click', function (e) {
            var b = e.target.closest('[' + attr + ']');
            if (b) choose(b.getAttribute(attr));
        });
        // Arrow keys move the choice, as in any radio group.
        el.addEventListener('keydown', function (e) {
            var d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
            if (!d) return;
            e.preventDefault();
            var next = values[(values.indexOf(current()) + d + values.length) % values.length];
            choose(next);
            var b = el.querySelector('[' + attr + '="' + next + '"]');
            if (b) b.focus();
        });
        return el;
    }
    function control(opts) {
        return group(opts, 'Theme', MODES, LABEL, ICON, 'data-theme-set', function () { return pref; }, set,
            function (m) { return LABEL[m] + (m === 'system' ? ': follow this device' : ' theme'); });
    }
    function styleControl(opts) {
        return group(opts, 'Style', STYLES, STYLE_LABEL, STYLE_ICON, 'data-style-set', function () { return style; }, setStyle,
            function (m) { return m === 'glass' ? 'Glass: translucent, glossy' : 'Classic: plain and flat'; });
    }

    // The theme manager: the mode and style switches, and a card for each palette (a small picture of it in the
    // mode it would show), as one radio group. Cards follow changes made anywhere (another switch, another tab).
    function manager() {
        var el = document.createElement('div');
        el.className = 'tmgr';
        el.innerHTML = '<div class="tmgr-row"><span class="tmgr-k">Mode</span></div><div class="tmgr-row"><span class="tmgr-k">Style</span></div>'
            + '<div class="tmgr-k">Palette</div><div class="tmgr-grid" role="radiogroup" aria-label="Palette"></div><p class="tmgr-note" hidden></p>';
        var rows = el.querySelectorAll('.tmgr-row');
        rows[0].appendChild(control({}));
        rows[1].appendChild(styleControl({}));
        var grid = el.querySelector('.tmgr-grid'), note = el.querySelector('.tmgr-note');
        function shown(p) { var t = pref === 'system' ? (mq && mq.matches ? 'dark' : 'light') : pref; return p.modes[t] ? t : Object.keys(p.modes)[0]; }
        function card(p) {
            var m = shown(p), v = p.modes[m], ink2 = v.ink2 || mix(v.ink, v.surface, 0.25), only = Object.keys(p.modes).length === 1;
            return '<button type="button" role="radio" class="tmgr-card" data-palette-set="' + p.id + '" aria-checked="' + (p.id === palette) + '"'
                + ' tabindex="' + (p.id === palette ? 0 : -1) + '" aria-label="' + p.name + (only ? ' (dark only)' : '') + '">'
                + '<span class="tmgr-prev" style="background:' + v.plane + (p.font ? ';font-family:' + p.font.replace(/"/g, '&quot;') : '') + '">'
                + '<span class="tmgr-panel" style="background:' + v.surface + ';border-color:' + mix(v.surface, v.ink, 0.15) + '">'
                + '<i class="tmgr-t" style="background:' + v.ink + '"></i><i class="tmgr-t s" style="background:' + v.ink3 + '"></i>'
                + '<span class="tmgr-bars"><i style="height:70%;background:' + v.gain + '"></i><i style="height:38%;background:' + v.gain + '"></i>'
                + '<i style="height:52%;background:' + v.loss + '"></i><i style="height:86%;background:' + v.gain + '"></i><i style="height:30%;background:' + v.loss + '"></i></span>'
                + '<i class="tmgr-pill" style="background:' + v.accent + '"></i></span>'
                + '<span class="tmgr-aa" style="color:' + ink2 + '">Aa</span></span>'
                + '<span class="tmgr-name">' + p.name + '<small>' + p.note + (only ? ' · dark only' : '') + '</small></span></button>';
        }
        function render() {
            grid.innerHTML = PALETTES.map(card).join('');
            var p = paletteOf(palette), t = pref === 'system' ? (mq && mq.matches ? 'dark' : 'light') : pref;
            note.hidden = !!p.modes[t];
            note.textContent = p.modes[t] ? '' : p.name + ' has no ' + t + ' version, so the page stays ' + resolve() + ' while it is chosen.';
        }
        render();
        grid.addEventListener('click', function (e) {
            var b = e.target.closest('[data-palette-set]');
            if (b) setPalette(b.getAttribute('data-palette-set'));
        });
        grid.addEventListener('keydown', function (e) {
            var d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
            if (!d) return;
            e.preventDefault();
            var ids = PALETTES.map(function (p) { return p.id; }), next = ids[(ids.indexOf(palette) + d + ids.length) % ids.length];
            setPalette(next);
            var b = grid.querySelector('[data-palette-set="' + next + '"]');
            if (b) b.focus();
        });
        // Re-drawn on any change (the previews follow the mode), keeping the focus on the chosen card.
        window.addEventListener('themechange', function () {
            if (!el.isConnected) return;
            var had = grid.contains(document.activeElement);
            render();
            if (had) { var b = grid.querySelector('[aria-checked="true"]'); if (b) b.focus(); }
        });
        return el;
    }

    window.NWTheme = {
        MODES: MODES, LABEL: LABEL, ICON: ICON, STYLES: STYLES, STYLE_LABEL: STYLE_LABEL, STYLE_ICON: STYLE_ICON,
        get pref() { return pref; },
        get theme() { return resolve(); },
        get style() { return style; },
        get palette() { return palette; },
        PALETTES: PALETTES,
        set: set,
        setStyle: setStyle,
        setPalette: setPalette,
        cycle: function () { set(MODES[(MODES.indexOf(pref) + 1) % MODES.length]); },
        control: control,
        styleControl: styleControl,
        manager: manager
    };
    paint();
})();
