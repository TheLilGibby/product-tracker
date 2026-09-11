"""
Amazon add-to-cart checks.

    python test_amazon_cart.py                          # offline: fixtures only, no Chrome
    python test_amazon_cart.py --login                  # ONE-TIME SETUP, see below
    python test_amazon_cart.py --import-cookies c.txt   # if --login keeps looping

The offline checks drive AmazonScraper._add_to_cart_with_driver against a fake
driver built out of HTML fixtures. Nothing here touches amazon.com; the live
paths are the two interactive helpers below, which only carry the user's own
session between their own browser profiles.

What is being checked
---------------------
Two invariants, the same two every retailer in this app now follows:

1. Never click a buy button you cannot attribute to the item being carted. An
   Amazon product page renders several add-to-cart forms - the buy box's, the
   other-sellers box's `add-to-cart-button-ubb`, and `add-to-cart-item-0/1/2`
   for the "Buy it with" bundle, whose items are different products. A saved
   page here has six `<form id="addToCart">` on it.

2. Never report a cart success on the absence of evidence. Success requires the
   product's ASIN among the ACTIVE cart's line items. The version this replaces
   accepted `len(.sc-list-item) > 0` - "the cart has something in it" - which is
   true of most people's carts before the app touches them, and is also true
   when the item Amazon actually added was somebody else's.

One-time setup (--login)
------------------------
Adding to the cart needs a signed-in session, and Amazon treats a cold profile
far more suspiciously than a warm one. `--login` opens the persistent profile
~/.chrome_profiles/amazon_profile in a VISIBLE window so you can sign in and
clear any challenge yourself; the cookies stay in that profile and later runs
reuse them. It waits indefinitely for the profile lock, so a check that is
mid-scrape is simply waited out.

The app must run as the SAME OS user, since it reads that same profile directory.

If the sign-in loops (--import-cookies)
---------------------------------------
Amazon sometimes keeps challenging a fresh profile no matter how often you clear
it. Export amazon.com cookies from the browser you are already signed into (any
Netscape cookies.txt extension) and run:

    python test_amazon_cart.py --import-cookies path/to/cookies.txt

That moves your own signed-in session between two of your own browser profiles.
It does not solve, bypass or automate any challenge.

Exit code is non-zero if any offline check fails.
"""
import argparse
import logging
import os
import sys
from contextlib import contextmanager

from bs4 import BeautifulSoup
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By
from urllib.parse import urlparse

# --login and --import-cookies must show a window whatever AMAZON_HEADLESS says;
# set it before the scraper module reads the variable at import time.
LOGIN_MODE = '--login' in sys.argv
IMPORT_MODE = '--import-cookies' in sys.argv
if LOGIN_MODE or IMPORT_MODE:
    os.environ['AMAZON_HEADLESS'] = '0'

from app.scrapers import amazon_scraper  # noqa: E402
from app.scrapers.amazon_scraper import (  # noqa: E402
    AMAZON_CART_URL, AmazonScraper, cookie_header_from_form, validate_cookie_header,
)
from app.scrapers.common import import_cookies_txt, profile_lock  # noqa: E402

