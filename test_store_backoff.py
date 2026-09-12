"""
Per-store backoff and per-store check interval checks for check_all_products.

    python test_store_backoff.py            # all checks
    python test_store_backoff.py -v         # with the scheduler's own logging

Everything here runs against an in-memory database and fake scrapers - no
network, no Chrome, no retailer is contacted.

Why this exists: the scheduler retried every store on every cycle, so when Best
Buy's Akamai started serving block pages it kept being hit every check interval,
which is exactly how a short rate limit turns into hours of blocking. A store
that fails repeatedly is now skipped for a while.

The second half of the file is about what the dashboard then says. A blocked
store used to keep reporting that it was answering, two ways at once: only the
scheduler recorded store health at all - the Update button and the JSON API
recorded nothing - and the scheduler recorded a success the moment a dict came
back, before the bot-wall title inside it was looked at.

Exit code is non-zero if any check fails.
"""
import argparse
import logging
import os
import sys
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import current_app

from app.config import (DEFAULT_GAMESTOP_CHECK_INTERVAL_SECONDS,
                        apply_gamestop_floor, parse_store_intervals)

# The app stores naive UTC via datetime.utcnow(); these checks compare against it.
warnings.filterwarnings('ignore', message='datetime.datetime.utcnow', category=DeprecationWarning)

from app import create_app, db
from app.models.product import Product
from app import tasks

BESTBUY_URLS = [
    'https://www.bestbuy.com/site/test-product-one/1.p?skuId=1',
    'https://www.bestbuy.com/site/test-product-two/2.p?skuId=2',
]
AMAZON_URL = 'https://www.amazon.com/dp/TESTASIN01'
GAMESTOP_URL = ('https://www.gamestop.com/video-games/nintendo-switch-2/consoles/'
                'products/test-console/123456.html')

# What a requests-based scraper hands back when Cloudflare answered instead of
# the retailer: a dict, so truthy, carrying the wall's own title and no price.
BLOCK_PAGE = {
    'name': 'Sorry, you have been blocked',
    'price': None,
    'available': False,
    'image_url': None,
}


class FakeScraper:
    """Answers scrape_product from a per-store script instead of the network."""

    def __init__(self, store_type, calls, behaviour):
        self.store_type = store_type
        self.calls = calls
        self.behaviour = behaviour

    def scrape_product(self, url):
        self.calls.append(self.store_type)
        mode = self.behaviour.get(self.store_type, 'ok')
        if mode == 'none':
            return None                      # what a block page looks like from here
        if mode == 'raise':
            raise RuntimeError('connection reset by peer')
        if mode == 'wall':
            return dict(BLOCK_PAGE)          # truthy, and not a product page
        return {
            'name': f"{self.store_type} product",
            'price': 99.99,
            'available': True,
            'image_url': 'https://example.invalid/image.jpg',
        }


