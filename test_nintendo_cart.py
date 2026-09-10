"""
Checks for NintendoScraper.add_to_cart (cart only - never checkout, never sign-in).

    python test_nintendo_cart.py                  # fake-driver checks + live pre-check
    python test_nintendo_cart.py --offline        # fake-driver checks only, no network
    NINTENDO_HEADLESS=0 python test_nintendo_cart.py --live-click <url>
    python test_nintendo_cart.py --login          # one-time sign-in, see below
    python test_nintendo_cart.py --import-cookies cookies.txt

Why most of this runs against a fake driver
-------------------------------------------
Exercising the click path for real means putting a real item in a real cart, so
the click path is checked against saved HTML instead: FakeDriver answers
find_elements() out of BeautifulSoup, and the selectors in nintendo_scraper are
plain CSS that soupsieve and Chrome both understand, including :has(). The
fixtures reproduce the two things that make this page dangerous - the buy box
carries no id or testid, and the page holds a carousel of other products that DO
have working add-to-cart buttons.

What runs live is the HTTP pre-check only: _fetch_order_state() reads
isSalableQty for the tracked products and for one product that is genuinely
orderable (the Sports Resort bundle, 129088), and nothing is clicked on any of
them. That is the path almost every real add_to_cart will take, since the Zelda
items are closed pre-orders.

--live-click is opt-in and takes an explicit URL. It really does add to a cart.

One-time setup (--login)
------------------------
The store lets a signed-out visitor build a guest cart, so carting normally needs
no account. If a product asks for one, or you would rather cart into your own
account, --login opens the persistent profile in a visible window and lets you
sign in yourself; the session stays in ~/.chrome_profiles/nintendo_profile and
later headless runs reuse it. The scraper itself never signs in.

--import-cookies carries a session over from your ordinary browser instead, for
the same reason it exists for Target. Treat the file as a password and delete it
afterwards.
"""
import base64
import os
import sys

# Tracked products (closed pre-orders as of 2026-09-10) and one that is orderable
CONSOLE_URL = ("https://www.nintendo.com/us/store/products/"
               "nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/")
CONTROLLER_URL = ("https://www.nintendo.com/us/store/products/"
                  "nintendo-switch-2-pro-controller-display-stand-the-legend-of-zelda-40th-"
                  "anniversary-edition-127076/")
CASE_URL = ("https://www.nintendo.com/us/store/products/"
            "nintendo-switch-2-carrying-case-screen-protector-the-legend-of-zelda-40th-"
            "anniversary-edition-127073/")
# Pre-check path ONLY. This one can actually be ordered, so it is never clicked.
SALABLE_URL = ("https://www.nintendo.com/us/store/products/"
               "nintendo-switch-2-nintendo-switch-sports-resort-bundle-129088/")
HOME_URL = "https://www.nintendo.com/us/"
ACCOUNT_URL = "https://accounts.nintendo.com/"

LOGIN_MODE = '--login' in sys.argv
IMPORT_MODE = '--import-cookies' in sys.argv
OFFLINE = '--offline' in sys.argv
if LOGIN_MODE or IMPORT_MODE:
    os.environ['NINTENDO_HEADLESS'] = '0'

from bs4 import BeautifulSoup                                          # noqa: E402
from app.scrapers import nintendo_scraper as ns                        # noqa: E402
from app.scrapers.nintendo_scraper import NintendoScraper              # noqa: E402
from app.scrapers.common import profile_lock, import_cookies_txt       # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

passed = failed = 0


def check(label, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ''))


