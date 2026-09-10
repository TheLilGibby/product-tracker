"""
Standalone check for the undetected-chromedriver Best Buy scraper.

Usage:
    python test_bestbuy_browser.py                 # scrape the two target products
    python test_bestbuy_browser.py --headed        # force a visible Chrome window
    python test_bestbuy_browser.py --cart          # also try add_to_cart on the first buyable product
    python test_bestbuy_browser.py <url> [<url>]   # scrape custom URLs
    python test_bestbuy_browser.py --offline       # only run the parser/lock checks (no Chrome)
    python test_bestbuy_browser.py --login         # ONE-TIME SETUP, see below

Requires Chrome installed locally. Hits bestbuy.com for everything except --offline.
Exit code is non-zero when a scrape returns None or violates the result contract.

One-time setup (--login)
------------------------
Akamai treats a signed-in profile with real cookies far more kindly than a cold
one, and the cart needs an account anyway. `--login` opens the persistent profile
~/.chrome_profiles/bestbuy_profile in a VISIBLE window so you can sign in and
clear any challenge yourself; the cookies stay in that profile and later headless
runs reuse them. It waits indefinitely for the profile lock, so a scheduled check
that is mid-scrape will simply be waited out.

The app must run as the SAME OS user, since it reads that same profile directory.
"""

import argparse
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager

from bs4 import BeautifulSoup

from app.scrapers.bestbuy_scraper import BESTBUY_CART, BestBuyScraper
from app.scrapers.common import ProfileBusyError, detect_block_page, profile_lock

# Importing app.scrapers runs the app package's DEBUG basicConfig; override it so
# selenium/urllib3 don't dump every page source to the console.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
for noisy in ('selenium', 'urllib3', 'uc', 'undetected_chromedriver'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

BESTBUY_SIGNIN = 'https://www.bestbuy.com/identity/global/signin'

TARGETS = [
    # Carrying case - listed as pre-order / coming soon (releases Oct 29 2026)
    "https://www.bestbuy.com/product/nintendo-switch-2-carrying-case-and-screen-protector-the-legend-of-zelda-40th-anniversary-edition-multi/J7GSL57WCW/sku/6691852",
    # Pro Controller - reported sold out at Best Buy
    "https://www.bestbuy.com/product/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-multi/J7GSL57W27",
]

# Minimal HTML fixtures mirroring bestbuy.com's current product-page markup
FIXTURE_TEMPLATE = """
<html><head><title>{name} - Best Buy</title>
<script type="application/ld+json">{{"@type": "Product", "name": "{name}", "sku": "{sku}",
 "image": [{{"@type": "ImageObject", "url": "https://pisces.bbystatic.com/image2/BestBuy_US/images/products/x.jpg"}}],
 "offers": [{{"@type": "Offer", "price": {price}, "availability": "https://schema.org/{ld_avail}"}}]}}</script>
</head><body><h1>{name}</h1>
<div data-testid="price-block-customer-price">${price} <span>$</span><span>{price}</span></div>
<button data-testid="pdp-{state}-{sku}" {disabled}>{label}</button>
<button data-testid="carousel-add-to-cart-1111111">Add to cart</button>
</body></html>
"""

FIXTURES = [
    # (state, label, disabled attr, JSON-LD availability, expected available)
    ('add-to-cart', 'Add to Cart', '', 'InStock', True),
    ('pre-order', 'Pre-Order', '', 'PreOrder', True),
    ('coming-soon', 'Coming Soon', 'disabled', 'InStock', False),   # JSON-LD lies; button wins
    ('sold-out', 'Sold Out', 'disabled', 'OutOfStock', False),
    ('add-to-cart', 'Add to Cart', 'disabled', 'InStock', False),   # disabled CTA => not buyable
]

CHROME_ERROR_PAGE = '<html><head><title>www.bestbuy.com</title></head><body><div id="main-frame-error"></div></body></html>'
ACCESS_DENIED_PAGE = '<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>Reference #18.abc</body></html>'


def check_contract(result):
    """Raise AssertionError if a scrape result violates the scraper contract."""
    assert isinstance(result, dict), f"expected dict, got {type(result).__name__}"
    assert set(result) == {'name', 'price', 'available', 'image_url'}, f"unexpected keys: {sorted(result)}"
    assert isinstance(result['name'], str) and result['name'], "name must be a non-empty string"
    assert result['price'] is None or isinstance(result['price'], float), "price must be float or None"
    assert isinstance(result['available'], bool), "available must be a bool"
    assert result['image_url'] is None or result['image_url'].startswith('http'), "image_url must be a URL or None"


def run_offline_checks():
    """Exercise the HTML-only extraction path and bot-wall detection without Chrome."""
    scraper = BestBuyScraper.__new__(BestBuyScraper)  # skip __init__: no profile dir, no Chrome lookup
    failures = 0

    for state, label, disabled, ld_avail, expected in FIXTURES:
        html = FIXTURE_TEMPLATE.format(name="Fixture Product", sku="6691852", price="39.99",
                                       state=state, label=label, disabled=disabled, ld_avail=ld_avail)
        soup = BeautifulSoup(html, 'html.parser')
        got = scraper.extract_availability_from_html(soup)
        price = scraper.extract_price_from_html(soup)
        name = scraper.extract_name_from_html(soup)
        image = scraper.extract_image_url_from_html(soup)
        ok = got == expected and price == 39.99 and name == "Fixture Product" and image.startswith('https://pisces')
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] pdp-{state} ({label}, disabled={bool(disabled)}, ld={ld_avail}) "
              f"-> available={got} price={price}")

    for label, html, expected in (
        ('chrome network error page', CHROME_ERROR_PAGE, True),
        ('akamai access denied', ACCESS_DENIED_PAGE, True),
        ('real product markup', FIXTURE_TEMPLATE.format(name="X", sku="6691852", price="1.00", state='sold-out',
                                                        label='Sold Out', disabled='disabled', ld_avail='OutOfStock'), False),
    ):
        got = BestBuyScraper.is_blocked_html(html, BeautifulSoup(html, 'html.parser').title.string)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] is_blocked_html: {label} -> {got}")

    for url, expected in (
        (TARGETS[0], '6691852'),
        (TARGETS[1], None),
        ("https://www.bestbuy.com/site/some-product/6613053.p?skuId=6613053", '6613053'),
    ):
        got = BestBuyScraper.extract_sku(url)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] extract_sku({url[-40:]}) -> {got}")

    return failures