class RecordingHandler(logging.Handler):
    """Keeps the records app.tasks emits so a check can count WARNINGs."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level):
        return [r.getMessage() for r in self.records if r.levelno == level]


def build_app():
    """A testing app with the background scheduler stopped."""
    app = create_app('testing')
    scheduler = getattr(app, 'scheduler', None)
    if scheduler is not None:
        # create_app starts the scheduler even under TESTING, and a fired job
        # would scrape retailers for real. Drop the jobs rather than shutting the
        # scheduler down, so create_app's atexit shutdown still has one to stop.
        scheduler.remove_all_jobs()
        scheduler.pause()
    return app


def seed_products():
    """Two Best Buy listings and one Amazon listing, all previously checked."""
    Product.query.delete()
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    for index, url in enumerate(BESTBUY_URLS):
        db.session.add(Product(name=f"Best Buy {index}", url=url, current_price=10.0,
                               available=True, last_checked=stamp))
    db.session.add(Product(name='Amazon', url=AMAZON_URL, current_price=20.0,
                           available=True, last_checked=stamp))
    db.session.commit()
    return stamp


def run_cycle(behaviour):
    """One check_all_products pass with fake scrapers. Returns the store calls."""
    calls = []
    original = tasks.get_scraper
    tasks.get_scraper = lambda store_type: FakeScraper(store_type, calls, behaviour)
    try:
        tasks.check_all_products()
    finally:
        tasks.get_scraper = original
    return calls


@contextmanager
def store_intervals(intervals):
    """Run the block with these per-store intervals in the app config."""
    config = current_app.config
    previous = config.get('STORE_CHECK_INTERVALS')
    config['STORE_CHECK_INTERVALS'] = intervals
    try:
        yield
    finally:
        config['STORE_CHECK_INTERVALS'] = previous


def report(ok, description, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {description}{': ' + detail if detail else ''}")
    return 0 if ok else 1


def check_healthy_cycle():
    """Nothing fails, so every product is scraped and no store is held back."""
    print("Healthy cycle")
    failures = 0
    tasks.reset_store_backoff()
    stamp = seed_products()

    calls = run_cycle({})

    failures += report(calls.count('bestbuy') == 2 and calls.count('amazon') == 1,
                       'every product is scraped', f"calls={calls}")
    failures += report(not tasks._store_failures and not tasks._store_retry_at,
                       'no backoff state is recorded')
    updated = Product.query.filter(Product.url == AMAZON_URL).first()
    failures += report(updated.last_checked > stamp and updated.current_price == 99.99,
                       'a scraped product is updated')
    return failures


def check_backoff_after_failures():
    """Two failed Best Buy scrapes back that store off; Amazon is unaffected."""
    print("Backoff after consecutive failures")
    failures = 0
    tasks.reset_store_backoff()
    stamp = seed_products()
    handler = RecordingHandler()
    tasks.logger.addHandler(handler)
    try:
        calls = run_cycle({'bestbuy': 'none'})
    finally:
        tasks.logger.removeHandler(handler)

    failures += report(calls.count('bestbuy') == 2,
                       'both Best Buy products are tried in the failing cycle', f"calls={calls}")
    failures += report(calls.count('amazon') == 1, 'Amazon is still scraped')
    failures += report(tasks._store_failures.get('bestbuy') == 2,
                       'two consecutive Best Buy failures are counted',
                       str(tasks._store_failures))
    failures += report('amazon' not in tasks._store_failures,
                       'the healthy store carries no failure count')

    warnings = [m for m in handler.messages(logging.WARNING) if 'bestbuy' in m]
    failures += report(len(warnings) == 1, 'exactly one WARNING announces the backoff',
                       f"{len(warnings)}: {warnings}")
    failures += report(bool(warnings) and '15 minutes' in warnings[0],
                       'the WARNING names the 15 minute default',
                       warnings[0] if warnings else 'no warning')

    retry_at = tasks._store_retry_at.get('bestbuy')
    expected = datetime.utcnow() + timedelta(minutes=15)
    failures += report(retry_at is not None and abs((retry_at - expected).total_seconds()) < 60,
                       'Best Buy is held off for about 15 minutes', str(retry_at))

    # The failed rows keep what was last known good.
    for product in Product.query.filter(Product.url.in_(BESTBUY_URLS)).all():
        failures += report(product.last_checked == stamp and product.current_price == 10.0,
                           f"product {product.id} keeps its stored data",
                           f"last_checked={product.last_checked} price={product.current_price}")
    return failures


def check_backed_off_store_is_skipped():
    """While the backoff stands, not one request goes to that store."""
    print("Backed-off store is skipped entirely")
    failures = 0
    tasks.reset_store_backoff()
    stamp = seed_products()
    run_cycle({'bestbuy': 'none'})

    # A store that is healthy again would still not be asked - that is the point.
    calls = run_cycle({})

    failures += report(calls.count('bestbuy') == 0,
                       'no Best Buy scrape is attempted', f"calls={calls}")
    failures += report(calls.count('amazon') == 1, 'Amazon keeps being checked')

    for product in Product.query.filter(Product.url.in_(BESTBUY_URLS)).all():
        failures += report(product.last_checked == stamp and product.current_price == 10.0,
                           f"skipped product {product.id} is left untouched",
                           f"last_checked={product.last_checked}")
    return failures


def check_retry_and_doubling():
    """When the wait is over the store is retried, and a further failure doubles it."""
    print("Retry after the wait, with a doubled delay")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()
    run_cycle({'bestbuy': 'none'})

    # Pretend the 15 minutes have passed.
    tasks._store_retry_at['bestbuy'] = datetime.utcnow() - timedelta(seconds=1)
    handler = RecordingHandler()
    tasks.logger.addHandler(handler)
    try:
        calls = run_cycle({'bestbuy': 'none'})
    finally:
        tasks.logger.removeHandler(handler)

    retried = [m for m in handler.messages(logging.INFO) if 'Retrying bestbuy' in m]
    failures += report(len(retried) == 1, 'exactly one INFO announces the retry',
                       f"{len(retried)}: {retried}")
    # The first retried product fails and re-arms the backoff, so the second one
    # is skipped again - a retry costs the retailer exactly one request.
    failures += report(calls.count('bestbuy') == 1, 'the retry is a single request',
                       f"calls={calls}")

    retry_at = tasks._store_retry_at.get('bestbuy')
    expected = datetime.utcnow() + timedelta(minutes=30)
    failures += report(tasks._store_failures.get('bestbuy') == 3,
                       'the failure count keeps climbing', str(tasks._store_failures))
    failures += report(retry_at is not None and abs((retry_at - expected).total_seconds()) < 60,
                       'the delay has doubled to 30 minutes', str(retry_at))
    return failures


def check_delay_schedule():
    """The delay doubles per failure but never passes STORE_BACKOFF_MAX_MINUTES."""
    print("Delay schedule and cap")
    failures = 0
    tasks.reset_store_backoff()
    now = datetime(2026, 1, 1, 12, 0, 0)
    seen = []
    for _ in range(7):
        tasks.record_store_failure('bestbuy', 'unit check', now=now)
        retry_at = tasks._store_retry_at.get('bestbuy')
        seen.append(None if retry_at is None else round((retry_at - now).total_seconds() / 60))

    failures += report(seen == [None, 15, 30, 60, 60, 60, 60],
                       'one failure is free, then 15/30/60 minutes capped at 60', str(seen))
    tasks.reset_store_backoff()
    return failures


def check_delay_cap_is_configurable():
    """STORE_BACKOFF_MAX_MINUTES moves the ceiling the doubling stops at."""
    print("The cap is configurable")
    failures = 0
    tasks.reset_store_backoff()
    now = datetime(2026, 1, 1, 12, 0, 0)
    config = current_app.config
    previous = config.get('STORE_BACKOFF_MAX_MINUTES')
    config['STORE_BACKOFF_MAX_MINUTES'] = 20
    try:
        seen = []
        for _ in range(4):
            tasks.record_store_failure('bestbuy', 'unit check', now=now)
            retry_at = tasks._store_retry_at.get('bestbuy')
            seen.append(None if retry_at is None else round((retry_at - now).total_seconds() / 60))
    finally:
        config['STORE_BACKOFF_MAX_MINUTES'] = previous

    failures += report(seen == [None, 15, 20, 20], 'the doubling stops at the configured 20 minutes',
                       str(seen))
    tasks.reset_store_backoff()
    return failures


def check_success_resets():
    """One good scrape wipes the failure history."""
    print("A successful scrape clears the backoff")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()
    run_cycle({'bestbuy': 'none'})
    tasks._store_retry_at['bestbuy'] = datetime.utcnow() - timedelta(seconds=1)

    calls = run_cycle({})

    failures += report(calls.count('bestbuy') == 2, 'both Best Buy products are scraped again',
                       f"calls={calls}")
    failures += report('bestbuy' not in tasks._store_failures,
                       'the failure count is cleared', str(tasks._store_failures))
    failures += report('bestbuy' not in tasks._store_retry_at,
                       'the retry time is cleared', str(tasks._store_retry_at))
    return failures


def check_exception_counts_as_failure():
    """A scraper that raises is as much a failure as one that returns None."""
    print("A raising scraper counts as a failure")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()
    handler = RecordingHandler()
    tasks.logger.addHandler(handler)
    try:
        run_cycle({'bestbuy': 'raise'})
    finally:
        tasks.logger.removeHandler(handler)

    failures += report(tasks._store_failures.get('bestbuy') == 2,
                       'the raised exceptions are counted', str(tasks._store_failures))
    failures += report('bestbuy' in tasks._store_retry_at, 'the store is backed off')
    warnings = [m for m in handler.messages(logging.WARNING) if 'bestbuy' in m]
    failures += report(bool(warnings) and 'RuntimeError' in warnings[0],
                       'the WARNING says what went wrong',
                       warnings[0] if warnings else 'no warning')
    return failures


def check_auto_cart_is_untouched():
    """Auto-cart is a separate, user-armed buy path and ignores the backoff."""
    print("Auto-cart still runs for a backed-off store")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()
    product = Product.query.filter(Product.url == BESTBUY_URLS[0]).first()
    product.auto_cart_enabled = True
    product.available = True
    product.notify_on_availability = True
    product.discord_webhook_url = None
    db.session.commit()

    run_cycle({'bestbuy': 'none'})
    failures += report('bestbuy' in tasks._store_retry_at, 'Best Buy is backed off first')

    from app import scrapers
    carted = []

    def fake_add_to_cart(store_type, url, quantity):
        carted.append(store_type)
        return {'success': True, 'message': 'added', 'cart_url': None, 'screenshot': None}

    original = scrapers.add_to_cart
    scrapers.add_to_cart = fake_add_to_cart
    try:
        # The scheduled job writes the Flask session through update_cart_count,
        # which needs a request; that is a separate bug, not this branch's.
        with current_app.test_request_context():
            tasks.check_auto_cart_opportunities()
    finally:
        scrapers.add_to_cart = original

    failures += report(carted == ['bestbuy'],
                       'the auto-cart attempt still happens', f"carted={carted}")
    tasks.reset_store_backoff()
    return failures


def check_interval_settings_are_parsed():
    """The env map is read in both forms, and unusable entries are dropped."""
    print("Store interval settings are parsed")
    failures = 0
    cases = [
        ('gamestop=60,bestbuy=15', {'gamestop': 60.0, 'bestbuy': 15.0}),
        (' GameStop = 60 , bestbuy=15 ', {'gamestop': 60.0, 'bestbuy': 15.0}),
        ('{"gamestop": 60, "bestbuy": 15}', {'gamestop': 60.0, 'bestbuy': 15.0}),
        ('', {}),
        ('gamestop', {}),
        ('gamestop=soon,bestbuy=15', {'bestbuy': 15.0}),
        ('gamestop=0,bestbuy=-5', {}),
        ('{oops', {}),
    ]
    for raw, expected in cases:
        got = parse_store_intervals(raw)
        failures += report(got == expected, f"{raw!r} reads as {expected}", str(got))
    return failures


def check_interval_due_boundaries():
    """When a store with an interval is due, including the grace at the edge."""
    print("Interval boundaries")
    failures = 0
    now = datetime(2026, 1, 1, 12, 0, 0)
    with store_intervals({'bestbuy': 15}):
        # 15 minutes less a 60 second grace, so anything past 14:00 is due.
        for elapsed, expected in [(timedelta(minutes=1), False),
                                  (timedelta(minutes=13, seconds=30), False),
                                  (timedelta(minutes=14, seconds=1), True),
                                  (timedelta(minutes=15), True),
                                  (timedelta(hours=2), True)]:
            got = tasks.store_check_is_due('bestbuy', now - elapsed, now=now)
            failures += report(got == expected, f"{elapsed} since the last check reads due={expected}",
                               str(got))
        failures += report(tasks.store_check_is_due('bestbuy', None, now=now),
                           'a product that was never checked is due')
        failures += report(tasks.store_check_is_due('amazon', now, now=now),
                           'a store with no interval of its own is always due')
        failures += report(tasks.store_check_interval('amazon') is None,
                           'and reports no interval of its own')
    return failures


def check_store_waits_for_its_interval():
    """A store with an hourly interval is checked once, then left alone."""
    print("A store with its own interval waits for it")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()

    with store_intervals({'bestbuy': 60}):
        first = run_cycle({})
        checked_at = {p.id: p.last_checked
                      for p in Product.query.filter(Product.url.in_(BESTBUY_URLS)).all()}
        second = run_cycle({})

        failures += report(first.count('bestbuy') == 2, 'the first cycle checks Best Buy',
                           f"calls={first}")
        failures += report(second.count('bestbuy') == 0, 'the second cycle leaves it alone',
                           f"calls={second}")
        # Amazon has no interval of its own, so an off-cycle run - the "Update
        # All Products" button calls check_all_products directly - still works.
        failures += report(second.count('amazon') == 1,
                           'a store with no interval of its own is still checked')
        failures += report(not tasks._store_failures and not tasks._store_retry_at,
                           'waiting for an interval is not a failure',
                           str(tasks._store_failures))
        for product in Product.query.filter(Product.url.in_(BESTBUY_URLS)).all():
            failures += report(product.last_checked == checked_at[product.id],
                               f"waiting product {product.id} keeps its last_checked")

        # Wind the clock back past the hour.
        for product in Product.query.filter(Product.url.in_(BESTBUY_URLS)).all():
            product.last_checked = datetime.utcnow() - timedelta(minutes=61)
        db.session.commit()

        third = run_cycle({})
        failures += report(third.count('bestbuy') == 2, 'it is checked again once the hour is up',
                           f"calls={third}")
    return failures


def check_backoff_beats_interval():
    """A backed-off store is skipped even when its interval says it is due."""
    print("Backoff still wins over the interval")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()

    with store_intervals({'bestbuy': 1}):
        run_cycle({'bestbuy': 'none'})
        failures += report('bestbuy' in tasks._store_retry_at, 'Best Buy is backed off')
        # last_checked is untouched by the failures, so the interval is long past.
        calls = run_cycle({})
        failures += report(calls.count('bestbuy') == 0,
                           'the due store is still skipped while backed off', f"calls={calls}")
    tasks.reset_store_backoff()
    return failures

# --------------------------------------------------------------------------
# What the dashboard's store status ends up saying.

def seed_gamestop():
    """One GameStop listing, previously checked and in stock."""
    Product.query.delete()
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    product = Product(name='Zelda console', url=GAMESTOP_URL, current_price=499.99,
                      available=True, last_checked=stamp)
    db.session.add(product)
    db.session.commit()
    return product, stamp


@contextmanager
def fake_scraper_on(module, behaviour):
    """Point one module's get_scraper at the scripted scraper for the block."""
    calls = []
    original = module.get_scraper
    module.get_scraper = lambda store_type: FakeScraper(store_type, calls, behaviour)
    try:
        yield calls
    finally:
        module.get_scraper = original