# Importing app.scrapers runs the app package's DEBUG basicConfig; override it so
# selenium/urllib3 do not dump every page source to the console.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
for noisy in ('selenium', 'urllib3', 'uc', 'undetected_chromedriver'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

AMAZON_HOME = 'https://www.amazon.com/'
AMAZON_SIGNIN = 'https://www.amazon.com/ap/signin'

# The Zelda 40th console - the product this app exists for.
CONSOLE_URL = 'https://www.amazon.com/dp/B0HJ6F8L6V'
CONSOLE_ASIN = 'B0HJ6F8L6V'
# Stands in for whatever else is in the cart, or is being cross-sold beside the
# console. Only its being a different ASIN matters.
OTHER_ASIN = 'B09B8V1LZ3'

# ------------------------------------------------------------------ fixtures
#
# Trimmed from the shape of a real Amazon product page. The buy box's own form
# carries the hidden ASIN field; the bundle and other-sellers forms carry their
# own, which is the only thing that tells three "Add to Cart" controls apart.

BUY_BOX_FORM = """
<form method="post" id="addToCart" action="/gp/product/handle-buy-box">
  <input type="hidden" name="items[0.base][asin]" value="{asin}">
  <input type="hidden" id="ASIN" name="ASIN" value="{asin}">
  <div id="desktop_qualifiedBuyBox">
    <span class="a-button a-button-primary"><span class="a-button-inner">
      <input id="add-to-cart-button" class="a-button-input" name="submit.add-to-cart"
             title="Add to Shopping Cart" type="submit" value="Add to cart">
    </span></span>
    <span class="a-button a-button-primary"><span class="a-button-inner">
      <input id="buy-now-button" class="a-button-input" name="submit.buy-now" type="submit">
    </span></span>
  </div>
</form>
"""

# "Buy it with": its own form, its own ASIN, its own working add-to-cart button.
# It is rendered ABOVE the buy box on some layouts, so "the first add-to-cart
# form in the document" is document order, not the buy box.
BUNDLE_FORM = """
<form method="post" id="addToCart" action="/gp/product/handle-buy-box">
  <input type="hidden" id="ASIN" name="ASIN" value="{asin}">
  <span class="a-button a-button-primary"><span class="a-button-inner">
    <input id="add-to-cart-button" class="a-button-input" name="submit.add-to-cart"
           type="submit" value="Add to cart">
  </span></span>
</form>
"""

# A buy box with no offer of Amazon's own: Buy Now only. A clean failure, and
# the Buy Now button is read for the message and never clicked.
BUY_NOW_ONLY_FORM = """
<form method="post" id="addToCart" action="/gp/product/handle-buy-box">
  <input type="hidden" id="ASIN" name="ASIN" value="{asin}">
  <span class="a-button a-button-primary"><span class="a-button-inner">
    <input id="buy-now-button" class="a-button-input" name="submit.buy-now" type="submit">
  </span></span>
</form>
"""

# No offer at all - the "sold by a third party, see all buying options" render.
NO_OFFER_MARKUP = """
<div id="unqualifiedBuyBox_feature_div">
  <a href="/gp/offer-listing/{asin}" class="a-button-text">See All Buying Options</a>
</div>
"""

PRODUCT_PAGE = """<html><head><title>{name}</title></head><body>
<h1 id="productTitle">{name}</h1>
{body}
</body></html>"""

# The side sheet Amazon renders on the PRODUCT page after a click it accepted.
# It proves the click landed, not that the cart was written.
SIDE_SHEET = ('<div id="attach-sidesheet"><span id="attach-sidesheet-checkout-button">'
              'Proceed to checkout</span></div>')

# A cart holding one line item, in the active cart section.
CART_WITH_ITEM = """<html><head><title>Amazon.com Shopping Cart</title></head><body>
<form name="activeCartViewForm">
  <div data-name="Active Items">
    <div class="sc-list-item" data-asin="{asin}">
      <a href="/{slug}/dp/{asin}/ref=sc_1">{name}</a>
      <span class="sc-product-price">$449.99</span>
    </div>
  </div>
</form>
<input id="sc-buy-box-ptc-button" type="submit" value="Proceed to checkout">
</body></html>"""

# The same cart, plus a "Saved for later" list and a recommendation rail that
# both mention the ASIN we are looking for. Neither is an item in the cart, and
# the rail in particular is happy to show the product whose add just failed.
CART_WITH_DECOYS = """<html><head><title>Amazon.com Shopping Cart</title></head><body>
<form name="activeCartViewForm">
  <div data-name="Active Items">
    <div class="sc-list-item" data-asin="{other}">
      <a href="/some-other-product/dp/{other}/ref=sc_1">Something the user already had in the cart</a>
    </div>
  </div>
</form>
<div id="sc-saved-cart">
  <div class="sc-list-item" data-asin="{asin}"><a href="/zelda/dp/{asin}">Saved for later</a></div>
</div>
<div id="rhf" data-name="Recommended">
  <div data-asin="{asin}"><a href="/zelda/dp/{asin}">Your recently viewed items</a></div>
</div>
</body></html>"""

EMPTY_CART = """<html><head><title>Amazon.com Shopping Cart</title></head><body>
<h2>Your Amazon Cart is empty</h2>
<a href="/">Shop today's deals</a>
</body></html>"""

# A wall served on the cart. Still a wall, whatever page was asked for.
CART_ROBOT_CHECK = """<html><head><title>Robot Check</title></head><body>
<h4>Enter the characters you see below</h4>
<p>Sorry, we just need to make sure you're not a robot.</p>
<p>To discuss automated access to Amazon data please contact...</p>
</body></html>"""

# A cart page this code does not recognise: no active-cart section, no "empty"
# wording. Markup drift, a sign-in interstitial, a partial render.
CART_UNRECOGNISED = """<html><head><title>Amazon.com Shopping Cart</title></head><body>
<div id="nav-belt"></div><div id="a-page">
<p>Sign in to see your cart</p></div>
</body></html>"""

PRODUCT_ROBOT_CHECK = """<html><head><title>Robot Check</title></head><body>
<p>Sorry, we just need to make sure you're not a robot.</p>
</body></html>"""


# ------------------------------------------------------------------- fakes

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
        # Recorded by id AND by the ASIN of the form the button sits in: three
        # controls on one page read "Add to cart" and two of them share the buy
        # box's id, so the id alone does not say which product was carted.
        self._driver.clicks.append(self.get_attribute('id') or self.text)
        form = self._tag.find_parent('form')
        field = form.find('input', attrs={'name': 'ASIN'}) if form else None
        self._driver.clicked_asins.append(field.get('value') if field else None)

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException(value)
        return found[0]

    def find_elements(self, by, value):
        if by == By.XPATH and value == 'ancestor::form[1]':
            form = self._tag.find_parent('form')
            return [FakeElement(self._driver, form)] if form else []
        return _select(self._driver, self._tag, by, value)


class FakeDriver:
    """
    Enough of a WebDriver to run _add_to_cart_with_driver against fixtures.

    It serves the product page for the product URL and the cart page for the
    cart URL. A fixture with no side sheet makes the flow's 10-second wait for
    an acknowledgement expire for real, which is the one slow case here.
    """

    # Chrome rejects a cookie whose domain does not match the page it is on;
    # the scraper relies on that, trying .amazon.com first and falling through.
    accepts_cookies = True

    def __init__(self, product_page, cart_page, url):
        self.product_page = product_page
        self.cart_page = cart_page
        self.page_source = product_page
        self.current_url = url
        self.clicks = []
        self.clicked_asins = []
        self.gets = []
        self.cookies = []

    def get(self, url):
        # The flow navigates to the product URL first and the cart second, so
        # serve by URL rather than by call order.
        self.gets.append(url)
        self.current_url = url
        self.page_source = self.cart_page if 'cart' in url else self.product_page

    def add_cookie(self, cookie):
        if not self.accepts_cookies:
            raise WebDriverException('invalid cookie domain')
        host = urlparse(self.current_url).hostname or ''
        domain = (cookie.get('domain') or '').lstrip('.')
        if not (host == domain or host.endswith('.' + domain)):
            raise WebDriverException('invalid cookie domain')
        self.cookies.append((cookie['name'], cookie['value']))


    def execute_script(self, script, *args):
        return None

    def get_screenshot_as_base64(self):
        return 'ZmFrZSBzY3JlZW5zaG90'

    def find_elements(self, by, value):
        return _select(self, BeautifulSoup(self.page_source, 'html.parser'), by, value)

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException(value)
        return found[0]


class CookieRejectingDriver(FakeDriver):
    """A browser that turns down every cookie, however it is addressed."""

    accepts_cookies = False


def _select(driver, soup, by, value):
    if by in (By.CSS_SELECTOR, By.TAG_NAME):
        return [FakeElement(driver, tag) for tag in soup.select(value)]
    if by == By.ID:
        return [FakeElement(driver, tag) for tag in soup.find_all(id=value)]
    return []


# ------------------------------------------------------------------ checks

def _product(body, name='Nintendo Switch 2 - Zelda 40th Anniversary Edition', side_sheet=True):
    return PRODUCT_PAGE.format(name=name, body=body + (SIDE_SHEET if side_sheet else ''))


def _run(product_page, cart_page, url=CONSOLE_URL, driver_class=FakeDriver):
    scraper = AmazonScraper.__new__(AmazonScraper)  # skip __init__: no profile dir, no Chrome lookup
    driver = driver_class(product_page, cart_page, url)
    asin = AmazonScraper.extract_asin(url)
    return scraper._add_to_cart_with_driver(driver, url, asin, 1), driver


@contextmanager
def cookie_state(header):
    """
    Pin what load_amazon_cookies() sees for the duration of a check.

    Both of its sources are ambient: the AMAZON_COOKIES environment variable
    and ~/.chrome_profiles/amazon_cookies.txt, which is a real file holding a
    real Amazon session on any machine where the user has pasted one. Without
    this, these checks would take a different branch on that machine than on a
    clean one - and the cart checks either side of them would too, silently.
    The file is pointed at a path that does not exist rather than touched.
    """
    previous = os.environ.get('AMAZON_COOKIES')
    previous_path = amazon_scraper.amazon_cookies_file_path
    os.environ['AMAZON_COOKIES'] = header
    amazon_scraper.amazon_cookies_file_path = lambda: os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'no-such-amazon-cookies.txt')
    try:
        yield
    finally:
        amazon_scraper.amazon_cookies_file_path = previous_path
        if previous is None:
            os.environ.pop('AMAZON_COOKIES', None)
        else:
            os.environ['AMAZON_COOKIES'] = previous


