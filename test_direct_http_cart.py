"""
Direct HTTP-based test for checking if a product is available without Selenium
This avoids the issues with running Chrome in Docker containers
"""

import os
import sys
import logging
import requests
from datetime import datetime
import traceback
from bs4 import BeautifulSoup
import json
import re

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_direct_http_test():
    """Run a direct HTTP-based test to check product availability"""
    try:
        logger.info("Starting direct HTTP test")
        
        # Import necessary modules
        from app import create_app, db
        from app.models.product import Product
        
        # Initialize Flask app context
        app = create_app()
        with app.app_context():
            # Get product ID 10 from the database
            product = Product.query.get(10)
            
            if not product:
                logger.error("Product with ID 10 not found")
                return False
            
            logger.info(f"Product details - ID: {product.id}, Name: {product.name}")
            logger.info(f"URL: {product.url}")
            logger.info(f"Auto-cart enabled: {product.auto_cart_enabled}")
            logger.info(f"Last cart status: {product.last_cart_status}")
            
            # Determine the store type from the URL
            store_mapping = {
                "newegg.com": "newegg",
                "bestbuy.com": "bestbuy",
                "amazon.com": "amazon"
            }
            
            store_type = None
            for domain, store in store_mapping.items():
                if domain in product.url:
                    store_type = store
                    break
            
            if not store_type:
                logger.error(f"Unknown store type for URL: {product.url}")
                return False
            
            logger.info(f"Detected store type: {store_type}")
            
            # Use direct HTTP request to check product details
            result = check_product_http(product.url, store_type)
            
            # Display the results
            logger.info(f"Product check result:")
            logger.info(f"Name: {result.get('name', 'Unknown')}")
            logger.info(f"Price: ${result.get('price', 'Unknown')}")
            logger.info(f"Available: {result.get('available', False)}")
            logger.info(f"Image URL: {result.get('image_url', 'None')}")
            
            # Update the product's last cart attempt and status
            product.last_cart_attempt = datetime.now()
            
            if result.get('available', False):
                product.last_cart_status = f"Product is available at ${result.get('price', 'Unknown')} - HTTP check"
            else:
                product.last_cart_status = "Product is not available - HTTP check"
            
            # Save changes to the database
            db.session.commit()
            
            return result.get('available', False)
                
    except Exception as e:
        logger.error(f"Error in run_direct_http_test: {e}")
        logger.error(traceback.format_exc())
        return False

def check_product_http(url, store_type):
    """
    Check product details using direct HTTP requests
    
    Args:
        url: Product URL
        store_type: Store type (newegg, bestbuy, amazon)
        
    Returns:
        dict: Product details
    """
    logger.info(f"Checking product via HTTP for {store_type}: {url}")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': f"https://www.{store_type}.com/",
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="122", "Chromium";v="122"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Upgrade-Insecure-Requests': '1'
    }
    
    try:
        logger.info(f"Sending HTTP request to: {url}")
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            logger.warning(f"Failed to get page, status code: {response.status_code}")
            return {
                'name': f"Product at {url}",
                'price': None,
                'available': False,
                'image_url': None
            }
        
        # Parse the HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract product info based on store type
        if store_type == "newegg":
            return extract_newegg_product_info(soup, url)
        elif store_type == "bestbuy":
            return extract_bestbuy_product_info(soup, url)
        elif store_type == "amazon":
            return extract_amazon_product_info(soup, url)
        else:
            logger.warning(f"No extraction method for store type: {store_type}")
            return {
                'name': f"Product at {url}",
                'price': None,
                'available': False,
                'image_url': None
            }
            
    except Exception as e:
        logger.error(f"Error in HTTP request: {str(e)}")
        return {
            'name': f"Product at {url}",
            'price': None,
            'available': False,
            'image_url': None
        }