def check_block_page_counts_as_failure():
    """A truthy dict carrying a bot-wall title is a failure, not a success."""
    print("A block page is not an answer")
    failures = 0
    tasks.reset_store_backoff()
    stamp = seed_products()

    run_cycle({'bestbuy': 'wall'})

    failures += report(tasks._store_failures.get('bestbuy') == 2,
                       'both walled scrapes are counted as failures',
                       str(tasks._store_failures))
    failures += report('bestbuy' in tasks._store_retry_at,
                       'the walled store backs off like any other failing one')
    failures += report('amazon' not in tasks._store_failures,
                       'the store that answered is left alone')

    # The wall said available=False. Writing that would turn a blocked store
    # into an out-of-stock listing, and fire an alert when it "came back".
    for product in Product.query.filter(Product.url.in_(BESTBUY_URLS)).all():
        failures += report(product.available and product.current_price == 10.0
                           and product.last_checked == stamp,
                           f"walled product {product.id} keeps its stored data",
                           f"available={product.available} price={product.current_price}")
    tasks.reset_store_backoff()
    return failures


def check_a_real_page_is_still_an_answer():
    """Refusing block pages must not make good scrapes stop counting."""
    print("A real page is still an answer")
    failures = 0
    tasks.reset_store_backoff()
    seed_products()
    run_cycle({'bestbuy': 'wall'})
    tasks._store_retry_at['bestbuy'] = datetime.utcnow() - timedelta(seconds=1)

    run_cycle({})

    failures += report('bestbuy' not in tasks._store_failures,
                       'a readable page clears the failure count',
                       str(tasks._store_failures))
    # A page with a price but no usable name is still an answer: the name may
    # simply not have been extracted, while a wall has neither.
    failures += report(tasks.scrape_answered({'name': 'Unknown Product', 'price': 499.99}),
                       'a price with no usable name counts as an answer')
    failures += report(tasks.scrape_answered({'name': 'Zelda console', 'price': None}),
                       'a name with no price counts as one too')
    failures += report(not tasks.scrape_answered(dict(BLOCK_PAGE)),
                       'a block page does not')
    failures += report(not tasks.scrape_answered(None), 'and neither does nothing at all')
    return failures


def check_manual_update_records_health():
    """The dashboard's Update button reports into the same store status."""
    print("The Update button counts towards store health")
    failures = 0
    tasks.reset_store_backoff()
    product, stamp = seed_gamestop()
    client = current_app.test_client()

    from app.routes import main as main_routes
    with fake_scraper_on(main_routes, {'gamestop': 'wall'}) as calls:
        response = client.get(f"/product/{product.id}/update", follow_redirects=True)
    body = response.get_data(as_text=True)

    failures += report(calls == ['gamestop'], 'the scrape was attempted', f"calls={calls}")
    failures += report(tasks._store_failures.get('gamestop') == 1,
                       'the blocked manual check is counted',
                       str(tasks._store_failures))
    failures += report('block page' in body,
                       'the page says it was blocked, not that the product is gone')
    failures += report('updated successfully' not in body,
                       'and does not claim the update worked')

    refreshed = Product.query.get(product.id)
    failures += report(refreshed.available and refreshed.last_checked == stamp,
                       'the listing keeps its stored data',
                       f"available={refreshed.available} last_checked={refreshed.last_checked}")

    rows = tasks.get_store_backoff_state()
    failures += report(len(rows) == 1 and rows[0]['store_type'] == 'gamestop',
                       'the dashboard now has a row for the store', str(rows))
    failures += report(bool(rows) and rows[0]['label'] == 'GameStop',
                       "the row is labelled from the scrapers registry, not 'Gamestop'",
                       rows[0]['label'] if rows else 'no row')

    # And a manual check that works clears it again.
    with fake_scraper_on(main_routes, {}):
        client.get(f"/product/{product.id}/update", follow_redirects=True)
    failures += report(not tasks.get_store_backoff_state(),
                       'a manual check that works clears the row',
                       str(tasks.get_store_backoff_state()))
    return failures