def run_lock_checks():
    """
    Chrome profile serialization, without Chrome: the lock is exclusive, it is
    released afterwards, and neither entry point lets contention escape as a
    traceback. Uses a scratch directory so a running check keeps its own profile.
    """
    failures = 0
    scratch = os.path.join(tempfile.mkdtemp(prefix='bestbuy_lock_'), 'bestbuy_profile')

    with profile_lock(scratch, timeout=1):
        start = time.monotonic()
        try:
            with profile_lock(scratch, timeout=1):
                got = 'acquired while already held'
        except ProfileBusyError:
            got = 'ProfileBusyError'
        waited = time.monotonic() - start
    ok = got == 'ProfileBusyError' and 0.9 <= waited < 5
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] a second holder is refused: {got} after {waited:.1f}s")

    # If the lock were not released, one finished scrape would wedge every later run.
    try:
        with profile_lock(scratch, timeout=1):
            ok = True
    except ProfileBusyError:
        ok = False
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] the lock is released when the session ends")

    scraper = BestBuyScraper.__new__(BestBuyScraper)  # skip __init__: no profile dir, no Chrome lookup
    scraper.profile_dir = scratch
    scraper.current_product_url = TARGETS[0]
    scraper.current_sku = '6691852'
    scraper.headless = True
    scraper.scrape_headed_fallback = False
    scraper.cart_headed_fallback = True

    @contextmanager
    def busy_browser(headless=None, timeout=None):
        raise ProfileBusyError(f"another process has held the Chrome profile {scratch} for over 60s")
        yield  # unreachable; makes this a generator so @contextmanager accepts it

    scraper._browser = busy_browser
    scraper.scrape_via_requests = lambda url: None  # keep the HTTP fallback off the network

    try:
        result = scraper.scrape_product(TARGETS[0])
        ok, detail = result is None, repr(result)
    except Exception as e:
        ok, detail = False, f"raised {type(e).__name__}: {e}"
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] scrape_product on a busy profile -> {detail}")

    try:
        cart = scraper.add_to_cart(quantity=1)
        ok = (cart.get('success') is False
              and 'profile is busy' in (cart.get('message') or '').lower()
              and cart.get('screenshot') is None)
        detail = f"success={cart.get('success')} message={cart.get('message')!r}"
    except Exception as e:
        ok, detail = False, f"raised {type(e).__name__}: {e}"
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] add_to_cart on a busy profile -> {detail}")

    return failures


