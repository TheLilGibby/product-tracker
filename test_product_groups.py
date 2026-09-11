"""
Product groups and per-listing stock history checks.

    python test_product_groups.py            # all checks
    python test_product_groups.py -v         # with the app's own logging

Everything here runs against an in-memory database and fake scrapers - no
network, no Chrome, no retailer is contacted, and no alert is sent.

Why this exists: one real product (the Zelda Switch 2 console) is tracked as a
separate listing at every retailer, and the dashboard showed them as unrelated
rows. Product groups put those listings together, and AvailabilityHistory keeps
each listing's stock changes so a group can show when each retailer last had it.
Grouping must never delete a listing or change how it is checked or carted.

Exit code is non-zero if any check fails.
"""
import argparse
import logging
import sys
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta

# The app stores naive UTC via datetime.utcnow(); these checks compare against it.
warnings.filterwarnings('ignore', message='datetime.datetime.utcnow', category=DeprecationWarning)

from flask import render_template
from sqlalchemy import inspect, text

from app import create_app, db
from app import tasks
from app.groups import assign_group, find_or_create_group, grouped_view, summarize
from app.history_backfill import backfill_from_stock_checks
from app.models.product import AvailabilityHistory, PriceHistory, Product, ProductGroup, ProductGroupMember
import seed_groups

URLS = {
    'target': 'https://www.target.com/p/zelda-switch-2/-/A-11111111',
    'bestbuy': 'https://www.bestbuy.com/site/zelda-switch-2/1111111.p?skuId=1111111',
    'walmart': 'https://www.walmart.com/ip/zelda-switch-2/1111111',
}
STAMP = datetime(2026, 1, 1, 12, 0, 0)
# The grouped view's table as markup. The bare class name also appears in
# index.html's script, so it cannot tell the two views apart on its own.
GROUPED_TABLE = 'class="grouped-table"'


class FakeScraper:
    """Answers scrape_product from a {url: (available, price)} map instead of the network."""

    def __init__(self, stock):
        self.stock = stock

    def scrape_product(self, url):
        available, price = self.stock[url]
        return {'name': 'Zelda Switch 2', 'price': price, 'available': available}


def build_app():
    """A testing app: every listing due on every cycle, no dashboard password."""
    app = create_app('testing')
    scheduler = getattr(app, 'scheduler', None)
    if scheduler is not None:
        scheduler.remove_all_jobs()
        scheduler.pause()
    # .env may carry per-store intervals or a password; neither belongs in a test.
    app.config['STORE_CHECK_INTERVALS'] = {}
    app.config['DASHBOARD_PASSWORD'] = ''
    return app


def seed(stock):
    """A fresh database holding one listing per URL, in the state `stock` gives it."""
    db.session.remove()
    db.drop_all()
    db.create_all()
    products = {}
    for store, url in URLS.items():
        available, price = stock[url]
        products[store] = Product(name=f'Zelda Switch 2 ({store})', url=url, current_price=price,
                                  available=available, last_checked=STAMP,
                                  notify_on_price_drop=False, notify_on_availability=False)
        db.session.add(products[store])
    db.session.commit()
    return products


def stock_of(target, bestbuy, walmart):
    """{url: (available, price)} for the three listings."""
    return {URLS['target']: target, URLS['bestbuy']: bestbuy, URLS['walmart']: walmart}


@contextmanager
def patched(module, **attrs):
    """Swap a module's attributes for the length of the block, then put them back."""
    originals = {name: getattr(module, name) for name in attrs}
    for name, value in attrs.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)


def faked(stock):
    """get_scraper and send_product_alert stand-ins: scrapes answer from `stock`, alerts go nowhere."""
    return {'get_scraper': lambda store_type: FakeScraper(stock),
            'send_product_alert': lambda *args, **kwargs: None}


def run_cycle(stock):
    """One check_all_products pass with fake scrapers and alerts switched off."""
    with patched(tasks, **faked(stock)):
        tasks.reset_store_backoff()
        tasks.check_all_products()


