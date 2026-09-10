import re
import os
import json
import time
import logging
from contextlib import ExitStack

import requests
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from app.scrapers.common import (DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text,
                                 detect_chrome_major, profile_lock, ProfileBusyError)

# Set up logging
logger = logging.getLogger('app.scrapers.walmart')

# availabilityStatus values from __NEXT_DATA__ that mean the item can be ordered now.
# A pre-order whose allocation is gone still reports OUT_OF_STOCK, so preOrder.isPreOrder
# alone is not treated as available.
WALMART_AVAILABLE_STATUSES = ('IN_STOCK', 'PREORDER', 'LIMITED_STOCK')

WALMART_CART_URL = 'https://www.walmart.com/cart'

# The item id is the trailing path segment of /ip/<slug>/<id> (Walmart calls it usItemId)
ITEM_ID_RE = re.compile(r'/ip/(?:[^/]+/)?(\d+)')

# The buy box, as opposed to the "similar items" carousels further down the page. This
# matters more here than on other retailers: Walmart server-renders working
# "Add to cart" buttons for RECOMMENDED products, so an unscoped
# button[data-automation-id="atc"] can cart something the user never asked for.
BUY_BOX_CONTAINER_SELECTORS = (
    '[data-testid="add-to-cart-section"]',
    '[data-testid="buy-box"]',
    '[data-testid="fulfillment-section"]',
    'div[data-dca-name="AddToCartSection"]',
)
BUY_BUTTON_SELECTOR = 'button[data-automation-id="atc"]'
BUY_BUTTON_TEXTS = ('add to cart', 'pre-order', 'preorder')

# Walmart's PerimeterX wall, by its own wording
CHALLENGE_TEXT_MARKERS = ('press & hold', 'press and hold', 'robot or human',
                          "confirm you're a human", 'verify you are a human')
CHALLENGE_IFRAME_SELECTOR = 'iframe[id*="px-captcha"]'
SIGN_IN_TEXT_MARKERS = ('sign in to your account', 'sign in for the best experience',
                        'create an account or sign in')

