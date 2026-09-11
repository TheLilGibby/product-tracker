"""
Best Buy scraper built on undetected-chromedriver.

bestbuy.com sits behind Akamai Bot Manager, which drops plain ``requests``
connections outright (no HTTP response at all) and, on some networks, also
drops headless Chrome. This scraper therefore:

* drives a real Chrome via undetected-chromedriver with a persistent profile
  under ``~/.chrome_profiles/bestbuy_profile`` so a one-time manual login
  survives between runs;
* runs ``--headless=new`` by default and, when that is blocked and a display
  is available, transparently retries with a visible window;
* serializes every Chrome session on that profile through
  ``common.profile_lock``, because Chrome refuses to run two instances against
  one ``--user-data-dir`` and the loser dies at launch with an error that reads
  exactly like a bot wall;
* keeps ``scrape_via_requests()`` / ``extract_*_from_html()`` as the HTTP
  fallback path used by the other browser scrapers, even though Akamai will
  usually refuse it.

Environment knobs (all optional):

``BESTBUY_HEADLESS``        ``1`` (default) or ``0`` to always show the window.
``BESTBUY_HEADED_FALLBACK`` Retry visibly when headless is blocked. Defaults to
                            on for ``add_to_cart`` (a window there is expected,
                            and lets the user log in) and off for
                            ``scrape_product``, so a scheduled check can never
                            pop windows on the desktop. Set it to ``1`` to allow
                            the retry while scraping too, or ``0`` to never open
                            a window at all.
``CHROME_MAJOR_VERSION``    Force the chromedriver major version (see
                            ``common.detect_chrome_major``); otherwise it is
                            detected from the installed Chrome.
"""

import json
import logging
import os
import platform
import random
import re
import time
from contextlib import contextmanager

import requests
from bs4 import BeautifulSoup

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from app.scrapers.common import (DEFAULT_HEADERS, PROFILE_LOCK_TIMEOUT, REQUEST_TIMEOUT, ProfileBusyError,
                                 detect_block_page, detect_chrome_major, profile_lock)

logger = logging.getLogger('app.scrapers.bestbuy')

BESTBUY_HOME = 'https://www.bestbuy.com/'
BESTBUY_CART = 'https://www.bestbuy.com/cart'

# Primary call-to-action on the product page: <button data-testid="pdp-<state>-<sku>">
PDP_BUTTON_SELECTOR = 'button[data-testid^="pdp-"]'
# Best Buy ids each add-to-cart control by the SKU it adds, buy box and
# recommendation rail alike ("pdp-add-to-cart-6691852",
# "carousel-add-to-cart-6641469"). That makes a foreign SKU proof that a button
# belongs to another product - see _find_buy_button.
BUTTON_SKU_RE = re.compile(r'-(\d{7})$')
IN_STOCK_STATES = ('add-to-cart', 'pre-order', 'preorder')
OUT_OF_STOCK_STATES = ('sold-out', 'coming-soon', 'unavailable', 'check-stores', 'notify')
# How long to keep waiting for the client-rendered CTA after the server's
# markup has arrived. The CTA is the only thing on the page that knows whether
# the item can be bought, so it is worth a few seconds of its own.
CTA_RENDER_TIMEOUT = 10

PRICE_RE = re.compile(r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)')

USER_AGENT_TEMPLATE = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36'
)
DEFAULT_CHROME_MAJOR = 152


def _env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ('0', 'false', 'no', 'off', '')


def _display_available():
    """True when a visible Chrome window can be shown on this machine."""
    if platform.system() in ('Windows', 'Darwin'):
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


class BestBuyBlocked(Exception):
    """Raised when Best Buy served a bot wall / error page instead of the product."""


