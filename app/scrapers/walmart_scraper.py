import re
import logging
import requests
from bs4 import BeautifulSoup
from app.scrapers.common import DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text

# Set up logging
logger = logging.getLogger('app.scrapers.walmart')

class WalmartScraper:
    """Scraper specifically for Walmart products"""
    
    def __init__(self):
        """Initialize the Walmart scraper."""
        logger.debug("Initializing WalmartScraper")
        self.headers = dict(DEFAULT_HEADERS)
    
    def scrape_product(self, url):
        """
        Scrape product information from Walmart URL
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            block_reason = detect_block_page(response.text)
            if block_reason:
                logger.warning(f"Walmart returned a block page for {url} (HTTP {response.status_code}): {block_reason}")
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
            logger.error(f"Error scraping Walmart product: {str(e)}")
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