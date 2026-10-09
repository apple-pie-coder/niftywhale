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
 * window.NWTheme: pref, theme, set(pref), cycle(), control(opts) -> a Light / System / Dark switch.
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
    var root = document.documentElement;
    var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

    function read() {
        try {
            var v = JSON.parse(localStorage.getItem(KEY));
            return MODES.indexOf(v) >= 0 ? v : 'system';
        } catch (e) { return 'system'; }
    }
    var pref = read(), fadeTimer = null;
    var resolve = function () { return pref === 'system' ? (mq && mq.matches ? 'dark' : 'light') : pref; };

    function paint() {
        var t = resolve();
        root.setAttribute('data-theme', t);
        root.setAttribute('data-theme-pref', pref);
        var metas = document.querySelectorAll('meta[name="theme-color"][data-' + t + ']');
        for (var i = 0; i < metas.length; i++) metas[i].setAttribute('content', metas[i].getAttribute('data-' + t));
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
            try { window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: after, pref: pref } })); } catch (e) { /* very old browser */ }
        };
        // The colours ease into the new theme (plain CSS transitions for a moment: nothing can get stuck).
        if (animate && before && before !== after && !(reduce && reduce.matches)) {
            root.classList.add('theme-fading');
            clearTimeout(fadeTimer);
            fadeTimer = setTimeout(function () { root.classList.remove('theme-fading'); }, 320);
        }
        commit();
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
    window.addEventListener('storage', function (e) { if (e.key === KEY) { pref = read(); apply(true); } });

    // A Light / System / Dark switch: a radio group of three buttons (icons, with labels unless `compact`).
    function control(opts) {
        opts = opts || {};
        var el = document.createElement('div');
        el.className = 'theme-seg' + (opts.compact ? ' compact' : '') + (opts.vertical ? ' vertical' : '');
        el.setAttribute('role', 'radiogroup');
        el.setAttribute('aria-label', 'Theme');
        el.innerHTML = MODES.map(function (m) {
            return '<button type="button" role="radio" data-theme-set="' + m + '" aria-checked="' + (m === pref) + '"'
                + ' title="' + LABEL[m] + (m === 'system' ? ': follow this device' : ' theme') + '" aria-label="' + LABEL[m] + '">'
                + ICON[m] + (opts.compact ? '' : '<span>' + LABEL[m] + '</span>') + '</button>';
        }).join('');
        el.addEventListener('click', function (e) {
            var b = e.target.closest('[data-theme-set]');
            if (b) set(b.getAttribute('data-theme-set'));
        });
        // Arrow keys move the choice, as in any radio group.
        el.addEventListener('keydown', function (e) {
            var d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
            if (!d) return;
            e.preventDefault();
            var next = MODES[(MODES.indexOf(pref) + d + MODES.length) % MODES.length];
            set(next);
            var b = el.querySelector('[data-theme-set="' + next + '"]');
            if (b) b.focus();
        });
        return el;
    }

    window.NWTheme = {
        MODES: MODES, LABEL: LABEL, ICON: ICON,
        get pref() { return pref; },
        get theme() { return resolve(); },
        set: set,
        cycle: function () { set(MODES[(MODES.indexOf(pref) + 1) % MODES.length]); },
        control: control
    };
    paint();
})();