def login():
    """Open the persistent profile in a visible window for a one-time sign-in."""
    scraper = BestBuyScraper(headless=False)
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    # Wait indefinitely rather than the usual 60s: a scheduled check may hold the
    # profile, and the person signing in needs it for as long as that takes.
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with scraper._browser(headless=False, timeout=None) as driver:
        driver.set_page_load_timeout(60)
        driver.get(BESTBUY_SIGNIN)
        print("\nSign in and clear any verification in the window, then press Enter here...")
        input()

        # Visit a product page and the cart once, so the profile carries the cookies
        # Akamai hands a session that has actually browsed.
        for url in (TARGETS[0], BESTBUY_CART):
            print(f"Visiting {url}")
            try:
                driver.get(url)
            except Exception as e:
                print(f"  (navigation error: {e})")
            reason = detect_block_page(driver.page_source)
            if reason:
                print(f"  Best Buy is still showing a block/challenge page ({reason}) - "
                      "clear it in the window, then press Enter...")
                input()
        print("\nDone. The profile now holds the session; later headless runs reuse it.")
        print("Run `python test_bestbuy_browser.py --cart` to test add-to-cart.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('urls', nargs='*', help="product URLs (default: the two Zelda 40th Anniversary items)")
    parser.add_argument('--headed', action='store_true', help="force a visible Chrome window")
    parser.add_argument('--cart', action='store_true', help="also call add_to_cart() on the first buyable product")
    parser.add_argument('--offline', action='store_true', help="only run parser/fixture checks, no Chrome")
    parser.add_argument('--login', action='store_true',
                        help="one-time setup: sign in to the persistent profile in a visible window")
    args = parser.parse_args()

    if args.login:
        return login()

    print("Offline parser checks:")
    failures = run_offline_checks()
    print("\nChrome profile lock checks:")
    failures += run_lock_checks()
    if args.offline:
        print(f"\n{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
        return 1 if failures else 0

    urls = args.urls or TARGETS
    scraper = BestBuyScraper(headless=False if args.headed else None)
    print(f"\nChrome major={scraper.chrome_major} headless={scraper.headless} "
          f"scrape_headed_fallback={scraper.scrape_headed_fallback} cart_headed_fallback={scraper.cart_headed_fallback}")

    buyable_url = None
    for url in urls:
        print(f"\nScraping: {url}")
        result = scraper.scrape_product(url)
        if result is None:
            print("  FAIL: scrape_product returned None (bot wall or network failure)")
            failures += 1
            continue
        try:
            check_contract(result)
        except AssertionError as e:
            print(f"  FAIL: contract violation: {e}")
            failures += 1
        print(f"  name:      {result['name']}")
        print(f"  price:     {result['price']}")
        print(f"  available: {result['available']}")
        print(f"  image_url: {result['image_url']}")
        print(f"  sku:       {scraper.current_sku}")
        if result['available'] and buyable_url is None:
            buyable_url = url

    if args.cart:
        if buyable_url is None:
            print("\nadd_to_cart skipped: no buyable product among the targets")
        else:
            print(f"\nadd_to_cart on {buyable_url}")
            scraper.current_product_url = buyable_url
            cart = scraper.add_to_cart(quantity=1)
            print(f"  success:   {cart.get('success')}")
            print(f"  message:   {cart.get('message')}")
            print(f"  cart_url:  {cart.get('cart_url')}")
            print(f"  screenshot:{'captured' if cart.get('screenshot') else 'none'}")
            if not cart.get('success'):
                failures += 1

    print(f"\n{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