def run_asin_checks():
    """extract_asin: the ASIN is in the URL by format, so this is parsing."""
    cases = [
        ('https://www.amazon.com/dp/B0HJ6F8L6V', 'B0HJ6F8L6V'),
        ('https://www.amazon.com/Nintendo-Switch-2/dp/B0HJ6F8L6V/ref=sr_1_1?keywords=zelda', 'B0HJ6F8L6V'),
        ('https://www.amazon.com/gp/product/B09B8V1LZ3', 'B09B8V1LZ3'),
        ('https://www.amazon.com/dp/1546179437', '1546179437'),          # ISBN-style ASIN
        ('https://www.amazon.com/dp/b0hj6f8l6v', 'B0HJ6F8L6V'),          # normalised
        ('https://www.amazon.com/s?k=nintendo+switch+2', None),          # a search, not a product
        ('https://www.amazon.com/stores/page/ABCDEF', None),
    ]
    failures = 0
    for url, expected in cases:
        got = AmazonScraper.extract_asin(url)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] extract_asin({url[-45:]}) -> {got}")
    return failures


def run_cart_verification_checks():
    """
    _add_to_cart_with_driver against fixtures, no Chrome.

    The rule under test: success requires the ASIN among the active cart's line
    items. A side sheet on the product page, a cart that merely fails to say it
    is empty, and a cart holding somebody else's item are all satisfied by a
    failed add.
    """
    buy_box = BUY_BOX_FORM.format(asin=CONSOLE_ASIN)
    cart_with_item = CART_WITH_ITEM.format(asin=CONSOLE_ASIN, slug='zelda-console', name='Nintendo Switch 2')
    cart_other_item = CART_WITH_ITEM.format(asin=OTHER_ASIN, slug='some-other-product',
                                            name='Something the user already had in the cart')
    cart_decoys = CART_WITH_DECOYS.format(asin=CONSOLE_ASIN, other=OTHER_ASIN)

    cases = [
        ('ASIN is a line item in the active cart',
         _product(buy_box), cart_with_item, CONSOLE_URL, True, 'Successfully added'),
        ('no side sheet, but the ASIN is in the cart',
         _product(buy_box, side_sheet=False), cart_with_item, CONSOLE_URL, True, 'Successfully added'),
        ('cart holds a different item (the old "any item" success)',
         _product(buy_box), cart_other_item, CONSOLE_URL, False, 'does not list ASIN'),
        ('saved-for-later and a recently-viewed rail are not the cart',
         _product(buy_box), cart_decoys, CONSOLE_URL, False, 'does not list ASIN'),
        ('cart is empty',
         _product(buy_box), EMPTY_CART, CONSOLE_URL, False, 'cart is empty'),
        ('wall served on the cart',
         _product(buy_box), CART_ROBOT_CHECK, CONSOLE_URL, False, 'block page on the cart'),
        ('cart page has no line items this recognises',
         _product(buy_box), CART_UNRECOGNISED, CONSOLE_URL, False, "could not be verified"),
    ]

    failures = 0
    for label, product_page, cart_page, url, expected, fragment in cases:
        result, driver = _run(product_page, cart_page, url)
        ok = (result['success'] is expected
              and fragment.lower() in result['message'].lower()
              and driver.clicks == ['add-to-cart-button']
              and result['cart_url'] == AMAZON_CART_URL
              and result['screenshot'])
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> success={result['success']} "
              f"({result['message'][:66]})")

    # Failures that never reach the cart, so nothing is clicked at all.
    unreachable = [
        ('only Buy Now: a clean failure, and Buy Now is not clicked',
         _product(BUY_NOW_ONLY_FORM.format(asin=CONSOLE_ASIN)), 'Buy Now but no Add to Cart'),
        ('no offer at all (third-party listing)',
         _product(NO_OFFER_MARKUP.format(asin=CONSOLE_ASIN)), 'No Add to Cart button'),
        ('wall served on the product page',
         PRODUCT_ROBOT_CHECK, 'block page instead of the product'),
    ]
    for label, product_page, fragment in unreachable:
        result, driver = _run(product_page, cart_with_item)
        ok = (result['success'] is False
              and fragment.lower() in result['message'].lower()
              and driver.clicks == []
              and result['cart_url'] is None)
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> clicked {driver.clicks} "
              f"({result['message'][:60]})")

    # A URL with no ASIN is refused before Chrome starts: there would be nothing
    # to look for on the cart page afterwards.
    result = AmazonScraper.__new__(AmazonScraper).add_to_cart('https://www.amazon.com/s?k=switch+2')
    ok = result['success'] is False and 'could not read an asin' in result['message'].lower()
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] a URL with no ASIN is refused before launching Chrome "
          f"-> {result['message'][:60]}")

    return failures


