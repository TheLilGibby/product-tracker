import re
import logging
import requests
from bs4 import BeautifulSoup
from app.scrapers.common import DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text

# Set up logging
logger = logging.getLogger('app.scrapers.microcenter')

class MicrocenterScraper:
    """Scraper specifically for Microcenter products"""
    
    def __init__(self):
        """Initialize the Microcenter scraper."""
        logger.debug("Initializing MicrocenterScraper")
        self.headers = dict(DEFAULT_HEADERS)
    
    def scrape_product(self, url):
        """
        Scrape product information from Microcenter URL
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            block_reason = detect_block_page(response.text)
            if block_reason:
                logger.warning(f"Microcenter returned a block page for {url} (HTTP {response.status_code}): {block_reason}")
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
            logger.error(f"Error scraping Microcenter product: {str(e)}")
            return None
    
    def extract_name(self, soup):
        """Extract product name from Microcenter page"""
        logger.debug("MicrocenterScraper: Extracting name")
        try:
            # Try product title element
            title_element = soup.find('h1', {'class': 'product-header'})
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
        """Extract product price from Microcenter page"""
        logger.debug("MicrocenterScraper: Extracting price")
        try:
            # Try various price selectors
            price_selectors = [
                '#pricing',
                '.price-wrapper',
                '[itemprop="price"]'
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
            
            logger.warning("Could not find product price")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None
    
    def extract_availability(self, soup):
        """Extract product availability from Microcenter page"""
        logger.debug("MicrocenterScraper: Extracting availability")
        try:
            # Check for out-of-stock indicators
            out_of_stock = soup.select_one('.inventory .out-of-stock')
            if out_of_stock:
                logger.debug("Product is out of stock")
                return False
            
            # Check for add to cart button
            add_to_cart = soup.select_one('.add-to-cart')
            if add_to_cart and not add_to_cart.get('disabled'):
                logger.debug("Found enabled add to cart button")
                return True

            # Check availability text (pre-order counts as available)
            availability = soup.select_one('.inventory')
            if availability:
                text = availability.get_text().strip().lower()
                if is_preorder_text(text):
                    logger.debug("Inventory text says pre-order, treating as available")
                    return True
                available = 'in stock' in text
                logger.debug(f"Found availability from text: {available}")
                return available
            
            logger.warning("Could not determine product availability")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False
    
    def extract_image_url(self, soup):
        """Extract product image URL from Microcenter page"""
        logger.debug("MicrocenterScraper: Extracting image URL")
        try:
            # Try various image selectors
            img = soup.select_one('.product-image img')
            if img and img.get('src'):
                image_url = img.get('src')
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