"""
Scraper for the My Nintendo Store (nintendo.com/us/store/products/...).

Nintendo serves a complete Next.js page to a plain HTTP request - no bot wall,
no rendering needed - so this is a requests + BeautifulSoup scraper with no
browser path at all.

Two things about this page are worth knowing before changing anything here.

Availability is isSalableQty, not the availability[] labels
-----------------------------------------------------------
A product node carries availability: ["Coming soon"] and prePurchase: true for
both an OPEN pre-order and one whose allocation is gone. The two states differ
only in isSalableQty. Observed live on 2026-09-10:

    sku 129088  availability ["Coming soon"]  prePurchase true  isSalableQty TRUE
    sku 121642  availability ["Coming soon"]  prePurchase true  isSalableQty FALSE

129088 could be pre-ordered that minute; 121642 (the Zelda 40th console) could
not. Keying on "Coming soon" or prePurchase would report all three Zelda items
as available on every check, from now until release.

The page describes ~30 products, not one
----------------------------------------
props.pageProps.initialApolloState is a normalised Apollo cache holding the
product plus its cross-sells and up-sells - 30 to 33 Product entries per page,
of which 14 to 22 are salable. Taking "the first product node", or "the first
salable node", would report the console as in stock because some unrelated
carrying case is. The node is therefore looked up by the exact cache key built
from the SKU in the URL, and that SKU is cross-checked against linkedData.

add_to_cart does need a browser
-------------------------------
Scraping needs no browser; carting does. The buy box is client-rendered and the
cart is a session on the store, so add_to_cart drives undetected-chromedriver on
a persistent profile (~/.chrome_profiles/nintendo_profile), the same shape as the
Target and Best Buy scrapers, and holds common.profile_lock while it runs.

The same 30-products-per-page hazard applies to the click, and worse: several of
the cross-sells ARE in stock and do have working buttons. See the selector notes
below for how the CTA is pinned to the sku in the URL.

Before any of that, the buy-box state is read over plain HTTP. Almost every
add_to_cart on the tracked Zelda items will fail with "the pre-order is not
open", and that answer costs one request rather than a Chrome launch.

This scraper carts and stops: it never signs in, never opens checkout, and never
clicks the price-spider "Buy now" / "Find retailers" controls that sit in the
same buy box and lead to other retailers.
"""

import json
import logging
import os
import re
import time
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from contextlib import ExitStack
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib.parse import urlparse
from app.scrapers.common import (DEFAULT_HEADERS, REQUEST_TIMEOUT, ProfileBusyError,
                                 detect_block_page, detect_chrome_major, profile_lock)

logger = logging.getLogger('app.scrapers.nintendo')

# A store URL ends .../products/<slug>-<sku>/ - the SKU is the trailing number
NINTENDO_SKU_RE = re.compile(r'-(\d+)$')

# Apollo stores prices under a key with the query arguments baked into it, so
# the literal string matters.
PRICES_KEY = 'prices({"personalized":false})'

# schema.org availability values that mean "you can order this now"
ORDERABLE_SCHEMA_AVAILABILITY = (
    'https://schema.org/InStock',
    'https://schema.org/PreOrder',
    'https://schema.org/LimitedAvailability',
    'https://schema.org/BackOrder',
)

# ------------------------------------------------------------------------ cart
# Nothing in the buy box has a stable id or data-testid: the CTA is
# <button class="MFcmt sc-a52be28-7 bNZluS G0A6l sQXAt">, hashed
# styled-components class names that change with every deploy. Two attributes do
# survive and both were verified live on 2026-09-10 across three products:
#
#   [data-ps-sku="<sku>"]                    the price-spider widget, exactly one
#                                            per page, carrying the SKU the buy
#                                            box belongs to
#   [data-drawer-id="add-to-cart-drawer"]    an empty placeholder rendered
#                                            immediately AFTER the CTA's wrapper
#
# The CTA is therefore addressed structurally, and the SKU anchor is what keeps
# the click off the 30-odd cross-sell products the same page describes.

