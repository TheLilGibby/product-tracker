import os
import re
import logging
import requests
from bs4 import BeautifulSoup
import time
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import base64
from io import BytesIO
from PIL import Image

# Set up logging
logger = logging.getLogger('app.scrapers.amazon')

# Buy-box copy that means the featured offer is not currently purchasable.
# "in stock" is matched separately with a lookbehind so "back in stock" in
# "we don't know when this will be back in stock" is not treated as available.
_UNAVAILABLE_MARKERS = (
    'currently unavailable',
    'temporarily out of stock',
    'out of stock',
    'no featured offers',
    "we don't know when",
    'not currently available',
    'will be released',
)
_IN_STOCK_RE = re.compile(r'(?<!back )in stock', re.IGNORECASE)
_PREORDER_RE = re.compile(r'pre[\s-]?order', re.IGNORECASE)


def amazon_cookies_file_path():
    """Where pasted Amazon cookies are stored so they survive restarts."""
    if os.path.isdir('/app/data'):
        return os.path.join('/app/data', 'amazon_cookies.txt')
    return os.path.join(os.path.expanduser('~'), '.chrome_profiles', 'amazon_cookies.txt')


def load_amazon_cookies():
    """Cookie header string for the logged-in Amazon session, or ''."""
    env_value = (os.environ.get('AMAZON_COOKIES') or '').strip()
    if env_value:
        return env_value
    path = amazon_cookies_file_path()
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as handle:
                return handle.read().strip()
    except OSError as exc:
        logger.warning(f"Could not read Amazon cookies file: {exc}")
    return ''


# A Cookie header is visible ASCII and spaces. Anything outside that - a
# newline above all else - is how a pasted value turns into extra lines in
# .env, where it would set arbitrary config on the next load. Rejected rather
# than stripped: silently dropping part of a credential yields a cookie that
# fails later in a confusing way.
_COOKIE_HEADER_RE = re.compile(r'^[\x20-\x7e]*$')


def validate_cookie_header(cookie_header):
    """Return the cleaned header, or raise ValueError if it cannot be stored."""
    cookie_header = (cookie_header or '').strip()
    if not _COOKIE_HEADER_RE.match(cookie_header):
        raise ValueError(
            'Amazon cookies contain characters that are not valid in a Cookie '
            'header (a line break or a control character). Copy the value '
            'again as a single line.'
        )
    return cookie_header


def save_amazon_cookies(cookie_header):
    """Persist Amazon cookies to the process env and the data file."""
    cookie_header = validate_cookie_header(cookie_header)
    os.environ['AMAZON_COOKIES'] = cookie_header
    path = amazon_cookies_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # The file holds live session cookies. os.open sets the mode as the file is
    # created, so it never exists world-readable; an existing file keeps its
    # old mode, hence the chmod. Both are no-ops on Windows, which is fine -
    # the deployment that matters here is the Linux container.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(cookie_header)
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.debug(f"Could not tighten permissions on the cookies file: {exc}")
    return path


# Names shown in Chrome Application → Cookies → https://www.amazon.com
AMAZON_COOKIE_FIELDS = (
    ('at-main', 'amazon_at_main'),
    ('sess-at-main', 'amazon_sess_at_main'),
    ('session-id', 'amazon_session_id'),
    ('session-token', 'amazon_session_token'),
    ('ubid-main', 'amazon_ubid_main'),
)


def _clean_cookie_value(cookie_name, value):
    """Strip table copy/paste extras: wrapping quotes and a leading name=."""
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].strip()
    prefix = cookie_name + '='
    if value.lower().startswith(prefix.lower()):
        value = value[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1].strip()
    return value


def cookie_header_from_form(form):
    """
    Build a Cookie header from the settings form.

    Named fields (Application → Cookies table) are the main path. A pasted
    full header in amazon_cookies still works if the user has one.
    """
    if form.get('clear_amazon_cookies'):
        return ''

    full = (form.get('amazon_cookies') or '').strip()
    if full:
        return full

    parts = []
    for cookie_name, field_name in AMAZON_COOKIE_FIELDS:
        value = _clean_cookie_value(cookie_name, form.get(field_name))
        if value:
            parts.append(f'{cookie_name}={value}')
    return '; '.join(parts)