# --------------------------------------------------------------------- fakes
class FakeElement:
    """Enough of a Selenium WebElement for the buy-box helpers, backed by a tag"""

    def __init__(self, tag, driver):
        self.tag = tag
        self.driver = driver

    @property
    def text(self):
        return self.tag.get_text(' ', strip=True)

    def get_attribute(self, name):
        if name not in self.tag.attrs:
            return None
        value = self.tag.attrs[name]
        if isinstance(value, list):          # class, rel, ...
            return ' '.join(value)
        return value if value != '' else 'true'   # bare `disabled` reads as "true"

    def is_enabled(self):
        return 'disabled' not in self.tag.attrs

    def click(self):
        self.driver.clicks.append(self.text)
        after = self.driver.on_click.pop(self.text, None)
        if after is not None:
            self.driver.load(after)


class FakeDriver:
    """
    A driver that answers out of BeautifulSoup instead of Chrome.

    The scraper's selectors are ordinary CSS - including :has(), which soupsieve
    supports - so the same strings that run against the real page run here.
    """

    def __init__(self, html, url='https://www.nintendo.com/us/store/products/x-1/', pages=None):
        self.pages = pages or {}
        self.on_click = {}
        self.clicks = []
        self.scripts = []
        self.load(html, url)

    def load(self, html, url=None):
        self.page_source = html
        self.soup = BeautifulSoup(html, 'html.parser')
        if url:
            self.current_url = url

    def get(self, url):
        self.current_url = url
        if url in self.pages:
            self.load(self.pages[url])

    def find_elements(self, by, selector):
        if by == 'tag name':
            return [FakeElement(tag, self) for tag in self.soup.find_all(selector)]
        return [FakeElement(tag, self) for tag in self.soup.select(selector)]

    def find_element(self, by, selector):
        elements = self.find_elements(by, selector)
        if not elements:
            raise Exception(f"no element for {selector!r}")
        return elements[0]

    def execute_script(self, script, *args):
        self.scripts.append(script)

    def set_page_load_timeout(self, seconds):
        pass

    def get_screenshot_as_base64(self):
        return base64.b64encode(b'png').decode()

    def quit(self):
        pass


# ------------------------------------------------------------------ fixtures
# Cut down from the live DOM on 2026-09-10. The class names are the real hashed
# styled-components ones, to make the point that they are not selectable.
def product_page(sku, cta='Add to cart', cta_attrs='', anchor_sku=None, extras=''):
    return f"""
<html><body>
  <header><a href="https://accounts.nintendo.com/">Log in / Sign up</a></header>
  <main>
    <h1>The product this page is about</h1>
    <section class="sc-buybox">
      <div class="price-spider" data-ps-sku="{anchor_sku or sku}"></div>
      <div class="sc-qty">
        <button aria-label="Subtract item" disabled>-</button>
        <div aria-live="polite">1</div>
        <button aria-label="Add item">+</button>
      </div>
      <div class="sc-a52be28-6 bNZluS">
        <button class="MFcmt sc-a52be28-7 bNZluS G0A6l sQXAt" {cta_attrs}>{cta}</button>
      </div>
      <div data-drawer-id="add-to-cart-drawer"></div>
      {extras}
    </section>
    <section class="cross-sell">
      <h2>You may also like</h2>
      <a href="/us/store/products/nintendo-switch-2-dock-set-123791/">Dock Set</a>
      <div class="sc-card"><button class="MFcmt sc-a52be28-7">Add to cart</button></div>
      <div class="sc-card"><button class="MFcmt sc-a52be28-7">Add to cart</button></div>
    </section>
  </main>
</body></html>"""


# The price-spider controls. They live in the same buy box and send the shopper
# to another retailer; if they ever land inside the CTA wrapper they must be
# refused rather than clicked.
RETAILER_ONLY_PAGE = """
<html><body>
  <section class="sc-buybox">
    <div class="price-spider" data-ps-sku="121642"></div>
    <div class="sc-a52be28-6">
      <button class="ps-widget">Buy now</button>
      <button class="ps-widget">Find retailers</button>
    </div>
    <div data-drawer-id="add-to-cart-drawer"></div>
  </section>
</body></html>"""

EMPTY_CART_PAGE = """
<html><body><h1>Cart</h1><p>Your cart looks lonely.</p></body></html>"""