def check_refresh_product_records_health():
    """The JSON API path, and so the MCP server, reports into it too."""
    print("refresh_product counts towards store health")
    failures = 0
    tasks.reset_store_backoff()
    product, stamp = seed_gamestop()

    with fake_scraper_on(tasks, {'gamestop': 'wall'}):
        walled = tasks.refresh_product(product)
    failures += report(not walled['success'], 'a walled refresh is not a success')
    failures += report('block page' in walled['message'],
                       'and says the store served a block page', walled['message'])
    failures += report(tasks._store_failures.get('gamestop') == 1,
                       'the failure is counted', str(tasks._store_failures))

    with fake_scraper_on(tasks, {'gamestop': 'none'}):
        empty = tasks.refresh_product(product)
    failures += report(not empty['success'], 'a refresh that returned nothing is not one either')
    failures += report(tasks._store_failures.get('gamestop') == 2,
                       'that failure is counted as well', str(tasks._store_failures))
    failures += report('gamestop' in tasks._store_retry_at,
                       'two failed manual checks back the store off')

    with fake_scraper_on(tasks, {'gamestop': 'raise'}):
        raised = tasks.refresh_product(product)
    failures += report(not raised['success'] and tasks._store_failures.get('gamestop') == 3,
                       'a refresh that raised is counted too', str(tasks._store_failures))

    refreshed = Product.query.get(product.id)
    failures += report(refreshed.available and refreshed.last_checked == stamp,
                       'none of the three touched the stored data',
                       f"available={refreshed.available} last_checked={refreshed.last_checked}")

    with fake_scraper_on(tasks, {}):
        good = tasks.refresh_product(product)
    failures += report(good['success'], 'a refresh that worked is a success')
    failures += report(not tasks.get_store_backoff_state(),
                       'and clears the store status', str(tasks.get_store_backoff_state()))
    tasks.reset_store_backoff()
    return failures


