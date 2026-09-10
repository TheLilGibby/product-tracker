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

Exit code is non-zero if any check fails.
"""
import argparse
import logging
import sys
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import current_app

from app.config import parse_store_intervals

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

CHECKS = [
    check_healthy_cycle,
    check_backoff_after_failures,
    check_backed_off_store_is_skipped,
    check_retry_and_doubling,
    check_delay_schedule,
    check_success_resets,
    check_exception_counts_as_failure,
    check_auto_cart_is_untouched,
    check_interval_settings_are_parsed,
    check_interval_due_boundaries,
    check_store_waits_for_its_interval,
    check_backoff_beats_interval,
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