NINTENDO_CART_URL = 'https://www.nintendo.com/us/cart/'

# The CTA's wrapper div is the immediate previous sibling of the drawer
# placeholder. :has() is supported by Chrome 105+ and by soupsieve, so the same
# selector works against a live driver and against saved HTML in the tests.
BUY_BUTTON_SELECTOR = 'div:has(+ [data-drawer-id="add-to-cart-drawer"]) button'
# Fallback: any button inside the section that holds this SKU's price widget.
BUY_BOX_SECTION_SELECTOR = 'section:has([data-ps-sku="%s"]) button'
SKU_ANCHOR_SELECTOR = '[data-ps-sku]'

# What the CTA says, observed live on 2026-09-10:
#   in stock          "Add to cart"   enabled    (sku 123791, Dock Set)
#   open pre-order    "Pre-purchase"  enabled    (sku 129088, Sports Resort bundle)
#   closed pre-order  "Sold out"      disabled   (sku 121642, the Zelda console)
BUY_BUTTON_TEXTS = ('add to cart', 'pre-purchase', 'pre-order', 'preorder')

# Never clicked, wherever they turn up. "Buy now" and "Find retailers" are the
# third-party price-spider widget sitting in the same buy box - they send the
# shopper to another retailer - and the rest are checkout. This scraper carts and
# stops.
FORBIDDEN_BUTTON_TEXTS = ('buy now', 'find retailers', 'checkout', 'check out',
                          'place order', 'proceed to', 'pay ')

# The quantity stepper, which sits in the buy box above the CTA
QUANTITY_UP_SELECTOR = 'button[aria-label="Add item"]'
QUANTITY_VALUE_SELECTOR = 'div[aria-live="polite"]'

# Nintendo's own wording when the buy box cannot be used
SOLD_OUT_TEXTS = ('sold out', 'out of stock', 'currently unavailable',
                  'no longer available', 'not available')

# The empty cart, so a failed add is not read as a success. Nintendo's phrasing
# is "Your cart looks lonely."; the second is defensive.
EMPTY_CART_MARKERS = ('your cart looks lonely', 'your cart is empty')

# detect_block_page() calls a page under 5 KB with no product markers a block
# page. That is right for a product page and wrong for the cart, which is small
# and mentions no products at all when it is empty - exactly the state that has
# to be readable. This is how that one reason is recognised and set aside.
SIZE_HEURISTIC_MARKER = 'no product markers'

# Sign-in lives on a different host. The page body is no help here: the header
# says "Log in / Sign up" on every page, signed in or not.
LOGIN_HOSTS = ('accounts.nintendo.com',)

# Where the post-add side sheet renders. Read only: see _log_cart_drawer for why
# nothing inside it is ever clicked.
CART_DRAWER_SELECTOR = '[data-drawer-id="add-to-cart-drawer"], [role="dialog"]'

# Each cart line item links back to its product page. Used only to read the line
# item's name; the sku in that href is what _cart_contains prefers anyway.
CART_PRODUCT_LINK_SELECTOR = 'a[href*="/store/products/"]'


