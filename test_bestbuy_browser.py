"""
Standalone check for the undetected-chromedriver Best Buy scraper.

Usage:
    python test_bestbuy_browser.py                 # scrape the two target products
    python test_bestbuy_browser.py --headed        # force a visible Chrome window
    python test_bestbuy_browser.py --cart          # also try add_to_cart on the first buyable product
    python test_bestbuy_browser.py <url> [<url>]   # scrape custom URLs
    python test_bestbuy_browser.py --offline       # only run the parser/lock/cart checks (no Chrome)
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
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

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

# An empty cart, which is what add_to_cart reads after a click that did not take.
# It is deliberately tiny and mentions no product: that is the shape detect_block_page
# would otherwise call a bot wall.
EMPTY_CART_PAGE = ('<html><head><title>Cart - Best Buy</title></head><body>'
                   '<h1>Your cart is empty</h1><a href="/">Continue shopping</a></body></html>')

# A wall served on the cart. Still a wall, whatever page was asked for.
CART_ACCESS_DENIED_PAGE = ('<html><head><title>Access Denied</title></head><body>'
                           '<h1>Access Denied</h1>Reference #18.def</body></html>')

# A cart holding one line item. The SKU appears in the item link and beside it,
# which is the positive evidence add_to_cart requires before reporting success.
CART_WITH_ITEM = ('<html><head><title>Cart - Best Buy</title></head><body><h1>Your cart</h1>'
                  '<div data-testid="cart-item"><a href="/site/fixture/{sku}.p?skuId={sku}">{name}</a>'
                  '<span>SKU: {sku}</span><span>$39.99</span></div>'
                  '<button>Go to Checkout</button></body></html>')

# The product page as it looks after a click Best Buy acknowledged. The toast is
# what `confirmed` keys off; it is rendered on the product page, before the cart
# is written, so on its own it proves nothing.
PDP_AFTER_CLICK = ('<html><head><title>{name} - Best Buy</title></head><body><h1>{name}</h1>'
                   '<button data-testid="{testid}">Add to Cart</button>'
                   '{toast}</body></html>')
ADDED_TOAST = '<div role="alert">Added to cart</div>'

# Another product's markup, of the shape a page-wide SKU scan also matches. On a
# real product page this is the recommendation rail, and there are hundreds of
# them; they sit above the CTA, so a scan of the whole page reads them first.
CROSS_SELL_MARKUP = ('<div data-testid="pdp-recommendation-6543210">'
                     '<span>Customers also viewed</span></div>'
                     '<script type="application/json">{"sku": "7654321"}</script>')


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

    # The cart is not a product page. add_to_cart reads it with expect_product=False
    # so that an empty cart reports "item did not appear in the cart" instead of a
    # bot wall - while a real wall on the cart still reports as one.
    for label, html, expect_product, expected in (
        ('empty cart, read as a cart', EMPTY_CART_PAGE, False, False),
        ('empty cart, read as a product page', EMPTY_CART_PAGE, True, True),
        ('access denied on the cart', CART_ACCESS_DENIED_PAGE, False, True),
        ('chrome network error on the cart', CHROME_ERROR_PAGE, False, True),
        ('empty response, whatever was asked for', '', False, True),
    ):
        title = BeautifulSoup(html, 'html.parser').title
        got = BestBuyScraper.is_blocked_html(html, title.string if title else '',
                                             expect_product=expect_product)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] is_blocked_html(expect_product={expect_product}): "
              f"{label} -> {got}")

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


class FakeElement:
    """A BeautifulSoup tag wearing just enough of the WebElement interface."""

    def __init__(self, driver, tag):
        self._driver = driver
        self._tag = tag

    @property
    def text(self):
        return self._tag.get_text(' ', strip=True)

    def get_attribute(self, name):
        value = self._tag.get(name)
        return ' '.join(value) if isinstance(value, list) else value

    def is_enabled(self):
        return not (self._tag.has_attr('disabled') or self._tag.get('aria-disabled') == 'true')

    def is_displayed(self):
        return True

    def click(self):
        self._driver.clicks.append(self.text or self.get_attribute('data-testid'))


class FakeDriver:
    """
    Enough of a WebDriver to run _add_to_cart_with_driver against fixtures.

    It serves the product page until the flow navigates to the cart, then serves
    the cart page - the same order the real flow sees them. A fixture with no
    confirmation toast makes the flow's 15-second wait expire for real, which is
    the one slow case in this suite.
    """

    def __init__(self, product_page, cart_page, url):
        self.product_page = product_page
        self.cart_page = cart_page
        self.page_source = product_page
        self.current_url = url
        self.clicks = []

    @property
    def title(self):
        title = BeautifulSoup(self.page_source, 'html.parser').title
        return title.string if title else ''

    def get(self, url):
        self.current_url = url
        self.page_source = self.cart_page

    def execute_script(self, script, *args):
        return None

    def get_screenshot_as_base64(self):
        return 'ZmFrZSBzY3JlZW5zaG90'

    def find_elements(self, by, value):
        if by in (By.CSS_SELECTOR, By.TAG_NAME):
            soup = BeautifulSoup(self.page_source, 'html.parser')
            return [FakeElement(self, tag) for tag in soup.select(value)]
        # The only XPATHs this flow uses are text probes: the confirmation
        # toast, and the upsell sheets it tries to dismiss.
        if 'added to' in value and 'added to' in self.page_source.lower():
            tag = BeautifulSoup(ADDED_TOAST, 'html.parser').div
            return [FakeElement(self, tag)]
        return []

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException(value)
        return found[0]


def run_cart_verification_checks():
    """
    _add_to_cart_with_driver against cart fixtures, no Chrome.

    The rule under test: success requires the SKU to be present on the cart
    page. A confirmation toast on the product page, or a cart page that merely
    fails to say "your cart is empty", is not evidence - a bot wall, a sign-in
    redirect and an error page all look exactly like that.
    """
    sku = '6691852'
    pdp = PDP_AFTER_CLICK.format(name='Fixture Product',
                                 testid=f'pdp-add-to-cart-{sku}', toast=ADDED_TOAST)
    pdp_no_toast = PDP_AFTER_CLICK.format(name='Fixture Product',
                                          testid=f'pdp-add-to-cart-{sku}', toast='')
    # A CTA whose test id carries no SKU, on a page with no JSON-LD: nothing to
    # recover the SKU from, on a URL that does not carry one either.
    pdp_anonymous = PDP_AFTER_CLICK.format(name='Fixture Product',
                                           testid='pdp-add-to-cart-button', toast=ADDED_TOAST)
    cart_with_item = CART_WITH_ITEM.format(sku=sku, name='Fixture Product')
    cart_other_item = CART_WITH_ITEM.format(sku='6543210', name='Some Other Thing')

    # The CTA carries the right SKU; the markup above it carries other products'.
    # A page-wide scan returns 6543210 here, which would then be looked for on
    # the cart page and never found - a successful add reported as a failure.
    pdp_with_cross_sell = PDP_AFTER_CLICK.format(
        name='Fixture Product', testid=f'pdp-add-to-cart-{sku}',
        toast=ADDED_TOAST).replace('<h1>', CROSS_SELL_MARKUP + '<h1>')

    cases = [
        ('SKU on the cart page', pdp, cart_with_item, TARGETS[0], True, 'Successfully added'),
        ('SKU recovered from the pdp markup', pdp, cart_with_item, TARGETS[1], True, 'Successfully added'),
        ('SKU comes off the CTA, not a neighbouring product', pdp_with_cross_sell, cart_with_item,
         TARGETS[1], True, 'Successfully added'),
        ('no toast, but the SKU is on the cart page', pdp_no_toast, cart_with_item, TARGETS[0],
         True, 'Successfully added'),
        ('cart holds a different item', pdp, cart_other_item, TARGETS[0], False, 'does not list SKU'),
        ('cart is empty', pdp, EMPTY_CART_PAGE, TARGETS[0], False, 'cart is empty'),
        ('wall served on the cart', pdp, CART_ACCESS_DENIED_PAGE, TARGETS[0], False, 'block page'),
        ('no SKU anywhere to verify against', pdp_anonymous, cart_with_item, TARGETS[1],
         False, "Could not identify"),
    ]

    failures = 0
    for label, product_page, cart_page, url, expected, fragment in cases:
        scraper = BestBuyScraper.__new__(BestBuyScraper)  # skip __init__: no profile dir, no Chrome lookup
        scraper.current_product_url = url
        scraper.current_sku = BestBuyScraper.extract_sku(url)
        scraper._human_pause = lambda *a, **k: None
        driver = FakeDriver(product_page, cart_page, url)

        result = scraper._add_to_cart_with_driver(driver, 1)
        ok = (result['success'] is expected
              and fragment.lower() in result['message'].lower()
              and driver.clicks
              and result['cart_url'] == BESTBUY_CART
              and result['screenshot'])
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> success={result['success']} "
              f"({result['message'][:70]})")

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
    print("\nCart verification checks:")
    failures += run_cart_verification_checks()
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
