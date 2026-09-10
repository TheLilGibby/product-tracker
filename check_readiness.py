#!/usr/bin/env python
"""
Drop-day go/no-go check.

Prints, per store, everything that has to be true before an auto-cart run can
work, and exits non-zero if any store carrying an auto-cart product is not
ready. That makes it usable as a launch gate:

    python check_readiness.py && python run.py

By default it touches no retailer at all: every check reads the local machine
(profile directories, the profile lock, the saved cookie jar, the installed
Chrome, the config, the database). Pass --live to add exactly one cheap request
per store, to confirm the retailer is not currently walling this client.

    python check_readiness.py            # local only, no network
    python check_readiness.py --live     # + one HTTP request per store, paced

Four honest limits, all called out in the output rather than papered over:

* Backoff state (app.tasks._store_failures) lives in the *tracker's* process.
  This script is a different process, so it can only report what it can see,
  which is nothing. A store that is backing off in the running app will not
  show up here. The dashboard is the place to read that.
* The --live probes use each store's HTTP path and never open Chrome, so they
  do not take the profile lock. Taking it would make this check contend with
  the running scheduler for no reason. A store with no HTTP-only path is
  skipped rather than being allowed to launch a browser behind your back.
* It reads whichever database this checkout points at, and every git worktree
  has its own instance/product_tracker.db. Run it from the checkout the tracker
  is running from, or point it at that one:

      DATABASE_URI=sqlite:///C:/path/to/instance/product_tracker.db python check_readiness.py

  The resolved path is printed at the top and an empty database is called out,
  because a green table read off the wrong database is the one way this tool
  could actively mislead you.
* The same is true of configuration. app/config.py loads .env from the working
  directory, .env is git-ignored so every worktree has its own, and a check
  interval changed on the settings page is only ever held in the running app's
  memory. Run from the tracker's checkout, or the config lines below describe
  stock defaults rather than your tracker. Which .env was found is printed too.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

# The stores that can hold an auto-cart product. Order is the print order.
STORES = ('target', 'walmart', 'bestbuy', 'amazon')

# Stores whose scrape_product() is pure requests. Anything not named here and
# not given an explicit probe below is skipped under --live rather than risk
# opening Chrome during a readiness check.
HTTP_ONLY_SCRAPE = frozenset({'walmart', 'amazon'})

LIVE_PROBE_PACING_SECONDS = 5

OK, WARN, FAIL, NA = 'OK', 'WARN', 'FAIL', 'n/a'
# Only FAIL gates the exit code. WARN is "this will still work, but worse":
# Target with no saved session tracks through Chrome instead of two HTTP calls.
GATING = (FAIL,)


def ascii_safe(value):
    """
    Windows consoles are cp1252 and product names are full of (TM). Print
    something readable rather than raising UnicodeEncodeError mid-table.
    """
    text = '' if value is None else str(value)
    return text.encode('ascii', 'replace').decode('ascii')


class Report:
    """The checks for one store, and whether they add up to a go."""

    def __init__(self, store):
        self.store = store
        self.checks = []          # (label, status, detail)
        self.tracked = 0
        self.auto_cart = 0

    def add(self, label, status, detail=''):
        self.checks.append((label, status, ascii_safe(detail)))
        return status

    @property
    def failures(self):
        return [c for c in self.checks if c[1] in GATING]

    @property
    def status(self):
        if self.failures:
            return FAIL
        if any(c[1] == WARN for c in self.checks):
            return WARN
        return OK

    @property
    def gates(self):
        """A store only gates the exit code once something is set to auto-cart on it."""
        return self.auto_cart > 0


# --------------------------------------------------------------------- checks
def check_profile(report, scraper):
    """The Chrome profile directory, and whether anything is holding it."""
    from app.scrapers.common import profile_lock, ProfileBusyError

    profile_dir = getattr(scraper, 'profile_dir', None)
    if not profile_dir:
        report.add('chrome profile', NA, 'this scraper does not use one')
        return

    if os.path.isdir(profile_dir):
        report.add('chrome profile', OK, profile_dir)
    else:
        # Not fatal: the scraper creates it on first launch. It does mean no
        # login or cookies are stored yet, which cart usually needs.
        report.add('chrome profile', WARN, f'{profile_dir} does not exist yet')

    # timeout=0 makes this a probe, not a wait: the first failed attempt raises.
    try:
        with profile_lock(profile_dir, timeout=0):
            report.add('profile lock', OK, 'free')
    except ProfileBusyError:
        # Expected while the scheduler is mid-scrape. Only a problem if it
        # never clears, which this cannot tell from one look.
        report.add('profile lock', WARN, 'busy - another process holds it right now')
    except OSError as e:
        report.add('profile lock', WARN, f'could not test: {e}')


def cookie_jar_path(store):
    """Where this store keeps a saved session, if it keeps one at all."""
    if store == 'target':
        from app.scrapers.target_scraper import TARGET_COOKIE_JAR
        return TARGET_COOKIE_JAR
    return None


def check_cookie_jar(report, store):
    """
    The saved session, read from the file rather than through load_cookie_jar,
    because load_cookie_jar deliberately collapses every reason to None and the
    whole point here is to say *which* reason.
    """
    from app.scrapers.common import CRITICAL_COOKIE_NAMES, _as_epoch

    path = cookie_jar_path(store)
    if not path:
        report.add('saved session', NA, 'this store has no cookie jar')
        return

    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except FileNotFoundError:
        report.add('saved session', WARN,
                   f'none at {path} - run test_{store}_cart.py --import-cookies')
        return
    except (OSError, ValueError) as e:
        report.add('saved session', WARN, f'unreadable ({e}); the browser path will be used')
        return

    if payload.get('stale'):
        report.add('saved session', WARN,
                   f'marked stale - the retailer rejected it; re-import to refresh')
        return

    now = time.time()
    cookies = payload.get('cookies') or []
    # A cookie with no expiry is a session cookie and counts as live, matching
    # what load_cookie_jar keeps.
    live = [c for c in cookies
            if _as_epoch(c.get('expires')) is None or _as_epoch(c.get('expires')) > now]
    missing = [name for name in CRITICAL_COOKIE_NAMES
               if not any(c.get('name') == name for c in live)]
    if missing:
        report.add('saved session', WARN,
                   f"{len(live)}/{len(cookies)} cookies live but {', '.join(missing)} "
                   f"is gone - Redsky will 403 and Chrome will be used")
        return

    soonest = min((_as_epoch(c.get('expires')) for c in live
                   if c.get('name') in CRITICAL_COOKIE_NAMES
                   and _as_epoch(c.get('expires'))), default=None)
    if soonest is None:
        expiry = 'session cookie, no expiry'
    else:
        minutes = int((soonest - now) // 60)
        expiry = (f'expires in {minutes} min' if minutes > 0 else 'EXPIRED')
    saved_at = payload.get('saved_at')
    age = f", saved {int((now - saved_at) // 60)} min ago" if saved_at else ''
    status = WARN if soonest is not None and soonest <= now else OK
    report.add('saved session', status,
               f"{len(live)}/{len(cookies)} cookies live, "
               f"{'/'.join(CRITICAL_COOKIE_NAMES)} {expiry}{age}")


def check_cart(report, scraper):
    """Whether this store can actually be carted, and how the dispatcher calls it."""
    import inspect

    if not hasattr(scraper, 'add_to_cart'):
        # Only fatal if something is set to auto-cart here; the exit code
        # decides that, not this line.
        report.add('add_to_cart', FAIL, 'not implemented by this scraper')
        return
    try:
        takes_url = 'url' in inspect.signature(scraper.add_to_cart).parameters
    except (TypeError, ValueError):
        takes_url = False
    report.add('add_to_cart', OK,
               'takes the url directly' if takes_url
               else 'needs a pre-scrape to set current_product_url')


def check_chrome(report, chrome_major):
    """
    undetected-chromedriver downloads the newest driver unless it is pinned,
    and the newest driver refuses to start against an older Chrome. Every
    browser scraper here pins with detect_chrome_major(), so the only question
    is whether detection worked.
    """
    if chrome_major is None:
        report.add('chrome/driver pin', FAIL,
                   'Chrome version not detected - uc will fetch the newest driver and '
                   'fail to launch; set CHROME_MAJOR_VERSION')
        return
    override = os.environ.get('CHROME_MAJOR_VERSION', '')
    source = 'CHROME_MAJOR_VERSION' if override.isdigit() else 'detected'
    report.add('chrome/driver pin', OK, f'Chrome {chrome_major} ({source}); uc pinned to it')


def check_schedule(report, store):
    """Per-store interval and whatever backoff this process can see."""
    from app.tasks import store_check_interval, get_store_backoff_state

    interval = store_check_interval(store)
    # This is the interval *this* process would use. STORE_CHECK_INTERVALS comes
    # from the environment, and a change made on the settings page only mutates
    # the running app's config - it is never written anywhere this can read. So
    # the value is only the tracker's if this ran with the tracker's .env.
    report.add('check interval', OK,
               f'{interval:g} min (its own)' if interval
               else "no per-store override here; follows the global interval")

    rows = [r for r in get_store_backoff_state() if r['store_type'] == store]
    if not rows:
        # See the module docstring: this process is not the tracker's process.
        report.add('backoff', NA, 'not visible from here - state lives in the tracker process')
        return
    row = rows[0]
    if row['backed_off']:
        report.add('backoff', FAIL,
                   f"backed off for another {row['minutes_remaining']} min "
                   f"after {row['failures']} failures")
    else:
        report.add('backoff', WARN, f"{row['failures']} consecutive failures recorded")


def check_products(report, products):
    """The tracked rows for this store, and what the last cart attempt did."""
    report.tracked = len(products)
    report.auto_cart = sum(1 for p in products if p.auto_cart_enabled)

    if not products:
        report.add('tracked rows', WARN, 'nothing tracked for this store')
        return
    report.add('tracked rows', OK,
               f'{report.tracked} tracked, {report.auto_cart} with auto-cart on')

    for product in products:
        if not product.auto_cart_enabled:
            continue
        status = product.last_cart_status
        when = (product.last_cart_attempt.strftime('%Y-%m-%d %H:%M')
                if product.last_cart_attempt else 'never attempted')
        name = ascii_safe(product.name)[:52]
        report.add(f'  cart: {name}',
                   FAIL if status and 'error' in status.lower() else OK,
                   f'{status or "no attempt yet"} ({when})')


def check_live(report, store, scraper, products):
    """
    One cheap request, to confirm the retailer is answering this client.

    Uses the store's HTTP path only - never a browser, so never the profile
    lock. A store with no HTTP path is skipped, loudly.
    """
    product = next((p for p in products if p.auto_cart_enabled), None) or \
        (products[0] if products else None)
    if product is None:
        report.add('live probe', NA, 'nothing tracked to probe with')
        return

    url = product.url
    try:
        if store == 'target':
            tcin = scraper.extract_tcin(url)
            if not tcin:
                report.add('live probe', WARN, f'no TCIN in {url}')
                return
            result = scraper.scrape_via_redsky(tcin, url)
        elif store == 'bestbuy':
            result = scraper.scrape_via_requests(url)
        elif store in HTTP_ONLY_SCRAPE:
            result = scraper.scrape_product(url)
        else:
            report.add('live probe', NA,
                       'no HTTP-only path; a probe here would open Chrome')
            return
    except Exception as e:
        report.add('live probe', WARN, f'{type(e).__name__}: {e}')
        return

    if not result:
        # Every one of these paths returns None rather than raising when it is
        # blocked, so this is the wall - or, for Target, simply no saved session.
        report.add('live probe', WARN,
                   'no data - blocked, or the cheap path deferred to the browser')
        return
    report.add('live probe', OK,
               f"{ascii_safe(result.get('name'))[:40]} | {result.get('price')} | "
               f"available={result.get('available')}")


# --------------------------------------------------------------------- output
STATUS_COLUMN = {OK: '[ ok ]', WARN: '[warn]', FAIL: '[FAIL]', NA: '[ -- ]'}


def print_report(report):
    print(f"\n{report.store.upper()}")
    print('-' * 78)
    for label, status, detail in report.checks:
        print(f"  {STATUS_COLUMN[status]}  {label:<22}  {detail}")


def print_summary(reports, gated):
    print('\n' + '=' * 78)
    print(f"{'STORE':<12}{'STATUS':<10}{'TRACKED':<10}{'AUTO-CART':<12}GATES LAUNCH")
    print('-' * 78)
    for report in reports:
        print(f"{report.store:<12}{STATUS_COLUMN[report.status]:<10}"
              f"{report.tracked:<10}{report.auto_cart:<12}"
              f"{'yes' if report.gates else 'no'}")
    print('=' * 78)

    blocking = [r for r in gated if r.status == FAIL]
    if blocking:
        print('\nNO-GO. These stores carry auto-cart products and have failures:')
        for report in blocking:
            for label, _, detail in report.failures:
                print(f"  {report.store}: {label.strip()} - {detail}")
        return 1

    if not gated:
        print('\nNothing has auto-cart enabled, so there is nothing to gate.')
        print('Turn auto-cart on for the console before the drop.')
        return 0

    warned = [r for r in gated if r.status == WARN]
    print('\nGO. Every store with an auto-cart product is ready.')
    if warned:
        print('Warnings that will not stop a cart but will slow it down:')
        for report in warned:
            for label, status, detail in report.checks:
                if status == WARN:
                    print(f"  {report.store}: {label.strip()} - {detail}")
    return 0


# ----------------------------------------------------------------------- main
def quiet_logging():
    """
    The table is the output. Every scraper logs at DEBUG and undetected-
    chromedriver narrates its Chrome hunt, which buries it. Warnings still
    print, on stderr, where they can be redirected away from the table.
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger().setLevel(logging.WARNING)
    for name in ('uc', 'urllib3', 'app', 'app.scrapers', 'app.tasks', 'werkzeug'):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_app():
    """
    A real app context on the real database, without starting the scheduler.

    create_app() starts the background jobs for any config but 'testing', and a
    readiness check has no business scraping. 'testing' skips them but points at
    an in-memory database, so the real URI is put back first.
    """
    from app import create_app
    from app.config import config as app_config

    app_config['testing'].SQLALCHEMY_DATABASE_URI = \
        app_config['default'].SQLALCHEMY_DATABASE_URI
    return create_app('testing')


def config_line():
    """
    Which .env this picked up - or that it picked up none.

    app/config.py calls load_dotenv() at import, which searches from the current
    working directory. Every worktree has its own .env (it is git-ignored), so
    running this from the wrong checkout silently reads stock defaults: no
    per-store intervals, no API token, no 2Captcha key. That would make the
    config half of this table describe a tracker that does not exist.
    """
    from dotenv import find_dotenv

    path = find_dotenv(usecwd=True)
    if not path:
        return ("Config:   no .env found from here - reading stock defaults. "
                "Per-store\n          intervals and tokens set for the tracker will "
                "NOT be reflected below.")
    return f"Config:   {path}"


def database_line(app):
    """Which database this is reading, said plainly enough to catch a wrong one."""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if uri.startswith('sqlite:///'):
        path = uri[len('sqlite:///'):]
        if not os.path.isabs(path):
            path = os.path.join(app.instance_path, path)
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        return f"Database: {path}" + ('' if exists else '  (does not exist yet)') \
            + (f"  ({size // 1024} KB)" if exists else '')
    return f"Database: {uri}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--live', action='store_true',
                        help='also make one cheap HTTP request per store (never opens Chrome)')
    args = parser.parse_args(argv)

    quiet_logging()
    app = build_app()
    with app.app_context():
        from app.models.product import Product
        from app.scrapers import get_scraper, detect_store_type
        from app.scrapers.common import detect_chrome_major

        all_products = Product.query.order_by(Product.id).all()
        by_store = {store: [] for store in STORES}
        for product in all_products:
            store = detect_store_type(product.url)
            if store in by_store:
                by_store[store].append(product)

        chrome_major = detect_chrome_major()
        print(f"Readiness check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
              f"{' (with live probes)' if args.live else ' (local only)'}")
        print(database_line(app))
        print(config_line())
        if not all_products:
            # Every worktree has its own instance/product_tracker.db, so this is
            # usually the wrong database rather than an empty tracker.
            print("  *** This database holds no products at all. If the tracker is "
                  "running,\n      it is using a different one - see DATABASE_URI in "
                  "--help.")

        reports = []
        probed = 0
        for store in STORES:
            report = Report(store)
            reports.append(report)
            try:
                scraper = get_scraper(store)
            except Exception as e:
                report.add('scraper', FAIL, f'{type(e).__name__}: {e}')
                check_products(report, by_store[store])
                print_report(report)
                continue

            report.add('scraper', OK, type(scraper).__name__)
            check_profile(report, scraper)
            check_cookie_jar(report, store)
            check_cart(report, scraper)
            check_chrome(report, chrome_major)
            check_schedule(report, store)
            check_products(report, by_store[store])

            if args.live:
                if probed:
                    time.sleep(LIVE_PROBE_PACING_SECONDS)
                probed += 1
                check_live(report, store, scraper, by_store[store])

            print_report(report)

        return print_summary(reports, [r for r in reports if r.gates])


if __name__ == '__main__':
    sys.exit(main())
