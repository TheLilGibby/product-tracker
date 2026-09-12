"""
What the availability charts say: local times, and a tile per retailer.

    python test_chart_display.py

Everything here runs against an in-memory database - no network, no Chrome, no
retailer is contacted.

Why this exists, both halves of it:

A chart tooltip read "Sat, Sep 12, 2026 at 04:02:48 UTC". The times are
rendered on the server (the browser cannot know which timezone was picked in
settings), so the whole dashboard spoke UTC to someone reading it in Utah at
ten in the evening. DEFAULT_TIMEZONE now starts at America/Denver.

And every retailer was named in text only. A group's chart draws five lanes at
once, so the lanes carry their retailer's tile beside the name, and a tooltip
carries it where the colour swatch used to be.
"""
import io
import os
import re
import sys
from datetime import datetime, timedelta

os.environ.setdefault('SCHEDULER_ENABLED', '0')
os.environ.setdefault('TELEGRAM_ALERTS_ENABLED', '0')

from app import create_app, db
from app.models.product import (AvailabilityHistory, Product, ProductGroup,
                                ProductGroupMember)

FAILED = []

# One listing per retailer, so a lane cannot pick up its neighbour's tile.
LISTINGS = [
    ('https://www.gamestop.com/products/zelda-console/1234.html', 'gamestop'),
    ('https://www.nintendo.com/us/store/products/zelda-console/', 'nintendo'),
    ('https://www.bestbuy.com/site/zelda-console/6577900.p?skuId=6577900', 'bestbuy'),
]


def report(ok, what, detail=''):
    print(('  [ok] ' if ok else '  [FAIL] ') + what
          + (('   <- ' + detail) if detail and not ok else ''))
    if not ok:
        FAILED.append(what)


def seed():
    """A group of three listings, each with a few hours of stock history."""
    now = datetime.utcnow()
    group = ProductGroup(name='Zelda 40th console')
    db.session.add(group)
    db.session.flush()
    for index, (url, _store) in enumerate(LISTINGS):
        product = Product(name=f'Zelda console {index}', url=url, current_price=499.99,
                          available=index == 0, last_checked=now - timedelta(minutes=index))
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductGroupMember(group_id=group.id, product_id=product.id))
        for step in range(4):
            db.session.add(AvailabilityHistory(
                product_id=product.id,
                available=(step % 2 == 0) if index == 0 else False,
                timestamp=now - timedelta(hours=6 - step),
            ))
    db.session.commit()
    return group


def check_default_timezone(app):
    """Times are written where they are read, not in UTC."""
    print("The server defaults to Utah time")
    import importlib

    config = importlib.import_module('app.config')
    report(config.DEFAULT_TIMEZONE == 'America/Denver',
           'the module default is America/Denver', config.DEFAULT_TIMEZONE)
    report(app.config['DEFAULT_TIMEZONE'] == 'America/Denver',
           'and a built app carries it', app.config['DEFAULT_TIMEZONE'])
    # The environment and the settings page both still win over it.
    for name in ('Config', 'DevelopmentConfig', 'TestingConfig', 'ProductionConfig'):
        klass = getattr(config, name)
        report(klass.DEFAULT_TIMEZONE == 'America/Denver',
               f'{name} too', klass.DEFAULT_TIMEZONE)