def check_store_rows_are_labelled():
    """Every store the app supports has a name for the status panel."""
    print("Store status rows are labelled")
    failures = 0
    tasks.reset_store_backoff()
    from app.scrapers import STORE_LABELS, supported_stores

    for store_type in supported_stores():
        tasks.record_store_failure(store_type, 'unit check')
    rows = {row['store_type']: row['label'] for row in tasks.get_store_backoff_state()}

    failures += report(set(rows) == set(supported_stores()),
                       'one row per supported store', str(sorted(rows)))
    failures += report(rows.get('gamestop') == 'GameStop',
                       'GameStop is spelled the way the retailer spells it',
                       rows.get('gamestop', 'missing'))
    failures += report(rows.get('nintendo') == 'My Nintendo Store',
                       'the Nintendo store is named', rows.get('nintendo', 'missing'))
    failures += report(rows.get('bh') == 'B&H Photo Video',
                       'and B&H is not read as a two-letter key', rows.get('bh', 'missing'))

    # The panel used to read its own copy of this table, which had gone stale.
    missing = sorted(set(supported_stores()) - set(STORE_LABELS))
    failures += report(not missing, 'no store falls back to its bare key', str(missing))
    tasks.reset_store_backoff()
    return failures


def check_gamestop_floor_is_applied():
    """
    GameStop gets a cadence floor even when nothing is configured.

    This is the one store where a check is not free: Cloudflare blocks headless
    Chrome on gamestop.com, so every scrape opens a real window on the user's
    desktop. On the scheduler's own cadence - 19 seconds on the live instance -
    that is a window taking the foreground every few seconds, all day. The
    mechanism to stop it already existed (STORE_CHECK_INTERVALS) and was simply
    never set, so the floor is folded into the same map rather than added as a
    second thing that can disagree with it.
    """
    print("GameStop has a default cadence floor")
    failures = 0

    # Unset: the floor applies, in minutes, because that is the map's unit.
    got = apply_gamestop_floor({}, DEFAULT_GAMESTOP_CHECK_INTERVAL_SECONDS)
    failures += report(got == {'gamestop': 5.0},
                       'unset means 300 seconds, which the map holds as 5 minutes', str(got))

    got = apply_gamestop_floor({'bestbuy': 15.0}, 300)
    failures += report(got == {'bestbuy': 15.0, 'gamestop': 5.0},
                       'and it does not disturb another store already listed', str(got))

    # An explicit entry wins: somebody who names a number has said what they want.
    got = apply_gamestop_floor({'gamestop': 60.0}, 300)
    failures += report(got == {'gamestop': 60.0},
                       'an explicit gamestop= entry beats the floor', str(got))
    got = apply_gamestop_floor({'gamestop': 0.5}, 300)
    failures += report(got == {'gamestop': 0.5},
                       'including one that asks for a shorter interval than the floor', str(got))

    # 0 is the off switch. It has to be, because parse_store_intervals drops a
    # gamestop=0 entry rather than keeping it, so there is no way to say "no
    # floor" through the map itself.
    failures += report(apply_gamestop_floor({}, 0) == {},
                       'zero seconds removes the floor entirely')
    failures += report(parse_store_intervals('gamestop=0') == {},
                       'which matters, because the map itself drops a 0 entry')

    # Garbage in the environment must not take the scheduler down.
    got = apply_gamestop_floor({}, 'soon')
    failures += report(got == {'gamestop': 5.0},
                       'an unreadable value falls back to the default', str(got))
    got = apply_gamestop_floor({}, None)
    failures += report(got == {'gamestop': 5.0}, 'and so does a missing one', str(got))
    return failures