class WalmartScraper:
    """Scraper specifically for Walmart products"""
    
    def __init__(self):
        """Initialize the Walmart scraper."""
        logger.debug("Initializing WalmartScraper")
        self.headers = dict(DEFAULT_HEADERS)
        # add_to_cart reads the URL off the instance; the dispatcher pre-scrapes to set it
        self.current_product_url = None
        # Persistent profile so Walmart's bot checks see a returning browser
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "walmart_profile")
    
    def scrape_product(self, url):
        """
        Scrape product information from Walmart URL
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        self.current_product_url = url
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            block_reason = detect_block_page(response.text)
            if block_reason:
                logger.warning(f"Walmart returned a block page for {url} (HTTP {response.status_code}): {block_reason}")
                return None
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Walmart is a Next.js app: the server-rendered product state is the
            # reliable source; the DOM selectors below are the legacy fallback
            product_data = self.extract_from_next_data(soup)
            if product_data:
                return product_data
            
            return {
                'name': self.extract_name(soup),
                'price': self.extract_price(soup),
                'available': self.extract_availability(soup),
                'image_url': self.extract_image_url(soup)
            }
        except Exception as e:
            logger.error(f"Error scraping Walmart product: {str(e)}")
            return None
    
    def extract_from_next_data(self, soup):
        """
        Extract product data from the <script id="__NEXT_DATA__"> JSON.
        
        Returns:
            dict with name, price, available and image_url, or None if the page has
            no product node there (older layouts, search pages, block pages)
        """
        logger.debug("WalmartScraper: Extracting from __NEXT_DATA__")
        try:
            script = soup.find('script', id='__NEXT_DATA__')
            if not script or not script.string:
                logger.debug("No __NEXT_DATA__ script on page")
                return None
            
            data = json.loads(script.string)
            product = data.get('props', {}).get('pageProps', {}).get('initialData', {}).get('data', {}).get('product')
            if not product or not product.get('name'):
                logger.debug("__NEXT_DATA__ has no product node")
                return None
            
            status = (product.get('availabilityStatus') or '').upper()
            is_pre_order = bool((product.get('preOrder') or {}).get('isPreOrder'))
            available = status in WALMART_AVAILABLE_STATUSES
            logger.debug(f"__NEXT_DATA__ availabilityStatus={status} isPreOrder={is_pre_order} -> available={available}")
            
            price = None
            current_price = (product.get('priceInfo') or {}).get('currentPrice') or {}
            if current_price.get('price') is not None:
                price = float(current_price['price'])
                logger.debug(f"Found price from __NEXT_DATA__: ${price}")
            
            image_url = (product.get('imageInfo') or {}).get('thumbnailUrl')
            
            return {
                'name': product['name'].strip(),
                'price': price,
                'available': available,
                'image_url': image_url
            }
        except Exception as e:
            logger.error(f"Error extracting from __NEXT_DATA__: {str(e)}")
            return None
    
    def extract_name(self, soup):
        """Extract product name from Walmart page"""
        logger.debug("WalmartScraper: Extracting name")
        try:
            # Try product title element
            title_element = soup.find('h1', {'itemprop': 'name'}) or soup.select_one('.prod-ProductTitle')
            if title_element:
                name = title_element.get_text().strip()
                logger.debug(f"Found name: {name}")
                return name
            
            # Try the meta title
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
        """Extract product price from Walmart page"""
        logger.debug("WalmartScraper: Extracting price")
        try:
            # Try various price selectors
            price_selectors = [
                '[itemprop="price"]',
                '.price-characteristic',
                '.prod-PriceSection .price-group'
            ]
            
            for selector in price_selectors:
                price_element = soup.select_one(selector)
                if price_element:
                    price_text = price_element.get_text().strip()
                    price_match = re.search(r'(\d+\,)?\d+\.\d{2}', price_text)
                    if price_match:
                        # Remove commas and convert to float
                        price = float(price_match.group(0).replace(',', ''))
                        logger.debug(f"Found price: ${price}")
                        return price
            
            # Try data attribute
            price_element = soup.select_one('[data-price-type="finalPrice"]')
            if price_element and price_element.get('data-price-value'):
                price = float(price_element.get('data-price-value'))
                logger.debug(f"Found price from data attribute: ${price}")
                return price
            
            logger.warning("Could not find product price")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None
    
    def extract_availability(self, soup):
        """Extract product availability from Walmart page"""
        logger.debug("WalmartScraper: Extracting availability")
        try:
            # Check for out-of-stock indicators
            out_of_stock = soup.select_one('.prod-ProductOffer-oosMsg') or soup.select_one('.product-out-of-stock')
            if out_of_stock:
                logger.debug("Product is out of stock")
                return False
                
            # Check for add to cart button
            add_to_cart = soup.select_one('.add-to-cart-btn:not([disabled])') or soup.select_one('.prod-ProductCTA--primary')
            if add_to_cart:
                logger.debug("Found enabled add to cart button")
                return True

            # A pre-order button or message counts as available
            for element in soup.select('button, .prod-ProductOffer-availabilityMsg, [data-testid="availability-message"]'):
                if is_preorder_text(element.get_text()) and not element.has_attr('disabled'):
                    logger.debug("Found pre-order indicator, treating as available")
                    return True

            # Check availability text
            availability = soup.select_one('.prod-ProductOffer-availabilityMsg')
            if availability:
                text = availability.get_text().strip().lower()
                available = 'out of stock' not in text and 'unavailable' not in text
                logger.debug(f"Found availability from text: {available}")
                return available
            
            logger.warning("Could not determine product availability")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False
    
    def extract_image_url(self, soup):
        """Extract product image URL from Walmart page"""
        logger.debug("WalmartScraper: Extracting image URL")
        try:
            # Try various image selectors
            img = soup.select_one('.prod-hero-image img') or soup.select_one('.product-detail-main-image img')
            if img and (img.get('src') or img.get('data-image-src')):
                image_url = img.get('src') or img.get('data-image-src')
                logger.debug(f"Found image URL: {image_url}")
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
    # -------------------------------------------------------------------- cart
    @staticmethod
    def extract_item_id(url):
        """Return the usItemId from a /ip/<slug>/<id> URL, or None"""
        match = ITEM_ID_RE.search(url or '')
        return match.group(1) if match else None

    def _fetch_order_state(self, url):
        """
        Read the buy-box state out of __NEXT_DATA__ over plain HTTP.

        Cheap enough to run before starting Chrome, and authoritative: it is the
        same server state the page renders from. Returns a dict, or None when the
        page could not be read (blocked or shape changed), in which case the
        caller falls back to the browser rather than assuming anything.
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            if detect_block_page(response.text):
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            tag = soup.find('script', id='__NEXT_DATA__')
            if not tag or not tag.string:
                return None
            product = json.loads(tag.string)['props']['pageProps']['initialData']['data']['product']
        except Exception as e:
            logger.debug(f"Could not read Walmart order state: {str(e)}")
            return None

        status = product.get('availabilityStatus')
        pre_order = (product.get('preOrder') or {}).get('isPreOrder')
        order_limit = product.get('orderLimit')
        orderable = status in WALMART_AVAILABLE_STATUSES
        # A pre-order whose allocation is gone still advertises isPreOrder
        if orderable and order_limit == 0:
            orderable = False
        return {
            'item_id': product.get('usItemId'),
            'name': product.get('name'),
            'status': status,
            'is_pre_order': bool(pre_order),
            'order_limit': order_limit,
            'orderable': orderable,
        }

    @staticmethod
    def _unorderable_reason(state):
        """Explain, in the page's own terms, why the buy box cannot be used"""
        status = (state.get('status') or 'unknown').replace('_', ' ').lower()
        if state.get('is_pre_order'):
            return f"the pre-order is not open (Walmart reports {status})"
        return f"Walmart reports the item as {status}"

    def add_to_cart(self, quantity=1):
        """
        Add the pre-scraped Walmart product to the cart. Cart only - this never
        continues to checkout or payment.

        Args:
            quantity: how many to add (best effort; the result says what happened)

        Returns:
            dict: {'success', 'message', 'cart_url', 'screenshot'}
        """
        url = self.current_product_url
        if not url:
            return self._cart_result(False, "No product URL set - call scrape_product() first")
        item_id = self.extract_item_id(url)
        if not item_id:
            return self._cart_result(False, f"Could not find an item id (/ip/.../<number>) in Walmart URL: {url}")

        # Check the server state before spending a browser launch on a sold-out item
        state = self._fetch_order_state(url)
        if state and not state['orderable']:
            return self._cart_result(False, f"Cannot add to cart: {self._unorderable_reason(state)}")
        product_name = (state or {}).get('name')

        logger.info(f"Adding Walmart product to cart: {url} (item {item_id}), quantity: {quantity}")
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
                    EC.presence_of_element_located((By.CSS_SELECTOR, f'{BUY_BUTTON_SELECTOR}, [itemprop="name"], h1')))
            except TimeoutException:
                logger.warning("Timed out waiting for Walmart product markup")
            time.sleep(2)

            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            button = self._find_buy_button(driver, item_id, product_name)
            if button is None:
                return self._cart_result(
                    False,
                    "No add-to-cart / pre-order button for this item in the buy box - it may be unavailable",
                    driver=driver)

            text = (button.text or '').strip()
            if button.get_attribute('disabled') is not None or not button.is_enabled():
                return self._cart_result(False, f'Cannot add to cart: the "{text}" button is disabled', driver=driver)

            logger.debug(f"Clicking Walmart buy button text={text!r}")
            self._click(driver, button)
            time.sleep(3)

            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            # Verify on the cart page rather than trusting the confirmation toast
            driver.get(WALMART_CART_URL)
            time.sleep(4)
            obstacle = self._page_obstacle(driver)
            if obstacle:
                return self._cart_result(False, obstacle, driver=driver)

            if not self._cart_contains(driver, item_id):
                return self._cart_result(
                    False, "Item was not found in the Walmart cart after clicking the button", driver=driver)

            note = ""
            if quantity > 1:
                if self._set_cart_quantity(driver, quantity):
                    note = f" (quantity set to {quantity})"
                else:
                    note = f" (quantity 1 - could not set quantity to {quantity})"

            logger.info(f"Walmart item {item_id} is in the cart")
            return self._cart_result(True, f"Product added to Walmart cart{note}",
                                     driver=driver, cart_url=driver.current_url)
        except ProfileBusyError as e:
            return self._cart_result(False, str(e), driver=driver)
        except Exception as e:
            logger.error(f"Error adding Walmart product to cart: {str(e)}", exc_info=True)
            return self._cart_result(False, f"Error: {str(e)}", driver=driver)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            profile.close()

    def _start_driver(self):
        """
        Launch Chrome on the shared Walmart profile, translating the launch failure
        that profile contention produces into something the caller can report.
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
        if os.environ.get('WALMART_HEADLESS', '1') != '0':
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
            logger.warning(f"Walmart add_to_cart failed: {message}")
        return {
            'success': success,
            'message': message,
            'cart_url': cart_url,
            'screenshot': screenshot,
        }

    def _page_obstacle(self, driver):
        """Return a failure message if the current page is a bot wall or a login wall, else None"""
        block_reason = detect_block_page(driver.page_source)
        if block_reason:
            return (f"Walmart showed a bot-protection page ({block_reason}). Run "
                    "`python test_walmart_cart.py --login` once to clear it in a visible window")
        challenge = self._challenge_present(driver)
        if challenge:
            return (f"Walmart showed a human-verification challenge ({challenge}). Run "
                    "`python test_walmart_cart.py --login` once and complete it in the browser window "
                    f"(profile {self.profile_dir}), then retry")
        if self._login_wall_present(driver):
            return ("Walmart is asking for a sign-in. Run `python test_walmart_cart.py --login` once, sign in "
                    f"to the browser window that opens (profile {self.profile_dir}), then retry")
        return None

    @staticmethod
    def _challenge_present(driver):
        """
        Walmart's press-and-hold verification, by its own wording. Returns the phrase
        that matched, or None. Detection only - the challenge is never automated.

        Like Target's, it renders inside the PerimeterX iframe, which is present on
        ordinary pages too: look at what is inside it, never at its existence.
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
        """True when Walmart has replaced the buy box with a sign-in prompt"""
        try:
            body = (driver.find_element(By.TAG_NAME, 'body').text or '').lower()
        except Exception:
            return False
        return any(marker in body for marker in SIGN_IN_TEXT_MARKERS)

    def _find_buy_button(self, driver, item_id, product_name=None):
        """
        Find THIS item's add-to-cart / pre-order button.

        Walmart server-renders working "Add to cart" buttons for recommended
        products, so a page-wide button[data-automation-id="atc"] frequently belongs
        to a carousel item. A button is only accepted when it can be attributed to
        the item being carted: inside the buy box, or labelled with this product.
        Anything unattributable is skipped rather than guessed at.
        """
        for selector in BUY_BOX_CONTAINER_SELECTORS:
            for container in driver.find_elements(By.CSS_SELECTOR, selector):
                for button in container.find_elements(By.CSS_SELECTOR, BUY_BUTTON_SELECTOR):
                    if self._button_is_buyable(button):
                        logger.debug(f"Found buy button inside buy-box container {selector!r}")
                        return button

        # No recognisable buy box: fall back to a page-wide search, but only accept a
        # button whose own label ties it to this item.
        for button in driver.find_elements(By.CSS_SELECTOR, BUY_BUTTON_SELECTOR):
            if not self._button_is_buyable(button):
                continue
            if self._button_matches_item(button, item_id, product_name):
                logger.debug("Found buy button by item attribution outside a recognised buy box")
                return button
            logger.debug("Skipping an add-to-cart button that belongs to a different item")
        return None

    @staticmethod
    def _button_is_buyable(button):
        """True when the button is a real add-to-cart / pre-order control"""
        try:
            text = (button.text or '').strip().lower()
            label = (button.get_attribute('aria-label') or '').lower()
        except Exception:
            return False
        haystack = f"{text} {label}"
        return any(marker in haystack for marker in BUY_BUTTON_TEXTS)

    @staticmethod
    def _button_matches_item(button, item_id, product_name=None):
        """True when the button's own attributes tie it to the item being carted"""
        try:
            attributes = ' '.join(filter(None, (
                button.get_attribute('aria-label'),
                button.get_attribute('data-item-id'),
                button.get_attribute('data-us-item-id'),
                button.get_attribute('data-dca-extras'),
            ))).lower()
        except Exception:
            return False
        if item_id and item_id in attributes:
            return True
        if product_name:
            # aria-label is "Add to cart - <product name>"; compare on a distinctive slice
            name = product_name.lower()
            return len(name) >= 12 and name[:40] in attributes
        return False

    @staticmethod
    def _click(driver, element):
        """Click via JS when needed: Walmart's sticky header intercepts ordinary clicks"""
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        except Exception:
            pass
        try:
            element.click()
        except Exception as e:
            logger.debug(f"Native click failed ({str(e)}); falling back to a JS click")
            driver.execute_script("arguments[0].click();", element)

    @staticmethod
    def _cart_contains(driver, item_id):
        """
        True when the cart page shows the item that was just added.

        Matching on the item id rather than the name: the id is what the cart links
        back to, and it cannot collide with a similarly-named accessory.
        """
        try:
            source = driver.page_source or ''
        except Exception:
            source = ''
        if item_id and item_id in source:
            return True
        try:
            for link in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/ip/"]'):
                if item_id and item_id in (link.get_attribute('href') or ''):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _set_cart_quantity(driver, quantity):
        """Best effort quantity change on the cart page. Returns True only if it took."""
        try:
            selectors = 'select[aria-label*="Quantity"], select[data-automation-id*="quantity"]'
            for select in driver.find_elements(By.CSS_SELECTOR, selectors):
                for option in select.find_elements(By.TAG_NAME, 'option'):
                    if (option.get_attribute('value') or '').strip() == str(quantity):
                        option.click()
                        time.sleep(2)
                        return True
        except Exception as e:
            logger.debug(f"Could not set Walmart cart quantity: {str(e)}")
        return False