# A recommendation rail. Its prices are real prices of real products - just not
# of this one. A live capture of the Zelda console page carried nine of these
# with no buy box at all, and a page-wide '.a-price' scan read $169.00 off one
# of them and recorded it as the console's price.
RAIL = """
<div id="similarities_feature_div">
  <span class="a-price"><span class="a-offscreen">$169.00</span></span>
  <span class="a-price"><span class="a-offscreen">$34.50</span></span>
</div>
"""

# The buy box's own price, where Amazon actually puts it.
CORE_PRICE = """
<div id="corePrice_feature_div">
  <span class="a-price"><span class="a-offscreen">${price}</span>
    <span class="a-price-whole">{whole}<span class="a-price-fraction">{cents}</span></span>
  </span>
</div>
"""

# "Currently unavailable": no buy box, no price of its own. The rails still render.
NO_BUY_BOX = '<div id="outOfStock"><span>Currently unavailable.</span></div>'


def run_price_attribution_checks():
    """
    The price belongs to the listing whose buy box it sits in.

    Same rule as the buy button, one field over: a product page prices a dozen
    other things, so the first '.a-price' on it is the right one only by
    document-order accident. These checks live in this file rather than in
    test_amazon.py so the two do not collide; the rule is the same one.
    """
    scraper = AmazonScraper.__new__(AmazonScraper)
    failures = 0

    def check(label, markup, expected):
        nonlocal failures
        got = scraper.extract_price(BeautifulSoup(markup, 'html.parser'))
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> {got} (expected {expected})")

    core = CORE_PRICE.format(price='519.99', whole='519', cents='99')

    check("the buy box's price wins over a rail above it", RAIL + core, 519.99)
    check("...and over a rail below it", core + RAIL, 519.99)
    check("a page with rails but no buy box has no price of its own",
          NO_BUY_BOX + RAIL, None)
    check("a buy-now-only pre-order still prices from its buy box",
          '<div id="desktop_qualifiedBuyBox">' + core + '</div>' + RAIL, 519.99)
    check("thousands separators survive",
          CORE_PRICE.format(price='1,199.99', whole='1,199', cents='99'), 1199.99)
    check("a buy box with no price at all reads None, not a rail's",
          '<div id="corePrice_feature_div"></div>' + RAIL, None)

    return failures


