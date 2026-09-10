import re
import logging
import time
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup
from app.scrapers.common import CHROME_VERSION, DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text, detect_chrome_major
import platform
import os
import random
import json
import requests
from app.captcha import TwoCaptcha

# Set up logging
logger = logging.getLogger('app.scrapers.adorama')

class AdoramaScraper:
    """Scraper specifically for Adorama products using undetected-chromedriver"""
    
    def __init__(self):
        """Initialize the Adorama scraper with undetected-chromedriver."""
        logger.debug("Initializing AdoramaScraper with undetected-chromedriver")
        
        # Set up persistent profile directory
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "adorama_profile")
        os.makedirs(self.profile_dir, exist_ok=True)
        
        # Initialize 2captcha solver with the API key from environment variables
        api_key = os.environ.get('TWOCAPTCHA_API_KEY')
        if not api_key:
            logger.warning("No 2captcha API key found in environment variables, fallback to default")
            api_key = '34c6d7576786cffd62e7483403ff6fd1'  # Fallback for backward compatibility
            
        self.solver = TwoCaptcha(api_key)
        logger.info("2captcha solver initialized with API key")
        
        # Store the most recent product URL for use in add_to_cart
        self.current_product_url = None
    
    def _get_chrome_options(self):
        """Get fresh ChromeOptions (to avoid reuse error)"""
        # Configure Chrome options
        options = uc.ChromeOptions()
        
        # Use persistent profile
        options.add_argument(f'--user-data-dir={self.profile_dir}')
        options.add_argument('--profile-directory=Default')
        
        # Add more realistic browser parameters
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        options.add_argument('--start-maximized')
        options.add_argument('--disable-gpu')
        options.add_argument('--enable-javascript')
        
        # Add WebGL and canvas support
        options.add_argument('--use-gl=desktop')
        options.add_argument('--enable-webgl')
        options.add_argument('--canvas-msaa-sample-count=2')
        options.add_argument('--ignore-certificate-errors')
        
        # Add privacy settings
        options.add_argument('--disable-web-security')
        options.add_argument('--allow-running-insecure-content')
        
        # Random viewport size from common resolutions
        viewports = [
            (1920, 1080),
            (1366, 768),
            (1536, 864),
            (1440, 900),
            (1280, 720)
        ]
        viewport = random.choice(viewports)
        options.add_argument(f'--window-size={viewport[0]},{viewport[1]}')
        
        # Random user agent from recent Chrome versions
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ]
        self.user_agent = random.choice(user_agents)
        options.add_argument(f'--user-agent={self.user_agent}')
        
        # Add touch events support
        options.add_argument('--touch-events=enabled')
        
        # Add fonts for better fingerprinting
        options.add_argument('--font-render-hinting=medium')
        
        return options
    
    def scrape_product(self, url):
        """Scrape product information from Adorama URL using undetected-chromedriver"""
        # Store the URL for potential add_to_cart operations
        self.current_product_url = url
        
        max_retries = 2
        current_retry = 0
        
        # Extract product ID from URL for fallback
        product_id = None
        
        # Try to extract product ID from URL
        id_match = re.search(r'/([^/]+)\.html', url)
        if id_match:
            product_id = id_match.group(1)
            logger.info(f"Extracted product ID: {product_id}")
            self.current_product_id = product_id
        
        # Try Selenium first
        while current_retry < max_retries:
            driver = None
            try:
                # Get fresh ChromeOptions to avoid reuse error
                options = self._get_chrome_options()
                
                # Add page load strategy for faster loading
                options.page_load_strategy = 'eager'
                
                # Create a new browser instance
                driver = uc.Chrome(options=options, version_main=detect_chrome_major())
                
                logger.debug(f"Accessing URL: {url}")
                
                # Use minimal stealth JS
                driver.execute_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false
                    });
                """)
                
                # Reduce delay - we need efficiency over stealth
                time.sleep(1)
                
                # Navigate to URL with timeout
                driver.set_page_load_timeout(30)  # 30 second timeout for page load
                driver.get(url)
                
                # Minimal waiting - we want to scrape fast
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Get the page source
                page_source = driver.page_source
                soup = BeautifulSoup(page_source, 'html.parser')
                
                # Extract product details
                name = self.extract_name(soup, driver)
                price = self.extract_price(soup, driver)
                available = self.extract_availability(soup, driver)
                image_url = self.extract_image_url(soup, driver)
                
                # If we got basic info, return it
                if name and name != "Unknown Product":
                    return {
                        'name': name,
                        'price': price,
                        'available': available,
                        'image_url': image_url
                    }
                
                # If we reached here, basic extraction failed
                current_retry += 1
                
            except Exception as e:
                logger.error(f"Error scraping Adorama product: {str(e)}")
                current_retry += 1
            finally:
                # Make sure we close the driver to free resources
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
        
        # If all retries failed, use simplified HTTP-based scraping as a fallback
        try:
            logger.info(f"Using HTTP-based fallback scraper for {url}")
            product_data = self.scrape_via_requests(url, product_id)
            if product_data:
                return product_data
        except Exception as e:
            logger.error(f"Error using fallback scraper: {str(e)}")

        # Nothing usable was extracted. Return None so callers keep the stored
        # name/price/availability instead of overwriting them with placeholders.
        logger.warning(f"Could not extract Adorama product data for {url}; leaving stored data unchanged")
        return None
    
    def scrape_via_requests(self, url, product_id=None):
        """Simple HTTP-based scraping as a fallback when Selenium fails."""
        logger.info(f"Attempting to scrape Adorama product via direct HTTP request: {url}")
        
        headers = {
            'User-Agent': DEFAULT_HEADERS['User-Agent'],
            'Accept': DEFAULT_HEADERS['Accept'],
            'Accept-Language': DEFAULT_HEADERS['Accept-Language'],
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://www.adorama.com/',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': f'"Not A(Brand";v="99", "Google Chrome";v="{CHROME_VERSION}", "Chromium";v="{CHROME_VERSION}"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Upgrade-Insecure-Requests': '1'
        }
        
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            block_reason = detect_block_page(response.text)
            if block_reason:
                logger.warning(f"Adorama returned a block page for {url} (HTTP {response.status_code}): {block_reason}")
                return None
            if response.status_code != 200:
                logger.warning(f"Failed to get page, status code: {response.status_code}")
                return None
            
            # Parse the HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract product info without using Selenium
            name = self.extract_name_from_html(soup)
            price = self.extract_price_from_html(soup)
            available = self.extract_availability_from_html(soup)
            image_url = self.extract_image_url_from_html(soup)
            
            logger.info(f"HTTP fallback scraper extracted: name={name}, price={price}, available={available}")
            
            if not name or name == "Unknown Product":
                # No product markup at all - most likely a challenge page we did not recognise
                logger.warning(f"HTTP fallback found no product name for {url}; not returning placeholder data")
                return None

            return {
                'name': name,
                'price': price,
                'available': available,
                'image_url': image_url
            }
        except Exception as e:
            logger.error(f"Error in HTTP-based scraper: {str(e)}")
            return None
            
    def extract_name_from_html(self, soup):
        """Extract product name from HTML without Selenium."""
        try:
            # Try to find the product title in various locations
            selectors = [
                'h1.pdp-title',  # Primary Adorama product title
                '.pdp-title',
                'h1.product-title',
                'h1'
            ]
            
            for selector in selectors:
                element = soup.select_one(selector)
                if element and element.text.strip():
                    return element.text.strip()
            
            # Try to get from meta tags
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                return meta_title.get('content').strip()
            
            # Try to find structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'name' in data:
                            return data['name']
                except:
                    continue
                
            return "Unknown Product"
        except Exception as e:
            logger.error(f"Error extracting name from HTML: {str(e)}")
            return "Unknown Product"

    def extract_price_from_html(self, soup):
        """Extract product price from HTML without Selenium."""
        try:
            # Try various price selectors for Adorama
            selectors = [
                '.current-price',  # Common Adorama price class
                '.price-current',
                '.price',
                '[data-price]',
                '.pdp-price',
                '.product-price',
                '.price-info'
            ]
            
            for selector in selectors:
                element = soup.select_one(selector)
                if element and element.text.strip():
                    # Extract dollar amount
                    price_text = element.text.strip()
                    price_match = re.search(r'(\$)?(\d+,?\d*\.?\d*)', price_text)
                    if price_match:
                        price_str = price_match.group(2).replace(',', '')
                        try:
                            return float(price_str)
                        except ValueError:
                            continue
            
            # Try to find structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'price' in offers:
                                return float(offers['price'])
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'price' in offer:
                                        return float(offer['price'])
                except:
                    continue
                
            # Try meta tags
            meta_price = soup.find('meta', property='og:price:amount')
            if meta_price and meta_price.get('content'):
                try:
                    return float(meta_price.get('content'))
                except:
                    pass
                
            # Try general price pattern in any text
            for element in soup.find_all(string=re.compile(r'\$\d+\.\d{2}')):
                price_match = re.search(r'\$(\d+,?\d*\.?\d*)', element)
                if price_match:
                    price_str = price_match.group(1).replace(',', '')
                    try:
                        return float(price_str)
                    except ValueError:
                        continue
                
            return None
        except Exception as e:
            logger.error(f"Error extracting price from HTML: {str(e)}")
            return None

    def extract_availability_from_html(self, soup):
        """Extract product availability from HTML without Selenium."""
        try:
            # Check for "Add to cart" button (enabled)
            add_to_cart_buttons = soup.select('button.add-to-cart, .pdp-add-to-cart')
            for button in add_to_cart_buttons:
                if button.get('disabled') is None and ('add to cart' in button.text.lower() or 'add' in button.text.lower() or is_preorder_text(button.text)):
                    return True
            
            # Look for in-stock text
            stock_indicators = soup.select('.stock-status, .availability, .inventory-status')
            for element in stock_indicators:
                text = element.text.lower()
                if is_preorder_text(text):
                    return True
                if 'in stock' in text and 'out of stock' not in text:
                    return True
                if 'out of stock' in text or 'unavailable' in text:
                    return False
                
            # Look for out-of-stock indicators
            out_of_stock_elements = soup.find_all(string=lambda text: text and ('out of stock' in text.lower() or 'unavailable' in text.lower()))
            if out_of_stock_elements:
                return False
            
            # Try to find structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'availability' in offers:
                                return 'InStock' in offers['availability']
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'availability' in offer:
                                        if 'InStock' in offer['availability']:
                                            return True
                except:
                    continue
                
            # Check for price elements - if price is shown, product is likely available
            price_elements = soup.select('.current-price, .price')
            for element in price_elements:
                if element and '$' in element.text:
                    return True
                
            # If we can't determine, default to False
            return False
        except Exception as e:
            logger.error(f"Error extracting availability from HTML: {str(e)}")
            return False

    def extract_image_url_from_html(self, soup):
        """Extract product image URL from HTML without Selenium."""
        try:
            # Try various image selectors for Adorama
            selectors = [
                '.pdp-main-image img',
                '.product-main-image img',
                '.product-gallery img',
                '.gallery-image img',
                '.product-image img',
                '.main-product-image'
            ]
            
            for selector in selectors:
                element = soup.select_one(selector)
                if element and element.get('src'):
                    return element.get('src')
            
            # Try meta tags
            meta_image = soup.find('meta', property='og:image')
            if meta_image and meta_image.get('content'):
                return meta_image.get('content')
            
            # Try structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'image' in data:
                            if isinstance(data['image'], str):
                                return data['image']
                            elif isinstance(data['image'], list) and len(data['image']) > 0:
                                return data['image'][0]
                except:
                    continue
                
            # Last resort - find any image that seems to be a product image
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if any(term in src.lower() for term in ['product', 'gallery', 'large', 'pdp']):
                    return src
                
            return None
        except Exception as e:
            logger.error(f"Error extracting image URL from HTML: {str(e)}")
            return None

    def extract_name(self, soup, driver):
        """Extract product name using Selenium"""
        logger.debug("AdoramaScraper: Extracting name")
        try:
            # Try multiple selectors for product name
            selectors = [
                (By.CSS_SELECTOR, "h1.pdp-title"),
                (By.CSS_SELECTOR, ".pdp-title"),
                (By.CSS_SELECTOR, "h1.product-title"),
                (By.TAG_NAME, "h1")
            ]
            
            for by, selector in selectors:
                try:
                    element = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((by, selector))
                    )
                    name = element.text.strip()
                    if name:
                        logger.debug(f"Found name: {name}")
                        return name
                except:
                    continue
            
            # Try structured data
            try:
                script = soup.find('script', {'type': 'application/ld+json'})
                if script and script.string:
                    import json
                    data = json.loads(script.string)
                    if isinstance(data, dict) and 'name' in data:
                        name = data['name']
                        logger.debug(f"Found name from structured data: {name}")
                        return name
            except:
                pass
            
            # Try meta tags
            try:
                meta_title = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:title"]')
                if meta_title:
                    name = meta_title.get_attribute('content')
                    if name:
                        logger.debug(f"Found name from meta: {name}")
                        return name
            except:
                pass
            
            logger.warning("Could not find product name")
            return "Unknown Product"
        except Exception as e:
            logger.error(f"Error extracting name: {str(e)}")
            return "Unknown Product"
    
    def extract_price(self, soup, driver):
        """Extract product price using Selenium"""
        logger.debug("AdoramaScraper: Extracting price")
        try:
            # Try multiple price selectors for Adorama
            price_selectors = [
                (By.CSS_SELECTOR, ".current-price"),
                (By.CSS_SELECTOR, ".pdp-price"),
                (By.CSS_SELECTOR, ".price-current"),
                (By.CSS_SELECTOR, ".price"),
                (By.CSS_SELECTOR, "[data-price]"),
                (By.CSS_SELECTOR, ".product-price"),
                (By.XPATH, "//*[contains(@class, 'price')]")
            ]
            
            for by, selector in price_selectors:
                try:
                    element = driver.find_element(by, selector)
                    price_text = element.text.strip()
                    
                    # If price element is found but empty, try getting text from child elements
                    if not price_text:
                        child_elements = element.find_elements(By.XPATH, ".//*")
                        for child in child_elements:
                            if child.text and '$' in child.text:
                                price_text = child.text
                                break
                    
                    if price_text:
                        # Extract dollar amount
                        price_match = re.search(r'(\$)?(\d+,?\d*\.?\d*)', price_text)
                        if price_match:
                            price_str = price_match.group(2).replace(',', '')
                            try:
                                price = float(price_str)
                                logger.debug(f"Found price: ${price}")
                                return price
                            except ValueError:
                                logger.warning(f"Could not convert price string to float: {price_str}")
                                continue
                except:
                    continue
            
            # Try data attributes
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, "[data-price]")
                for element in elements:
                    price_data = element.get_attribute("data-price")
                    if price_data:
                        try:
                            price = float(price_data)
                            logger.debug(f"Found price from data-price: ${price}")
                            return price
                        except ValueError:
                            pass
            except:
                pass
            
            # Try structured data
            try:
                structured_data = driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for data_element in structured_data:
                    try:
                        data = json.loads(data_element.get_attribute('textContent'))
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'price' in offers:
                                try:
                                    price = float(offers['price'])
                                    logger.debug(f"Found price from structured data: ${price}")
                                    return price
                                except (ValueError, TypeError):
                                    pass
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'price' in offer:
                                        try:
                                            price = float(offer['price'])
                                            logger.debug(f"Found price from structured data offers list: ${price}")
                                            return price
                                        except (ValueError, TypeError):
                                            pass
                    except:
                        pass
            except:
                pass
            
            # Look for prices in the entire page
            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text
                price_matches = re.findall(r'\$(\d+,?\d*\.?\d*)', page_text)
                if price_matches:
                    try:
                        price = float(price_matches[0].replace(',', ''))
                        logger.debug(f"Found price from page text: ${price}")
                        return price
                    except (ValueError, IndexError):
                        pass
            except:
                pass
            
            logger.warning("Could not extract price from page")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None
    
    def extract_availability(self, soup, driver):
        """Extract product availability using Selenium"""
        logger.debug("AdoramaScraper: Extracting availability")
        try:
            # Check "Add to cart" button availability
            add_to_cart_selectors = [
                (By.CSS_SELECTOR, "button.add-to-cart:not([disabled])"),
                (By.CSS_SELECTOR, ".pdp-add-to-cart:not([disabled])"),
                (By.XPATH, "//button[contains(text(), 'Add to Cart') and not(@disabled)]"),
                (By.XPATH, "//button[contains(@class, 'add-to-cart') and not(@disabled)]")
            ]
            
            for by, selector in add_to_cart_selectors:
                try:
                    element = driver.find_element(by, selector)
                    if element and element.is_displayed():
                        logger.debug("Found enabled Add to Cart button - product is in stock")
                        return True
                except:
                    continue
            
            # Check for explicit in-stock text
            stock_text_selectors = [
                (By.CSS_SELECTOR, ".stock-status"),
                (By.CSS_SELECTOR, ".availability"),
                (By.CSS_SELECTOR, ".inventory-status"),
                (By.XPATH, "//*[contains(text(), 'In Stock')]")
            ]
            
            for by, selector in stock_text_selectors:
                try:
                    element = driver.find_element(by, selector)
                    if element:
                        text = element.text.lower()
                        if "in stock" in text and "out of stock" not in text:
                            logger.debug(f"Found in-stock text: {text}")
                            return True
                        if "out of stock" in text or "unavailable" in text:
                            logger.debug(f"Found out-of-stock text: {text}")
                            return False
                except:
                    continue
            
            # Pre-order buttons and stock text count as available
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, "button.add-to-cart, .pdp-add-to-cart, .stock-status, .availability, .inventory-status"):
                    if element.is_displayed() and element.is_enabled() and is_preorder_text(element.text):
                        logger.debug("Found pre-order indicator - treating as available")
                        return True
            except:
                pass

            # Check for out-of-stock indicators
            out_of_stock_selectors = [
                (By.XPATH, "//*[contains(text(), 'OUT OF STOCK')]"),
                (By.XPATH, "//*[contains(text(), 'Out of Stock')]"),
                (By.XPATH, "//*[contains(text(), 'out of stock')]"),
                (By.XPATH, "//*[contains(text(), 'Unavailable')]"),
                (By.XPATH, "//button[contains(@class, 'add-to-cart') and @disabled]")
            ]
            
            for by, selector in out_of_stock_selectors:
                try:
                    element = driver.find_element(by, selector)
                    if element and element.is_displayed():
                        logger.debug("Found out-of-stock indicator")
                        return False
                except:
                    continue
            
            # Try structured data
            try:
                structured_data = driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for data_element in structured_data:
                    try:
                        data = json.loads(data_element.get_attribute('textContent'))
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'availability' in offers:
                                available = 'InStock' in offers['availability']
                                logger.debug(f"Found availability from structured data: {available}")
                                return available
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'availability' in offer:
                                        if 'InStock' in offer['availability']:
                                            return True
                    except:
                        pass
            except:
                pass
            
            # If we can find a current price, the product is likely in stock
            try:
                price_elements = driver.find_elements(By.CSS_SELECTOR, ".current-price, .price")
                for element in price_elements:
                    if element.is_displayed() and '$' in element.text:
                        logger.debug("Found price display - assuming product is in stock")
                        return True
            except:
                pass
            
            logger.warning("Could not determine product availability - defaulting to False")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False
    
    def extract_image_url(self, soup, driver):
        """Extract product image URL using Selenium"""
        logger.debug("AdoramaScraper: Extracting image URL")
        try:
            # Try multiple image selectors for Adorama
            selectors = [
                (By.CSS_SELECTOR, ".pdp-main-image img"),
                (By.CSS_SELECTOR, ".product-main-image img"),
                (By.CSS_SELECTOR, ".product-gallery img"),
                (By.CSS_SELECTOR, ".gallery-image img"),
                (By.CSS_SELECTOR, ".product-image img"),
                (By.CSS_SELECTOR, ".main-product-image"),
                (By.CSS_SELECTOR, "img.product-image")
            ]
            
            for by, selector in selectors:
                try:
                    element = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((by, selector))
                    )
                    image_url = element.get_attribute("src")
                    if image_url:
                        logger.debug(f"Found image URL: {image_url}")
                        return image_url
                except:
                    continue
            
            # Try meta tags
            try:
                meta_image = driver.find_element(By.CSS_SELECTOR, 'meta[property="og:image"]')
                if meta_image:
                    image_url = meta_image.get_attribute("content")
                    if image_url:
                        logger.debug(f"Found image URL from meta: {image_url}")
                        return image_url
            except:
                pass
            
            # Try json-ld structured data for images
            try:
                structured_data = driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for data_element in structured_data:
                    try:
                        data = json.loads(data_element.get_attribute('textContent'))
                        if isinstance(data, dict) and 'image' in data:
                            if isinstance(data['image'], str):
                                return data['image']
                            elif isinstance(data['image'], list) and len(data['image']) > 0:
                                return data['image'][0]
                    except:
                        pass
            except:
                pass
                
            # Last resort: look for any image with product in the src
            try:
                all_images = driver.find_elements(By.TAG_NAME, "img")
                for img in all_images:
                    src = img.get_attribute("src")
                    if src and any(term in src.lower() for term in ["product", "gallery", "large", "pdp"]):
                        return src
            except:
                pass
            
            logger.warning("Could not find product image URL")
            return None
        except Exception as e:
            logger.error(f"Error extracting image URL: {str(e)}")
            return None

    def _take_screenshot(self, driver):
        """Take a screenshot and return as base64 string"""
        try:
            import base64
            screenshot = driver.get_screenshot_as_base64()
            return screenshot
        except Exception as e:
            logger.error(f"Error taking screenshot: {str(e)}")
            return None

    def add_to_cart(self, quantity=1):
        """
        Add a product to Adorama cart.
        
        Args:
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: A dictionary with cart status information
                {
                    'success': True/False,
                    'message': str,
                    'cart_url': str,
                    'screenshot': str (base64)
                }
        """
        logger.info(f"Attempting to add product to Adorama cart with quantity {quantity}")
        
        if not hasattr(self, 'current_product_url') or not self.current_product_url:
            logger.error("No product URL available. Use scrape_product first.")
            return {
                'success': False,
                'message': "No product URL available. Please scrape the product first.",
                'cart_url': None,
                'screenshot': None
            }
        
        url = self.current_product_url
        driver = None
        
        try:
            # Get fresh ChromeOptions to avoid reuse error
            options = self._get_chrome_options()
            
            # Create browser instance
            driver = uc.Chrome(options=options, version_main=detect_chrome_major())
            
            # Navigate to product page
            logger.info(f"Navigating to product page: {url}")
            driver.get(url)
            
            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
            
            # Check if product is available
            try:
                # Look for typical out-of-stock indicators
                out_of_stock_elements = driver.find_elements(By.XPATH, 
                    "//div[contains(text(), 'Out of Stock') or contains(text(), 'Unavailable')]")
                
                if out_of_stock_elements:
                    screenshot = self._take_screenshot(driver)
                    return {
                        'success': False,
                        'message': "Product is out of stock",
                        'cart_url': None,
                        'screenshot': screenshot
                    }
            except Exception as e:
                logger.warning(f"Error checking stock status: {str(e)}")
            
            # Look for quantity field and set quantity if found
            try:
                quantity_field = driver.find_element(By.CSS_SELECTOR, 
                    "input[type='number'], input.quantity, [data-quantity]")
                quantity_field.clear()
                quantity_field.send_keys(str(quantity))
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Failed to set quantity: {str(e)}")
            
            # Find and click the Add to Cart button 
            try:
                # Try various Add to Cart selectors for Adorama
                add_to_cart_selectors = [
                    "//button[contains(text(), 'Add to Cart')]",
                    "//button[contains(@class, 'add-to-cart')]",
                    "//a[contains(text(), 'Add to Cart')]",
                    "//a[contains(@class, 'add-to-cart')]",
                    "//button[contains(@id, 'pdp-add-to-cart')]"
                ]
                
                add_button = None
                for selector in add_to_cart_selectors:
                    elements = driver.find_elements(By.XPATH, selector)
                    if elements:
                        add_button = elements[0]
                        break
                
                if not add_button:
                    screenshot = self._take_screenshot(driver)
                    return {
                        'success': False,
                        'message': "Could not find Add to Cart button",
                        'cart_url': None,
                        'screenshot': screenshot
                    }
                
                # Click the Add to Cart button
                logger.info("Clicking Add to Cart button")
                
                # Scroll to the button to make it visible
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", add_button)
                time.sleep(1)
                
                # Try regular click first
                try:
                    add_button.click()
                except Exception as e:
                    logger.warning(f"Regular click failed: {str(e)}, trying JavaScript click")
                    driver.execute_script("arguments[0].click();", add_button)
                
                # Wait for cart to update
                time.sleep(3)
                
                # Check if a popup appears and handle it
                try:
                    popup_close_buttons = driver.find_elements(By.XPATH, 
                        "//button[contains(@class, 'close') or contains(@class, 'btn-close')]")
                    if popup_close_buttons:
                        popup_close_buttons[0].click()
                        time.sleep(1)
                except Exception as e:
                    logger.warning(f"Error handling popup: {str(e)}")
                
                # Look for success indicators
                success_indicators = [
                    (By.XPATH, "//div[contains(text(), 'Item added to cart')]"),
                    (By.XPATH, "//div[contains(text(), 'Added to cart')]"),
                    (By.CSS_SELECTOR, ".cart-confirmation"),
                    (By.CSS_SELECTOR, ".mini-cart-content")
                ]
                
                success = False
                for by, selector in success_indicators:
                    try:
                        WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((by, selector))
                        )
                        success = True
                        break
                    except:
                        continue
                
                # Go to cart page to verify
                try:
                    cart_url_selectors = [
                        (By.CSS_SELECTOR, "a.view-cart"),
                        (By.XPATH, "//a[contains(text(), 'View Cart')]"),
                        (By.XPATH, "//a[contains(@href, '/cart')]")
                    ]
                    
                    cart_link = None
                    for by, selector in cart_url_selectors:
                        try:
                            elements = driver.find_elements(by, selector)
                            if elements:
                                cart_link = elements[0]
                                break
                        except:
                            continue
                    
                    if cart_link:
                        cart_link.click()
                    else:
                        # If no cart link found, navigate directly to cart URL
                        driver.get("https://www.adorama.com/cart")
                    
                    time.sleep(3)
                except Exception as e:
                    logger.warning(f"Error navigating to cart: {str(e)}")
                    # If navigation fails, try direct cart URL
                    driver.get("https://www.adorama.com/cart")
                    time.sleep(3)
                
                # Take a screenshot of the cart
                screenshot = self._take_screenshot(driver)
                
                # Check if the cart has items
                cart_empty = len(driver.find_elements(By.XPATH, 
                    "//div[contains(text(), 'Your shopping cart is empty')]")) > 0
                
                if cart_empty and not success:
                    return {
                        'success': False,
                        'message': "Failed to add product to cart",
                        'cart_url': driver.current_url,
                        'screenshot': screenshot
                    }
                
                return {
                    'success': True,
                    'message': "Product added to cart successfully",
                    'cart_url': driver.current_url,
                    'screenshot': screenshot
                }
                
            except Exception as e:
                logger.error(f"Error adding to cart: {str(e)}")
                if driver:
                    screenshot = self._take_screenshot(driver)
                else:
                    screenshot = None
                    
                return {
                    'success': False,
                    'message': f"Error adding to cart: {str(e)}",
                    'cart_url': None,
                    'screenshot': screenshot
                }
        except Exception as e:
            logger.error(f"Unexpected error in add_to_cart: {str(e)}")
            if driver:
                try:
                    screenshot = self._take_screenshot(driver)
                except:
                    screenshot = None
            else:
                screenshot = None
                
            return {
                'success': False,
                'message': f"Unexpected error: {str(e)}",
                'cart_url': None,
                'screenshot': screenshot
            }
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass 