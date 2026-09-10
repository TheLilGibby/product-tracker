"""
Target scraper.

Target only server-renders the buy box for real browsers: the HTML a plain
`requests` client receives is a stripped page with item metadata (title, image,
street date) but no price or stock, and Redsky - Target's public product API -
answers non-browser clients with a captcha challenge (HTTP 403, JSON body with
"captchaRelativeURL"). So this scraper:

1. Tries the Redsky JSON API first (cheap, and it works from some networks).
2. Falls back to undetected-chromedriver and reads the rendered DOM: the price
   sits in span[data-test="product-price"] and the buy button is
   button#addToCartButtonOrTextIdFor<tcin> (data-test preorderButton /
   preorderButtonDisabled / shippingButton / ...).
3. Returns None when neither path yields product data, so callers keep the
   stored name/price/availability instead of writing placeholders.

Redsky API key
--------------
Target's web bundle embeds a public API key that has been stable for years.
If Redsky ever starts answering with a JSON error about the key, re-extract it
from any product page:

    re.search(r'(?:apiKey|key)["\\']?\\s*[:=]\\s*["\\']([0-9a-f]{40})', html)

The TCIN (Target item number) is the trailing /A-<tcin> segment of the URL.

The saved session (~/.chrome_profiles/target_profile/cookies.json)
------------------------------------------------------------------
Redsky's 403 is PerimeterX refusing a client with no clearance token, not Target
withholding the data - the same request carrying a valid _px3 is answered 200. So
`test_target_cart.py --import-cookies` now writes the browser's target.com
cookies to cookies.json beside the profile they came from - opened 0600, though
on Windows that mode is ignored and the file inherits the profile directory's
ACL instead - and step 1 loads them. It lives with the profile rather than in
the checkout because the repo has several worktrees and the session belongs to
neither of them; being outside the repo, it also cannot be committed. While that session is good, tracking Target costs one HTTP
request and never opens Chrome; the profile is only touched for cart attempts,
which is also the only thing that can get it flagged.

PX tokens are short-lived, so the jar going cold is the normal case, not a
failure: an expired cookie is dropped on load and a rejected one is flagged
stale, and either way step 2 runs exactly as it does today. Nothing here solves
a challenge - it carries a session the user established themselves.
"""

import re
import html
import logging
import os
import time
from contextlib import ExitStack
import requests
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from app.scrapers.common import (DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text,
                                 detect_chrome_major, profile_lock, ProfileBusyError,
                                 load_cookie_jar, mark_cookie_jar_stale)

# Set up logging
logger = logging.getLogger('app.scrapers.target')

# Public key from Target's web bundle (see module docstring)
REDSKY_API_KEY = '9f36aeafbe60771e321a7cc95a78140772ab3e96'
REDSKY_PDP_URL = 'https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1'
# Pricing is national for the items we track; any store id satisfies the API
REDSKY_STORE_ID = '3991'

# Redsky shipping availability_status values that mean the item can be ordered
REDSKY_ORDERABLE_STATUSES = ('IN_STOCK', 'PRE_ORDER_SELLABLE', 'LIMITED_STOCK')

# Responses that mean the saved session is no longer accepted, as opposed to
# Redsky being briefly unwell. A 5xx or a timeout leaves the jar alone.
REDSKY_STALE_SESSION_STATUSES = (401, 403)

# The persistent Chrome profile Target's bot checks are meant to recognise.
TARGET_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "target_profile")

# Where --import-cookies leaves the session for the requests path to reuse. It sits
# beside the profile rather than in the checkout, because it IS that profile's
# session: the repo is cloned into several worktrees, and a per-checkout path meant
# importing from one and running the app from another silently wrote the session
# where nothing would look for it. Being outside the repo entirely, it also cannot
# be committed by any branch. TARGET_COOKIE_JAR overrides it.
TARGET_COOKIE_JAR = os.environ.get('TARGET_COOKIE_JAR') or os.path.join(
    TARGET_PROFILE_DIR, 'cookies.json')