def check_gamestop_rows_wait_for_the_floor():
    """The floor, as the check loop sees it: a row checked recently is left alone."""
    print("A GameStop row inside the floor is skipped")
    failures = 0
    now = datetime(2026, 1, 1, 12, 0, 0)

    with store_intervals(apply_gamestop_floor({}, 300)):
        failures += report(
            not tasks.store_check_is_due('gamestop', now - timedelta(seconds=10), now=now),
            'checked 10 seconds ago: not due, so no window opens')
        failures += report(
            tasks.store_check_is_due('gamestop', now - timedelta(seconds=400), now=now),
            'checked 400 seconds ago: due')
        failures += report(
            tasks.store_check_is_due('amazon', now - timedelta(seconds=10), now=now),
            'a non-GameStop row 10 seconds old is unaffected')
        failures += report(
            tasks.store_check_is_due('gamestop', None, now=now),
            'a GameStop row that was never checked is due')
        failures += report(tasks.store_check_interval('gamestop') == 5.0,
                           'and the interval reports itself in minutes',
                           str(tasks.store_check_interval('gamestop')))
    return failures


def check_gamestop_window_is_offscreen():
    """
    The window placement switch, without launching anything.

    Reading the flag and building the options is all that can be checked
    offline; whether Windows honours the position is a question only a real
    launch answers, and it is recorded on the PR.
    """
    print("The GameStop window opens off-screen by default")
    failures = 0
    from app.scrapers import gamestop_scraper
    from app.scrapers.gamestop_scraper import OFFSCREEN_POSITION, window_offscreen

    previous = os.environ.get('GAMESTOP_WINDOW_OFFSCREEN')
    try:
        os.environ.pop('GAMESTOP_WINDOW_OFFSCREEN', None)
        failures += report(window_offscreen(), 'unset means off-screen')
        for value, expected in [('1', True), ('0', False), ('false', False),
                                ('off', False), ('yes', True), ('', True)]:
            os.environ['GAMESTOP_WINDOW_OFFSCREEN'] = value
            failures += report(window_offscreen() is expected,
                               f'GAMESTOP_WINDOW_OFFSCREEN={value!r} reads as {expected}')

        os.environ['GAMESTOP_WINDOW_OFFSCREEN'] = '1'
        scraper = gamestop_scraper.GameStopScraper()
        arguments = scraper._get_chrome_options().arguments
        failures += report(f'--window-position={OFFSCREEN_POSITION}' in arguments,
                           'the position is on the command line', str(arguments))
        # The pair matters: an off-screen window with no size can come up 0x0,
        # and a viewport that size is its own bot signal.
        failures += report(any(a.startswith('--window-size=') for a in arguments),
                           'and an explicit size travels with it', str(arguments))
        failures += report('--headless=new' not in arguments,
                           'off-screen is not headless - Cloudflare blocks headless here')

        os.environ['GAMESTOP_WINDOW_OFFSCREEN'] = '0'
        arguments = gamestop_scraper.GameStopScraper()._get_chrome_options().arguments
        failures += report(not any(a.startswith('--window-position=') for a in arguments),
                           'and 0 gives back a window a human can watch', str(arguments))
    finally:
        if previous is None:
            os.environ.pop('GAMESTOP_WINDOW_OFFSCREEN', None)
        else:
            os.environ['GAMESTOP_WINDOW_OFFSCREEN'] = previous
    return failures