def extract_newegg_product_info(soup, url):
    """Extract product info from Newegg HTML"""
    try:
        # Extract product name
        name = extract_newegg_name(soup)
        
        # Extract product price
        price = extract_newegg_price(soup)
        
        # Extract product availability
        available = extract_newegg_availability(soup)
        
        # Extract product image URL
        image_url = extract_newegg_image(soup)
        
        logger.info(f"Extracted Newegg product - Name: {name}, Price: {price}, Available: {available}")
        
        if not name or name == "Unknown Product":
            # Try to get product ID from URL
            product_id = None
            id_match = re.search(r'/p/([A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,5})', url)
            if id_match:
                product_id = id_match.group(1)
                name = f"Newegg Product {product_id}"
        
        return {
            'name': name,
            'price': price,
            'available': available,
            'image_url': image_url
        }
    except Exception as e:
        logger.error(f"Error extracting Newegg product info: {str(e)}")
        return {
            'name': "Unknown Newegg Product",
            'price': None,
            'available': False,
            'image_url': None
        }

def extract_newegg_name(soup):
    """Extract product name from Newegg HTML"""
    try:
        # Try to find the product title in various locations
        selectors = [
            'h1.product-title',
            '[data-selenium="product-title"]',
            '.product-title',
            '.page-title'
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

def extract_newegg_price(soup):
    """Extract product price from Newegg HTML"""
    try:
        # Try various price selectors
        selectors = [
            '.price-current',
            '.price-current strong',
            '.product-price',
            '.product-price strong',
            '[data-selenium="salePrice"]',
            '[data-selenium="itemPrice"]',
            '.price'
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
            
        return None
    except Exception as e:
        logger.error(f"Error extracting price from HTML: {str(e)}")
        return None

def extract_newegg_availability(soup):
    """Extract product availability from Newegg HTML"""
    try:
        # Check for "Add to cart" button (enabled)
        add_to_cart_buttons = soup.select('button.btn-primary')
        for button in add_to_cart_buttons:
            if button.get('disabled') is None and ('add to cart' in button.text.lower() or 'add' in button.text.lower()):
                return True
        
        # Look for in-stock text
        for element in soup.select('.product-inventory, [data-selenium="inStock"]'):
            text = element.text.lower()
            if 'in stock' in text and 'out of stock' not in text:
                return True
            
        # Look for out-of-stock indicators
        out_of_stock_elements = soup.find_all(string=lambda text: text and ('out of stock' in text.lower() or 'OUT OF STOCK' in text))
        if out_of_stock_elements:
            return False
        
        # Find price elements
        price_elements = soup.select('.price, .price-current')
        for element in price_elements:
            if element and '$' in element.text:
                # If a price is displayed, it's likely in stock
                return True
            
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
            
        # If we can't determine, default to False
        return False
    except Exception as e:
        logger.error(f"Error extracting availability from HTML: {str(e)}")
        return False

def extract_newegg_image(soup):
    """Extract product image URL from Newegg HTML"""
    try:
        # Try various image selectors
        selectors = [
            '.product-view-img-original',
            '[data-selenium="product-image"]',
            '.product-gallery img',
            '.swiper-zoom-container img',
            '.product-view-img-container img',
            '#product_preview_img',
            'div.swiper-slide.swiper-slide-active img',
            'img.mainSlide'
        ]
        
        for selector in selectors:
            element = soup.select_one(selector)
            if element and element.get('src'):
                return element.get('src')
        
        # Try meta tags
        meta_image = soup.find('meta', property='og:image')
        if meta_image and meta_image.get('content'):
            return meta_image.get('content')
        
        # Try media gallery
        for media in soup.select('[data-slide-index="0"] img'):
            if media.get('src'):
                return media.get('src')
            
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
            if any(term in src.lower() for term in ['product', 'gallery', 'large']):
                return src
            
        return None
    except Exception as e:
        logger.error(f"Error extracting image URL from HTML: {str(e)}")
        return None

def extract_bestbuy_product_info(soup, url):
    """Extract product info from Best Buy HTML"""
    # This is a stub - implement if needed
    return {
        'name': "Best Buy product extraction not implemented",
        'price': None,
        'available': False,
        'image_url': None
    }

def extract_amazon_product_info(soup, url):
    """Extract product info from Amazon HTML"""
    # This is a stub - implement if needed
    return {
        'name': "Amazon product extraction not implemented",
        'price': None,
        'available': False,
        'image_url': None
    }

if __name__ == "__main__":
    # Run the HTTP test
    success = run_direct_http_test()
    
    if success:
        logger.info("Direct HTTP test completed successfully! Product is available.")
        sys.exit(0)
    else:
        logger.error("Direct HTTP test failed or product is not available")
        sys.exit(1) 