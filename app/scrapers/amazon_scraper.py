import re
import logging
import requests
from bs4 import BeautifulSoup
from app.scrapers.common import DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text, detect_chrome_major
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

class AmazonScraper:
    """Scraper specifically for Amazon products"""
    
    def __init__(self):
        """Initialize the Amazon scraper."""
        logger.debug("Initializing AmazonScraper")
        self.headers = dict(DEFAULT_HEADERS)
    
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
        Add a product to the Amazon cart
        
        Args:
            url: The product URL to add to cart
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: A dictionary with cart status information
        """
        logger.info(f"Adding Amazon product to cart: {url}, quantity: {quantity}")
        
        try:
            # Create a new undetected-chromedriver instance
            logger.debug("Starting undetected-chromedriver for Amazon add to cart")
            options = uc.ChromeOptions()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            
            # Proxy setup if needed
            # options.add_argument('--proxy-server=your-proxy-server')
            
            driver = uc.Chrome(options=options, version_main=detect_chrome_major())
            
            try:
                # Set window size
                driver.set_window_size(1366, 768)
                
                # Navigate to the product page
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