FULL_CART_PAGE = """
<html><body><h1>Cart</h1>
  <ul><li>
    <a href="/us/store/products/nintendo-switch-2-dock-set-123791/">Nintendo Switch 2 Dock Set</a>
    <span>$119.99</span>
  </li></ul>
  <button>Checkout</button>
</body></html>"""

# A cart holding something else entirely - the add failed, and saying otherwise
# would report a product as carted when it is not.
OTHER_CART_PAGE = FULL_CART_PAGE.replace('123791', '999999').replace('Dock Set', 'Something Else')

# A cart holding a DIFFERENT product whose name starts the same way. Nintendo
# names share long prefixes, so the name fallback has to compare whole names:
# a substring test on the first 40 characters called this a successful add.
PREFIX_CART_PAGE = """
<html><body><h1>Cart</h1>
  <ul><li>
    <a href="/us/store/products/nintendo-switch-2-dock-set-carrying-case-999999/">Nintendo Switch 2 Dock Set - Zelda 40th Anniversary Edition Carrying Case</a>
    <span>$34.99</span>
  </li></ul>
  <button>Checkout</button>
</body></html>"""


# ------------------------------------------------------- the click path, faked
def offline_checks():
    print("\nBuy box (fake driver over saved HTML)")
    scraper = NintendoScraper()

    driver = FakeDriver(product_page('123791'))
    button, problem = scraper._find_buy_button(driver, '123791')
    check("finds the add-to-cart button", button is not None and problem is None,
          f"problem={problem!r}")
    check("and it is the buy box's, not a cross-sell's",
          button is not None and button.tag.parent.get('class') == ['sc-a52be28-6', 'bNZluS'],
          "the selector matched a carousel card")
    check("the page really does hold cross-sell buttons too",
          len(driver.find_elements('css selector', 'button')) > 3)

    driver = FakeDriver(product_page('129088', cta='Pre-purchase'))
    button, problem = scraper._find_buy_button(driver, '129088')
    check("an open pre-order's Pre-purchase counts as the buy button",
          button is not None and button.text == 'Pre-purchase', f"problem={problem!r}")

    driver = FakeDriver(product_page('121642', cta='Sold out', cta_attrs='disabled'))
    button, problem = scraper._find_buy_button(driver, '121642')
    check("a sold-out CTA comes back as a disabled button",
          button is not None and problem is None and not button.is_enabled())
    check("and reports why", bool(scraper._sold_out_reason(driver)))

    driver = FakeDriver(RETAILER_ONLY_PAGE)
    button, problem = scraper._find_buy_button(driver, '121642')
    check("never treats the price-spider controls as the buy button", button is None)
    check("and says so", problem is not None and 'third-party' in (problem or ''),
          f"problem={problem!r}")

    driver = FakeDriver(product_page('121642', anchor_sku='999999'))
    button, problem = scraper._find_buy_button(driver, '121642')
    check("refuses a page whose buy box is for another SKU",
          button is None and problem is not None and '999999' in problem, f"problem={problem!r}")

    driver = FakeDriver(product_page('121642'))
    check("no complaint when the SKU anchor agrees",
          scraper._sku_mismatch(driver, '121642') is None)

    print("\nQuantity, cart verification and the sign-in wall")
    driver = FakeDriver(product_page('123791'))
    check("reads the quantity stepper", scraper._current_quantity(driver) == 1)

    driver = FakeDriver(EMPTY_CART_PAGE)
    check("an empty cart is not a successful add",
          scraper._cart_contains(driver, '123791') is False)
    driver = FakeDriver(FULL_CART_PAGE)
    check("finds the SKU on the cart page", scraper._cart_contains(driver, '123791') is True)
    driver = FakeDriver(OTHER_CART_PAGE)
    check("a cart holding something else is not a successful add",
          scraper._cart_contains(driver, '123791') is False)
    check("but matches by name when the cart links differently",
          scraper._cart_contains(driver, '123791', 'Nintendo Switch 2 Something Else') is True)
    check("and the name match ignores case, spacing and punctuation",
          scraper._cart_contains(driver, '123791', 'nintendo  switch 2 - SOMETHING else!') is True)
    check("a partial name is not a match",
          scraper._cart_contains(driver, '123791', 'Something Else') is False)

    # The name fallback has to compare whole names. Nintendo's line items share
    # long prefixes, so a cart holding the carrying case must not report the
    # console as added.
    driver = FakeDriver(PREFIX_CART_PAGE)
    check("a cart line that only shares a prefix is not a match",
          scraper._cart_contains(
              driver, '123791', 'Nintendo Switch 2 Dock Set - Zelda 40th Anniversary Edition') is False)

    # detect_block_page calls any short page with no product markers a block page,
    # which an empty cart is. Reading that as a bot wall would bury the real answer.
    driver = FakeDriver(EMPTY_CART_PAGE)
    check("a small empty cart is not mistaken for a bot wall",
          scraper._page_obstacle(driver, expect_product=False) is None,
          str(scraper._page_obstacle(driver, expect_product=False)))
    check("but a short PRODUCT page still is",
          scraper._page_obstacle(driver) is not None)

    # The regression this replaces: every Nintendo page's header says
    # "Log in / Sign up", so a body-text login test fails every single add.
    driver = FakeDriver(product_page('123791'))
    check('"Log in / Sign up" in the header is not a sign-in wall',
          scraper._login_wall_present(driver) is False)
    check("and the page has no other obstacle", scraper._page_obstacle(driver) is None)

    driver = FakeDriver('<html><body>Sign in</body></html>',
                        url='https://accounts.nintendo.com/login?client_id=x')
    check("being sent to accounts.nintendo.com is a sign-in wall",
          scraper._login_wall_present(driver) is True)
    obstacle = scraper._page_obstacle(driver)
    check("reported with what to do about it",
          obstacle is not None and 'NINTENDO_HEADLESS=0' in obstacle, f"obstacle={obstacle!r}")

    driver = FakeDriver('<html><body><form><input type="password"></form></body></html>',
                        url='https://www.nintendo.com/us/store/products/x-1/')
    check("so is a password field on the store itself",
          scraper._login_wall_present(driver) is True)

    print("\nUnorderable, explained in the store's own terms")
    reason = NintendoScraper._unorderable_reason(
        {'pre_purchase': True, 'availability': ['Coming soon']})
    check("a closed pre-order says so", 'pre-order is not open' in reason, reason)
    reason = NintendoScraper._unorderable_reason(
        {'pre_purchase': False, 'availability': ['Out of stock']})
    check("anything else quotes the availability label", 'Out of stock' in reason, reason)