def run_attribution_checks():
    """
    Never click a buy button you cannot attribute to the item being carted.

    Both fixtures below render two enabled controls reading "Add to cart", one
    for a different product. Only the hidden ASIN field in each button's own
    form tells them apart.
    """
    cart_with_item = CART_WITH_ITEM.format(asin=CONSOLE_ASIN, slug='zelda-console', name='Nintendo Switch 2')
    buy_box = BUY_BOX_FORM.format(asin=CONSOLE_ASIN)
    bundle = BUNDLE_FORM.format(asin=OTHER_ASIN)

    failures = 0

    # The bundle's form comes first in the document, and its button carries the
    # buy box's own id, so only the hidden ASIN separates them.
    result, driver = _run(_product(bundle + buy_box), cart_with_item)
    ok = result['success'] is True and driver.clicked_asins == [CONSOLE_ASIN]
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] a bundle's add-to-cart above the buy box is skipped "
          f"-> carted {driver.clicked_asins}")

    # Only a foreign ASIN on the page: nothing here adds this product, so
    # nothing is clicked. Clicking anyway would put someone else's item in the
    # cart and then report THIS product as missing from it.
    result, driver = _run(_product(bundle), cart_with_item)
    ok = (result['success'] is False and driver.clicks == []
          and f"adds ASIN {CONSOLE_ASIN}" in result['message'])
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] a button that adds another ASIN is never clicked "
          f"-> clicked {driver.clicks} ({result['message'][:50]})")

    # A buy box whose hidden ASIN field is missing. Still usable - that field is
    # markup Amazon could rename - but only because nothing better is there.
    anonymous = buy_box.replace(f'<input type="hidden" id="ASIN" name="ASIN" value="{CONSOLE_ASIN}">', '')
    anonymous = anonymous.replace(f'<input type="hidden" name="items[0.base][asin]" value="{CONSOLE_ASIN}">', '')
    result, driver = _run(_product(anonymous), cart_with_item)
    ok = result['success'] is True and driver.clicks == ['add-to-cart-button']
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] a button naming no ASIN is still usable "
          f"-> success={result['success']}")

    # ...but it loses to one that names the right ASIN, wherever it sits.
    result, driver = _run(_product(anonymous + buy_box), cart_with_item)
    ok = result['success'] is True and driver.clicked_asins == [CONSOLE_ASIN]
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] a button naming the right ASIN wins over one naming none "
          f"-> carted {driver.clicked_asins}")

    # The controls that start an order are never touched, on any path above.
    ok = driver.clicks == ['add-to-cart-button']
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] Buy Now and the side sheet's checkout button stay unclicked "
          f"-> clicked {driver.clicks}")

    return failures