class BestBuyScraper:
    """Scraper for Best Buy product pages using undetected-chromedriver."""

    def __init__(self, headless=None):
        """
        Args:
            headless: True/False to force the mode, or None to read
                      ``BESTBUY_HEADLESS`` (default: headless).
        """
        logger.debug("Initializing BestBuyScraper with undetected-chromedriver")

        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "bestbuy_profile")
        os.makedirs(self.profile_dir, exist_ok=True)

        self.headless = _env_flag('BESTBUY_HEADLESS', True) if headless is None else bool(headless)
        # Scraping runs unattended on a schedule, so it must not pop windows on the
        # user's desktop; a cart attempt is user-initiated, so a window is fine there.
        self.scrape_headed_fallback = _env_flag('BESTBUY_HEADED_FALLBACK', False)
        self.cart_headed_fallback = _env_flag('BESTBUY_HEADED_FALLBACK', True)

        self.chrome_major = detect_chrome_major()
        self.user_agent = USER_AGENT_TEMPLATE.format(major=self.chrome_major or DEFAULT_CHROME_MAJOR)

        # Set by scrape_product(); used by add_to_cart() (same convention as Newegg)
        self.current_product_url = None
        self.current_sku = None

    # ------------------------------------------------------------------ #
    # Chrome setup
    # ------------------------------------------------------------------ #
    def _get_chrome_options(self, headless=None):
        """Build fresh ChromeOptions for every launch (reusing an options object raises)."""
        headless = self.headless if headless is None else headless
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
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-gpu')
        options.add_argument('--lang=en-US,en')

        width, height = random.choice([(1920, 1080), (1536, 864), (1440, 900), (1366, 768)])
        options.add_argument(f'--window-size={width},{height}')
        options.add_argument(f'--user-agent={self.user_agent}')
        options.page_load_strategy = 'eager'
        return options

    def _launch(self, headless=None):
        """
        Start Chrome, retrying once with the browser-reported major version on a
        driver mismatch. The profile lock must already be held - go through
        _browser() rather than calling this directly.
        """
        headless = self.headless if headless is None else headless
        version_main = self.chrome_major
        last_error = None

        for attempt in range(2):
            try:
                driver = uc.Chrome(options=self._get_chrome_options(headless), version_main=version_main)
                driver.set_page_load_timeout(45)
                driver.set_script_timeout(20)
                self._apply_stealth(driver)
                logger.info(f"Chrome launched (headless={headless}, version_main={version_main})")
                return driver
            except WebDriverException as e:
                last_error = e
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
        busy = self._profile_busy_error(last_error)
        if busy:
            raise busy from last_error
        raise last_error

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
        One Chrome session on the shared bestbuy_profile, launch to quit.

        Chrome will not run two instances against a single --user-data-dir, so
        the scheduled scrape, a user-initiated cart attempt and the --login
        helper all take the profile lock first and hold it until the driver is
        gone. The driver has to die inside the lock: releasing it earlier would
        let the next holder start Chrome while ours is still shutting down.

        Args:
            headless: passed to _launch(); None uses the configured mode
            timeout: seconds to wait for the current holder to finish. None waits
                     indefinitely, which only the interactive --login path asks for.

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
    def _apply_stealth(driver):
        try:
            driver.execute_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                if (!navigator.plugins || navigator.plugins.length === 0) {
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
                }
                window.chrome = window.chrome || { runtime: {} };
            """)
        except Exception as e:
            logger.debug(f"Stealth script failed: {e}")

    @staticmethod
    def _human_pause(low=0.6, high=1.6):
        time.sleep(random.uniform(low, high))

    def _human_scroll(self, driver):
        try:
            for _ in range(random.randint(1, 3)):
                driver.execute_script(f"window.scrollBy(0, {random.randint(200, 600)});")
                self._human_pause(0.3, 0.8)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

    @staticmethod
    def _quit(driver):
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def _take_screenshot(driver):
        try:
            return driver.get_screenshot_as_base64()
        except Exception as e:
            logger.error(f"Error taking screenshot: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Bot wall / error detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_blocked_html(html, title='', expect_product=True):
        """
        Detect Akamai "Access Denied", a JS challenge, or Chrome's own network
        error page (what a dropped connection looks like from Selenium).

        Pass expect_product=False for a page that is not a product page - the
        cart. Two rules here are statements about a product page rather than
        about a wall: detect_block_page's "under 5 KB with none of the product
        markers", and the requirement below that the page carry the pdp CTA or
        Product JSON-LD. An empty cart trips both by design, so on the cart they
        turn a plain "your cart is empty" into "Best Buy is blocking us", which
        sends whoever reads the log off to fix the wrong thing.

        Everything that identifies a wall by its own content stays live in both
        modes - the title and body markers, Chrome's error page, "Reference #" -
        and those are the whole reason to keep calling this on the cart at all.

        The trade is deliberate and worth naming: with expect_product=False, a
        wall that is BOTH under 5 KB AND carries none of those markers now reads
        as "item not in cart" rather than "bot wall". Both answers stop the add
        and neither proceeds to checkout, but only the false wall points at the
        wrong cause.
        """
        if not html:
            return True
        reason = detect_block_page(html, expect_product=expect_product)
        if reason:
            logger.debug(f"Block page detected: {reason}")
            return True
        head = html[:4000]
        if 'main-frame-error' in head or 'chrome-error://' in head:
            return True
        if re.search(r'Reference #\d|Pardon Our Interruption|Request unsuccessful', html[:20000]):
            return True
        if re.search(r'Access Denied|Just a moment', title or '', re.I):
            return True
        # A real product page always carries the pdp CTA or Product JSON-LD.
        if expect_product and 'data-testid="pdp-' not in html and 'application/ld+json' not in html:
            return True
        return False

    def _load_product_page(self, driver, url):
        """Navigate to the product page and return its HTML, raising BestBuyBlocked on a wall."""
        driver.get(url)
        try:
            WebDriverWait(driver, 20).until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, PDP_BUTTON_SELECTOR)),
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'script[type="application/ld+json"]')),
                    EC.presence_of_element_located((By.CSS_SELECTOR, '#main-frame-error')),
                )
            )
        except TimeoutException:
            logger.warning("Timed out waiting for product markup")

        # The wait above is satisfied by the JSON-LD, which arrives in the
        # server's first response, while the CTA is rendered client-side a
        # moment later. Returning here hands extract_availability a page with no
        # CTA on it, and the JSON-LD is not a substitute for one - see
        # _availability_from_jsonld.
        if not driver.find_elements(By.CSS_SELECTOR, PDP_BUTTON_SELECTOR):
            try:
                WebDriverWait(driver, CTA_RENDER_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, PDP_BUTTON_SELECTOR)))
            except TimeoutException:
                logger.warning("Product markup arrived but the pdp CTA never rendered")

        self._human_pause()
        self._human_scroll(driver)

        html = driver.page_source
        if self.is_blocked_html(html, driver.title):
            raise BestBuyBlocked(f"Best Buy served a block/error page (title={driver.title!r})")
        return html

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @staticmethod
    def extract_sku(url, html=None):
        """SKU from a /sku/<n> or skuId=<n> URL, else from the pdp button / JSON-LD in the page."""
        for pattern in (r'/sku/(\d{7})', r'[?&]skuId=(\d{7})', r'/(\d{7})\.p\b'):
            match = re.search(pattern, url or '')
            if match:
                return match.group(1)
        if html:
            match = re.search(r'data-testid="pdp-[a-z-]+-(\d{7})"', html)
            if match:
                return match.group(1)
            match = re.search(r'"sku"\s*:\s*"?(\d{7})"?', html)
            if match:
                return match.group(1)
        return None

    def scrape_product(self, url):
        """
        Scrape a Best Buy product page.

        Returns:
            {'name', 'price', 'available', 'image_url'} or None when the page
            could not be retrieved (bot wall, network failure).
        """
        self.current_product_url = url
        self.current_sku = self.extract_sku(url)

        for headless in self._modes(self.scrape_headed_fallback):
            try:
                with self._browser(headless) as driver:
                    html = self._load_product_page(driver, url)
                    soup = BeautifulSoup(html, 'html.parser')
                    self.current_sku = self.extract_sku(url, html) or self.current_sku

                    result = {
                        'name': self.extract_name(soup, driver),
                        'price': self.extract_price(soup, driver),
                        'available': self.extract_availability(soup, driver),
                        'image_url': self.extract_image_url(soup, driver),
                    }
                    logger.info(f"Scraped Best Buy sku={self.current_sku}: {result['name']!r} "
                                f"price={result['price']} available={result['available']}")
                    return result
            except ProfileBusyError as e:
                # Something else owns the profile; a second browser mode would only
                # queue behind it. The HTTP fallback below needs no profile.
                logger.warning(f"Skipping browser scrape of {url}: {e}")
                break
            except BestBuyBlocked as e:
                logger.warning(f"{e} (headless={headless})")
            except Exception as e:
                logger.error(f"Error scraping Best Buy product with browser (headless={headless}): {e}")

        # HTTP fallback (Akamai usually refuses it, but it is cheap to try)
        try:
            return self.scrape_via_requests(url)
        except Exception as e:
            logger.error(f"HTTP fallback failed for {url}: {e}")
            return None

    def _modes(self, allow_headed_fallback):
        """Browser modes to try in order: configured mode, then visible if headless was blocked."""
        modes = [self.headless]
        if self.headless and allow_headed_fallback and _display_available():
            modes.append(False)
        return modes

    def scrape_via_requests(self, url):
        """Plain-HTTP fallback. Returns a product dict or None."""
        logger.info(f"Attempting HTTP fallback for Best Buy product: {url}")
        headers = dict(DEFAULT_HEADERS)
        headers.update({
            'User-Agent': self.user_agent,
            'Referer': BESTBUY_HOME,
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Upgrade-Insecure-Requests': '1',
        })
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.warning(f"HTTP fallback connection failed: {e}")
            return None
        if response.status_code != 200:
            logger.warning(f"HTTP fallback got status {response.status_code}")
            return None
        if self.is_blocked_html(response.text):
            logger.warning("HTTP fallback got a block page")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')
        self.current_sku = self.extract_sku(url, response.text) or self.current_sku
        return {
            'name': self.extract_name_from_html(soup),
            'price': self.extract_price_from_html(soup),
            'available': self.extract_availability_from_html(soup),
            'image_url': self.extract_image_url_from_html(soup),
        }

    # ------------------------------------------------------------------ #
    # Extraction: browser path (soup + live driver)
    # ------------------------------------------------------------------ #
    def extract_name(self, soup, driver):
        return self.extract_name_from_html(soup)

    def extract_price(self, soup, driver):
        price = self.extract_price_from_html(soup)
        if price is None and driver is not None:
            try:
                for element in driver.find_elements(
                        By.CSS_SELECTOR, '[data-testid="price-block-customer-price"], .price-block-customer-price'):
                    price = self._parse_price(element.text)
                    if price is not None:
                        break
            except Exception as e:
                logger.debug(f"Live price lookup failed: {e}")
        return price

    def extract_availability(self, soup, driver):
        """Enabled Add to Cart / Pre-Order => True; Sold Out / Coming Soon => False; JSON-LD as backup."""
        state = self._button_state(soup)
        if state is None and driver is not None:
            try:
                for button in driver.find_elements(By.CSS_SELECTOR, PDP_BUTTON_SELECTOR):
                    testid = (button.get_attribute('data-testid') or '').lower()
                    label = (button.text or '').strip().lower()
                    state = self._classify_button(testid, label, enabled=button.is_enabled())
                    if state is not None:
                        break
            except Exception as e:
                logger.debug(f"Live button lookup failed: {e}")
        if state is not None:
            return state
        return self._availability_from_jsonld(soup)

    def extract_image_url(self, soup, driver):
        return self.extract_image_url_from_html(soup)

    # ------------------------------------------------------------------ #
    # Extraction: HTML-only path (shared by both)
    # ------------------------------------------------------------------ #
    def extract_name_from_html(self, soup):
        try:
            h1 = soup.find('h1')
            if h1 and h1.get_text(strip=True):
                return h1.get_text(' ', strip=True)
            product = self._jsonld_product(soup)
            if product and product.get('name'):
                return str(product['name']).strip()
            og = soup.find('meta', property='og:title')
            if og and og.get('content'):
                return og['content'].strip()
            if soup.title and soup.title.string:
                return re.sub(r'\s*-\s*Best Buy\s*$', '', soup.title.string).strip()
        except Exception as e:
            logger.error(f"Error extracting name: {e}")
        return "Unknown Product"

    def extract_price_from_html(self, soup):
        try:
            for selector in (
                '[data-testid="price-block-customer-price"]',
                '.price-block-customer-price',
                '[data-testid="customer-price"]',
                '.priceView-customer-price span',
            ):
                for element in soup.select(selector):
                    price = self._parse_price(element.get_text(' ', strip=True))
                    if price is not None:
                        return price
            for offer in self._offers(self._jsonld_product(soup)):
                if offer.get('price') not in (None, ''):
                    try:
                        return float(str(offer['price']).replace(',', ''))
                    except ValueError:
                        continue
            meta = soup.find('meta', property='product:price:amount')
            if meta and meta.get('content'):
                return float(meta['content'])
        except Exception as e:
            logger.error(f"Error extracting price: {e}")
        return None

    def extract_availability_from_html(self, soup):
        try:
            state = self._button_state(soup)
            if state is not None:
                return state
            return self._availability_from_jsonld(soup)
        except Exception as e:
            logger.error(f"Error extracting availability: {e}")
            return False

    def extract_image_url_from_html(self, soup):
        try:
            product = self._jsonld_product(soup)
            if product:
                images = product.get('image')
                if isinstance(images, str):
                    return images
                if isinstance(images, dict) and images.get('url'):
                    return images['url']
                if isinstance(images, list) and images:
                    first = images[0]
                    if isinstance(first, str):
                        return first
                    if isinstance(first, dict) and first.get('url'):
                        return first['url']
            og = soup.find('meta', property='og:image')
            if og and og.get('content'):
                return og['content']
            for img in soup.select('img[src*="pisces.bbystatic.com"]'):
                src = img.get('src') or img.get('data-src')
                if src:
                    return re.sub(r';maxHeight=\d+;maxWidth=\d+', ';maxHeight=640;maxWidth=640', src)
        except Exception as e:
            logger.error(f"Error extracting image URL: {e}")
        return None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_price(text):
        if not text:
            return None
        match = PRICE_RE.search(text)
        if not match:
            return None
        try:
            return float(match.group(1).replace(',', ''))
        except ValueError:
            return None

    @staticmethod
    def _jsonld_product(soup):
        for script in soup.find_all('script', type='application/ld+json'):
            raw = script.string or script.get_text()
            if not raw or 'Product' not in raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                if item.get('@type') == 'Product':
                    return item
                for node in item.get('@graph') or []:
                    if isinstance(node, dict) and node.get('@type') == 'Product':
                        return node
        return None

    @staticmethod
    def _offers(product):
        if not product:
            return []
        offers = product.get('offers')
        if isinstance(offers, dict):
            return [offers]
        if isinstance(offers, list):
            return [o for o in offers if isinstance(o, dict)]
        return []

    @staticmethod
    def _classify_button(testid, label, enabled):
        """Map a pdp button to True (buyable), False (not buyable) or None (unknown)."""
        key = f"{testid} {label}".replace('_', '-')
        if any(state in key for state in OUT_OF_STOCK_STATES) or 'sold out' in key or 'coming soon' in key:
            return False
        if any(state in key for state in IN_STOCK_STATES) or 'add to cart' in key:
            return bool(enabled)
        return None

    def _button_state(self, soup):
        """Availability from the page's own pdp CTA; None when no CTA is present."""
        for button in soup.select(PDP_BUTTON_SELECTOR):
            testid = (button.get('data-testid') or '').lower()
            label = button.get_text(' ', strip=True).lower()
            enabled = not (button.has_attr('disabled') or button.get('aria-disabled') == 'true')
            state = self._classify_button(testid, label, enabled)
            if state is not None:
                logger.debug(f"pdp button {testid!r} ({label!r}, enabled={enabled}) -> available={state}")
                return state
        # Legacy layout
        for button in soup.select('.fulfillment-add-to-cart-button button, .add-to-cart-button'):
            label = button.get_text(' ', strip=True).lower()
            enabled = not (button.has_attr('disabled') or 'disabled' in button.get('class', []))
            state = self._classify_button('', label, enabled)
            if state is not None:
                return state
        return None

    def _availability_from_jsonld(self, soup):
        """
        The JSON-LD may veto availability. It may never vouch for it.

        Best Buy's Product JSON-LD is marketing metadata, not the buy box. Both
        saved captures of the Zelda 40th accessories carry
        ``"availability": "https://schema.org/InStock"`` on a page whose own CTA
        is ``<button data-testid="pdp-coming-soon-6691852" disabled>Coming
        Soon</button>`` - SKUs 6691852 and 6691849, InStock in the metadata and
        unbuyable on the page, in the same capture.

        That is the reported contradiction: the scrape reads True here, alerts,
        and the auto-cart job then refuses with "Product is not purchasable
        (coming soon)" every cycle, because the click path reads the CTA and the
        CTA is right. So a positive claim from this source is discarded - like a
        cart that is merely not empty, it is evidence of nothing in particular.
        A negative claim is still worth having, since nobody advertises
        OutOfStock by mistake, and an absent CTA with nothing to veto stays
        False, which is what this already returned when it could not tell.
        """
        for offer in self._offers(self._jsonld_product(soup)):
            availability = str(offer.get('availability', ''))
            if not availability:
                continue
            if re.search(r'OutOfStock|SoldOut|Discontinued|PreSale', availability):
                logger.debug(f"JSON-LD availability {availability} -> False")
                return False
            if re.search(r'InStock|PreOrder|LimitedAvailability|OnlineOnly', availability):
                logger.warning(
                    f"JSON-LD claims {availability} but the page rendered no pdp CTA to confirm it; "
                    "reporting unavailable rather than alerting on metadata")
                return False
        logger.warning("Could not determine availability; defaulting to False")
        return False

    # ------------------------------------------------------------------ #
    # Add to cart
    # ------------------------------------------------------------------ #
    def add_to_cart(self, quantity=1, url=None):
        """
        Add the most recently scraped product (or ``url``) to the Best Buy cart.

        Returns:
            {'success': bool, 'message': str, 'cart_url': str|None, 'screenshot': base64|None}
        """
        if url:
            self.current_product_url = url
            self.current_sku = self.extract_sku(url) or self.current_sku
        if not self.current_product_url:
            return {
                'success': False,
                'message': "No product URL available. Please scrape the product first.",
                'cart_url': None,
                'screenshot': None,
            }
        url = self.current_product_url
        quantity = max(1, int(quantity or 1))
        logger.info(f"Adding Best Buy product to cart (qty={quantity}): {url}")

        last = {'success': False, 'message': 'Could not reach Best Buy', 'cart_url': None, 'screenshot': None}
        for headless in self._modes(self.cart_headed_fallback):
            try:
                # The inner try keeps failure handling inside the session: a
                # screenshot can only be taken while the driver is still alive.
                with self._browser(headless) as driver:
                    try:
                        self._load_product_page(driver, url)
                        return self._add_to_cart_with_driver(driver, quantity)
                    except BestBuyBlocked as e:
                        logger.warning(f"{e} (headless={headless})")
                        last = {'success': False, 'message': str(e), 'cart_url': None,
                                'screenshot': self._take_screenshot(driver)}
                    except Exception as e:
                        logger.error(f"Error adding to cart (headless={headless}): {e}", exc_info=True)
                        last = {'success': False, 'message': f"Error adding to cart: {e}", 'cart_url': None,
                                'screenshot': self._take_screenshot(driver)}
            except ProfileBusyError as e:
                # Retrying in another window would just wait on the same profile.
                logger.warning(f"Not adding to cart: {e}")
                return {'success': False, 'message': f"Chrome profile is busy: {e}",
                        'cart_url': None, 'screenshot': None}
            except Exception as e:
                # Chrome never started, so there is no driver to photograph.
                logger.error(f"Could not start Chrome for the cart (headless={headless}): {e}", exc_info=True)
                last = {'success': False, 'message': f"Error adding to cart: {e}",
                        'cart_url': None, 'screenshot': None}
        return last

    def _find_buy_button(self, driver):
        """
        Return (button, state, label) for the pdp CTA; state is True/False/None
        as in _classify_button.

        A button whose test id names a different SKU is never a candidate, and
        one that names the right SKU wins over one that names none. The saved
        product page carries nine working add-to-cart buttons for recommended
        products; they are ids'd "carousel-" rather than "pdp-" today, so the
        selector alone excludes them - but that is a naming convention, and this
        selector sweeps the whole document, so "the first match" is document
        order rather than the buy box. The SKU in the id is the part that
        actually attributes a button to a product.

        Clicking a recommendation's button would add someone else's product and
        then report this one as missing from the cart: it fails closed, but it
        blames the wrong thing while another item sits in the cart.
        """
        fallback = None
        for button in driver.find_elements(By.CSS_SELECTOR, PDP_BUTTON_SELECTOR):
            testid = (button.get_attribute('data-testid') or '').lower()
            label = (button.text or '').strip().lower()
            state = self._classify_button(testid, label, enabled=button.is_enabled())
            if state is None:
                continue

            sku = self._sku_from_button(button)
            if self.current_sku and sku:
                if sku == self.current_sku:
                    return button, state, label or testid
                logger.debug(f"Ignoring pdp button {testid!r}: it adds SKU {sku}, not {self.current_sku}")
                continue

            # Either the button names no SKU or this scrape has none to compare
            # it against. Usable, but only if nothing better turns up.
            if fallback is None:
                fallback = (button, state, label or testid)
        return fallback or (None, None, None)

    @staticmethod
    def _sku_from_button(button):
        """
        The SKU off the CTA's own data-testid ("pdp-<state>-<sku>"), or None.

        Preferred over extract_sku's page-wide scan because it is unambiguous:
        it is the button this add is about to click. A real Best Buy product
        page carries ~275 other "sku" values in its cross-sell JSON, and the
        page-wide fallback lands on the right one only because the page's own
        happens to come first in the document.
        """
        match = BUTTON_SKU_RE.search(button.get_attribute('data-testid') or '')
        return match.group(1) if match else None

    def _add_to_cart_with_driver(self, driver, quantity):
        button, state, label = self._find_buy_button(driver)
        if button is None:
            return {'success': False, 'message': "Could not find the Add to Cart button",
                    'cart_url': None, 'screenshot': self._take_screenshot(driver)}
        if not state:
            return {'success': False, 'message': f"Product is not purchasable ({label})",
                    'cart_url': None, 'screenshot': self._take_screenshot(driver)}

        # Verification at the end of this method is "the SKU is on the cart
        # page", never "the cart does not say it is empty" - a bot wall carries
        # neither, so absence must never read as success. Recover the SKU from
        # the pdp markup now, while the product page is still loaded, for the
        # URL shapes extract_sku cannot read on their own.
        if not self.current_sku:
            self.current_sku = self._sku_from_button(button) or self.extract_sku(
                self.current_product_url or '', driver.page_source)
            if self.current_sku:
                logger.info(f"Recovered SKU {self.current_sku} from the product page")

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        self._human_pause(0.4, 1.0)
        try:
            button.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", button)
        logger.info(f"Clicked '{label}' button")

        # Wait for the add-to-cart confirmation (sheet/modal or cart counter)
        confirmed = False
        try:
            WebDriverWait(driver, 15).until(
                EC.any_of(
                    EC.presence_of_element_located(
                        (By.XPATH, "//*[contains(translate(., 'ADED', 'aded'), 'added to')]")),
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'a[href*="/cart"] [data-testid*="count"], .cart-count, [data-testid="cart-icon-count"]')),
                    EC.url_contains('/cart'),
                )
            )
            confirmed = True
        except TimeoutException:
            logger.warning("No add-to-cart confirmation detected; checking the cart page anyway")
        self._human_pause()

        # Dismiss protection-plan / upsell sheets if they appeared
        for xpath in ("//button[contains(., 'No, thanks')]", "//button[contains(., 'No thanks')]",
                      "//button[contains(., 'Continue')]", "//button[@aria-label='Close']"):
            try:
                for element in driver.find_elements(By.XPATH, xpath):
                    if element.is_displayed():
                        element.click()
                        self._human_pause(0.3, 0.7)
                        break
            except Exception:
                continue

        driver.get(BESTBUY_CART)
        try:
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        except TimeoutException:
            pass
        self._human_pause(1.0, 2.0)

        if quantity > 1:
            self._set_cart_quantity(driver, quantity)

        page = driver.page_source
        screenshot = self._take_screenshot(driver)

        # Check the cart page for a wall BEFORE reading anything into its
        # contents. Without this, an Akamai page served on /cart is neither
        # empty nor carrying the SKU, so a click that looked confirmed on the
        # product page reported success for an item that never reached the cart.
        # expect_product=False because an empty cart is small and mentions no
        # products - see is_blocked_html.
        if self.is_blocked_html(page, driver.title, expect_product=False):
            logger.warning("Best Buy served a block page on the cart; cannot verify the add")
            return {'success': False,
                    'message': "Best Buy served a block page on the cart, so the add could not be "
                               "verified. The item may or may not be in the cart - check it before retrying",
                    'cart_url': driver.current_url, 'screenshot': screenshot}

        # Success needs positive evidence: the SKU has to be on the cart page.
        # The confirmation toast is only a hint - it is rendered on the product
        # page before the cart is written, and any page that is not the cart
        # (a wall, an error, a sign-in) is missing the SKU too.
        empty = bool(re.search(r'cart is empty|nothing in your cart', page, re.I))
        in_cart = bool(self.current_sku and self.current_sku in page)

        if not self.current_sku:
            logger.warning("No SKU for this product, so the cart could not be verified")
            return {'success': False,
                    'message': "Could not identify the product's SKU, so the add could not be "
                               "verified against the cart. Check the cart before retrying",
                    'cart_url': driver.current_url, 'screenshot': screenshot}

        if not in_cart:
            reason = ('the cart is empty' if empty
                      else f"the cart page does not list SKU {self.current_sku}")
            logger.warning(f"Add to cart not verified: {reason} "
                           f"(confirmation on the product page: {confirmed})")
            return {'success': False, 'message': f"Item did not appear in the cart ({reason})",
                    'cart_url': driver.current_url, 'screenshot': screenshot}

        return {'success': True, 'message': f"Successfully added {quantity} item(s) to Best Buy cart",
                'cart_url': driver.current_url, 'screenshot': screenshot}

    def _set_cart_quantity(self, driver, quantity):
        """Best-effort quantity change on the cart page."""
        try:
            for select in driver.find_elements(
                    By.CSS_SELECTOR, 'select[aria-label*="uantity"], select[name*="quantity"], select[id*="quantity"]'):
                Select(select).select_by_value(str(quantity))
                self._human_pause()
                logger.info(f"Cart quantity set to {quantity}")
                return True
            for field in driver.find_elements(By.CSS_SELECTOR, 'input[aria-label*="uantity"], input[name*="quantity"]'):
                field.clear()
                field.send_keys(str(quantity))
                self._human_pause()
                return True
        except Exception as e:
            logger.warning(f"Could not set cart quantity to {quantity}: {e}")
        return False
