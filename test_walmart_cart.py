"""
Live check of WalmartScraper.add_to_cart (cart only - never checkout).

    python test_walmart_cart.py                      # headless, uses ~/.chrome_profiles/walmart_profile
    WALMART_HEADLESS=0 python test_walmart_cart.py   # visible window
    python test_walmart_cart.py --login              # ONE-TIME SETUP, see below
    python test_walmart_cart.py --import-cookies cookies.txt   # if --login keeps looping

One-time setup (--login)
------------------------
Walmart serves a PerimeterX "Robot or human? / Press & hold" wall to fresh
automated profiles - a brand new headless profile gets it on the very first
product page - and gates the cart behind a sign-in. Neither is solved in code:
`--login` opens the persistent profile in a VISIBLE window so you can sign in
and clear the challenge yourself. The cookies stay in
~/.chrome_profiles/walmart_profile and later headless runs reuse them.

The app must run as the SAME OS user, since it reads that same profile directory.
The lock is held for the whole session, so a scheduled check cannot open Chrome
on this profile while you are signing in.

If the challenge loops (--import-cookies)
-----------------------------------------
Once Walmart has flagged a profile, its press-and-hold can loop forever in a
webdriver-controlled window. Rather than automate the challenge (never do that),
carry over the session you already have in your ordinary browser:

    1. In your normal Chrome, sign in to walmart.com and clear any verification.
    2. Export cookies for walmart.com - the "Get cookies.txt LOCALLY" extension
       writes the Netscape format this reads; a JSON export also works.
    3. python test_walmart_cart.py --import-cookies path/to/cookies.txt

Treat the export as a password: it is your live session. Delete it afterwards.

The live target
---------------
CONSOLE_URL is the item that will actually be auto-carted on restock (item
21002656445, the Switch 2 Zelda 40th Anniversary Edition console). The Pro
Controller is checked alongside it.

Expected today: both fail cleanly with "the pre-order is not open" WITHOUT
launching a browser - the buy-box state is read over plain HTTP first.
Screenshots, when a browser did run, are written to walmart_cart_<item_id>.png.
"""
import base64
import os
import sys

CONTROLLER_URL = ("https://www.walmart.com/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-"
                  "Anniversary-Edition/20954470204")
# The user's auto-cart row - the item that actually gets carted on restock.
# The slug matters: a bare /ip/<id> URL is served the bot wall, not the product.
CONSOLE_URL = ("https://www.walmart.com/ip/Nintendo-Switch-2-The-Legend-of-Zelda-40th-"
               "Anniversary-Edition/21002656445")
ACCOUNT_URL = "https://www.walmart.com/account"
HOME_URL = "https://www.walmart.com/"

# --login and --import-cookies must show a window whatever WALMART_HEADLESS says;
# set it before the scraper module reads the variable at import time
LOGIN_MODE = '--login' in sys.argv
IMPORT_MODE = '--import-cookies' in sys.argv
FIXTURE_MODE = '--fixtures' in sys.argv
if LOGIN_MODE or IMPORT_MODE:
    os.environ['WALMART_HEADLESS'] = '0'

from app.scrapers.walmart_scraper import WalmartScraper, WALMART_CART_URL  # noqa: E402
from app.scrapers.common import profile_lock, import_cookies_txt  # noqa: E402

# Product names contain ™ / –; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def login():
    """Open the persistent profile in a visible window for a one-time sign-in / verification."""
    scraper = WalmartScraper()
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    # Wait indefinitely: a scheduled check may hold the profile, and the human
    # here needs it for as long as signing in takes.
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with profile_lock(scraper.profile_dir, timeout=None):
        _login_session(scraper)