def run_cookie_checks():
    """
    The pasted-session branch: Settings -> Amazon cookies, or AMAZON_COOKIES.

    Cookies can only be handed to a browser that is already on the domain, so
    the cart flow loads the page, applies them, and loads it AGAIN - a cookie
    added after a render does not apply to that render. That second load is the
    only thing the branch changes about the flow, so it is what these count.

    The flow also has to survive the two ways it goes wrong without a Chrome to
    ask: nothing configured (the common case, a guest cart) and configured but
    refused by the browser (an expired or malformed paste). Neither may change
    the cart result, because the cart page is what decides that.
    """
    buy_box = BUY_BOX_FORM.format(asin=CONSOLE_ASIN)
    product_page = _product(buy_box)
    cart_page = CART_WITH_ITEM.format(asin=CONSOLE_ASIN, slug='zelda-console', name='Nintendo Switch 2')

    # A header the way a browser hands it over: padded spaces, a quoted
    # session-token whose base64 padding is itself '=', and a stray segment.
    HEADER = 'session-id=141-000; at-main = Atza|xyz ; session-token="Abc=="; garbage; ubid-main=133-1'
    EXPECTED = [('session-id', '141-000'), ('at-main', 'Atza|xyz'),
                ('session-token', 'Abc=='), ('ubid-main', '133-1')]

    scraper = AmazonScraper.__new__(AmazonScraper)
    failures = 0

    parsed = scraper._parse_cookie_header(HEADER)
    ok = parsed == EXPECTED
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] header parsed into {len(parsed)} pairs "
          f"(a value may contain '=', a segment without one is dropped)")

    # Configured and accepted: every pair reaches the browser, and the product
    # page is loaded a second time so the session applies to it.
    with cookie_state(HEADER):
        result, driver = _run(product_page, cart_page)
    product_loads = [url for url in driver.gets if url == CONSOLE_URL]
    ok = (driver.cookies == EXPECTED and len(product_loads) == 2
          and driver.gets[-1] == AMAZON_CART_URL and result['success'] is True)
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] cookies applied -> {len(driver.cookies)} sent, "
          f"product page loaded {len(product_loads)}x, success={result['success']}")

    # Nothing configured: the branch is not entered at all. This is the case
    # every other check in this file runs under.
    with cookie_state(''):
        result, driver = _run(product_page, cart_page)
    product_loads = [url for url in driver.gets if url == CONSOLE_URL]
    ok = (driver.cookies == [] and len(product_loads) == 1
          and driver.clicks == ['add-to-cart-button'] and result['success'] is True)
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] no cookies configured -> none sent, "
          f"product page loaded {len(product_loads)}x, success={result['success']}")

    # Configured but the browser turns all of them down - an expired or
    # malformed paste. No reload, because there is no new session to apply; no
    # raise; and the cart still decides the outcome.
    with cookie_state(HEADER):
        result, driver = _run(product_page, cart_page, driver_class=CookieRejectingDriver)
    product_loads = [url for url in driver.gets if url == CONSOLE_URL]
    ok = (driver.cookies == [] and len(product_loads) == 1 and result['success'] is True)
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] every cookie refused -> no reload, "
          f"cart still decides (success={result['success']})")

    # validate_cookie_header guards a write into .env, where a newline in the
    # value would become a second config line.
    validate_cases = [
        ('a normal header is kept', 'session-id=141-000; at-main=Atza|abc',
         'session-id=141-000; at-main=Atza|abc'),
        ('surrounding whitespace is trimmed', '  session-id=141-000  ', 'session-id=141-000'),
        ('empty clears', '', ''),
        ('a newline is refused, not stripped', 'session-id=1\nSECRET_KEY=hijacked', ValueError),
        ('a control character is refused', 'session-id=1\x00', ValueError),
    ]
    for label, value, expected in validate_cases:
        try:
            got = validate_cookie_header(value)
            ok = got == expected
        except ValueError:
            ok = expected is ValueError
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] validate: {label}")

    # The settings form: five named fields copied out of Chrome's cookie table,
    # where a value is easily copied with its name or its quotes attached.
    form_cases = [
        ('named fields become a header, in cookie order',
         {'amazon_at_main': 'Atza|abc', 'amazon_session_id': '141-000'},
         'at-main=Atza|abc; session-id=141-000'),
        ('a pasted name= prefix and wrapping quotes are stripped',
         {'amazon_at_main': '"Atza|abc"', 'amazon_session_id': 'session-id=141-000'},
         'at-main=Atza|abc; session-id=141-000'),
        ('a full pasted header wins over the named fields',
         {'amazon_cookies': 'session-id=whole; at-main=header', 'amazon_at_main': 'ignored'},
         'session-id=whole; at-main=header'),
        ('clear beats anything else on the form',
         {'clear_amazon_cookies': '1', 'amazon_cookies': 'session-id=whole'},
         ''),
        ('an empty form is an empty header, not a stray separator',
         {}, ''),
    ]
    for label, form, expected in form_cases:
        got = cookie_header_from_form(form)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] form: {label}")

    return failures


