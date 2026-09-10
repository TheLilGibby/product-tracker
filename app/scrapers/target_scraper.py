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
"""

import re
import logging
import os
import subprocess
import time
import requests
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from app.scrapers.common import DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text

# Set up logging
logger = logging.getLogger('app.scrapers.target')

# Public key from Target's web bundle (see module docstring)
REDSKY_API_KEY = '9f36aeafbe60771e321a7cc95a78140772ab3e96'
REDSKY_PDP_URL = 'https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1'
# Pricing is national for the items we track; any store id satisfies the API
REDSKY_STORE_ID = '3991'

# Redsky shipping availability_status values that mean the item can be ordered
REDSKY_ORDERABLE_STATUSES = ('IN_STOCK', 'PRE_ORDER_SELLABLE', 'LIMITED_STOCK')

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


class TargetScraper:
    """Scraper for Target products: Redsky JSON API with an undetected-chromedriver fallback"""

    def __init__(self):
        """Initialize the Target scraper."""
        logger.debug("Initializing TargetScraper")
        self.headers = dict(DEFAULT_HEADERS)

        # Persistent profile so Target's bot checks see a returning browser
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "target_profile")
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
        logger.info(f"Attempting Redsky API for TCIN {tcin}")
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

        response = requests.get(REDSKY_PDP_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            # 403 with a "captchaRelativeURL" body is Target's bot wall for non-browser clients
            logger.warning(f"Redsky returned HTTP {response.status_code} for TCIN {tcin}: {response.text[:120]!r}")
            return None

        payload = response.json()
        product = (payload.get('data') or {}).get('product') or {}
        item = product.get('item') or {}

        name = ((item.get('product_description') or {}).get('title') or '').strip()
        if not name:
            logger.warning(f"Redsky response for TCIN {tcin} has no product title")
            return None

        price = self._price_from_redsky(product.get('price') or {})
        available = self._availability_from_redsky(product)
        image_url = ((item.get('enrichment') or {}).get('images') or {}).get('primary_image_url')

        logger.info(f"Redsky extracted: name={name}, price={price}, available={available}")
        return {
            'name': name,
            'price': price,
            'available': available,
            'image_url': image_url
        }

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
        """Derive availability from Redsky's fulfillment block (pre-order sellable counts as available)"""
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

        logger.warning("Could not determine availability from Redsky fulfillment data")
        return False

    # ----------------------------------------------------------------- browser
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

    @staticmethod
    def _detect_chrome_major():
        """
        Major version of the installed Chrome, so undetected-chromedriver fetches a
        matching driver instead of the newest one. CHROME_MAJOR_VERSION overrides.
        Returns None to let undetected-chromedriver decide.
        """
        override = os.environ.get('CHROME_MAJOR_VERSION', '')
        if override.isdigit():
            return int(override)

        for exe in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
            try:
                output = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            match = re.search(r'(\d+)\.\d+\.\d+', output or '')
            if match:
                return int(match.group(1))

        # Windows installs keep a <version> directory next to chrome.exe
        try:
            exe = uc.find_chrome_executable()
            if exe:
                for entry in os.listdir(os.path.dirname(exe)):
                    match = re.fullmatch(r'(\d+)\.\d+\.\d+\.\d+', entry)
                    if match:
                        return int(match.group(1))
        except Exception as e:
            logger.debug(f"Could not detect Chrome version from install directory: {str(e)}")
        return None

    def scrape_via_browser(self, url, tcin):
        """
        Render the product page with undetected-chromedriver and parse the DOM.

        Returns:
            dict, or None if the page did not render a product
        """
        logger.info(f"Attempting browser scrape for Target TCIN {tcin}")
        driver = None
        try:
            options = self._get_chrome_options()
            driver = uc.Chrome(options=options, version_main=self._detect_chrome_major())
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
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

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