def _login_session(scraper):
    driver = scraper._start_driver()
    try:
        driver.set_page_load_timeout(60)
        driver.get(ACCOUNT_URL)
        print("\nSign in and complete any verification in the window, then press Enter here...")
        input()

        # Visit the product page and the cart once so the profile carries the cleared cookies
        for url in (CONTROLLER_URL, WALMART_CART_URL):
            print(f"Visiting {url}")
            try:
                driver.get(url)
            except Exception as e:
                print(f"  (navigation error: {e})")
            challenge = scraper._challenge_present(driver)
            if challenge:
                print(f"  Still showing a verification challenge ({challenge}) - complete it in the window, "
                      "then press Enter...")
                input()
        print("\nDone. The profile now holds the session; headless runs will reuse it.")
        print("Run `python test_walmart_cart.py` to test add-to-cart.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def import_cookies(path):
    """
    Load a walmart.com cookie export from the user's ordinary browser into the
    persistent profile, then report whether the way to the cart is clear.

    This moves the user's own signed-in session between two of their own browser
    profiles. It does not solve, bypass or automate the press-and-hold challenge.
    """
    scraper = WalmartScraper()
    print(f"Importing {path} into profile: {scraper.profile_dir}")
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with profile_lock(scraper.profile_dir, timeout=None):
        driver = scraper._start_driver()
        try:
            driver.set_page_load_timeout(60)
            # add_cookie() writes into the current document's store, so the domain
            # has to be loaded before any cookie for it will be accepted.
            print("Opening https://www.walmart.com/ so the cookies have a home...")
            driver.get(HOME_URL)
            added, rejected = import_cookies_txt(driver, path, 'walmart.com')
            print(f"Added {added} cookies ({rejected} rejected by Chrome)")

            for url in (CONSOLE_URL, WALMART_CART_URL):
                print(f"\nReloading {url}")
                try:
                    driver.get(url)
                except Exception as e:
                    print(f"  navigation error: {e}")
                    continue
                obstacle = scraper._page_obstacle(driver)
                print(f"  {obstacle if obstacle else 'clear'}")
            print("\nIf both pages are clear, headless runs will reuse this session.")
            print("Delete the cookie file now - it is a live login.")
        finally:
            try:
                driver.quit()
            except Exception:
                pass


# An empty Walmart cart, trimmed to the shape that matters: a few hundred bytes,
# no product markers, no challenge wording. Before expect_product=False this was
# reported as a bot-protection page.
EMPTY_CART_HTML = """<!DOCTYPE html><html><head><title>Cart - Walmart.com</title></head>
<body><div data-testid="empty-cart"><h1>Your cart is empty</h1>
<a href="/">Continue shopping</a></div></body></html>"""

# The same cart page, but served as Walmart's bot wall. Must still read as blocked.
BLOCKED_CART_HTML = """<!DOCTYPE html><html><head><title>Robot or human?</title></head>
<body><div id="px-captcha"></div><p>Activate and hold the button to confirm that
you are human.</p></body></html>"""


class FakeElement:
    def __init__(self, text=''):
        self.text = text


class FakeDriver:
    """Enough driver for _page_obstacle: page source, body text, url, no elements."""

    def __init__(self, html, body_text='', current_url='https://www.walmart.com/cart'):
        self.page_source = html
        self.current_url = current_url
        self._body = FakeElement(body_text)

    def find_element(self, *args):
        return self._body

    def find_elements(self, *args):
        return []


def fixtures():
    """Offline checks for the empty-cart / bot-wall distinction. No network, no Chrome."""
    from app.scrapers.common import detect_block_page

    passed, failed = [], []

    def check_that(name, condition):
        (passed if condition else failed).append(name)
        print(('  ok   ' if condition else '  FAIL ') + name)

    print("\ndetect_block_page")
    check_that("an empty cart is a bot wall when a product was expected",
               detect_block_page(EMPTY_CART_HTML) is not None)
    check_that("...and is not one when it was not",
               detect_block_page(EMPTY_CART_HTML, expect_product=False) is None)
    check_that("the default is unchanged, so no existing caller shifts",
               detect_block_page(EMPTY_CART_HTML)
               == detect_block_page(EMPTY_CART_HTML, expect_product=True))
    check_that("Walmart's own wall is still caught on the cart",
               detect_block_page(BLOCKED_CART_HTML, expect_product=False) is not None)
    check_that("...caught by its title, not by its size",
               'robot or human' in (detect_block_page(BLOCKED_CART_HTML,
                                                      expect_product=False) or ''))
    check_that("an empty body is still a failed load either way",
               detect_block_page('', expect_product=False) == 'empty response')

    print("\n_page_obstacle")
    scraper = WalmartScraper()
    check_that("the cart call reports no obstacle on an empty cart",
               scraper._page_obstacle(FakeDriver(EMPTY_CART_HTML), expect_product=False) is None)
    check_that("the product-page call still reports one",
               scraper._page_obstacle(FakeDriver(EMPTY_CART_HTML)) is not None)
    check_that("Walmart's wall on the cart is still reported",
               scraper._page_obstacle(FakeDriver(BLOCKED_CART_HTML),
                                      expect_product=False) is not None)
    # Walmart reads the rendered body, not the URL, for both of these - unlike
    # Target's _login_wall_present, which also looks at current_url. So these
    # drive body text; see the note in the commit message about the asymmetry.
    check_that("press-and-hold wording in the body is still caught on the cart",
               scraper._page_obstacle(
                   FakeDriver(EMPTY_CART_HTML,
                              body_text='Press & Hold to confirm you are a human (and not a bot)'),
                   expect_product=False) is not None)
    check_that("a sign-in wall is still caught on the cart",
               scraper._page_obstacle(
                   FakeDriver(EMPTY_CART_HTML,
                              body_text='Sign in to your account to continue'),
                   expect_product=False) is not None)

    # The reason a newly-permissive cart check is still safe: verification needs
    # positive evidence of the item, so a wall that now slips past the block
    # check fails as "not in the cart" rather than being reported as success.
    check_that("a wall that slips past still cannot be mistaken for a full cart",
               scraper._cart_contains(FakeDriver(BLOCKED_CART_HTML), '5564066414') is False)
    check_that("and an empty cart is not a full one either",
               scraper._cart_contains(FakeDriver(EMPTY_CART_HTML), '5564066414') is False)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


def check(url):
    item_id = WalmartScraper.extract_item_id(url)
    print(f"\nURL: {url}")
    scraper = WalmartScraper()

    # add_to_cart needs current_product_url, so pre-scrape like app.scrapers.add_to_cart does
    info = scraper.scrape_product(url)
    print(f"Scrape: {info and info.get('name')} | price={info and info.get('price')} "
          f"| available={info and info.get('available')}")

    try:
        result = scraper.add_to_cart(quantity=1)
        print(f"Success: {result.get('success')}")
        print(f"Message: {result.get('message')}")
        print(f"Cart URL: {result.get('cart_url')}")
        if result.get('screenshot'):
            path = f"walmart_cart_{item_id}.png"
            with open(path, 'wb') as f:
                f.write(base64.b64decode(result['screenshot']))
            print(f"Screenshot: {path}")
    except Exception as e:
        print(f"Error: {str(e)}")
        print(f"Type: {type(e).__name__}")


def _cookie_path():
    """The path after --import-cookies"""
    index = sys.argv.index('--import-cookies')
    if index + 1 >= len(sys.argv):
        sys.exit("Usage: python test_walmart_cart.py --import-cookies <cookies.txt>")
    return sys.argv[index + 1]


if __name__ == '__main__':
    if FIXTURE_MODE:
        sys.exit(fixtures())
    elif IMPORT_MODE:
        import_cookies(_cookie_path())
    elif LOGIN_MODE:
        login()
    else:
        print("Testing Walmart add_to_cart...")
        for product_url in (CONSOLE_URL, CONTROLLER_URL):
            check(product_url)