# ------------------------------------------------------- interactive helpers

def login():
    """Open the persistent profile in a visible window for a one-time sign-in."""
    scraper = AmazonScraper()
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    # Wait indefinitely: a scheduled scrape may hold the profile, and the person
    # here needs it for as long as signing in takes.
    with scraper._browser(headless=False, timeout=None) as driver:
        driver.get(AMAZON_SIGNIN)
        print("\nSign in and clear any verification in the window, then press Enter here...")
        input()
        for url in (CONSOLE_URL, AMAZON_CART_URL):
            print(f"Visiting {url}")
            try:
                driver.get(url)
            except Exception as e:
                print(f"  (navigation error: {e})")
        print("\nDone. The profile now holds the session; later runs reuse it.")


def import_cookies(path):
    """
    Load an amazon.com cookie export from the user's ordinary browser into the
    persistent profile.

    This moves the user's own signed-in session between two of their own browser
    profiles. It does not solve, bypass or automate any challenge.
    """
    scraper = AmazonScraper()
    print(f"Importing {path} into profile: {scraper.profile_dir}")
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with scraper._browser(headless=False, timeout=None) as driver:
        # add_cookie() writes into the current document's store, so the domain
        # has to be loaded before any cookie for it will be accepted.
        print(f"Opening {AMAZON_HOME} so the cookies have a home...")
        driver.get(AMAZON_HOME)
        added, rejected = import_cookies_txt(driver, path, 'amazon.com')
        print(f"Added {added} cookies ({rejected} rejected by Chrome)")

        driver.get(AMAZON_CART_URL)
        print(f"Cart page title: {driver.title!r}")
        print("If that says 'Robot Check' or a sign-in page, the export was stale - re-export and retry.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--login', action='store_true',
                        help="open a visible Chrome on the persistent profile for a one-time sign-in")
    parser.add_argument('--import-cookies', metavar='COOKIES.TXT',
                        help="load an amazon.com cookie export into the persistent profile")
    parser.add_argument('-v', '--verbose', action='store_true', help="show scraper DEBUG logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger('app.scrapers.amazon').setLevel(logging.DEBUG)

    if args.login:
        login()
        return 0
    if args.import_cookies:
        import_cookies(args.import_cookies)
        return 0

    # Pinned for the whole offline run, not just the cookie checks. Otherwise
    # a machine where the user has pasted an Amazon session sends every cart
    # check down the apply-cookies branch, and these results would differ from
    # one box to the next for a reason nothing on screen would mention.
    with cookie_state(''):
        print("ASIN parsing:")
        failures = run_asin_checks()

        print("\nBuy button attribution:")
        failures += run_attribution_checks()

        print("\nPrice attribution:")
        failures += run_price_attribution_checks()

        print("\nCart verification:")
        failures += run_cart_verification_checks()

        print("\nPasted Amazon session:")
        failures += run_cookie_checks()

    print("")
    print(f"{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