def flow_checks():
    """add_to_cart from end to end, with the browser replaced by FakeDriver"""
    import tempfile

    print("\nadd_to_cart end to end (no Chrome, no real cart)")
    dock_url = "https://www.nintendo.com/us/store/products/nintendo-switch-2-dock-set-123791/"
    drawer = ('<div data-drawer-id="add-to-cart-drawer">'
              '<p>Added to cart</p><button>Checkout</button></div>')
    after_click = product_page('123791').replace(
        '<div data-drawer-id="add-to-cart-drawer"></div>', drawer)

    def build(cart_page):
        scraper = NintendoScraper()
        scraper.profile_dir = tempfile.mkdtemp(prefix='nintendo_profile_test_')
        scraper.current_product_url = dock_url
        scraper._fetch_order_state = lambda url: {
            'sku': '123791', 'name': 'Nintendo Switch 2 Dock Set', 'salable': True,
            'pre_purchase': False, 'availability': [], 'release_date': None,
            'max_quantity': 3, 'orderable': True}
        driver = FakeDriver(product_page('123791'), url=dock_url,
                            pages={ns.NINTENDO_CART_URL: cart_page})
        driver.on_click['Add to cart'] = after_click
        scraper._start_driver = lambda: driver
        return scraper, driver

    scraper, driver = build(FULL_CART_PAGE)
    result = scraper.add_to_cart(quantity=1)
    check("adds and verifies on the cart page", result['success'] is True, result['message'])
    check("returns the cart URL", result['cart_url'] == ns.NINTENDO_CART_URL,
          str(result['cart_url']))
    check("returns a screenshot", bool(result['screenshot']))
    check("clicked the buy button exactly once", driver.clicks == ['Add to cart'],
          str(driver.clicks))
    check("never clicked anything inside the drawer",
          'Checkout' not in driver.clicks, str(driver.clicks))

    scraper, driver = build(EMPTY_CART_PAGE)
    result = scraper.add_to_cart(quantity=1)
    check("an empty cart afterwards is a failure, not a success", result['success'] is False,
          result['message'])
    check("and says the item was not found",
          'not found in the Nintendo cart' in result['message'], result['message'])

    # The common case for the tracked items: the HTTP pre-check answers, and no
    # browser is launched at all.
    scraper = NintendoScraper()
    scraper.current_product_url = CONSOLE_URL

    def no_browser():
        raise AssertionError("add_to_cart launched Chrome for a product that is not salable")

    scraper._start_driver = no_browser
    scraper._fetch_order_state = lambda url: {
        'sku': '121642', 'name': 'Zelda console', 'salable': False, 'pre_purchase': True,
        'availability': ['Coming soon'], 'release_date': None, 'max_quantity': 1,
        'orderable': False}
    result = scraper.add_to_cart(quantity=1)
    check("a closed pre-order fails without launching a browser", result['success'] is False,
          result['message'])
    check("with Nintendo's own explanation", 'pre-order is not open' in result['message'],
          result['message'])
    check("and no screenshot, because nothing was opened", result['screenshot'] is None)

    scraper = NintendoScraper()
    result = scraper.add_to_cart(quantity=1)
    check("no URL set is a clean failure",
          result['success'] is False and 'scrape_product' in result['message'],
          result['message'])