# Redsky is an XHR from the product page. With a cookie jar attached the request
# has to look like that XHR and not like a bare script, so it carries the client
# hints and fetch metadata a Chrome tab would send. These are only added on the
# cookie path: sending them without cookies changes nothing (Redsky 403s a bare
# client either way) and would only make the plain attempt harder to read in logs.
REDSKY_BROWSER_HEADERS = {
    'sec-ch-ua': '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-site',
}

TCIN_RE = re.compile(r'/A-(\d+)')
PRICE_RE = re.compile(r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)')

# Buy-box buttons on the rendered page, in preference order
BUY_BUTTON_SELECTORS = (
    'button[id^="addToCartButtonOrTextIdFor"]',
    '[data-test="shippingButton"]',
    '[data-test="orderPickupButton"]',
    '[data-test="preorderButton"]',
    '[data-test="addToCartButton"]',
)
ORDERABLE_BUTTON_TEXTS = ('add to cart', 'ship it', 'pick up', 'pickup', 'add for')

TARGET_CART_URL = 'https://www.target.com/cart'

# Side-sheet controls that appear after "Add to cart": decline add-ons, confirm a
# fulfillment choice. Nothing here proceeds to checkout.
SIDE_SHEET_DISMISS_SELECTORS = (
    '[data-test="espModalDeclineButton"]',
    '[data-test="declineCoverage"]',
    '[data-test="content-wrapper"] button[data-test="shippingButton"]',
)
SIDE_SHEET_DISMISS_TEXTS = {'decline coverage', 'no thanks', 'decline', 'continue', 'ship it', 'add to cart', 'view cart'}

# Target intermittently serves a PerimeterX press-and-hold challenge instead of the
# page (most often on /cart, and more readily to an aged automation profile). It is
# only detected here so the caller fails cleanly - solving it is a person's job, in a
# visible window (TARGET_HEADLESS=0).
CHALLENGE_TEXT_MARKERS = ('press & hold', 'press and hold', 'quick verification', "confirm you're a human")
# The challenge lives in this iframe. It exists on ordinary pages too, so its presence
# is not the signal - its contents are.
CHALLENGE_IFRAME_SELECTOR = 'iframe[id*="px-captcha"]'


