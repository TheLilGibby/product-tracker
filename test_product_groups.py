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
from datetime import datetime

# The app stores naive UTC via datetime.utcnow(); these checks compare against it.
warnings.filterwarnings('ignore', message='datetime.datetime.utcnow', category=DeprecationWarning)

from flask import render_template

from app import create_app, db
from app import tasks
from app.groups import assign_group, find_or_create_group, grouped_view, summarize
from app.models.product import AvailabilityHistory, Product, ProductGroup, ProductGroupMember
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


def run_cycle(stock):
    """One check_all_products pass with fake scrapers and alerts switched off."""
    originals = tasks.get_scraper, tasks.send_product_alert
    tasks.get_scraper = lambda store_type: FakeScraper(stock)
    tasks.send_product_alert = lambda *args, **kwargs: None
    try:
        tasks.reset_store_backoff()
        tasks.check_all_products()
    finally:
        tasks.get_scraper, tasks.send_product_alert = originals


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

    # "Update Now" writes product.available without logging. The next scheduled
    # check compares against the last logged row, so the change is not lost.
    bestbuy.available = False
    db.session.commit()
    stock[URLS['bestbuy']] = (False, 489.99)
    run_cycle(stock)
    rows = history_of(bestbuy)
    failures += report(len(rows) == 2 and rows[-1].available is False,
                       'a change Update Now saw first is logged on the next check',
                       f"{[r.available for r in rows]}")
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
        failed += check_membership()
        failed += check_rollup()
        failed += check_routes(app)
        failed += check_seed()

    print()
    print('All checks passed' if not failed else f'{failed} check(s) failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