class NintendoScraper:
    """Scraper for My Nintendo Store product pages"""

    def __init__(self):
        """Initialize the Nintendo scraper."""
        logger.debug("Initializing NintendoScraper")
        self.headers = dict(DEFAULT_HEADERS)
        # Set by scrape_product; add_to_cart reads it, because the dispatcher in
        # app/scrapers/__init__.py pre-scrapes rather than passing a URL
        self.current_product_url = None
        # Only add_to_cart launches Chrome, but the profile is prepared here so a
        # permissions problem shows up before a drop rather than during one
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "nintendo_profile")
        os.makedirs(self.profile_dir, exist_ok=True)

    @staticmethod
    def extract_sku(url):
        """
        Return the SKU from a My Nintendo Store URL, or None.

        The SKU is the trailing numeric segment of the product slug:
            /us/store/products/nintendo-switch-2-...-40th-anniversary-edition-121642/
        Query strings and fragments are ignored, so a tracked URL carrying
        campaign parameters still resolves.
        """
        try:
            path = urlparse(url or '').path
        except ValueError:
            logger.debug(f"Could not parse a path out of URL: {url!r}")
            return None
        match = NINTENDO_SKU_RE.search(path.rstrip('/'))
        return match.group(1) if match else None

    def scrape_product(self, url):
        """
        Scrape product information from a My Nintendo Store URL.

        Args:
            url: The product URL to scrape

        Returns:
            dict with name, price, available and image_url, or None if the page
            could not be read as a product page. None means "no answer" - the
            caller must not read it as "not available".
        """
        self.current_product_url = url

        sku = self.extract_sku(url)
        if not sku:
            logger.error(f"No SKU in Nintendo URL, cannot identify the product: {url}")
            return None

        logger.info(f"Scraping Nintendo product {sku}: {url}")
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.error(f"Request to Nintendo failed: {str(e)}")
            return None

        if response.status_code != 200:
            logger.error(f"Nintendo returned HTTP {response.status_code} for {url}")
            return None

        block_reason = detect_block_page(response.text)
        if block_reason:
            logger.warning(f"Nintendo served a block page ({block_reason}); reporting no result")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')
        return self.extract_from_next_data(soup, sku)

    def extract_from_next_data(self, soup, sku):
        """
        Pull the product out of <script id="__NEXT_DATA__">.

        Args:
            soup: the parsed page
            sku: the SKU from the URL - the node is looked up by this and by
                 nothing else, because the page describes ~30 products

        Returns:
            dict, or None if this page has no node for that SKU
        """
        try:
            script = soup.find('script', id='__NEXT_DATA__')
            if not script or not script.string:
                logger.debug("No __NEXT_DATA__ script on page")
                return None

            page_props = json.loads(script.string).get('props', {}).get('pageProps', {})
            apollo = page_props.get('initialApolloState') or {}

            # The Apollo cache key is the literal string Nintendo writes, quotes
            # and all: Product:{"sku":"121642"}
            node = apollo.get('Product:{"sku":"%s"}' % sku)
            if not node:
                others = [key for key in apollo if key.startswith('Product:')]
                logger.error(f"No Apollo product node for SKU {sku} "
                             f"({len(others)} other products are on the page); "
                             "the URL may point at a product that has moved")
                return None

            linked = self._linked_data(page_props)
            # If linkedData names a different SKU we are reading the wrong page -
            # a redirect to a replacement product, say. Say so rather than
            # reporting on a product nobody asked about.
            linked_sku = str((linked or {}).get('sku') or '')
            if linked_sku and linked_sku != sku:
                logger.error(f"URL asks for SKU {sku} but the page describes {linked_sku}; "
                             "refusing to report on a different product")
                return None

            name = (node.get('name') or (linked or {}).get('name') or '').strip()
            if not name:
                logger.debug(f"Apollo node for {sku} has no name; not a usable product page")
                return None

            available = self._is_orderable(node, linked)

            price = None
            prices = node.get(PRICES_KEY) or {}
            for key in ('finalPrice', 'regularPrice'):
                value = prices.get(key)
                if isinstance(value, (int, float)):
                    price = float(value)
                    break
            if price is None:
                price = self._price_from_offers((linked or {}).get('offers'))

            image_url = (linked or {}).get('image')

            logger.info(f"Nintendo {sku}: {name[:60]} ${price} available={available}")
            return {
                'name': name,
                'price': price,
                'available': available,
                'image_url': image_url
            }
        except Exception as e:
            logger.error(f"Error extracting Nintendo product from __NEXT_DATA__: {str(e)}")
            return None

    @staticmethod
    def _is_orderable(node, linked=None):
        """
        Whether this product can actually be ordered right now.

        isSalableQty is the gate. It is the one field that separates an open
        pre-order from a closed one - see the module docstring. availability[]
        and prePurchase describe what the product IS, not whether Nintendo will
        take an order for it, so they are logged and otherwise ignored.

        linkedData's schema.org availability is consulted only to log a
        disagreement: it is a static per-page annotation, so it never overrides
        isSalableQty.
        """
        salable = node.get('isSalableQty')
        available = salable is True

        logger.debug(f"Nintendo {node.get('sku')}: isSalableQty={salable!r} "
                     f"availability={node.get('availability') or []} "
                     f"prePurchase={node.get('prePurchase')!r} -> available={available}")

        schema_availability = None
        if linked:
            offers = linked.get('offers') or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                schema_availability = offers.get('availability')
        if schema_availability:
            schema_orderable = schema_availability in ORDERABLE_SCHEMA_AVAILABILITY
            if schema_orderable != available:
                logger.warning(f"Nintendo {node.get('sku')}: isSalableQty says available="
                               f"{available} but linkedData says {schema_availability}; "
                               "trusting isSalableQty")
        return available

    @staticmethod
    def _linked_data(page_props):
        """Return the page's JSON-LD Product dict, or None. Nintendo sends it as a list."""
        linked = page_props.get('linkedData')
        if isinstance(linked, list):
            for entry in linked:
                if isinstance(entry, dict) and entry.get('@type') == 'Product':
                    return entry
            return linked[0] if linked and isinstance(linked[0], dict) else None
        return linked if isinstance(linked, dict) else None

    @staticmethod
    def _price_from_offers(offers):
        """Pull a float price out of a JSON-LD offers object or list, or None"""
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            return None
        try:
            return float(str(offers.get('price')).replace(',', ''))
        except (TypeError, ValueError):
            return None

    # -------------------------------------------------------------------- cart
    def _fetch_order_state(self, url):
        """
        Read the buy-box state over plain HTTP, before spending a browser launch.

        Nintendo serves the whole page to a plain request, so this is the same
        server state the page renders from. Returns a dict, or None when the page
        could not be read - in which case the caller opens the browser rather
        than assuming anything.
        """
        sku = self.extract_sku(url)
        if not sku:
            return None
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200 or detect_block_page(response.text):
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            script = soup.find('script', id='__NEXT_DATA__')
            if not script or not script.string:
                return None
            page_props = json.loads(script.string).get('props', {}).get('pageProps', {})
            node = (page_props.get('initialApolloState') or {}).get('Product:{"sku":"%s"}' % sku)
            if not node:
                return None
        except Exception as e:
            logger.debug(f"Could not read Nintendo order state: {str(e)}")
            return None

        return {
            'sku': sku,
            'name': (node.get('name') or '').strip() or None,
            'salable': node.get('isSalableQty'),
            'pre_purchase': bool(node.get('prePurchase')),
            'availability': node.get('availability') or [],
            'release_date': node.get('releaseDateDisplay') or node.get('releaseDate'),
            'max_quantity': node.get('maxQtyAllowedInCart'),
            'orderable': node.get('isSalableQty') is True,
        }

    @staticmethod
    def _unorderable_reason(state):
        """Explain, in the store's own terms, why the buy box cannot be used"""
        labels = ', '.join(str(label) for label in state.get('availability') or []) or 'no availability label'
        if state.get('pre_purchase'):
            return (f"the pre-order is not open - Nintendo reports isSalableQty false "
                    f"while showing {labels}")
        return f"Nintendo reports the item as not salable ({labels})"

    def add_to_cart(self, quantity=1):
        """
        Add the most recently scraped product to the My Nintendo Store cart.

        Cart only. This never continues to checkout, never touches payment, and
        never signs in - a sign-in wall is reported as a failure with what the
        person has to do about it.

        The URL comes off the instance rather than the arguments: the dispatcher
        in app/scrapers/__init__.py pre-scrapes, which sets current_product_url.

        Returns:
            dict: {'success', 'message', 'cart_url', 'screenshot'}
        """
        url = self.current_product_url
        if not url:
            return self._cart_result(False, "No product URL set - call scrape_product() first")
        sku = self.extract_sku(url)
        if not sku:
            return self._cart_result(
                False, f"Could not find a SKU (trailing -<number>) in Nintendo URL: {url}")

        # A closed pre-order is the ordinary case for the items being tracked, and
        # it does not deserve a Chrome launch: the plain HTTP page already says so
        # in isSalableQty. Only an unreadable page falls through to the browser.
        state = self._fetch_order_state(url)
        if state and not state['orderable']:
            return self._cart_result(False, f"Cannot add to cart: {self._unorderable_reason(state)}")
        product_name = (state or {}).get('name')
        if state is None:
            logger.info(f"Could not pre-check Nintendo {sku} over HTTP; letting the browser decide")

        logger.info(f"Adding Nintendo product to cart: {url} (SKU {sku}), quantity: {quantity}")
        driver = None
        profile = ExitStack()
        try:
            profile.enter_context(profile_lock(self.profile_dir))
        except ProfileBusyError as e:
            return self._cart_result(False, str(e))

        try:
            driver = self._start_driver()
            driver.set_page_load_timeout(45)
            driver.get(url)
            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, SKU_ANCHOR_SELECTOR + ', h1')))
            except TimeoutException:
                logger.warning("Timed out waiting for the Nintendo buy box; reading whatever rendered")
            time.sleep(2)   # the CTA hydrates a moment after the rest of the buy box

            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            button, problem = self._find_buy_button(driver, sku)
            if problem:
                return self._cart_result(False, problem, driver=driver)
            if button is None:
                reason = self._sold_out_reason(driver) or \
                    "no add-to-cart / pre-purchase button in the buy box"
                return self._cart_result(False, f"Cannot add to cart: {reason}", driver=driver)

            label = (button.text or '').strip()
            if button.get_attribute('disabled') is not None or not button.is_enabled():
                reason = self._sold_out_reason(driver) or f'the "{label}" button is disabled'
                return self._cart_result(False, f"Cannot add to cart: {reason}", driver=driver)

            note = ""
            if quantity > 1:
                if self._set_quantity(driver, quantity):
                    note = f" (quantity {quantity})"
                else:
                    note = f" (quantity 1 - could not set quantity to {quantity})"

            logger.debug(f"Clicking the Nintendo buy button, which reads {label!r}")
            self._click(driver, button)
            time.sleep(3)
            self._log_cart_drawer(driver)

            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            # Verify on the cart page rather than trusting the drawer. The drawer
            # is a side sheet with its own buttons, one of which is checkout, so
            # nothing inside it is clicked - it is read and left alone.
            driver.get(NINTENDO_CART_URL)
            time.sleep(4)
            obstacle = self._page_obstacle(driver, expect_product=False)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            if not self._cart_contains(driver, sku, product_name):
                return self._cart_result(
                    False, f'Item was not found in the Nintendo cart after clicking "{label}"',
                    driver=driver)

            logger.info(f"Nintendo SKU {sku} is in the cart")
            return self._cart_result(True, f"Product added to Nintendo cart{note}",
                                     driver=driver, cart_url=driver.current_url)
        except ProfileBusyError as e:
            return self._cart_result(False, str(e), driver=driver)
        except Exception as e:
            logger.error(f"Error adding Nintendo product to cart: {str(e)}", exc_info=True)
            return self._cart_result(False, f"Error: {str(e)}", driver=driver)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            profile.close()

    # ----------------------------------------------------------------- browser
    def _start_driver(self):
        """
        Launch Chrome on the Nintendo profile, translating the launch failure that
        profile contention produces into something the caller can report.

        The lock must already be held: Chrome will not run two instances against
        one --user-data-dir, and the loser dies with "chrome not reachable".
        """
        try:
            return uc.Chrome(options=self._get_chrome_options(), version_main=detect_chrome_major())
        except WebDriverException as e:
            message = str(e)
            if 'not reachable' in message or 'session not created' in message:
                raise ProfileBusyError(
                    f"Chrome could not start on the profile {self.profile_dir}. Another Chrome is most "
                    "likely using it - close any window opened from that profile and retry") from e
            raise

    def _get_chrome_options(self):
        """Get fresh ChromeOptions (reusing an options object raises in undetected-chromedriver)"""
        options = uc.ChromeOptions()
        options.add_argument(f'--user-data-dir={self.profile_dir}')
        options.add_argument('--profile-directory=Default')
        if os.environ.get('NINTENDO_HEADLESS', '1') != '0':
            options.add_argument('--headless=new')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1366,768')
        options.add_argument('--lang=en-US')
        return options

    @staticmethod
    def _cart_result(success, message, driver=None, cart_url=None):
        """Build the add_to_cart result dict, grabbing a screenshot while the driver is alive"""
        screenshot = None
        if driver is not None:
            try:
                screenshot = driver.get_screenshot_as_base64()
            except Exception as e:
                logger.debug(f"Could not take screenshot: {str(e)}")
        if not success:
            logger.warning(f"Nintendo add_to_cart failed: {message}")
        return {
            'success': success,
            'message': message,
            'cart_url': cart_url,
            'screenshot': screenshot
        }

    def _page_obstacle(self, driver, expect_product=True):
        """
        Return a failure message if the current page is a bot wall or a sign-in, else None.

        expect_product=False for the cart page: see SIZE_HEURISTIC_MARKER.
        """
        try:
            source = driver.page_source
        except Exception as e:
            return f"Could not read the Nintendo page: {str(e)}"

        block_reason = detect_block_page(source)
        if block_reason and not expect_product and SIZE_HEURISTIC_MARKER in block_reason:
            logger.debug(f"Ignoring {block_reason!r} on a page that is not a product page")
            block_reason = None
        if block_reason:
            return (f"Nintendo served a block page ({block_reason}); try again later or run with "
                    "NINTENDO_HEADLESS=0")
        if self._login_wall_present(driver):
            return ("Nintendo is asking for a Nintendo Account sign-in. Run once with "
                    "NINTENDO_HEADLESS=0, sign in to the window that opens (profile "
                    "~/.chrome_profiles/nintendo_profile), then retry. This scraper never signs in "
                    "and never goes past the cart")
        return None

    @staticmethod
    def _login_wall_present(driver):
        """
        Whether the browser has been sent to a Nintendo Account sign-in.

        Deliberately NOT body text. Nintendo's header renders "Log in / Sign up" on
        every page whether or not there is a session, so the Target-shaped "does the
        page mention signing in" test reports a login wall on a perfectly ordinary
        product page. What is unambiguous is the host - sign-in lives on
        accounts.nintendo.com - and a password field, which the store itself has none of.
        """
        try:
            host = (urlparse(driver.current_url or '').hostname or '').lower()
        except Exception:
            host = ''
        if any(host == login_host or host.endswith('.' + login_host) for login_host in LOGIN_HOSTS):
            return True
        try:
            return bool(driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]'))
        except Exception:
            return False

    # ---------------------------------------------------------------- buy box
    def _find_buy_button(self, driver, sku):
        """
        The add-to-cart / pre-purchase control for THIS sku.

        Returns (element, problem). `problem` is a message and means stop - the
        page is describing a different product, or the buy box holds only
        controls this scraper must not click. (None, None) means the page
        rendered no buy button at all, which the caller explains with
        _sold_out_reason().

        Scoping matters more here than on most stores: a Nintendo product page
        carries 30-odd cross-sell products, several of which are in stock and
        have their own buttons. The CTA is found structurally, as the button in
        the wrapper immediately before the add-to-cart drawer placeholder, and
        the page's single [data-ps-sku] anchor is checked first to prove the buy
        box belongs to the sku in the URL.
        """
        mismatch = self._sku_mismatch(driver, sku)
        if mismatch:
            return None, mismatch

        candidates = self._elements(driver, BUY_BUTTON_SELECTOR)
        if not candidates:
            candidates = self._elements(driver, BUY_BOX_SECTION_SELECTOR % sku)
        if not candidates:
            logger.debug("Neither buy-box selector matched anything")
            return None, None

        forbidden_seen = []
        for button in candidates:
            label = (self._text(button) or '').strip()
            lowered = label.lower()
            if any(bad in lowered for bad in FORBIDDEN_BUTTON_TEXTS):
                # "Buy now" and "Find retailers" are the third-party price-spider
                # widget in the same buy box; they send the shopper to another
                # retailer. Never clicked, and never counted as the CTA.
                forbidden_seen.append(label)
                continue
            if any(good in lowered for good in BUY_BUTTON_TEXTS):
                logger.debug(f"Nintendo buy button for {sku} reads {label!r}")
                return button, None
            if any(dead in lowered for dead in SOLD_OUT_TEXTS):
                logger.debug(f"Buy box for {sku} reads {label!r}")
                return button, None

        if forbidden_seen:
            return None, ("The Nintendo buy box offered only " +
                          ', '.join(f'"{label}"' for label in forbidden_seen) +
                          " - those are the third-party retailer links, not Nintendo's own "
                          "cart, so nothing was clicked")
        labels = ', '.join(repr((self._text(b) or '').strip()) for b in candidates[:5])
        logger.debug(f"No recognisable buy button for {sku}; candidates were {labels}")
        return None, None

    def _sku_mismatch(self, driver, sku):
        """
        A message when the buy box on screen belongs to a different product, else None.

        The price-spider widget carries data-ps-sku and there is exactly one per
        page - it is the only place in the rendered DOM that names the sku the buy
        box is for. If the page has none (a layout change, a slow hydrate) this
        says nothing rather than guessing; the sku is then only as good as the URL.
        """
        anchors = self._elements(driver, SKU_ANCHOR_SELECTOR)
        found = [(a.get_attribute('data-ps-sku') or '').strip() for a in anchors]
        found = [value for value in found if value]
        if not found:
            logger.debug("No [data-ps-sku] anchor on the page; cannot cross-check the buy box")
            return None
        if sku in found:
            return None
        return (f"The page's buy box is for SKU {', '.join(found)}, not {sku} from the URL - "
                "refusing to add a different product to the cart")

    def _sold_out_reason(self, driver):
        """Nintendo's own words for why the buy box cannot be used, or None"""
        try:
            body = driver.find_element(By.TAG_NAME, 'body').text.lower()
        except Exception:
            return None
        for phrase in SOLD_OUT_TEXTS:
            if phrase in body:
                return f'Nintendo reports "{phrase}"'
        return None

    @staticmethod
    def _elements(driver, selector):
        """find_elements that returns [] instead of raising on a selector the DOM rejects"""
        try:
            return driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception as e:
            logger.debug(f"Selector {selector!r} failed: {str(e)}")
            return []

    @staticmethod
    def _text(element):
        """An element's visible text, or '' when the driver will not give it up"""
        try:
            return element.text or ''
        except Exception:
            return ''

    # ------------------------------------------------------------ click / cart
    @staticmethod
    def _click(driver, element):
        """Scroll into view and click, falling back to a JS click when something overlays it"""
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"Could not scroll to the element ({str(e)}); clicking where it is")
        try:
            element.click()
        except Exception as e:
            logger.debug(f"Native click failed ({str(e)}); using JS click")
            driver.execute_script("arguments[0].click();", element)

    def _set_quantity(self, driver, quantity):
        """
        Raise the quantity stepper to `quantity` before adding. True if it got there.

        The stepper is a pair of +/- buttons with the count in an aria-live div;
        there is no input to type into, so this presses + and reads back. Nintendo
        caps quantity per product, so the + goes dead at the cap and the caller
        reports the quantity it actually got.
        """
        ups = self._elements(driver, QUANTITY_UP_SELECTOR)
        if not ups:
            logger.debug("No quantity stepper in the buy box")
            return False
        up = ups[0]
        for _ in range(max(0, quantity - 1)):
            if up.get_attribute('disabled') is not None or not up.is_enabled():
                break
            self._click(driver, up)
            time.sleep(0.4)
        current = self._current_quantity(driver)
        if current is None:
            logger.debug("Could not read the quantity back; assuming the stepper worked")
            return True
        if current != quantity:
            logger.warning(f"Nintendo quantity stepper stopped at {current}, wanted {quantity}")
        return current == quantity

    def _current_quantity(self, driver):
        """The number showing in the quantity stepper, or None"""
        for element in self._elements(driver, QUANTITY_VALUE_SELECTOR):
            digits = re.search(r'\d+', self._text(element))
            if digits:
                return int(digits.group(0))
        return None

    def _log_cart_drawer(self, driver):
        """
        Record what the add-to-cart drawer said. Read only - nothing in it is clicked.

        Nintendo answers a successful add with a side sheet whose markup could not
        be captured while writing this, because capturing it means putting a real
        item in someone's cart. Rather than guess at its buttons and risk clicking
        a checkout control inside an unobserved modal, the drawer's text is logged
        and the add is confirmed on the cart page instead.
        """
        for element in self._elements(driver, CART_DRAWER_SELECTOR):
            text = ' '.join(self._text(element).split())
            if text:
                logger.info(f"Nintendo cart drawer says: {text[:300]}")
                return
        logger.debug("No cart drawer content after the click")

    def _cart_contains(self, driver, sku, product_name=None):
        """
        Whether the cart page is showing this sku.

        Only positive evidence counts: the sku, or failing that an exact name
        match. Anything else - an empty cart, a cart holding other things, a
        page that is not the cart at all - is a no, so a failed add can never
        read as a success.

        The sku is looked for in the page source rather than the visible text,
        because the cart lists each line item as a link back to the product URL,
        which ends in -<sku>.
        """
        try:
            source = driver.page_source or ''
            body = driver.find_element(By.TAG_NAME, 'body').text
        except Exception as e:
            logger.debug(f"Could not read the Nintendo cart page: {str(e)}")
            return False

        lowered = body.lower()
        for marker in EMPTY_CART_MARKERS:
            if marker in lowered:
                logger.debug(f"Nintendo cart page says {marker!r}")
                return False

        if re.search(r'-%s(?=[/"?#\s])' % re.escape(sku), source):
            return True

        # Name fallback, for a cart layout that does not link back to the
        # product URL. It has to be an exact match on the normalised name, not a
        # substring: Nintendo's product names overlap heavily and share long
        # prefixes ("Nintendo Switch 2 ..."), and the cart page carries
        # cross-sell blocks, so a substring test would read a recommendation for
        # something else as the line item that was just added.
        wanted = self._normalised_name(product_name)
        if wanted:
            for candidate in self._cart_line_names(driver, body):
                if candidate == wanted:
                    logger.debug("Matched the cart line by product name rather than SKU")
                    return True

        logger.debug(f"SKU {sku} not found on the cart page")
        return False

    @staticmethod
    def _normalised_name(text):
        """Lowercase, punctuation flattened to single spaces - for comparing names."""
        return re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()

    def _cart_line_names(self, driver, body):
        """
        Names the cart page could be calling its line items.

        The text of every link back to a store product, which is what a real
        cart renders, plus each individual line of the page's own text as a
        fallback for a layout this has not been run against. Both are compared
        whole, so neither can match on a shared prefix.
        """
        names = [self._normalised_name(self._text(link))
                 for link in self._elements(driver, CART_PRODUCT_LINK_SELECTOR)]
        names.extend(self._normalised_name(line) for line in (body or '').splitlines())
        return [name for name in names if name]
