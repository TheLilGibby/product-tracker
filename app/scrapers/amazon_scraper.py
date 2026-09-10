"""
Amazon scraper.

Scraping is plain ``requests`` + BeautifulSoup. ``add_to_cart`` needs a real
browser and a signed-in session, so it drives undetected-chromedriver against a
persistent profile under ``~/.chrome_profiles/amazon_profile`` - the same
arrangement Best Buy and Target use - serialized on ``common.profile_lock``,
because Chrome refuses to run two instances against one ``--user-data-dir`` and
the loser dies at launch with an error that reads exactly like a bot wall.

Environment knobs (both optional):

``AMAZON_HEADLESS``       ``1`` (default) or ``0`` to show the Chrome window.
``CHROME_MAJOR_VERSION``  Force the chromedriver major version (see
                          ``common.detect_chrome_major``); otherwise it is
                          detected from the installed Chrome.
"""

import re
import logging
import os
import random
import requests
from bs4 import BeautifulSoup
from contextlib import contextmanager
from app.scrapers.common import (DEFAULT_HEADERS, PROFILE_LOCK_TIMEOUT, REQUEST_TIMEOUT, ProfileBusyError,
                                 detect_block_page, detect_chrome_major, is_preorder_text, profile_lock)
import time
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import base64
from io import BytesIO
from PIL import Image

# Set up logging
logger = logging.getLogger('app.scrapers.amazon')

# Amazon builds the buy box out of one of three feature divs, and which one it
# picks is the clearest statement of whether the listing can be ordered at all:
#   desktop_qualifiedBuyBox        - a real offer, in stock or open for pre-order
#   outOfStockBuyBox_feature_div   - "Currently unavailable"
#   unqualifiedBuyBox_feature_div  - no offer at all, only "See All Buying Options"
QUALIFIED_BUY_BOX_ID = 'desktop_qualifiedBuyBox'
UNORDERABLE_BUY_BOX_IDS = ('outOfStockBuyBox_feature_div', 'unqualifiedBuyBox_feature_div')

# Wording Amazon uses in #availability / #outOfStock when nothing can be ordered
UNAVAILABLE_MARKERS = (
    'currently unavailable',
    'temporarily out of stock',
    'out of stock',
    'currently not available',
    'no longer available',
    'not available for purchase',
)

# "in stock" as a claim of its own. Amazon's out-of-stock message ends "...we
# don't know when or if this item will be back in stock", and a plain substring
# test on that reported the Zelda 40th console as available.
IN_STOCK_RE = re.compile(r'(?<!back )\bin stock\b', re.IGNORECASE)

AMAZON_CART_URL = 'https://www.amazon.com/gp/cart/view.html'

# Every canonical Amazon product link carries the ASIN by format, so reading one
# back out is parsing rather than inference. Worth keeping it that way: a saved
# product page carries ~40 other ASINs in its cross-sell markup, and a page-wide
# scan for "an ASIN" would land on the right one only by document order.
# An ASIN is ten characters - B0-style for most things, a 10-digit ISBN for books.
ASIN_RE = re.compile(r'/(?:dp|gp/product|gp/aw/d|gp/offer-listing)/([A-Z0-9]{10})(?![A-Z0-9])',
                     re.IGNORECASE)

# The buy box's own Add to Cart. A product page carries several other
# add-to-cart forms - see _find_add_to_cart_button - and this id is the buy
# box's alone.
ADD_TO_CART_BUTTON_ID = 'add-to-cart-button'
BUY_NOW_BUTTON_ID = 'buy-now-button'

# Each add-to-cart form names what it will add in a hidden field. Confirmed
# against saved product pages for both an ordinary ASIN and a 10-digit ISBN one.
BUTTON_ASIN_SELECTOR = 'input[name="ASIN"], input[name="ASIN.0"], input[name="asin"]'