# ----------------------------------------------------------------- live, HTTP
def live_precheck():
    """
    Read the real buy-box state over HTTP. Nothing here opens a browser.

    add_to_cart is called only for products the pre-check has already declared
    unorderable, which is exactly the path that cannot reach a click.
    """
    print("\nLive pre-check (HTTP only, nothing is clicked)")
    scraper = NintendoScraper()

    for url in (CONSOLE_URL, CONTROLLER_URL, CASE_URL):
        sku = NintendoScraper.extract_sku(url)
        state = scraper._fetch_order_state(url)
        if state is None:
            check(f"{sku}: pre-check reachable", False, "no state - blocked, offline, or moved")
            continue
        print(f"  {sku}: {(state['name'] or '?')[:52]} | isSalableQty={state['salable']!r} "
              f"| prePurchase={state['pre_purchase']} | {state['availability']}")
        if state['orderable']:
            print(f"  ..  {sku} IS orderable right now - not carting it from a test run")
            continue
        scraper.current_product_url = url
        result = scraper.add_to_cart(quantity=1)
        check(f"{sku}: clean not-orderable failure",
              result['success'] is False and 'Cannot add to cart' in result['message'],
              result['message'])
        check(f"{sku}: failed before opening a browser", result['screenshot'] is None)

    # The pre-check has to say yes to something, or a false "not orderable" would
    # look like a pass everywhere. This product can genuinely be ordered, so it is
    # read and left alone - add_to_cart is deliberately not called on it.
    state = scraper._fetch_order_state(SALABLE_URL)
    if state is None:
        check("129088: pre-check reachable", False, "no state")
    else:
        print(f"  129088: {(state['name'] or '?')[:52]} | isSalableQty={state['salable']!r}")
        check("129088: a salable product reads as orderable", state['orderable'] is True,
              f"isSalableQty={state['salable']!r}")