def check_tiles_exist(app):
    """Every store the app supports can be drawn."""
    print("Every supported store has a tile")
    from app.scrapers import store_icon_path, supported_stores

    static = os.path.join(app.root_path, 'static')
    for store in sorted(set(supported_stores()) | {'generic'}):
        path = os.path.join(static, store_icon_path(store).replace('/', os.sep))
        if not os.path.exists(path):
            report(False, f'{store} has a tile', path)
            continue
        svg = io.open(path, encoding='utf-8').read()
        # An SVG is XML, so every & in it has to be an entity. B&H's own name is
        # what makes this worth checking: a bare & there is a tile that never
        # draws. Matched by hand rather than parsed - these are files this repo
        # generates, and a parser here would only add a dependency.
        loose = re.findall(r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', svg)
        if loose:
            report(False, f"{store}'s tile has a bare & in it", svg.strip()[:120])
            continue
        if not svg.rstrip().endswith('</svg>'):
            report(False, f"{store}'s tile is a complete SVG", svg.strip()[-60:])
            continue
        report(True, f'{store}: {store_icon_path(store)}')
    report(store_icon_path('a-store-with-no-tile-yet') == 'img/stores/generic.svg',
           'a store with no tile of its own falls back to the generic one',
           store_icon_path('a-store-with-no-tile-yet'))


def check_group_chart(app, group):
    """A group's lanes and its tooltips both know their retailer."""
    print("The group chart carries a tile per retailer")
    from app.charts import group_chart_ranges
    from app.groups import group_listings

    with app.test_request_context('/'):
        ranges = group_chart_ranges(group_listings(group))
    report(bool(ranges), 'the chart has something to draw')
    if not ranges:
        return
    window = ranges[0]

    icons = [lane['icon'] for lane in window['lanes']]
    report(all(icons), 'every lane has one', str(icons))
    report(len(set(icons)) == len(LISTINGS),
           'each retailer gets its own rather than one shared tile', str(icons))
    report(set(icons) == {'/static/img/stores/gamestop.svg',
                          '/static/img/stores/nintendo.svg',
                          '/static/img/stores/bestbuy.svg'},
           'and they are the right retailers', str(sorted(set(icons))))
    # The tooltip is built from the series, which never sees the lane.
    report([series['icon'] for series in window['series']] == icons,
           'the series carry the same tiles as their lanes')

    full = window['series'][0]['points'][0]['full']
    report(' MDT' in full or ' MST' in full, f'a tooltip title reads {full!r}', full)
    report('UTC' not in full, 'and no longer says UTC', full)


def check_listing_chart(app):
    """One listing's chart carries the same tile its lane has on the group's."""
    print("The listing chart carries its retailer's tile")
    from app.charts import availability_chart_ranges

    product = Product.query.order_by(Product.id).first()
    with app.test_request_context('/'):
        ranges = availability_chart_ranges(product)
    report(bool(ranges), 'the chart has something to draw')
    if not ranges:
        return
    report(ranges[0]['icon'] == '/static/img/stores/gamestop.svg',
           'the range carries the tile', str(ranges[0].get('icon')))
    full = ranges[0]['points'][0]['full']
    report(' MDT' in full or ' MST' in full, 'its tooltip is in local time too', full)


def check_pages_render(app, group):
    """Both pages serve, with the tiles and the code that paints them."""
    print("Both pages render")
    client = app.test_client()

    response = client.get(f'/group/{group.id}')
    body = response.get_data(as_text=True)
    report(response.status_code == 200, 'the group page renders', str(response.status_code))
    report('/static/img/stores/gamestop.svg' in body, 'with the tiles in its chart data')
    report('laneIcons' in body, 'the plugin that draws them down the y axis')
    report('labelPointStyle' in body, 'and the callback that puts one in a tooltip')

    product = Product.query.order_by(Product.id).first()
    response = client.get(f'/product/{product.id}')
    body = response.get_data(as_text=True)
    report(response.status_code == 200, 'the listing page renders', str(response.status_code))
    report('/static/img/stores/gamestop.svg' in body, 'with its tile')
    report('MDT' in body or 'MST' in body, 'and local times')


def main():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
        group = seed()

        check_default_timezone(app)
        check_tiles_exist(app)
        check_group_chart(app, group)
        check_listing_chart(app)
        check_pages_render(app, group)

    print()
    if FAILED:
        print(f'{len(FAILED)} FAILED')
        for item in FAILED:
            print('  - ' + item)
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
