import json
import re
import logging
import requests
from bs4 import BeautifulSoup

# Set up logging
logger = logging.getLogger('app.scrapers.bestbuy')

class BestBuyScraper:
    """Scraper specifically for Best Buy products"""
    
    def __init__(self):
        """Initialize the BestBuy scraper."""
        logger.debug("Initializing BestBuyScraper")
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    
    def scrape_product(self, url):
        """
        Scrape product information from Best Buy URL
        
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
                'image_url': self.extract_image_url(soup) or self.image_url_from_url(url, response.text)
            }
        except Exception as e:
            logger.error(f"Error scraping Best Buy product: {str(e)}")
            return None
    
    def extract_name(self, soup):
        """Extract product name from Best Buy page"""
        logger.debug("BestBuyScraper: Extracting name")
        try:
            # Try product title element (covering different layouts)
            title_selectors = [
                '.sku-title h1',
                '.heading-5',
                'h1.v-fw-regular',
                '[data-testid="heading-product-title"]',
                '.product-title'
            ]
            
            for selector in title_selectors:
                title_element = soup.select_one(selector)
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
        """Extract product price from Best Buy page"""
        logger.debug("BestBuyScraper: Extracting price")
        try:
            # Try various price selectors (covering different layouts)
            price_selectors = [
                '.priceView-customer-price span',
                '.priceView-hero-price span',
                '[data-testid="customer-price"]',
                '.pricing-price__regular',
                '.pricing-price__current-price',
                '.pricing-price__regular-price',
                '.pricing-price__sale-price'
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
            
            # Try price in JSON-LD data
            script_tags = soup.find_all('script', {'type': 'application/ld+json'})
            for script in script_tags:
                if script and script.string and ('price' in script.string or 'Product' in script.string):
                    price_match = re.search(r'"price"\s*:\s*"?(\d+\.?\d*)"?', script.string)
                    if price_match:
                        price = float(price_match.group(1))
                        logger.debug(f"Found price from JSON-LD: ${price}")
                        return price
            
            # Try looking for any number that looks like a price
            price_regex = re.compile(r'\$\s*(\d+(?:,\d+)*\.?\d*)')
            for element in soup.find_all(text=price_regex):
                match = price_regex.search(element)
                if match:
                    price_text = match.group(1).replace(',', '')
                    price = float(price_text)
                    logger.debug(f"Found price from regex: ${price}")
                    return price
            
            logger.warning("Could not find product price")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None
    
    def extract_availability(self, soup):
        """Extract product availability from Best Buy page"""
        logger.debug("BestBuyScraper: Extracting availability")
        try:
            # Check for out-of-stock indicators
            availability_selectors = [
                '.fulfillment-add-to-cart-button',
                '.add-to-cart-button',
                '.fulfillment-fulfillment-summary',
                '[data-testid="button-state"]',
                '[data-testid="availability-message"]',
                '.shop-availability-msg',
                '.availability'
            ]
            
            for selector in availability_selectors:
                element = soup.select_one(selector)
                if element:
                    text = element.get_text().strip().lower()
                    if any(status in text for status in ['sold out', 'unavailable', 'out of stock']):
                        logger.debug("Product is not available (from selector)")
                        return False
                    if 'add to cart' in text and 'disabled' not in element.get('class', []):
                        logger.debug("Product is available (from add to cart button)")
                        return True
            
            # Check for add to cart button status
            for btn_selector in ['.add-to-cart-button', '[data-button-state="ADD_TO_CART"]', 'button[data-track="Add to Cart"]']:
                btn = soup.select_one(btn_selector)
                if btn and 'disabled' not in btn.get('class', []):
                    if btn.name == 'button' and not btn.has_attr('disabled'):
                        logger.debug("Product is available (from button)")
                        return True
            
            # Check for specific unavailable texts
            for text in soup.find_all(text=True):
                if any(status in text.lower() for status in ['sold out', 'unavailable', 'out of stock']):
                    logger.debug("Product is not available (from text)")
                    return False
                
            # Check JSON-LD data for availability info
            script_tags = soup.find_all('script', {'type': 'application/ld+json'})
            for script in script_tags:
                if script and script.string and 'availability' in script.string:
                    if 'InStock' in script.string:
                        logger.debug("Product is available (from JSON-LD)")
                        return True
                    if 'OutOfStock' in script.string:
                        logger.debug("Product is not available (from JSON-LD)")
                        return False
            
            logger.warning("Could not determine product availability")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False
    
    @staticmethod
    def extract_sku(url, html=None):
        """Numeric SKU from a Best Buy URL, or from product-page HTML when given."""
        if url:
            for pattern in (r'/sku/(\d{7,8})', r'[?&]skuId=(\d{7,8})', r'/(\d{7,8})\.p\b'):
                match = re.search(pattern, url)
                if match:
                    return match.group(1)
        if html:
            match = re.search(r'data-testid="pdp-[a-z-]+-(\d{7,8})"', html)
            if match:
                return match.group(1)
            match = re.search(r'"sku"\s*:\s*"?(\d{7,8})"?', html)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def pisces_image_url(sku):
        """Best Buy CDN URL for a numeric SKU. Alphanumeric combo IDs are not valid here."""
        sku = str(sku or '').strip()
        if not sku.isdigit():
            return None
        return f"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/{sku[:4]}/{sku}_sd.jpg"

    @classmethod
    def image_url_from_url(cls, url, html=None):
        """Build a product image URL from a Best Buy link without loading the PDP."""
        return cls.pisces_image_url(cls.extract_sku(url, html))

    def extract_image_url(self, soup):
        """Extract product image URL from Best Buy page"""
        logger.debug("BestBuyScraper: Extracting image URL")
        try:
            product = self._jsonld_product(soup)
            if product:
                image = self._first_jsonld_image(product.get('image'))
                if image:
                    logger.debug(f"Found image URL from JSON-LD: {image}")
                    return image

            # Try various image selectors (covering different layouts)
            image_selectors = [
                '.primary-image',
                '.carousel-media img',
                '.product-image img',
                '[data-testid="carousel-img"]',
                '.gallery-player-inner img',
                '.primary-image-wrapper img'
            ]
            
            for selector in image_selectors:
                img = soup.select_one(selector)
                if img and img.get('src'):
                    image_url = img.get('src')
                    logger.debug(f"Found image URL: {image_url}")
                    return image_url
                if img and img.get('data-src'):
                    image_url = img.get('data-src')
                    logger.debug(f"Found image URL from data-src: {image_url}")
                    return image_url

            for img in soup.select('img[src*="pisces.bbystatic.com"], img[data-src*="pisces.bbystatic.com"]'):
                src = img.get('src') or img.get('data-src')
                if src and src.startswith('http'):
                    logger.debug(f"Found image URL from pisces img: {src}")
                    return src
                
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
    def _first_jsonld_image(images):
        if isinstance(images, str) and images.startswith('http'):
            return images
        if isinstance(images, dict) and str(images.get('url') or '').startswith('http'):
            return images['url']
        if isinstance(images, list):
            for item in images:
                found = BestBuyScraper._first_jsonld_image(item)
                if found:
                    return found
        return None 