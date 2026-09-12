"""Draw the store tiles under app/static/img/stores.

Kept as a script rather than done by hand so the eleven tiles stay identical
apart from their two variables, and so adding a retailer is one row.
"""
import io
import os

# store_type: (mark, background, ink). The colours are each retailer's own, so
# a lane is recognisable at 14 pixels where its name is not.
TILES = {
    'amazon':      ('a',   '#FF9900', '#232F3E'),
    'walmart':     ('W',   '#0071DC', '#FFC220'),
    'newegg':      ('N',   '#F26722', '#FFFFFF'),
    'microcenter': ('MC',  '#0057B8', '#FFFFFF'),
    'bestbuy':     ('BB',  '#FFE000', '#003B64'),
    'bh':          ('B&H', '#F5F5F5', '#111111'),
    'adorama':     ('A',   '#E31837', '#FFFFFF'),
    'target':      ('T',   '#CC0000', '#FFFFFF'),
    'gamestop':    ('GS',  '#E71316', '#FFFFFF'),
    'nintendo':    ('N',   '#E60012', '#FFFFFF'),
    'test':        ('TS',  '#6B7280', '#FFFFFF'),
    # Drawn for a store with no tile of its own, so the chart never has a hole.
    'generic':     ('?',   '#6B7280', '#FFFFFF'),
}

# One glyph can be drawn large; three have to fit the same 32px tile. The
# tiles are drawn at 18px on the charts, so a mark that only just fits at
# 32 is mush at the size anyone actually reads it.
SIZES = {1: 21, 2: 16, 3: 12}

TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" '
    'viewBox="0 0 32 32" role="img" aria-label="{label}">\n'
    '  <title>{label}</title>\n'
    '  <rect width="32" height="32" rx="7" fill="{bg}"/>\n'
    '  <text x="16" y="16" fill="{ink}" font-size="{size}" font-weight="700"\n'
    '        font-family="Segoe UI, Roboto, Helvetica, Arial, sans-serif"\n'
    '        text-anchor="middle" dominant-baseline="central">{mark}</text>\n'
    '</svg>\n'
)

LABELS = {'generic': 'Store'}
try:
    from app.scrapers import STORE_LABELS
    LABELS.update(STORE_LABELS)
except Exception:
    pass

def escape(text):
    """An SVG is XML: B&H's own name is not well-formed until the & is one."""
    return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;'))


out = os.path.join('app', 'static', 'img', 'stores')
for store, (mark, bg, ink) in sorted(TILES.items()):
    svg = TEMPLATE.format(
        label=escape(LABELS.get(store, store.title())),
        bg=bg, ink=ink, size=SIZES[len(mark)],
        mark=escape(mark),
    )
    io.open(os.path.join(out, store + '.svg'), 'w', encoding='utf-8',
            newline='\n').write(svg)
    print('wrote', store + '.svg')