def history_of(product):
    """A listing's AvailabilityHistory rows, oldest first."""
    return (AvailabilityHistory.query.filter_by(product_id=product.id)
            .order_by(AvailabilityHistory.timestamp, AvailabilityHistory.id).all())


def report(ok, description, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {description}{': ' + detail if detail else ''}")
    return 0 if ok else 1


def check_history():
    """A listing's first check logs its state; after that only changes are logged."""
    print("Stock history")
    failures = 0
    stock = stock_of((False, 499.99), (True, 489.99), (False, 479.99))
    products = seed(stock)
    target, bestbuy = products['target'], products['bestbuy']

    run_cycle(stock)
    counts = {store: len(history_of(p)) for store, p in products.items()}
    failures += report(set(counts.values()) == {1}, 'the first check logs every listing once', str(counts))
    first = history_of(bestbuy)[0]
    failures += report(first.available is True and first.price == 489.99,
                       "that row is the listing's starting state and price",
                       f"available={first.available} price={first.price}")

    run_cycle(stock)
    counts = {store: len(history_of(p)) for store, p in products.items()}
    failures += report(set(counts.values()) == {1}, 'an unchanged listing logs nothing more', str(counts))

    stock[URLS['target']] = (True, 499.99)
    run_cycle(stock)
    rows = history_of(target)
    failures += report(len(rows) == 2 and rows[-1].available is True, 'a restock is logged',
                       f"{[r.available for r in rows]}")

    stock[URLS['target']] = (False, 499.99)
    run_cycle(stock)
    rows = history_of(target)
    failures += report(len(rows) == 3 and rows[-1].available is False, 'selling out is logged',
                       f"{[r.available for r in rows]}")

    # A change that reached product.available without being logged (the state
    # a database written before every check path logged can be in) is logged
    # by the next check, which compares against the last logged row.
    bestbuy.available = False
    db.session.commit()
    stock[URLS['bestbuy']] = (False, 489.99)
    run_cycle(stock)
    rows = history_of(bestbuy)
    failures += report(len(rows) == 2 and rows[-1].available is False,
                       'a change Update Now saw first is logged on the next check',
                       f"{[r.available for r in rows]}")
    return failures


def check_one_writer(app):
    """Every other way a listing gets checked logs its stock the same way the scheduler does."""
    print("One stock history writer")
    failures = 0
    failures += report('stock_checks' not in inspect(db.engine).get_table_names(),
                       'a new database has no stock_checks table')
    stock = stock_of((False, 499.99), (True, 489.99), (False, 479.99))
    products = seed(stock)
    target, bestbuy = products['target'], products['bestbuy']

    # refresh_product is the API's and the MCP server's check, and the API's add.
    with patched(tasks, **faked(stock)):
        tasks.refresh_product(target)
        first = history_of(target)
        tasks.refresh_product(target)
        unchanged = len(history_of(target))
        stock[URLS['target']] = (True, 459.99)
        tasks.refresh_product(target)
    rows = history_of(target)
    failures += report(len(first) == 1 and first[0].available is False and unchanged == 1
                       and [(r.available, r.price) for r in rows] == [(False, 499.99), (True, 459.99)]
                       and rows[-1].timestamp == target.last_checked,
                       'refresh_product logs the first state and each change, at the time of the check',
                       str([(r.available, r.price) for r in rows]))

    client = app.test_client()
    routes = sys.modules['app.routes.main']
    with patched(routes, **faked(stock)):
        client.get(f'/product/{bestbuy.id}/update')
        stock[URLS['bestbuy']] = (False, 489.99)
        client.get(f'/product/{bestbuy.id}/update')
        client.get(f'/product/{bestbuy.id}/update')
    db.session.expire_all()
    rows = history_of(bestbuy)
    failures += report([r.available for r in rows] == [True, False],
                       'Update Now logs the first state and the sell-out, once each',
                       str([r.available for r in rows]))

    url = 'https://www.walmart.com/ip/zelda-switch-2-bundle/2222222'
    stock[url] = (True, 519.99)
    with patched(routes, **faked(stock)):
        response = client.post('/product/add', data={'url': url, 'scraper_type': 'walmart'})
    db.session.expire_all()
    added = Product.query.filter_by(url=url).first()
    rows = history_of(added) if added else []
    failures += report(response.status_code == 302 and added is not None
                       and [(r.available, r.price) for r in rows] == [(True, 519.99)]
                       and rows[0].timestamp == added.last_checked,
                       'adding a listing logs its starting state once',
                       f"status {response.status_code}, {[(r.available, r.price) for r in rows]}")
    return failures


def check_membership():
    """Groups hold listings; creating, moving and deleting never loses a listing."""
    print("Group membership")
    failures = 0
    products = seed(stock_of((True, 1.0), (True, 1.0), (True, 1.0)))

    group = find_or_create_group('  Zelda   Switch 2  ')
    failures += report(group.name == 'Zelda Switch 2', 'group names are trimmed', repr(group.name))
    for product in products.values():
        assign_group(product, group)
    db.session.commit()
    failures += report(find_or_create_group('ZELDA switch 2') is group,
                       'a name matches its group whatever the case')
    failures += report(ProductGroupMember.query.count() == 3, 'three listings, three memberships')

    walmart = products['walmart']
    other = find_or_create_group('Pro Controller')
    assign_group(walmart, other)
    db.session.commit()
    failures += report(walmart.group_membership.group is other and ProductGroupMember.query.count() == 3,
                       'moving a listing keeps it in exactly one group')

    assign_group(walmart, None)
    db.session.commit()
    failures += report(walmart.group_membership is None and ProductGroupMember.query.count() == 2
                       and db.session.get(Product, walmart.id) is not None,
                       'ungrouping removes the membership and keeps the listing')

    target = products['target']
    target_id = target.id
    db.session.add(AvailabilityHistory(product_id=target_id, available=True, timestamp=STAMP))
    db.session.commit()
    db.session.delete(target)
    db.session.commit()
    failures += report(ProductGroupMember.query.filter_by(product_id=target_id).count() == 0
                       and AvailabilityHistory.query.filter_by(product_id=target_id).count() == 0,
                       "deleting a listing removes its membership and its history")

    db.session.delete(group)
    db.session.commit()
    failures += report(Product.query.count() == 2 and ProductGroupMember.query.count() == 0,
                       'deleting a group keeps its listings',
                       f"products={Product.query.count()} members={ProductGroupMember.query.count()}")
    return failures


def check_rollup():
    """The grouped row: in stock at N of M, and a best price you can actually buy at."""
    print("Grouped roll-up")
    failures = 0
    products = seed(stock_of((False, 449.99), (True, 499.99), (True, 489.99)))
    target, bestbuy, walmart = products['target'], products['bestbuy'], products['walmart']

    summary = summarize([target, bestbuy, walmart])
    failures += report(summary['total'] == 3 and summary['in_stock'] == 2, 'in stock at 2 of 3',
                       f"{summary['in_stock']} of {summary['total']}")
    failures += report(summary['best'] is walmart and summary['best_is_in_stock'],
                       'the best price is the cheapest listing that is in stock, not the cheapest overall',
                       summary['best'].url if summary['best'] else 'none')
    failures += report(summary['best_store'] == 'Walmart', 'the best price names its retailer',
                       str(summary['best_store']))

    for product in (bestbuy, walmart):
        product.available = False
    summary = summarize([target, bestbuy, walmart])
    failures += report(summary['in_stock'] == 0 and summary['best'] is target
                       and not summary['best_is_in_stock'],
                       'with nothing in stock it falls back to the cheapest listing, marked out of stock')

    empty = summarize([])
    failures += report(empty['total'] == 0 and empty['best'] is None, 'an empty group rolls up to nothing')

    bestbuy.available = True
    console = find_or_create_group('Zelda console')
    case = find_or_create_group('A case')
    assign_group(bestbuy, console)
    assign_group(walmart, console)
    assign_group(target, case)
    db.session.commit()
    groups, ungrouped = grouped_view()
    names = [row['group'].name for row in groups]
    failures += report(names == ['Zelda console', 'A case'], 'groups with stock somewhere come first', str(names))
    failures += report(ungrouped == [], 'every listing is in a group, so none is ungrouped')

    assign_group(target, None)
    db.session.commit()
    groups, ungrouped = grouped_view()
    failures += report([row['product'] for row in ungrouped] == [target],
                       'an ungrouped listing is listed on its own')
    return failures


def check_routes(app):
    """The group routes change membership, and every page renders."""
    print("Routes and pages")
    failures = 0
    products = seed(stock_of((False, 449.99), (True, 499.99), (True, 489.99)))
    bestbuy_id, walmart_id, target_id = (products[s].id for s in ('bestbuy', 'walmart', 'target'))
    client = app.test_client()

    response = client.post(f'/product/{bestbuy_id}/group', data={'group_name': 'Zelda Switch 2'})
    db.session.expire_all()
    group = ProductGroup.query.first()
    failures += report(response.status_code == 302 and group is not None and group.name == 'Zelda Switch 2'
                       and [m.product_id for m in group.members] == [bestbuy_id],
                       'posting a new name creates the group with the listing in it')

    client.post(f'/product/{walmart_id}/group', data={'group_name': 'zelda switch 2'})
    db.session.expire_all()
    failures += report(ProductGroup.query.count() == 1 and len(group.members) == 2,
                       'posting an existing name joins that group')

    response = client.get('/?view=grouped')
    html = response.get_data(as_text=True)
    failures += report(response.status_code == 200 and GROUPED_TABLE in html
                       and 'Zelda Switch 2' in html and 'In stock at 2 of 2' in html,
                       'the grouped dashboard renders the group', f"status {response.status_code}")
    failures += report('Not in a group' in html and 'Target' in html,
                       'the ungrouped listing is shown under its own heading')

    html = client.get('/').get_data(as_text=True)
    failures += report(GROUPED_TABLE in html, 'the view choice sticks for the session')

    response = client.get('/?view=all')
    html = response.get_data(as_text=True)
    failures += report(response.status_code == 200 and GROUPED_TABLE not in html
                       and f'/product/{target_id}/toggle/alerts' in html,
                       'All sites still renders the flat table')

    response = client.get('/groups/partial')
    failures += report(response.status_code == 200 and 'id="grouped-view"' in response.get_data(as_text=True),
                       'the refresh fragment renders')

    response = client.get(f'/group/{group.id}')
    html = response.get_data(as_text=True)
    failures += report(response.status_code == 200 and 'Best Buy' in html and 'Walmart' in html
                       and 'Stock history' in html, 'the group page renders its listings',
                       f"status {response.status_code}")
    failures += report(client.get('/group/9999').status_code == 404, 'an unknown group is a 404')

    other = ProductGroup(name='Pro Controller')
    db.session.add(other)
    db.session.commit()
    client.post(f'/group/{other.id}/rename', data={'name': 'ZELDA switch 2'})
    db.session.expire_all()
    failures += report(other.name == 'Pro Controller', 'renaming onto an existing name is refused')
    client.post(f'/group/{group.id}/rename', data={'name': 'Zelda console'})
    db.session.expire_all()
    failures += report(group.name == 'Zelda console', 'a group can be renamed')

    client.post(f'/product/{walmart_id}/group', data={'group_name': ''})
    db.session.expire_all()
    failures += report(db.session.get(Product, walmart_id).group_membership is None,
                       'posting a blank name takes the listing out of its group')

    with app.test_request_context():
        html = render_template('products/_group_picker.html', product=db.session.get(Product, bestbuy_id))
    failures += report('value="Zelda console"' in html and '<option value="Pro Controller">' in html,
                       "the listing page's group picker shows the current group and suggests the others")

    response = client.get(f'/product/{bestbuy_id}')
    html = response.get_data(as_text=True)
    failures += report(response.status_code == 200 and f'action="/product/{bestbuy_id}/group"' in html
                       and 'value="Zelda console"' in html,
                       'the listing page includes the group picker', f"status {response.status_code}")

    group_id = group.id
    response = client.post(f'/group/{group_id}/delete')
    db.session.expire_all()
    failures += report(response.status_code == 302 and db.session.get(ProductGroup, group_id) is None
                       and Product.query.count() == 3,
                       'deleting a group from its page keeps every listing')
    return failures


SEED_URLS = {
    'console_target': 'https://www.target.com/p/nintendo-8482-switch-2-the-legend-of-zelda-40th-anniversary-edition'
                      '-console-system/-/A-1013322047',
    'console_amazon': 'https://www.amazon.com/dp/B0HJ6F8L6V',
    'console_nintendo': 'https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda'
                        '-40th-anniversary-edition-121642/',
    'controller_nintendo': 'https://www.nintendo.com/us/store/products/nintendo-switch-2-pro-controller-display'
                           '-stand-the-legend-of-zelda-40th-anniversary-edition-127076/',
    'controller_walmart': 'https://www.walmart.com/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda'
                          '-40th-Anniversary-Edition/20954470204',
    'case_bestbuy': 'https://www.bestbuy.com/product/nintendo-switch-2-carrying-case-and-screen-protector-the'
                    '-legend-of-zelda-40th-anniversary-edition-multi/J7GSL57WCW/sku/6691852',
    'test_store': 'https://test-store.example.com/product/1?scenario=success&name=Zelda+console',
    'lookalike': 'https://www.nintendo.com/us/store/products/something-else-1216420/',
}


def check_seed():
    """seed_groups.py groups the known Zelda listings by retailer item id, and only ever adds."""
    print("Seeding the Zelda groups")
    failures = 0
    db.session.remove()
    db.drop_all()
    db.create_all()
    products = {}
    for key, url in SEED_URLS.items():
        products[key] = Product(name=key, url=url, current_price=1.0, available=False, last_checked=STAMP,
                                notify_on_price_drop=False, notify_on_availability=False)
        db.session.add(products[key])
    # The console group was already made by hand, in other case, and the Walmart
    # controller was put in a group of someone's own.
    mine = ProductGroup(name='My controllers')
    db.session.add_all([ProductGroup(name='ZELDA 40th Console'), mine])
    assign_group(products['controller_walmart'], mine)
    db.session.commit()

    rows = seed_groups.seed(apply=False)
    db.session.expire_all()
    failures += report(ProductGroup.query.count() == 2 and ProductGroupMember.query.count() == 1,
                       'a dry run writes nothing')
    actions = {row.product.name: row.action for row in rows}
    expected = {'console_target': 'add', 'console_amazon': 'add', 'console_nintendo': 'add',
                'controller_nintendo': 'add', 'controller_walmart': 'kept', 'case_bestbuy': 'add',
                'test_store': 'unmatched', 'lookalike': 'unmatched'}
    failures += report(actions == expected, 'the dry run says what it would do to each listing', str(actions))

    seed_groups.seed(apply=True)
    db.session.expire_all()
    grouped = {}
    for key, product in products.items():
        membership = db.session.get(Product, product.id).group_membership
        grouped[key] = membership.group.name if membership else None
    console = 'ZELDA 40th Console'
    failures += report(grouped['console_target'] == grouped['console_amazon'] == grouped['console_nintendo'] == console
                       and ProductGroup.query.count() == 4,
                       'the console listings join the existing console group, whatever its case', str(grouped))
    failures += report(grouped['controller_nintendo'] == 'Zelda 40th Pro Controller'
                       and grouped['case_bestbuy'] == 'Zelda 40th carrying case',
                       'the controller and case listings get groups of their own')
    failures += report(grouped['controller_walmart'] == 'My controllers',
                       'a listing someone put in another group stays there')
    failures += report(grouped['test_store'] is None and grouped['lookalike'] is None,
                       'other listings, and ids that only start the same, are left alone')

    rows = seed_groups.seed(apply=True)
    db.session.expire_all()
    failures += report(not [row for row in rows if row.action == 'add'] and ProductGroup.query.count() == 4
                       and ProductGroupMember.query.count() == 6 and Product.query.count() == len(SEED_URLS),
                       'running it again changes nothing')
    return failures


def check_chart(app):
    """The detail chart reads AvailabilityHistory, closed by the latest check."""
    print("Availability chart")
    failures = 0
    products = seed(stock_of((False, 499.99), (True, 489.99), (False, 479.99)))
    target, bestbuy, walmart = products['target'], products['bestbuy'], products['walmart']
    now = STAMP
    for ago, available in ((timedelta(days=3), False), (timedelta(hours=2), True),
                           (timedelta(minutes=30), False)):
        db.session.add(AvailabilityHistory(product_id=target.id, timestamp=now - ago,
                                           available=available, price=499.99))
    target.available, target.last_checked = False, now - timedelta(minutes=1)
    # Best Buy: in stock for two days, but not checked for the last three hours.
    db.session.add(AvailabilityHistory(product_id=bestbuy.id, timestamp=now - timedelta(days=2),
                                       available=True, price=489.99))
    bestbuy.available, bestbuy.last_checked = True, now - timedelta(hours=3)
    db.session.commit()

    def shape(window):
        return [(now - p['timestamp'], p['available'], p['carry']) for p in window['points']]

    windows = {w['key']: w for w in target.availability_windows(now=now)}
    hour, day, week = windows['hour'], windows['day'], windows['week']
    failures += report(shape(hour) == [(timedelta(hours=1), True, True),
                                       (timedelta(minutes=30), False, False),
                                       (timedelta(minutes=1), False, False)]
                       and hour['changes'] == 1 and hour['checked'],
                       'the last hour starts with the state carried in and runs to the latest check',
                       str(shape(hour)))
    failures += report(shape(day) == [(timedelta(days=1), False, True),
                                      (timedelta(hours=2), True, False),
                                      (timedelta(minutes=30), False, False),
                                      (timedelta(minutes=1), False, False)]
                       and day['changes'] == 2, 'the last day shows the restock and the sell-out',
                       str(shape(day)))
    failures += report(shape(week)[0] == (timedelta(days=3), False, False)
                       and not any(p['carry'] for p in week['points']) and week['changes'] == 2,
                       'a listing first seen inside a range has nothing to carry in', str(shape(week)))

    windows = {w['key']: w for w in bestbuy.availability_windows(now=now)}
    failures += report(shape(windows['hour']) == [(timedelta(hours=1), True, True), (timedelta(0), True, True)]
                       and not windows['hour']['checked'] and windows['hour']['changes'] == 0,
                       'a range with no check holds the last known state out to now',
                       str(shape(windows['hour'])))
    failures += report(shape(windows['day']) == [(timedelta(days=1), True, True), (timedelta(hours=3), True, False)]
                       and windows['day']['checked'],
                       "a range with a check but no change is flat up to that check",
                       str(shape(windows['day'])))
    failures += report(all(not w['points'] for w in walmart.availability_windows(now=now)),
                       'a listing with nothing logged has no points, whatever last_checked says')

    from app.routes.main import _availability_chart_ranges
    with app.test_request_context():
        ranges = {r['key']: r for r in _availability_chart_ranges(target)}
        empty = _availability_chart_ranges(walmart)
    ticks_ok = True
    for key, spacing in (('hour', 600_000), ('day', 10_800_000), ('week', 86_400_000)):
        values = [tick['v'] for tick in ranges[key]['ticks']]
        ticks_ok &= (len(values) >= 5 and all(ranges[key]['start'] <= v <= ranges[key]['end'] for v in values)
                     and all(tick['label'] for tick in ranges[key]['ticks']))
        # A DST change can make one gap an hour longer or shorter.
        ticks_ok &= all(abs((b - a) - spacing) <= 3_600_000 for a, b in zip(values, values[1:]))
    failures += report(ticks_ok, 'every range has labelled ticks on round times inside it',
                       str({k: len(r['ticks']) for k, r in ranges.items()}))
    points = ranges['hour']['points']
    failures += report(points[0]['t'] == ranges['hour']['start'] and points[0]['carry']
                       and points[-1]['t'] <= ranges['hour']['end'] and ranges['hour']['last_checked'],
                       'the page gets time-axis points from each range start to the latest check')
    failures += report(empty == [], 'a listing with nothing logged gets no chart ranges')

    client = app.test_client()
    html = client.get(f'/product/{target.id}').get_data(as_text=True)
    failures += report('id="availabilityChart"' in html and '"ticks"' in html,
                       'the listing page draws the chart from the history')
    html = client.get(f'/product/{walmart.id}').get_data(as_text=True)
    failures += report('id="availabilityChart"' not in html and 'No stock history recorded yet' in html,
                       'a listing with nothing logged says so instead of drawing an empty chart')

    body = client.get(f'/api/products/{target.id}').get_json()
    history = body['product']['availability_history']
    failures += report([h['available'] for h in history] == [False, True, False]
                       and all(h['price'] == 499.99 for h in history),
                       'the API lists the logged changes oldest first, with their prices', str(history))
    return failures


def check_backfill():
    """stock_checks' changes from before the logged history are copied across, once."""
    print("Retired stock_checks")
    failures = 0
    products = seed(stock_of((False, 499.99), (True, 489.99), (False, 479.99)))
    target, bestbuy = products['target'], products['bestbuy']
    base = max(p.created_at for p in products.values())

    def at(minutes):
        return (base + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S.%f')

    db.session.execute(text('CREATE TABLE stock_checks (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, '
                            'available BOOLEAN NOT NULL, timestamp DATETIME NOT NULL)'))
    try:
        checks = [(target.id, 1, 0), (target.id, 2, 0), (target.id, 3, 1), (target.id, 4, 1),
                  (target.id, 5, 0), (target.id, 11, 0), (target.id, 12, 1),
                  (bestbuy.id, -5, 0), (bestbuy.id, 1, 1), (bestbuy.id, 2, 1),
                  (9999, 1, 1)]
        for product_id, minutes, available in checks:
            db.session.execute(text('INSERT INTO stock_checks (product_id, available, timestamp) '
                                    'VALUES (:p, :a, :t)'), {'p': product_id, 'a': available, 't': at(minutes)})
        db.session.add(PriceHistory(product_id=target.id, price=450.0, timestamp=base + timedelta(minutes=2.5)))
        # The first row the new history logged for Target; checks after it are already covered.
        db.session.add(AvailabilityHistory(product_id=target.id, timestamp=base + timedelta(minutes=10),
                                           available=False, price=499.99))
        db.session.commit()

        added = backfill_from_stock_checks()
        rows = [(round((r.timestamp - base).total_seconds() / 60), r.available, r.price) for r in history_of(target)]
        failures += report(rows == [(1, False, None), (3, True, 450.0), (5, False, 450.0), (10, False, 499.99)],
                           'the changes before the first logged row are copied, with the price then', str(rows))
        rows = [(round((r.timestamp - base).total_seconds() / 60), r.available) for r in history_of(bestbuy)]
        failures += report(rows == [(1, True)],
                           'a listing with no logged history gets its first state, not checks from before it existed',
                           str(rows))
        failures += report(added == 4 and AvailabilityHistory.query.filter_by(product_id=9999).count() == 0,
                           'checks of a deleted listing are left alone', f"added {added}")
        failures += report(backfill_from_stock_checks() == 0, 'running it again copies nothing')
    finally:
        db.session.rollback()
        db.session.execute(text('DROP TABLE IF EXISTS stock_checks'))
        db.session.commit()
    failures += report(backfill_from_stock_checks() == 0, 'a database without stock_checks is left alone')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-v', '--verbose', action='store_true', help="show the app's own log output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.CRITICAL,
                        format='%(levelname)s %(name)s %(message)s', force=True)
    if not args.verbose:
        # app.tasks logs with its own level; keep its warnings out of the check output.
        tasks.logger.propagate = False
        tasks.logger.addHandler(logging.NullHandler())

    app = build_app()
    failed = 0
    with app.app_context():
        failed += check_history()
        failed += check_one_writer(app)
        failed += check_membership()
        failed += check_rollup()
        failed += check_routes(app)
        failed += check_seed()
        failed += check_chart(app)
        failed += check_backfill()

    print()
    print('All checks passed' if not failed else f'{failed} check(s) failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