CHECKS = [
    check_healthy_cycle,
    check_backoff_after_failures,
    check_backed_off_store_is_skipped,
    check_retry_and_doubling,
    check_delay_schedule,
    check_delay_cap_is_configurable,
    check_success_resets,
    check_exception_counts_as_failure,
    check_auto_cart_is_untouched,
    check_interval_settings_are_parsed,
    check_interval_due_boundaries,
    check_store_waits_for_its_interval,
    check_gamestop_floor_is_applied,
    check_gamestop_rows_wait_for_the_floor,
    check_gamestop_window_is_offscreen,
    check_backoff_beats_interval,
    check_block_page_counts_as_failure,
    check_a_real_page_is_still_an_answer,
    check_manual_update_records_health,
    check_refresh_product_records_health,
    check_store_rows_are_labelled,
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="show the scheduler's own log output")
    args = parser.parse_args()

    app = build_app()
    # After create_app, which installs handlers of its own.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.CRITICAL,
                        format='%(levelname)s %(name)s %(message)s', force=True)
    # The recording handler reads records off app.tasks directly, so that logger
    # must keep emitting them even when nothing is printing them.
    tasks.logger.setLevel(logging.DEBUG)
    tasks.logger.propagate = args.verbose
    if not args.verbose:
        # Without a handler of its own, logging's last-resort handler would print
        # every WARNING here bare to stderr and bury the check output.
        tasks.logger.addHandler(logging.NullHandler())

    failed = 0
    with app.app_context():
        for check in CHECKS:
            failed += check()
            print()

    print(f"{'FAILED' if failed else 'PASSED'}: {failed} failed check(s)")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
