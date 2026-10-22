"""
The README's diagrams (sections 3.1, 3.3, 5.5 and 10.3) as SVG, in the app's palette (static/theme.css), light or dark
as the reader's device is set. Edit the boxes here and run it again when the text they draw changes:

  python3 scripts/readme-diagrams.py            # writes docs/diagrams/*.svg
"""
import html
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'diagrams')
STYLE = """<style>
  .bg { fill: #f8fafc; stroke: #cdd7e1; }
  .box { fill: #ffffff; stroke: #9fb0c2; stroke-width: 1.2; }
  .act { fill: #e2f1f5; stroke: #0b6e8a; stroke-width: 1.5; }
  .good { fill: #e3f4ea; stroke: #1a7f4b; stroke-width: 1.2; }
  .bad { fill: #fbe4e2; stroke: #b42318; stroke-width: 1.2; }
  .end { fill: #eef2f6; stroke: #9fb0c2; stroke-width: 1.2; stroke-dasharray: 4 3; }
  text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif; fill: #0f1d2b; }
  .t { font-size: 15px; font-weight: 600; }
  .s { font-size: 12px; fill: #3a4a5a; }
  .lab { font-size: 12px; fill: #3a4a5a; }
  .k { font-size: 11px; font-weight: 600; letter-spacing: 0.06em; fill: #6b7b8c; }
  .ln { stroke: #6b7b8c; stroke-width: 1.5; fill: none; }
  .arw { fill: #6b7b8c; }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #13202e; stroke: #2a3a4b; }
    .box { fill: #0f1a26; stroke: #4a5f74; }
    .act { fill: #11313d; stroke: #3fb5d4; }
    .good { fill: #10301f; stroke: #4cc38a; }
    .bad { fill: #3a1714; stroke: #f0857a; }
    .end { fill: #16232f; stroke: #4a5f74; }
    text { fill: #e6edf3; }
    .s, .lab { fill: #b6c3cf; }
    .k { fill: #7d8e9e; }
    .ln { stroke: #7d8e9e; }
    .arw { fill: #7d8e9e; }
  }
</style>
"""

def svg(name, w, h, title, desc, body, pad=0):
    body = f'<g transform="translate(0 {pad})">\n{body}\n</g>' if pad else body
    h += pad
    doc = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-labelledby="t d">