class AmazonScraper:
    """Scraper specifically for Amazon products"""
    
    def __init__(self):
        """Initialize the Amazon scraper."""
        logger.debug("Initializing AmazonScraper")
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "amazon_profile")
        os.makedirs(self.profile_dir, exist_ok=True)

    def _get_chrome_options(self):
        """Fresh ChromeOptions with the persistent Amazon profile (do not reuse)."""
        options = uc.ChromeOptions()
        options.add_argument(f'--user-data-dir={self.profile_dir}')
        options.add_argument('--profile-directory=Default')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        if os.environ.get('AMAZON_HEADLESS', '1') != '0':
            options.add_argument('--headless=new')
        return options

    def _parse_cookie_header(self, cookie_header):
        cookies = []
        for pair in (cookie_header or '').split(';'):
            pair = pair.strip()
            if not pair or '=' not in pair:
                continue
            name, value = pair.split('=', 1)
            name, value = name.strip(), _clean_cookie_value(name.strip(), value)
            if name:
                cookies.append((name, value))
        return cookies

    def _apply_amazon_cookies(self, driver):
        """Inject the user's Amazon session cookies into this browser."""
        cookie_header = load_amazon_cookies()
        if not cookie_header:
            logger.warning("No Amazon cookies configured; cart will be a guest session")
            return 0

        applied = 0
        for name, value in self._parse_cookie_header(cookie_header):
            added = False
            for domain in ('.amazon.com', 'www.amazon.com', 'amazon.com'):
                try:
                    driver.add_cookie({
                        'name': name,
                        'value': value,
                        'domain': domain,
                        'path': '/',
                    })
                    applied += 1
                    added = True
                    break
                except Exception:
                    continue
            if not added:
                logger.debug(f"Could not add Amazon cookie {name}")
        logger.info(f"Applied {applied} Amazon cookie(s) for a logged-in cart")
        return applied

    def _amazon_looks_signed_in(self, driver):
        """Best-effort check of the Amazon nav account line."""
        try:
            el = driver.find_element(By.ID, 'nav-link-accountList-nav-line-1')
            text = (el.text or '').strip().lower()
            return bool(text) and 'sign in' not in text
        except Exception:
            return False
    
    def scrape_product(self, url):
        """
        Scrape product information from Amazon URL
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        try:
            response = requests.get(url, headers=self.headers)
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
        Add a product to the Amazon cart
        
        Args:
            url: The product URL to add to cart
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: A dictionary with cart status information
        """
        logger.info(f"Adding Amazon product to cart: {url}, quantity: {quantity}")
        
        try:
            logger.debug("Starting undetected-chromedriver for Amazon add to cart")
            options = self._get_chrome_options()
            driver = uc.Chrome(options=options)
            
            try:
                driver.set_window_size(1366, 768)

                logger.debug("Opening Amazon so session cookies can be applied")
                driver.get('https://www.amazon.com/')
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                self._apply_amazon_cookies(driver)
                driver.refresh()
                time.sleep(1)
                if not self._amazon_looks_signed_in(driver):
                    logger.warning(
                        "Amazon nav still looks signed out. Paste a fresh Cookie header from "
                        "your logged-in browser into Settings → Amazon Auto-Cart."
                    )

                logger.debug(f"Navigating to {url}")
                driver.get(url)
                
                # Wait for the page to load
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Update quantity if needed (only if quantity > 1)
                if quantity > 1:
                    try:
                        logger.debug(f"Setting quantity to {quantity}")
                        quantity_dropdown = WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.ID, "quantity"))
                        )
                        quantity_dropdown.click()
                        time.sleep(1)
                        
                        # Find and click the desired quantity option
                        # This works for quantities up to 10 which Amazon typically shows in dropdown
                        if quantity <= 10:
                            quantity_option = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.XPATH, f"//select[@id='quantity']/option[@value='{quantity}']"))
                            )
                            quantity_option.click()
                            time.sleep(1)
                    except (TimeoutException, NoSuchElementException) as e:
                        logger.warning(f"Could not set quantity: {str(e)}")
                
                # Check if there's an "Add to Cart" button
                try:
                    add_to_cart_button = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.ID, "add-to-cart-button"))
                    )
                    logger.debug("Found Add to Cart button, clicking...")
                    add_to_cart_button.click()
                    
                    # Wait for the cart confirmation
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.ID, "attach-sidesheet-checkout-button"))
                        )
                        logger.debug("Product successfully added to cart")
                        
                        # Take a screenshot
                        screenshot = self._take_screenshot(driver)
                        
                        # Get the cart URL
                        driver.get("https://www.amazon.com/gp/cart/view.html")
                        time.sleep(2)
                        cart_url = driver.current_url
                        
                        return {
                            'success': True,
                            'message': "Product successfully added to cart",
                            'cart_url': cart_url,
                            'screenshot': screenshot
                        }
                    except TimeoutException:
                        # Sometimes Amazon doesn't show the checkout sheet, try another approach
                        logger.debug("No side checkout sheet, trying to proceed to cart")
                        
                        try:
                            # Try to click "Cart" button if available
                            cart_button = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.ID, "nav-cart"))
                            )
                            cart_button.click()
                            time.sleep(2)
                            
                            # Check if the product is in cart
                            items_in_cart = len(driver.find_elements(By.CSS_SELECTOR, ".sc-list-item"))
                            if items_in_cart > 0:
                                logger.debug(f"Found {items_in_cart} items in cart")
                                screenshot = self._take_screenshot(driver)
                                return {
                                    'success': True,
                                    'message': f"Product added to cart ({items_in_cart} items in cart)",
                                    'cart_url': driver.current_url,
                                    'screenshot': screenshot
                                }
                            else:
                                return {
                                    'success': False,
                                    'message': "Product could not be added to cart",
                                    'cart_url': driver.current_url,
                                    'screenshot': self._take_screenshot(driver)
                                }
                        except TimeoutException:
                            logger.warning("Could not verify if product was added to cart")
                            screenshot = self._take_screenshot(driver)
                            return {
                                'success': False,
                                'message': "Could not verify if product was added to cart",
                                'cart_url': None,
                                'screenshot': screenshot
                            }
                except TimeoutException:
                    logger.warning("Could not find Add to Cart button")
                    
                    # Try to find "Buy Now" button instead
                    try:
                        buy_now_button = WebDriverWait(driver, 3).until(
                            EC.element_to_be_clickable((By.ID, "buy-now-button"))
                        )
                        logger.debug("Found Buy Now button but not Add to Cart - product may require special handling")
                        screenshot = self._take_screenshot(driver)
                        return {
                            'success': False,
                            'message': "Product requires special handling (only Buy Now available)",
                            'cart_url': None,
                            'screenshot': screenshot
                        }
                    except TimeoutException:
                        logger.error("No Add to Cart or Buy Now buttons found")
                        screenshot = self._take_screenshot(driver)
                        return {
                            'success': False,
                            'message': "No Add to Cart or Buy Now buttons found - product may be unavailable",
                            'cart_url': None,
                            'screenshot': screenshot
                        }
            finally:
                driver.quit()
                
        except Exception as e:
            logger.error(f"Error adding Amazon product to cart: {str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f"Error: {str(e)}",
                'cart_url': None,
                'screenshot': None
            }
            
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
    
    def _buy_button_enabled(self, soup, button_id):
        """True when the named buy-box input exists and is not disabled."""
        button = soup.find(id=button_id)
        if not button:
            return False
        if button.has_attr('disabled') or str(button.get('aria-disabled', '')).lower() == 'true':
            return False
        if button.find_parent(class_=re.compile(r'a-button-disabled')):
            return False
        return True

    def extract_availability(self, soup):
        """Extract product availability from Amazon page"""
        logger.debug("AmazonScraper: Extracting availability")
        try:
            # Read buy-box copy first. A leftover Buy Now / Pre-order button is
            # not the same as a current in-stock offer — Amazon also serves
            # "Currently unavailable" / future-release text in #availability.
            for node in (
                soup.find(id='availability'),
                soup.find(id='outOfStock'),
                soup.select_one('#availabilityInsideBuyBox_feature_div'),
            ):
                if not node:
                    continue
                text = node.get_text(' ', strip=True)
                if not text:
                    continue
                lowered = text.lower()
                if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
                    logger.debug(f"Availability text is unavailable: {text[:160]}")
                    return False
                if _PREORDER_RE.search(text):
                    logger.debug(f"Availability text is pre-order, not treating as in stock: {text[:160]}")
                    return False
                if _IN_STOCK_RE.search(text):
                    logger.debug("Availability text says in stock")
                    return True

            if self._buy_button_enabled(soup, 'add-to-cart-button'):
                logger.debug("Found enabled #add-to-cart-button, product is available")
                return True

            logger.warning("Could not determine product availability")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False
    
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