class TargetScraper:
    """Scraper for Target products: Redsky JSON API with an undetected-chromedriver fallback"""

    def __init__(self):
        """Initialize the Target scraper."""
        logger.debug("Initializing TargetScraper")
        self.headers = dict(DEFAULT_HEADERS)

        # Persistent profile so Target's bot checks see a returning browser
        self.profile_dir = TARGET_PROFILE_DIR
        os.makedirs(self.profile_dir, exist_ok=True)

        # Store the most recent product URL for parity with the other browser scrapers
        self.current_product_url = None

    @staticmethod
    def extract_tcin(url):
        """Return the TCIN from a Target product URL (the /A-<tcin> segment), or None"""
        match = TCIN_RE.search(url or '')
        return match.group(1) if match else None

    def scrape_product(self, url):
        """
        Scrape product information from a Target URL

        Args:
            url: The product URL to scrape

        Returns:
            dict: Product information including name, price, availability, and image URL,
                  or None if nothing usable could be extracted
        """
        self.current_product_url = url

        tcin = self.extract_tcin(url)
        if not tcin:
            logger.error(f"Could not find a TCIN (/A-<number>) in Target URL: {url}")
            return None
        logger.info(f"Extracted TCIN: {tcin}")

        # 1. Redsky JSON API
        try:
            product_data = self.scrape_via_redsky(tcin, url)
            if product_data:
                return product_data
        except Exception as e:
            logger.error(f"Error scraping Target product via Redsky: {str(e)}")

        # 2. Rendered page via undetected-chromedriver
        try:
            product_data = self.scrape_via_browser(url, tcin)
            if product_data:
                return product_data
        except Exception as e:
            logger.error(f"Error scraping Target product via browser: {str(e)}")

        logger.warning(f"Could not extract Target product data for {url}; leaving stored data unchanged")
        return None

    # ------------------------------------------------------------------ Redsky
    def scrape_via_redsky(self, tcin, url):
        """
        Fetch the product from Redsky's pdp_client_v1 aggregation.

        Returns:
            dict, or None when Redsky blocks the client or returns no product
        """
        params = {
            'key': REDSKY_API_KEY,
            'tcin': tcin,
            'pricing_store_id': REDSKY_STORE_ID,
            'has_pricing_store_id': 'true',
            'channel': 'WEB',
            'page': f'/p/A-{tcin}',
        }
        headers = dict(self.headers)
        headers.update({
            'Accept': 'application/json',
            'Origin': 'https://www.target.com',
            'Referer': url,
        })

        # The user's imported session, if --import-cookies has left one. With a
        # valid _px3 Redsky answers 200 and no browser is needed for tracking at
        # all; without one it 403s exactly as it does today and the caller falls
        # through to the browser.
        cookies = self._redsky_cookies()
        if cookies is not None:
            headers.update(REDSKY_BROWSER_HEADERS)
            logger.info(f"Attempting Redsky API for TCIN {tcin} with the saved session "
                        f"({len(cookies)} cookies)")
        else:
            logger.info(f"Attempting Redsky API for TCIN {tcin} (no saved session)")

        response = requests.get(REDSKY_PDP_URL, params=params, headers=headers,
                                cookies=cookies, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            # 403 with a "captchaRelativeURL" body is Target's bot wall for non-browser clients
            logger.warning(f"Redsky returned HTTP {response.status_code} for TCIN {tcin}: {response.text[:120]!r}")
            if cookies is not None and response.status_code in REDSKY_STALE_SESSION_STATUSES:
                # The cookies were rejected, so they will be rejected next cycle too.
                # Flag them once and let every later check go straight to the browser
                # rather than spending a doomed request on them every minute.
                mark_cookie_jar_stale(TARGET_COOKIE_JAR)
            return None

        payload = response.json()
        product = (payload.get('data') or {}).get('product') or {}
        item = product.get('item') or {}

        # Redsky returns raw HTML-entity text: "Nintendo&#8482; Switch 2 ..."
        name = html.unescape(((item.get('product_description') or {}).get('title') or '')).strip()
        if not name:
            logger.warning(f"Redsky response for TCIN {tcin} has no product title")
            return None

        available = self._availability_from_redsky(product)
        if available is None:
            # A name and a price with a guessed availability is worse than no
            # answer: the caller would store the guess. Hand the whole product
            # to the browser path instead.
            logger.warning(f"Redsky gave no availability for TCIN {tcin}; deferring to the browser")
            return None

        price = self._price_from_redsky(product.get('price') or {})
        image_url = ((item.get('enrichment') or {}).get('images') or {}).get('primary_image_url')
        if image_url:
            image_url = html.unescape(image_url)

        logger.info(f"Redsky extracted: name={name}, price={price}, available={available}")
        return {
            'name': name,
            'price': price,
            'available': available,
            'image_url': image_url
        }

    @staticmethod
    def _redsky_cookies():
        """
        Build a requests cookie jar from the session --import-cookies saved, or
        return None when there is no usable one.

        None means "ask the browser", never "the product is unavailable".

        Domain scoping is done properly rather than by shoving every cookie at
        every host: _px3 is set on .target.com and must reach redsky.target.com,
        while a www.target.com cookie must not. http.cookiejar decides that from
        the leading dot, so a dotless "target.com" - which some exporters write,
        and which parse_cookie_file deliberately produces for chromedriver - is
        promoted back to ".target.com" here. Without that promotion the one
        cookie that matters would be silently left behind and every request
        would 403 with a jar that looks perfectly healthy on disk.
        """
        saved = load_cookie_jar(TARGET_COOKIE_JAR)
        if not saved:
            return None

        jar = requests.cookies.RequestsCookieJar()
        for cookie in saved:
            domain = cookie.get('domain') or ''
            if domain.lstrip('.') == 'target.com':
                domain = '.target.com'
            try:
                jar.set_cookie(requests.cookies.create_cookie(
                    name=cookie['name'],
                    value=cookie.get('value') or '',
                    domain=domain,
                    path=cookie.get('path') or '/',
                    secure=bool(cookie.get('secure')),
                    expires=cookie.get('expires'),
                ))
            except Exception as e:
                # One malformed entry must not cost us the whole session
                logger.debug(f"Skipping saved cookie {cookie.get('name')!r}: {str(e)}")
        if not len(jar):
            logger.warning("The saved Target session held no usable cookies; using the browser path")
            return None
        return jar

    @staticmethod
    def _price_from_redsky(price):
        """Pick the numeric current price out of Redsky's price object"""
        for key in ('current_retail', 'current_retail_min'):
            value = price.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        formatted = price.get('formatted_current_price') or ''
        match = PRICE_RE.search(formatted)
        if match:
            return float(match.group(1).replace(',', ''))
        logger.debug("No current price in Redsky price object")
        return None

    @staticmethod
    def _availability_from_redsky(product):
        """
        Derive availability from Redsky's fulfillment block, or return None when
        the payload does not say.

        None is not False. Redsky answers 200 for these tcins with no
        product.fulfillment key at all, and reporting that as "unavailable"
        would mark an orderable item sold out on every cheap-path cycle - and
        then fire a bogus back-in-stock alert the moment the browser path
        disagreed. Unknown belongs to the browser path, which can actually see
        the buy box.

        Pre-order sellable counts as available.
        """
        fulfillment = product.get('fulfillment') or {}
        if fulfillment.get('sold_out') is True:
            logger.debug("Redsky fulfillment.sold_out is true")
            return False

        shipping = fulfillment.get('shipping_options') or {}
        status = (shipping.get('availability_status') or '').upper()
        if status:
            available = status in REDSKY_ORDERABLE_STATUSES
            logger.debug(f"Redsky shipping availability_status={status} -> available={available}")
            return available

        if fulfillment.get('is_out_of_stock_in_all_store_locations') is False:
            logger.debug("Redsky reports stock in stores")
            return True

        logger.warning(
            "Could not determine availability from Redsky fulfillment data; "
            f"product keys={sorted(product.keys())}, fulfillment keys={sorted(fulfillment.keys())}")
        return None

    # ----------------------------------------------------------------- browser
    def _start_driver(self):
        """
        Launch Chrome on the shared profile, translating the launch failure that
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
        if os.environ.get('TARGET_HEADLESS', '1') != '0':
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

    def scrape_via_browser(self, url, tcin):
        """
        Render the product page with undetected-chromedriver and parse the DOM.

        Returns:
            dict, or None if the page did not render a product
        """
        logger.info(f"Attempting browser scrape for Target TCIN {tcin}")
        driver = None
        html = None
        challenge = None
        profile = ExitStack()
        try:
            profile.enter_context(profile_lock(self.profile_dir))
        except ProfileBusyError as e:
            logger.warning(f"Skipping browser scrape for Target TCIN {tcin}: {str(e)}")
            return None
        try:
            driver = self._start_driver()
            driver.set_page_load_timeout(45)
            driver.get(url)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '[data-test="product-price"], [data-test="product-title"]'))
                )
            except TimeoutException:
                logger.warning("Timed out waiting for Target product markup; parsing whatever rendered")
            # The buy box hydrates shortly after first paint
            time.sleep(2)
            html = driver.page_source
            # Must be read before the driver is torn down below
            challenge = self._challenge_present(driver)
        except ProfileBusyError as e:
            logger.warning(f"Could not start Chrome for Target TCIN {tcin}: {str(e)}")
            return None
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            profile.close()

        if challenge:
            logger.warning(f"Target showed a human-verification challenge ({challenge}) for {url}; "
                           "run test_target_cart.py --login to clear it in a visible window")
            return None

        if html is None:
            # uc.Chrome() or driver.get() raised before the page source was captured
            logger.warning(f"Browser did not return a page for {url}")
            return None

        block_reason = detect_block_page(html)
        if block_reason:
            logger.warning(f"Target returned a block page in the browser for {url}: {block_reason}")
            return None

        soup = BeautifulSoup(html, 'html.parser')
        return self.extract_from_rendered_html(soup, tcin)

    def extract_from_rendered_html(self, soup, tcin):
        """Build the product dict from a browser-rendered Target page"""
        name = self.extract_name(soup)
        if not name or name == "Unknown Product":
            logger.warning("Rendered Target page has no product title")
            return None

        product_data = {
            'name': name,
            'price': self.extract_price(soup),
            'available': self.extract_availability(soup, tcin),
            'image_url': self.extract_image_url(soup)
        }
        logger.info(f"Browser extracted: name={name}, price={product_data['price']}, available={product_data['available']}")
        return product_data

    def extract_name(self, soup):
        """Extract product name from a Target page"""
        logger.debug("TargetScraper: Extracting name")
        try:
            title_element = soup.select_one('h1[data-test="product-title"]')
            if title_element:
                name = title_element.get_text(" ", strip=True)
                if name:
                    logger.debug(f"Found name: {name}")
                    return name

            meta_title = soup.find('meta', {'property': 'og:title'})
            if meta_title and meta_title.get('content'):
                name = meta_title.get('content').strip()
                logger.debug(f"Found name from meta: {name}")
                return name

            title = soup.find('h1')
            if title:
                name = title.get_text(" ", strip=True)
                logger.debug(f"Found name from h1: {name}")
                return name

            logger.warning("Could not find product name")
            return "Unknown Product"
        except Exception as e:
            logger.error(f"Error extracting name: {str(e)}")
            return "Unknown Product"

    def extract_price(self, soup):
        """Extract product price from a Target page"""
        logger.debug("TargetScraper: Extracting price")
        try:
            for element in soup.select('[data-test="product-price"]'):
                match = PRICE_RE.search(element.get_text(" ", strip=True))
                if match:
                    price = float(match.group(1).replace(',', ''))
                    logger.debug(f"Found price: ${price}")
                    return price

            logger.warning("Could not find product price")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None

    def extract_availability(self, soup, tcin=None):
        """Extract product availability from the buy-box button of a Target page"""
        logger.debug("TargetScraper: Extracting availability")
        try:
            button = None
            if tcin:
                button = soup.select_one(f'button#addToCartButtonOrTextIdFor{tcin}')
            for selector in BUY_BUTTON_SELECTORS:
                if button is not None:
                    break
                button = soup.select_one(selector)

            if button is not None:
                data_test = (button.get('data-test') or '').lower()
                text = button.get_text(" ", strip=True).lower()
                disabled = button.has_attr('disabled') or 'disabled' in data_test
                logger.debug(f"Buy button data-test={data_test!r} text={text!r} disabled={disabled}")
                if disabled:
                    return False
                # An enabled "Preorder" button is orderable, same as "Add to cart"
                if is_preorder_text(text) or any(keyword in text for keyword in ORDERABLE_BUTTON_TEXTS):
                    return True
                logger.debug("Buy button present but text not recognised; treating as unavailable")
                return False

            if soup.select_one('[data-test="soldOutBlock"]'):
                logger.debug("Found soldOutBlock")
                return False

            logger.warning("Could not determine product availability")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False

    def extract_image_url(self, soup):
        """Extract product image URL from a Target page"""
        logger.debug("TargetScraper: Extracting image URL")
        try:
            meta_img = soup.find('meta', {'property': 'og:image'})
            if meta_img and meta_img.get('content'):
                image_url = meta_img.get('content')
                logger.debug(f"Found image URL from meta: {image_url}")
                return image_url

            img = soup.select_one('[data-test="image-gallery-item-0"] img, img[src*="scene7.com/is/image/Target"]')
            if img and img.get('src'):
                image_url = img.get('src')
                logger.debug(f"Found image URL: {image_url}")
                return image_url

            logger.warning("Could not find product image URL")
            return None
        except Exception as e:
            logger.error(f"Error extracting image URL: {str(e)}")
            return None

    # --------------------------------------------------------------- auto-cart
    def add_to_cart(self, quantity=1):
        """
        Add the most recently scraped product to the Target cart. Cart only - this
        never proceeds to checkout or payment.

        Requires scrape_product() to have run first so current_product_url is set
        (app.scrapers.add_to_cart pre-scrapes 'target' the way it does Newegg). Uses
        the persistent target_profile: if Target asks for a sign-in, run once with
        TARGET_HEADLESS=0, sign in to the window that opens, and retry.

        Args:
            quantity: Quantity to add to cart (default: 1)

        Returns:
            dict: A dictionary with cart status information
            {
                'success': True/False,
                'message': str,
                'cart_url': str or None,
                'screenshot': base64 PNG or None,
            }
        """
        url = self.current_product_url
        if not url:
            return self._cart_result(False, "No product URL set - call scrape_product() first")
        tcin = self.extract_tcin(url)
        if not tcin:
            return self._cart_result(False, f"Could not find a TCIN (/A-<number>) in Target URL: {url}")

        logger.info(f"Adding Target product to cart: {url} (TCIN {tcin}), quantity: {quantity}")
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
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '[data-test="product-title"], [data-test="product-price"]'))
                )
            except TimeoutException:
                logger.warning("Timed out waiting for Target product markup")
            time.sleep(2)

            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            button = self._find_buy_button(driver, tcin)
            if button is None:
                return self._cart_result(False, "No add-to-cart / preorder button on the page - product may be unavailable", driver=driver)

            data_test = (button.get_attribute('data-test') or '').lower()
            text = (button.text or '').strip()
            if button.get_attribute('disabled') is not None or 'disabled' in data_test or not button.is_enabled():
                reason = self._sold_out_reason(driver) or f'the "{text}" button is disabled'
                return self._cart_result(False, f"Cannot add to cart: {reason}", driver=driver)

            logger.debug(f"Clicking buy button data-test={data_test!r} text={text!r}")
            self._click(driver, button)
            time.sleep(3)
            self._dismiss_side_sheet(driver)

            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            # Verify on the cart page rather than trusting the side sheet
            driver.get(TARGET_CART_URL)
            time.sleep(4)
            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            if not self._cart_contains(driver, tcin):
                return self._cart_result(False, "Item was not found in the Target cart after clicking the button", driver=driver)

            note = ""
            if quantity > 1:
                if self._set_cart_quantity(driver, quantity):
                    note = f" (quantity set to {quantity})"
                else:
                    note = f" (quantity 1 - could not set quantity to {quantity})"

            logger.info(f"Target product {tcin} is in the cart")
            return self._cart_result(True, f"Product added to Target cart{note}", driver=driver, cart_url=driver.current_url)
        except ProfileBusyError as e:
            return self._cart_result(False, str(e), driver=driver)
        except Exception as e:
            logger.error(f"Error adding Target product to cart: {str(e)}", exc_info=True)
            return self._cart_result(False, f"Error: {str(e)}", driver=driver)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            profile.close()

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
            logger.warning(f"Target add_to_cart failed: {message}")
        return {
            'success': success,
            'message': message,
            'cart_url': cart_url,
            'screenshot': screenshot
        }

    def _page_obstacle(self, driver):
        """Return a failure message if the current page is a bot wall or a login wall, else None"""
        block_reason = detect_block_page(driver.page_source)
        if block_reason:
            return f"Target showed a bot-protection page ({block_reason}); try again later or run with TARGET_HEADLESS=0"
        challenge = self._challenge_present(driver)
        if challenge:
            return (f"Target showed a human-verification challenge ({challenge}). Run with TARGET_HEADLESS=0 and complete "
                    "it once in the browser window (profile ~/.chrome_profiles/target_profile), then retry")
        if self._login_wall_present(driver):
            return ("Target is asking for a sign-in. Run once with TARGET_HEADLESS=0, sign in to the browser window "
                    "that opens (profile ~/.chrome_profiles/target_profile), then retry")
        return None

    @staticmethod
    def _challenge_present(driver):
        """
        Target's press-and-hold verification, by its own wording. Returns the phrase
        that matched, or None. Detection only - the challenge is never automated.

        The challenge renders inside the PerimeterX iframe (#px-captcha-modal), which
        is present on ordinary pages too, so the top-level body text never contains it
        and the iframe's mere existence proves nothing: look at what is inside it.
        """
        def matched(text):
            lowered = (text or '').lower()
            for marker in CHALLENGE_TEXT_MARKERS:
                if marker in lowered:
                    return f'"{marker}"'
            return None

        try:
            found = matched(driver.find_element(By.TAG_NAME, 'body').text)
            if found:
                return found
        except Exception:
            pass

        for frame in driver.find_elements(By.CSS_SELECTOR, CHALLENGE_IFRAME_SELECTOR):
            try:
                driver.switch_to.frame(frame)
            except Exception:
                continue
            try:
                found = matched(driver.find_element(By.TAG_NAME, 'body').text)
            except Exception:
                found = None
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
            if found:
                return found
        return None

    @staticmethod
    def _login_wall_present(driver):
        """True when Target redirected to its sign-in flow or rendered a login form"""
        current_url = (driver.current_url or '').lower()
        if '/login' in current_url or 'login.target.com' in current_url or '/account/signin' in current_url:
            return True
        return bool(driver.find_elements(By.CSS_SELECTOR, 'form#login, [data-test="login-form"], input#username, input[name="username"]'))

    @staticmethod
    def _find_buy_button(driver, tcin):
        """The buy-box button for this TCIN, or the first generic buy button, or None"""
        selectors = [f'button#addToCartButtonOrTextIdFor{tcin}'] + list(BUY_BUTTON_SELECTORS)
        for selector in selectors:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed():
                    return element
        return None

    @staticmethod
    def _sold_out_reason(driver):
        """Target's own wording for why the buy button is disabled, if visible"""
        try:
            body = driver.find_element(By.TAG_NAME, 'body').text.lower()
        except Exception:
            return None
        for phrase in ('preorders have sold out', 'sold out', 'out of stock'):
            if phrase in body:
                return f'Target reports "{phrase}"'
        return None

    @staticmethod
    def _click(driver, element):
        """Scroll into view and click, falling back to a JS click when something overlays the element"""
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.5)
        try:
            element.click()
        except Exception as e:
            logger.debug(f"Native click failed ({str(e)}); using JS click")
            driver.execute_script("arguments[0].click();", element)

    def _dismiss_side_sheet(self, driver):
        """
        After adding, Target opens a side sheet that may offer a protection plan or ask
        for a fulfillment choice. Decline extras / confirm, up to a few rounds. Never
        clicks anything that reads like checkout.
        """
        for _ in range(3):
            clicked = False
            for selector in SIDE_SHEET_DISMISS_SELECTORS:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        logger.debug(f"Side sheet: clicking {selector}")
                        self._click(driver, element)
                        clicked = True
                        break
                if clicked:
                    break
            if not clicked:
                for dialog in driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]'):
                    for button in dialog.find_elements(By.TAG_NAME, 'button'):
                        label = (button.text or '').strip().lower()
                        if label in SIDE_SHEET_DISMISS_TEXTS and button.is_displayed():
                            logger.debug(f"Side sheet: clicking button {label!r}")
                            self._click(driver, button)
                            clicked = True
                            break
                    if clicked:
                        break
            if not clicked:
                return
            time.sleep(2)

    @staticmethod
    def _cart_contains(driver, tcin):
        """True when the cart page lists this TCIN and is not the empty-cart view"""
        html = driver.page_source
        try:
            body = driver.find_element(By.TAG_NAME, 'body').text.lower()
        except Exception:
            body = ''
        if 'your cart is empty' in body:
            logger.debug("Cart page says the cart is empty")
            return False
        return f'/A-{tcin}' in html or f'cartItem-{tcin}' in html

    @staticmethod
    def _set_cart_quantity(driver, quantity):
        """Best effort: pick the quantity in the cart item's <select>"""
        try:
            from selenium.webdriver.support.ui import Select
            for select in driver.find_elements(By.CSS_SELECTOR, 'select[data-test*="qty"], select[data-test*="uantity"], select[aria-label*="uantity"]'):
                if select.is_displayed():
                    Select(select).select_by_value(str(quantity))
                    time.sleep(2)
                    return True
        except Exception as e:
            logger.debug(f"Could not set cart quantity: {str(e)}")
        return False