# Controls this scraper must never click, whatever the page offers. Each of
# these starts an order; adding to the cart is as far as the app goes, and the
# user places the order themselves.
NEVER_CLICK_IDS = ('buy-now-button', 'attach-sidesheet-checkout-button', 'sc-buy-box-ptc-button')

# Cart line items, scoped to the ACTIVE cart. The cart page also renders "Saved
# for later" and a recommendation rail, and an ASIN in either of those is not an
# item in the cart - the rail is quite capable of showing the very product whose
# add just failed.
CART_ACTIVE_SCOPES = ('#sc-active-cart', 'form[name="activeCartViewForm"]', 'div[data-name="Active Items"]')
CART_ITEM_SELECTOR = ', '.join(scope + ' [data-asin]' for scope in CART_ACTIVE_SCOPES)
CART_ITEM_LINK_SELECTOR = ', '.join(scope + ' a[href*="/dp/"]' for scope in CART_ACTIVE_SCOPES)

CART_EMPTY_RE = re.compile(r'your (?:amazon )?(?:shopping )?cart is empty', re.IGNORECASE)

DEFAULT_CHROME_MAJOR = 152


def _env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ('0', 'false', 'no', 'off', '')


class AmazonScraper:
    """Scraper specifically for Amazon products"""
    
    def __init__(self):
        """Initialize the Amazon scraper."""
        logger.debug("Initializing AmazonScraper")
        self.headers = dict(DEFAULT_HEADERS)

        # Shared with test_amazon_cart.py's --login helper so a one-time manual
        # sign-in survives between runs. Created on first launch, not here: the
        # scraping path is plain HTTP and never starts Chrome at all, and this
        # class is constructed once per scheduled check.
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "amazon_profile")
        self.headless = _env_flag('AMAZON_HEADLESS', True)
        self.chrome_major = None

    # ------------------------------------------------------------------ #
    # Chrome setup
    # ------------------------------------------------------------------ #
    def _get_chrome_options(self, headless=None):
        """Build fresh ChromeOptions for every launch (reusing an options object raises)."""
        headless = self.headless if headless is None else headless
        os.makedirs(self.profile_dir, exist_ok=True)
        options = uc.ChromeOptions()

        options.add_argument(f'--user-data-dir={self.profile_dir}')
        options.add_argument('--profile-directory=Default')

        if headless:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--disable-gpu')
        options.add_argument('--lang=en-US,en')

        width, height = random.choice([(1920, 1080), (1536, 864), (1440, 900), (1366, 768)])
        options.add_argument(f'--window-size={width},{height}')
        options.page_load_strategy = 'eager'
        return options

    def _launch(self, headless=None):
        """
        Start Chrome, retrying once with the browser-reported major version on a
        driver mismatch. The profile lock must already be held - go through
        _browser() rather than calling this directly.
        """
        if self.chrome_major is None:
            self.chrome_major = detect_chrome_major() or DEFAULT_CHROME_MAJOR
        version_main = self.chrome_major

        for attempt in range(2):
            try:
                driver = uc.Chrome(options=self._get_chrome_options(headless), version_main=version_main)
                driver.set_page_load_timeout(45)
                driver.set_script_timeout(20)
                logger.info(f"Chrome launched (version_main={version_main})")
                return driver
            except WebDriverException as e:
                match = re.search(r'Current browser version is (\d+)\.', str(e))
                if match and attempt == 0:
                    version_main = int(match.group(1))
                    self.chrome_major = version_main
                    logger.warning(f"chromedriver/Chrome mismatch; retrying with version_main={version_main}")
                    continue
                busy = self._profile_busy_error(e)
                if busy:
                    raise busy from e
                raise

    def _profile_busy_error(self, error):
        """
        ProfileBusyError for a launch failure caused by profile contention, else
        None. The lock keeps our own paths apart, but a Chrome the user opened on
        that profile by hand holds it too, and "chrome not reachable" otherwise
        gets reported as a bot wall.
        """
        message = str(error)
        if 'not reachable' in message or 'session not created' in message:
            return ProfileBusyError(
                f"Chrome could not start on the profile {self.profile_dir}; another Chrome is most "
                "likely using it - close any window opened from that profile and retry")
        return None

    @contextmanager
    def _browser(self, headless=None, timeout=PROFILE_LOCK_TIMEOUT):
        """
        One Chrome session on the shared amazon_profile, launch to quit.

        The driver has to die inside the lock: releasing it any earlier would let
        the next holder start Chrome while ours is still shutting down.

        Raises:
            ProfileBusyError: the profile did not come free within `timeout`
        """
        with profile_lock(self.profile_dir, timeout=timeout):
            driver = None
            try:
                driver = self._launch(headless)
                yield driver
            finally:
                self._quit(driver)

    @staticmethod
    def _quit(driver):
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    
    def scrape_product(self, url):
        """
        Scrape product information from Amazon URL
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            block_reason = detect_block_page(response.text)
            if block_reason:
                logger.warning(f"Amazon returned a block page for {url} (HTTP {response.status_code}): {block_reason}")
                return None
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            return {
                'name': self.extract_name(soup),
                'price': self.extract_price(soup),
                'available': self.extract_availability(soup),
                'image_url': self.extract_image_url(soup)
            }
        except Exception as e:
            logger.error(f"Error scraping Amazon product: {str(e)}")
            return None
    
    def add_to_cart(self, url, quantity=1):
        """
        Add an Amazon product to the cart, and verify that it actually landed there.

        Success requires this product's own ASIN among the ACTIVE cart's line
        items. Two things that look like proof and are not: the add-to-cart side
        sheet, which is rendered on the product page before the cart is written
        and survives a wall or a sign-in redirect afterwards; and "the cart has
        items in it", which the user's cart usually does anyway.

        Args:
            url: The product URL to add to cart
            quantity: Quantity to add to cart (default: 1)

        Returns:
            {'success': bool, 'message': str, 'cart_url': str|None, 'screenshot': base64|None}
        """
        quantity = max(1, int(quantity or 1))
        asin = self.extract_asin(url)
        if not asin:
            # Refused up front rather than clicked and then unverifiable: there
            # would be nothing to look for on the cart page afterwards.
            logger.warning(f"No ASIN in {url}; not adding a product the cart cannot be checked for")
            return {
                'success': False,
                'message': ("Could not read an ASIN from the product URL, so an add could not be "
                            "verified against the cart. Use the /dp/<ASIN> form of the link"),
                'cart_url': None,
                'screenshot': None,
            }

        logger.info(f"Adding Amazon product {asin} to cart (qty={quantity}): {url}")
        try:
            with self._browser() as driver:
                try:
                    return self._add_to_cart_with_driver(driver, url, asin, quantity)
                except Exception as e:
                    # Inside the session on purpose: a screenshot can only be
                    # taken while the driver is still alive.
                    logger.error(f"Error adding Amazon product to cart: {e}", exc_info=True)
                    return {
                        'success': False,
                        'message': f"Error adding to cart: {e}",
                        'cart_url': None,
                        'screenshot': self._take_screenshot(driver),
                    }
        except ProfileBusyError as e:
            logger.warning(f"Not adding to cart: {e}")
            return {
                'success': False,
                'message': f"Chrome profile is busy: {e}",
                'cart_url': None,
                'screenshot': None,
            }
        except Exception as e:
            # Chrome never started, so there is no driver to photograph.
            logger.error(f"Could not start Chrome for the Amazon cart: {e}", exc_info=True)
            return {
                'success': False,
                'message': f"Error: {e}",
                'cart_url': None,
                'screenshot': None,
            }

    def _add_to_cart_with_driver(self, driver, url, asin, quantity):
        """The cart flow with the driver supplied, so the fakes can drive it."""
        logger.debug(f"Navigating to {url}")
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        blocked = detect_block_page(driver.page_source)
        if blocked:
            logger.warning(f"Amazon served a block page for {url}: {blocked}")
            return {
                'success': False,
                'message': f"Amazon served a block page instead of the product ({blocked})",
                'cart_url': None,
                'screenshot': self._take_screenshot(driver),
            }

        if quantity > 1:
            self._set_quantity(driver, quantity)

        button, reason = self._find_add_to_cart_button(driver, asin)
        if button is None:
            logger.warning(f"Not adding {asin} to cart: {reason}")
            return {
                'success': False,
                'message': reason,
                'cart_url': None,
                'screenshot': self._take_screenshot(driver),
            }

        button.click()
        logger.info(f"Clicked the buy box's Add to Cart for {asin}")

        acknowledged = self._await_click_acknowledgement(driver)
        return self._verify_cart(driver, asin, quantity, acknowledged)

    def _find_add_to_cart_button(self, driver, asin):
        """
        (button, None) for the buy box's own Add to Cart, or (None, reason).

        Two gates, in this order.

        The id. A product page renders several add-to-cart forms: the buy box's,
        `add-to-cart-button-ubb` for the other-sellers box, and
        `add-to-cart-item-0/1/2` for the "Buy it with" bundle, whose items are
        different products. A saved page here carries six <form id="addToCart">.
        `#add-to-cart-button` is the buy box's alone - but that is a naming
        convention, so:

        The ASIN. Each of those forms carries a hidden field naming what it
        adds, which is what actually attributes a button to a product. A foreign
        ASIN is proof the button belongs to something else and is never a
        candidate; one naming the right ASIN beats one naming none.

        A button naming no ASIN at all is still usable - the hidden field is
        markup Amazon could rename - but only when nothing better is on the page.
        """
        buttons = driver.find_elements(By.ID, ADD_TO_CART_BUTTON_ID)
        if not buttons:
            # Both of these are clean failures, and neither is retried by
            # reaching for another control: #buy-now-button starts an order, so
            # it is on NEVER_CLICK_IDS and is only ever read for the message.
            if driver.find_elements(By.ID, BUY_NOW_BUTTON_ID):
                return None, "Product offers Buy Now but no Add to Cart, so it was left alone"
            return None, ("No Add to Cart button on the page - the listing is unavailable, or sold "
                          "by a third party with no Amazon offer")

        fallback = None
        for button in buttons:
            if not (button.is_enabled() and button.is_displayed()):
                logger.debug("Ignoring a disabled or hidden add-to-cart button")
                continue
            found = self._asin_from_button(button)
            if found == asin:
                return button, None
            if found:
                logger.debug(f"Ignoring an add-to-cart button: it adds ASIN {found}, not {asin}")
                continue
            if fallback is None:
                fallback = button

        if fallback is not None:
            logger.debug(f"Using an add-to-cart button that names no ASIN for {asin}")
            return fallback, None
        return None, f"No usable Add to Cart button on the page adds ASIN {asin}"

    @staticmethod
    def _asin_from_button(button):
        """
        The ASIN this button's own form will add, or None.

        <form id="addToCart"> carries <input type="hidden" name="ASIN" value="...">.
        Confirmed against saved product pages for both an ordinary ASIN
        (B09B8V1LZ3) and a 10-digit ISBN one (1546179437).
        """
        try:
            form = button.find_element(By.XPATH, 'ancestor::form[1]')
        except (NoSuchElementException, WebDriverException):
            return None
        for field in form.find_elements(By.CSS_SELECTOR, BUTTON_ASIN_SELECTOR):
            value = (field.get_attribute('value') or '').strip().upper()
            if value:
                return value
        return None

    @staticmethod
    def _await_click_acknowledgement(driver):
        """
        True when Amazon acknowledged the click on the product page.

        Logged, never returned as success. The side sheet is rendered before the
        cart is written, so a wall or a sign-in redirect on the cart afterwards
        carries it just as happily as a real add. Its checkout button is on
        NEVER_CLICK_IDS: this scraper adds to the cart and stops there.
        """
        try:
            WebDriverWait(driver, 10).until(EC.any_of(
                EC.presence_of_element_located((By.ID, 'attach-sidesheet-checkout-button')),
                EC.presence_of_element_located((By.ID, 'sw-atc-details-single-container')),
                EC.presence_of_element_located((By.ID, 'NATC_SMART_WAGON_CONF_MSG_SUCCESS')),
            ))
            logger.debug("Amazon acknowledged the add on the product page")
            return True
        except TimeoutException:
            logger.warning("No add-to-cart confirmation on the product page; checking the cart anyway")
            return False

    def _verify_cart(self, driver, asin, quantity, acknowledged):
        """
        Read the cart and decide, on positive evidence only.

        Every failure below is a distinct message on purpose: at a drop the log
        line is all anyone gets, and "blocked", "empty", "holds something else"
        and "could not read the cart" call for four different responses.
        """
        driver.get(AMAZON_CART_URL)
        page = driver.page_source
        screenshot = self._take_screenshot(driver)
        cart_url = driver.current_url

        blocked = detect_block_page(page, expect_product=False)
        if blocked:
            logger.warning(f"Amazon served a block page on the cart ({blocked}); cannot verify the add")
            return {
                'success': False,
                'message': (f"Amazon served a block page on the cart ({blocked}), so the add could not "
                            "be verified. The item may or may not be in the cart - check it before retrying"),
                'cart_url': cart_url,
                'screenshot': screenshot,
            }

        # The one positive test, run first and on its own: the ASIN is a line
        # item in the active cart. Everything below it only decides which
        # failure to report, so no wording on the page can turn into a success.
        in_cart = self._cart_asins(driver)
        if in_cart and asin in in_cart:
            return {
                'success': True,
                'message': f"Successfully added {quantity} item(s) to the Amazon cart",
                'cart_url': cart_url,
                'screenshot': screenshot,
            }

        if CART_EMPTY_RE.search(page):
            logger.warning(f"Add to cart not verified: the cart is empty "
                           f"(acknowledged on the product page: {acknowledged})")
            return {
                'success': False,
                'message': "Item did not appear in the cart (the cart is empty)",
                'cart_url': cart_url,
                'screenshot': screenshot,
            }

        if in_cart is None:
            # Not "the item is missing": the cart section this reads was not
            # found at all, so there is no evidence either way. Fails closed,
            # and says which of the two things went wrong.
            logger.warning("No recognisable cart line items on the cart page; cannot verify the add")
            return {
                'success': False,
                'message': ("Could not find the cart's line items, so the add could not be verified. "
                            "Check the cart before retrying"),
                'cart_url': cart_url,
                'screenshot': screenshot,
            }

        logger.warning(f"Add to cart not verified: the cart holds {in_cart or 'nothing'} but not "
                       f"{asin} (acknowledged on the product page: {acknowledged})")
        return {
            'success': False,
            'message': f"Item did not appear in the cart (it does not list ASIN {asin})",
            'cart_url': cart_url,
            'screenshot': screenshot,
        }

    def _cart_asins(self, driver):
        """
        The ASINs of the ACTIVE cart's line items, or None when the page carries
        no cart section this recognises.

        Scoped deliberately. The cart page also renders "Saved for later" and a
        recommendation rail; an ASIN in either is not an item in the cart, and
        the rail is quite capable of showing the very product whose add just
        failed. Counting line items instead - which is what this method
        replaces - answers "is there anything in the cart", which is true of
        most people's carts before the app touches them.
        """
        asins = set()
        found_cart = False

        for element in driver.find_elements(By.CSS_SELECTOR, CART_ITEM_SELECTOR):
            found_cart = True
            value = (element.get_attribute('data-asin') or '').strip().upper()
            if value:
                asins.add(value)

        # Amazon has moved the item's ASIN between a data attribute and the
        # product link more than once; both are inside the active cart.
        for link in driver.find_elements(By.CSS_SELECTOR, CART_ITEM_LINK_SELECTOR):
            found_cart = True
            value = self.extract_asin(link.get_attribute('href') or '')
            if value:
                asins.add(value)

        return sorted(asins) if found_cart else None

    @staticmethod
    def extract_asin(url):
        """The ASIN out of an Amazon product URL, uppercased, or None."""
        match = ASIN_RE.search(url or '')
        return match.group(1).upper() if match else None

    def _set_quantity(self, driver, quantity):
        """Best-effort quantity select on the product page. A failure here is not fatal."""
        try:
            logger.debug(f"Setting quantity to {quantity}")
            quantity_dropdown = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "quantity"))
            )
            quantity_dropdown.click()
            time.sleep(1)

            # Amazon's dropdown only offers up to 10
            if quantity <= 10:
                quantity_option = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, f"//select[@id='quantity']/option[@value='{quantity}']"))
                )
                quantity_option.click()
                time.sleep(1)
        except (TimeoutException, NoSuchElementException, WebDriverException) as e:
            logger.warning(f"Could not set quantity: {e}")

    def _take_screenshot(self, driver):
        """Take a screenshot and convert it to base64 for embedding in HTML"""
        try:
            # Take screenshot and convert to base64
            screenshot = driver.get_screenshot_as_base64()
            return screenshot
        except Exception as e:
            logger.error(f"Error taking screenshot: {str(e)}")
            return None
    
    def extract_name(self, soup):
        """Extract product name from Amazon page"""
        logger.debug("AmazonScraper: Extracting name")
        try:
            title_element = soup.find(id='productTitle')
            if title_element:
                name = title_element.get_text().strip()
                logger.debug(f"Found name: {name}")
                return name
                
            # Try meta title
            meta_title = soup.find('meta', {'property': 'og:title'})
            if meta_title and meta_title.get('content'):
                name = meta_title.get('content').strip()
                logger.debug(f"Found name from meta: {name}")
                return name
                
            # Fallback to any h1
            title = soup.find('h1')
            if title:
                name = title.get_text().strip()
                logger.debug(f"Found name from h1: {name}")
                return name
            
            logger.warning("Could not find product name")
            return "Unknown Product"
        except Exception as e:
            logger.error(f"Error extracting name: {str(e)}")
            return "Unknown Product"
    
    def extract_price(self, soup):
        """Extract product price from Amazon page"""
        logger.debug("AmazonScraper: Extracting price")
        try:
            # Check multiple possible price elements (Amazon changes these often)
            price_elements = [
                soup.find(id='priceblock_ourprice'),
                soup.find(id='priceblock_dealprice'),
                soup.select_one('.a-price .a-offscreen'),
                soup.select_one('#price_inside_buybox'),
                soup.select_one('#newBuyBoxPrice')
            ]
            
            for element in price_elements:
                if element:
                    price_text = element.get_text().strip()
                    price_match = re.search(r'(\d+\,)?\d+\.\d{2}', price_text)
                    if price_match:
                        # Remove commas and convert to float
                        price = float(price_match.group(0).replace(',', ''))
                        logger.debug(f"Found price: ${price}")
                        return price
            
            logger.warning("Could not find product price")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None
    
    def extract_availability(self, soup):
        """
        Extract product availability from an Amazon page.

        "Available" means the listing can be ordered right now - in stock, or open
        for pre-order. A listing Amazon is not selling reads False even when the
        page still carries a price, a hidden add-to-cart form or a marketplace
        offer under "See All Buying Options".
        """
        logger.debug("AmazonScraper: Extracting availability")
        try:
            # 1. Which buy box Amazon rendered. The most reliable signal there is,
            #    and it does not depend on wording.
            if soup.find(id=QUALIFIED_BUY_BOX_ID) is None:
                for box_id in UNORDERABLE_BUY_BOX_IDS:
                    if soup.find(id=box_id) is not None:
                        logger.debug(f"Page rendered #{box_id} and no qualified buy box; not orderable")
                        return False

            # 2. What the page says. Checked before the buttons: Amazon keeps
            #    rendering add-to-cart markup on listings it will not sell.
            text = self._availability_text(soup)
            marker = next((m for m in UNAVAILABLE_MARKERS if m in text), None)
            if marker:
                logger.debug(f"Availability text says {marker!r}, product is not orderable")
                return False

            # 3. A live buy box button. #availability alone reads pre-order /
            #    "will be released" wording as out of stock, so this comes first
            #    among the positive signals.
            button = self._buy_box_button(soup)
            if button is not None:
                logger.debug(f"Found a live #{button.get('id')}, product is available")
                return True

            if not text:
                logger.warning("Could not determine product availability")
                return False
            if is_preorder_text(text):
                logger.debug("Availability text says pre-order, treating as available")
                return True
            available = bool(IN_STOCK_RE.search(text))
            logger.debug(f"Found availability from availability element: {available}")
            return available
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False

    @staticmethod
    def _availability_text(soup):
        """Lowercased text of the availability blocks, or '' when the page has none."""
        parts = []
        for element_id in ('availability', 'outOfStock'):
            element = soup.find(id=element_id)
            if element:
                parts.append(element.get_text(' ', strip=True))
        return ' '.join(parts).strip().lower()

    def _buy_box_button(self, soup):
        """
        The add-to-cart / buy-now control, but only when it is a live button in a
        buy box that is actually selling something. Returns None otherwise.
        """
        for button_id in ('add-to-cart-button', 'buy-now-button'):
            for button in soup.find_all(id=button_id):
                reason = self._button_dead_reason(button)
                if reason:
                    logger.debug(f"Ignoring #{button_id}: {reason}")
                    continue
                return button
        return None

    @staticmethod
    def _button_dead_reason(button):
        """
        Why this buy-box button should not count as an offer, or None if it counts.

        Amazon disables the control with either the `disabled` attribute or the
        a-button-disabled class on the wrapper span, hides the whole form with
        aok-hidden, and keeps a full add-to-cart form inside the unqualified buy
        box on listings that have no offer at all.
        """
        if button.has_attr('disabled') or button.get('aria-disabled') == 'true':
            return 'disabled'
        for element in [button] + list(button.parents):
            if not hasattr(element, 'get'):
                continue
            if (element.get('id') or '') in UNORDERABLE_BUY_BOX_IDS:
                return f"inside #{element.get('id')}"
            classes = element.get('class') or []
            if 'a-button-disabled' in classes:
                return 'disabled button widget'
            if 'aok-hidden' in classes or 'a-hidden' in classes:
                return 'inside a hidden container'
            if 'display:none' in (element.get('style') or '').replace(' ', ''):
                return 'inside a display:none container'
        return None
    
    def extract_image_url(self, soup):
        """Extract product image URL from Amazon page"""
        logger.debug("AmazonScraper: Extracting image URL")
        try:
            img = soup.find(id='landingImage')
            if img:
                image_url = img.get('src') or img.get('data-old-hires')
                if image_url:
                    logger.debug(f"Found image URL from landingImage: {image_url}")
                    return image_url
                
            # Try another common image ID
            img = soup.find(id='imgBlkFront')
            if img:
                image_url = img.get('src') or img.get('data-a-dynamic-image')
                if image_url:
                    logger.debug(f"Found image URL from imgBlkFront: {image_url}")
                    return image_url
            
            # Try meta image
            meta_img = soup.find('meta', {'property': 'og:image'})
            if meta_img and meta_img.get('content'):
                image_url = meta_img.get('content')
                logger.debug(f"Found image URL from meta: {image_url}")
                return image_url
            
            logger.warning("Could not find product image URL")
            return None
        except Exception as e:
            logger.error(f"Error extracting image URL: {str(e)}")
            return None 