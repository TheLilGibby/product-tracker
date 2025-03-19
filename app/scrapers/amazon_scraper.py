import re
import logging
import requests
from bs4 import BeautifulSoup

# Set up logging
logger = logging.getLogger('app.scrapers.amazon')

class AmazonScraper:
    """Scraper specifically for Amazon products"""
    
    def __init__(self):
        """Initialize the Amazon scraper."""
        logger.debug("Initializing AmazonScraper")
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    
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
        """Extract product availability from Amazon page"""
        logger.debug("AmazonScraper: Extracting availability")
        try:
            availability = soup.find(id='availability')
            if availability:
                text = availability.get_text().strip().lower()
                available = 'in stock' in text
                logger.debug(f"Found availability from availability element: {available}")
                return available
                
            # Check add to cart button existence
            add_to_cart = soup.find(id='add-to-cart-button')
            if add_to_cart:
                logger.debug("Found 'Add to Cart' button, product is available")
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