def live_click(url):
    """Really add a real product to the real cart. Opt-in, and it stops at the cart."""
    scraper = NintendoScraper()
    print(f"\nURL: {url}")
    info = scraper.scrape_product(url)
    print(f"Scrape: {info and info.get('name')} | price={info and info.get('price')} "
          f"| available={info and info.get('available')}")
    result = scraper.add_to_cart(quantity=1)
    print(f"Success:  {result.get('success')}")
    print(f"Message:  {result.get('message')}")
    print(f"Cart URL: {result.get('cart_url')}")
    if result.get('screenshot'):
        path = f"nintendo_cart_{NintendoScraper.extract_sku(url)}.png"
        with open(path, 'wb') as f:
            f.write(base64.b64decode(result['screenshot']))
        print(f"Screenshot: {path}")


# ------------------------------------------------------------ profile setup
def login():
    """Open the persistent profile in a visible window for a one-time sign-in."""
    scraper = NintendoScraper()
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with profile_lock(scraper.profile_dir, timeout=None):
        driver = scraper._start_driver()
        try:
            driver.set_page_load_timeout(60)
            driver.get(ACCOUNT_URL)
            print("\nSign in in the window, then press Enter here...")
            input()
            for url in (CONSOLE_URL, ns.NINTENDO_CART_URL):
                print(f"Visiting {url}")
                try:
                    driver.get(url)
                except Exception as e:
                    print(f"  (navigation error: {e})")
                print(f"  sign-in wall: {'yes' if scraper._login_wall_present(driver) else 'no'}")
            print("\nDone. The profile now holds the session; headless runs will reuse it.")
        finally:
            try:
                driver.quit()
            except Exception:
                pass


def import_cookies(path):
    """
    Load a nintendo.com cookie export from the user's ordinary browser into the
    persistent profile, then report whether the way to the cart is clear.

    This moves the user's own session between two of their own browser profiles.
    """
    scraper = NintendoScraper()
    print(f"Importing {path} into profile: {scraper.profile_dir}")
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with profile_lock(scraper.profile_dir, timeout=None):
        driver = scraper._start_driver()
        try:
            driver.set_page_load_timeout(60)
            # add_cookie() writes into the current document's store, so the domain
            # has to be loaded before any cookie for it will be accepted.
            print(f"Opening {HOME_URL} so the cookies have a home...")
            driver.get(HOME_URL)
            added, rejected = import_cookies_txt(driver, path, 'nintendo.com')
            print(f"Added {added} cookies ({rejected} rejected by Chrome)")

            for url in (CONSOLE_URL, ns.NINTENDO_CART_URL):
                print(f"\nReloading {url}")
                try:
                    driver.get(url)
                except Exception as e:
                    print(f"  navigation error: {e}")
                    continue
                obstacle = scraper._page_obstacle(driver)
                print(f"  obstacle: {obstacle or 'none - clear'}")
            print("\nDelete the cookie file now - it is a live login.")
        finally:
            try:
                driver.quit()
            except Exception:
                pass


def _arg_after(flag, usage):
    index = sys.argv.index(flag)
    if index + 1 >= len(sys.argv):
        sys.exit(usage)
    return sys.argv[index + 1]


if __name__ == '__main__':
    if IMPORT_MODE:
        import_cookies(_arg_after('--import-cookies',
                                  "Usage: python test_nintendo_cart.py --import-cookies <cookies.txt>"))
    elif LOGIN_MODE:
        login()
    elif '--live-click' in sys.argv:
        live_click(_arg_after('--live-click',
                              "Usage: python test_nintendo_cart.py --live-click <product url>"))
    else:
        print("Testing NintendoScraper.add_to_cart")
        offline_checks()
        flow_checks()
        if not OFFLINE:
            live_precheck()
        print(f"\n{passed} passed, {failed} failed")
        sys.exit(1 if failed else 0)