<title id="t">{html.escape(title)}</title>
<desc id="d">{html.escape(desc)}</desc>
{STYLE}<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="arw" d="M0 0L10 5L0 10z"/></marker></defs>
<rect class="bg" x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="10"/>
{body}</svg>
'''
    open(f'{OUT}/{name}.svg', 'w').write(doc)

def box(x, y, w, h, cls, title, *subs):
    lines = [title, *subs]
    lh_t, lh_s = 18, 15
    total = lh_t + lh_s * len(subs)
    top = y + (h - total) / 2 + 13
    out = [f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="7"/>',
           f'<text class="t" x="{x + w/2}" y="{top}" text-anchor="middle">{html.escape(title)}</text>']
    for i, s in enumerate(subs):
        out.append(f'<text class="s" x="{x + w/2}" y="{top + lh_t + i*lh_s}" text-anchor="middle">{html.escape(s)}</text>')
    return '\n'.join(out)

def path(d):
    return f'<path class="ln" d="{d}" marker-end="url(#a)"/>'

def line(d):
    return f'<path class="ln" d="{d}"/>'

def lab(x, y, text, anchor='middle', cls='lab'):
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{html.escape(text)}</text>'

# 1. The two jobs (3.1)
b = [lab(30, 36, 'EVENING · 16:15 IST, WEEKDAYS', 'start', 'k'),
     lab(480, 36, 'MARKET HOURS · 09:15–15:30 IST', 'start', 'k'),
     box(20, 50, 340, 96, 'box', 'Scan · daily candles', 'Steps 1–8 + provisional R:R', 'over the whole universe'),
     box(480, 50, 360, 96, 'box', 'Watch · 15-minute candles', 'Steps 9–12, after every candle', 'for the watched zones only'),
     path('M360 98 H476'), lab(418, 90, 'zones'),
     path('M660 146 V218'), lab(672, 188, 'CHoCH with R:R ≥ 1:3', 'start'),
     box(530, 222, 260, 50, 'act', 'Alert', 'on the dashboard and Telegram')]
svg('two-jobs', 860, 292, 'The two jobs',
    'In the evening, at 16:15 IST on weekdays, the scan reads daily candles and runs Steps 1 to 8 plus the provisional R:R '
    'over the whole universe; what passes becomes zones. In market hours, 09:15 to 15:30 IST, the watcher reads 15-minute '
    'candles for the watched zones only and runs Steps 9 to 12 after every candle. A CHoCH with R:R of at least 1:3 raises '
    'an alert on the dashboard and Telegram.', '\n'.join(b))

# 2. Swing zone life cycle (3.3)
b = [box(20, 60, 120, 44, 'box', 'watching'),
     path('M140 82 H296'), lab(218, 58, 'price trades'), lab(218, 73, 'into the zone'),
     box(300, 60, 120, 44, 'box', 'tapped'),
     path('M420 82 H556'), lab(470, 73, 'sweep + CHoCH'),
     box(560, 50, 200, 64, 'act', 'triggered', 'R:R ≥ 1:3: alert sent,', 'paper trade open'),
     path('M760 82 H780 V38 H796'), path('M780 82 V126 H796'),
     box(800, 14, 130, 48, 'good', 'won', 'hit its target'),
     box(800, 102, 130, 48, 'bad', 'lost', 'hit its stop'),
     line('M500 82 V214'), path('M500 214 H556'),
     box(560, 190, 200, 48, 'end', 'rejected', 'R:R < 1:3 or too late; no alert'),
     line('M80 104 V300'), path('M80 214 H226'), path('M80 300 H226'),
     box(230, 182, 240, 64, 'end', 'expired', 'the next scan no longer passes it,', 'or it is 10 days old'),
     box(230, 276, 240, 48, 'end', 'dismissed', 'you pressed Dismiss')]
svg('swing-zone-life-cycle', 950, 344, 'A swing zone\'s life cycle',
    'A watched zone becomes tapped when price trades into it. A sweep and CHoCH then either trigger it (R:R of at least '
    '1:3: an alert is sent and a paper trade opens, which is won at its target or lost at its stop) or reject it (R:R under '
    '1:3, or found too late; no alert). A watched zone expires when the next scan no longer passes it or it is 10 days old, '
    'and is dismissed when you press Dismiss.', '\n'.join(b), pad=12)

# 3. Intraday zone life cycle (5.5)
b = [box(20, 60, 120, 44, 'box', 'watching'),
     path('M140 82 H296'), lab(218, 58, 'price trades'), lab(218, 73, 'into the zone'),
     box(300, 60, 120, 44, 'box', 'tapped'),
     path('M420 82 H556'), lab(470, 58, '5m sweep'), lab(470, 73, '+ CHoCH'),
     box(560, 56, 200, 52, 'act', 'open trade', 'R:R ≥ 1:2: alert sent'),
     path('M760 82 H780 V28 H796'), path('M760 82 H796'), path('M780 82 V142 H796'),
     box(800, 5, 150, 46, 'good', 'target', 'won: +R:R'),
     box(800, 59, 150, 46, 'bad', 'stop', 'lost: −1R'),
     box(800, 113, 150, 58, 'end', 'squared off', 'at 15:10, for whatever', 'it was worth'),
     line('M500 82 V214'), path('M500 214 H556'),
     box(560, 190, 200, 48, 'end', 'rejected', 'R:R < 1:2, or after 14:30'),
     line('M80 104 V300'), path('M80 214 H226'), path('M80 300 H226'),
     box(230, 174, 250, 80, 'end', 'expired', 'no longer a 15m setup,', 'broke past the leg origin,', 'or no CHoCH by 14:30'),
     box(230, 276, 250, 48, 'end', 'dismissed', 'by you')]
svg('intraday-zone-life-cycle', 970, 344, 'An intraday zone\'s life cycle',
    'A watched intraday zone becomes tapped when price trades into it. A 5-minute sweep and CHoCH then either open a trade '
    '(R:R of at least 1:2: an alert is sent), which ends at its target (won, plus R:R), at its stop (lost, minus 1R) or '
    'squared off at 15:10 for whatever it was worth, or reject it (R:R under 1:2, or after 14:30). A watched zone expires '
    'when it is no longer a 15m setup, breaks past its leg origin, or has no CHoCH by 14:30, and is dismissed by you.',
    '\n'.join(b), pad=12)

# 4. Where the rules come from (10.3)
b = [box(20, 34, 230, 60, 'box', 'Built-in defaults', 'smc.Rules'),
     path('M250 64 H306'),
     box(310, 34, 230, 60, 'box', 'SMC_* in .env', 'replaces a default'),
     path('M540 64 H596'),
     box(600, 34, 240, 60, 'act', 'Saved from the rule tuner', 'replaces both'),
     lab(135, 118, 'LOWEST PRIORITY', 'middle', 'k'), lab(720, 118, 'HIGHEST PRIORITY', 'middle', 'k')]
svg('rules-precedence', 860, 136, 'Where the rules come from',
    'Each rule starts from the built-in default (smc.Rules). An SMC_ value in .env replaces the default, and a value saved '
    'from the rule tuner replaces both: it has the highest priority.', '\n'.join(b))
print('docs/diagrams: 4 diagrams written')
