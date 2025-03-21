import os
import sys
import logging
import requests
import datetime
import traceback
import json
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, urljoin

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_direct_http_test():
    """Run the direct HTTP test without using a browser"""
    try:
        logger.info("Starting direct HTTP test with cart functionality")
        
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
            
            # Check product via HTTP
            logger.info(f"Checking product via HTTP for {store_type}: {product.url}")
            product_info = check_product_http(product.url, store_type)
            
            if not product_info:
                logger.error("Failed to get product information via HTTP")
                return False
            
            logger.info("Product check result:")
            logger.info(f"Name: {product_info['name']}")
            logger.info(f"Price: ${product_info['price']}")
            logger.info(f"Available: {product_info['available']}")
            logger.info(f"Image URL: {product_info['image_url']}")
            
            # If the product is available, try to add it to cart
            if product_info['available']:
                # Get quantity from product settings
                quantity = product.auto_cart_quantity or 1
                
                # Try to add to cart via HTTP
                logger.info(f"Attempting to add product to cart via HTTP ({quantity} units)")
                result = add_to_cart_http(product.url, store_type, quantity)
                
                if result['success']:
                    logger.info("Successfully added product to cart!")
                    logger.info(f"Cart URL: {result['cart_url']}")
                    
                    # Update the product's last cart attempt and status
                    product.last_cart_attempt = datetime.datetime.now()
                    product.last_cart_status = result['message']
                    
                    # Save changes to the database
                    db.session.commit()
                    
                else:
                    logger.error(f"Failed to add product to cart: {result['message']}")
                
                logger.info("Direct HTTP test completed!")
                return result['success']
            else:
                logger.info("Product is not available for purchase")
                return False
    
    except Exception as e:
        logger.error(f"Error in run_direct_http_test: {e}")
        logger.error(traceback.format_exc())
        return False

def check_product_http(url, store_type):
    """Check product availability via HTTP request"""
    try:
        logger.info(f"Sending HTTP request to: {url}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Failed to get product page: Status code {response.status_code}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract product information based on store type
        if store_type == 'newegg':
            return extract_newegg_product_info(soup, url)
        elif store_type == 'bestbuy':
            return extract_bestbuy_product_info(soup, url)
        elif store_type == 'amazon':
            return extract_amazon_product_info(soup, url)
        else:
            logger.error(f"Unsupported store type: {store_type}")
            return None
        
    except Exception as e:
        logger.error(f"Error checking product via HTTP: {e}")
        logger.error(traceback.format_exc())
        return None

def add_to_cart_http(url, store_type, quantity=1):
    """
    Attempt to add a product to cart using HTTP requests
    
    Returns:
        dict: Result of the operation with keys:
            - success: Boolean indicating if the operation succeeded
            - message: Status message
            - cart_url: URL to the cart (if available)
    """
    result = {
        'success': False,
        'message': 'Not implemented for this store',
        'cart_url': None
    }
    
    try:
        # Create a session to maintain cookies between requests
        session = requests.Session()
        
        # Set user agent to appear as a normal browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        session.headers.update(headers)
        
        # Different cart addition logic based on store
        if store_type == 'newegg':
            return add_to_newegg_cart(session, url, quantity)
        elif store_type == 'bestbuy':
            result['message'] = 'BestBuy direct cart addition not supported'
            return result
        elif store_type == 'amazon':
            result['message'] = 'Amazon direct cart addition not supported'
            return result
        else:
            result['message'] = f'Unsupported store type: {store_type}'
            return result
            
    except Exception as e:
        logger.error(f"Error adding to cart via HTTP: {e}")
        logger.error(traceback.format_exc())
        result['message'] = f"Exception: {str(e)}"
        return result

def add_to_newegg_cart(session, url, quantity=1):
    """
    Attempt to add a Newegg product to cart
    
    Args:
        session: requests.Session object
        url: Product URL
        quantity: Quantity to add to cart
        
    Returns:
        dict: Result of the operation
    """
    result = {
        'success': False,
        'message': 'Failed to add to cart',
        'cart_url': None
    }
    
    try:
        # First, get the product page to extract necessary information
        logger.info(f"Getting Newegg product page: {url}")
        response = session.get(url)
        
        if response.status_code != 200:
            result['message'] = f"Failed to load product page: Status code {response.status_code}"
            return result
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract the item number using multiple methods
        item_number = None
        
        # Method 1: Extract from URL
        url_match = re.search(r'/p/([^/]+)(?:/|$)', url)
        if url_match:
            item_number = url_match.group(1)
            logger.debug(f"Extracted item number from URL: {item_number}")
            
        # Method 2: Look for product_id in script tags
        if not item_number:
            script_tags = soup.find_all('script')
            for script in script_tags:
                if script.string and 'product_id' in script.string:
                    match = re.search(r'"product_id"\s*:\s*"([^"]+)"', script.string)
                    if match:
                        item_number = match.group(1)
                        logger.debug(f"Extracted item number from product_id: {item_number}")
                        break
        
        # Method 3: Look for hidden input fields
        if not item_number:
            hidden_inputs = soup.find_all('input', {'type': 'hidden'})
            for input_field in hidden_inputs:
                if input_field.get('id') in ['hiddenItemNumber', 'Item']:
                    item_number = input_field.get('value')
                    logger.debug(f"Extracted item number from hidden input: {item_number}")
                    break
        
        # Method 4: Look for structured data
        if not item_number:
            for script in soup.find_all('script', {'type': 'application/ld+json'}):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and 'sku' in data:
                        item_number = data['sku']
                        logger.debug(f"Extracted item number from structured data: {item_number}")
                        break
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue
        
        if not item_number:
            # Print the first part of the HTML response for debugging
            logger.debug("Could not find item number, showing first 1000 chars of HTML:")
            logger.debug(response.text[:1000])
            result['message'] = "Could not find item number in product page"
            return result
        
        logger.info(f"Found Newegg item number: {item_number}")
        
        # Try two different cart endpoints
        cart_urls = [
            "https://secure.newegg.com/Shopping/AddtoCart.aspx",
            "https://www.newegg.com/api/add-to-cart"
        ]
        
        for cart_url in cart_urls:
            logger.info(f"Trying cart URL: {cart_url}")
            
            # Prepare form data
            if "api/add-to-cart" in cart_url:
                # For the API endpoint
                data = json.dumps({
                    "ItemList": [
                        {
                            "ItemNumber": item_number,
                            "Quantity": quantity,
                            "ActionType": 1
                        }
                    ]
                })
                headers = session.headers.copy()
                headers.update({
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'Referer': url
                })
                cart_response = session.post(cart_url, data=data, headers=headers, allow_redirects=True)
            else:
                # For the traditional cart form
                data = {
                    'ItemList': item_number,
                    'Quantity': str(quantity)
                }
                
                # Find the form if possible
                form = soup.find('form', {'id': 'ProductBuy'})
                if form:
                    # Extract other form data if available
                    for input_tag in form.find_all('input'):
                        if input_tag.get('name') and input_tag.get('value'):
                            if input_tag.get('name') not in data:
                                data[input_tag.get('name')] = input_tag.get('value')
                
                logger.debug(f"Sending form data: {data}")
                cart_response = session.post(cart_url, data=data, allow_redirects=True)
            
            logger.debug(f"Cart response URL: {cart_response.url}")
            logger.debug(f"Cart response status: {cart_response.status_code}")
            
            # Check if the add to cart was successful
            if cart_response.status_code == 200:
                # Look for success indicators
                if 'ShoppingCart.aspx' in cart_response.url or '/cart' in cart_response.url:
                    result['success'] = True
                    result['message'] = f"Successfully added {quantity} unit(s) to cart"
                    result['cart_url'] = cart_response.url
                    return result
                
                # Try to see if the response contains JSON success indicator
                try:
                    resp_json = cart_response.json()
                    if resp_json.get('Success') is True:
                        result['success'] = True
                        result['message'] = f"Successfully added {quantity} unit(s) to cart"
                        result['cart_url'] = "https://secure.newegg.com/shop/cart"
                        return result
                except:
                    pass
        
        # If we get here, neither cart endpoint worked
        result['message'] = "Add to cart request did not redirect to cart page"
        return result
        
    except Exception as e:
        logger.error(f"Error adding to Newegg cart: {e}")
        logger.error(traceback.format_exc())
        result['message'] = f"Exception: {str(e)}"
        return result

def extract_newegg_product_info(soup, url):
    """Extract product information from Newegg product page"""
    try:
        name = extract_newegg_name(soup)
        price = extract_newegg_price(soup)
        available = extract_newegg_availability(soup)
        image_url = extract_newegg_image(soup)
        
        logger.info(f"Extracted Newegg product - Name: {name}, Price: {price}, Available: {available}")
        
        return {
            'name': name,
            'price': price,
            'available': available,
            'image_url': image_url,
            'url': url
        }
    except Exception as e:
        logger.error(f"Error extracting Newegg product info: {e}")
        return None

def extract_newegg_name(soup):
    """Extract product name from Newegg product page"""
    try:
        # Try different selectors for product name
        selectors = [
            'h1.product-title',
            'h1.product-name',
            'h1.product',
            'h1.productTitle',
            'h1'
        ]
        
        for selector in selectors:
            name_element = soup.select_one(selector)
            if name_element and name_element.text.strip():
                return name_element.text.strip()
        
        # Another approach: look for meta tags
        meta_name = soup.find('meta', {'property': 'og:title'})
        if meta_name and meta_name.get('content'):
            return meta_name.get('content').strip()
            
        return "Unknown Product"
    except Exception as e:
        logger.error(f"Error extracting Newegg name: {e}")
        return "Unknown Product"

def extract_newegg_price(soup):
    """Extract product price from Newegg product page"""
    try:
        # Try different selectors for price
        price_selectors = [
            'li.price-current',
            'div.product-price',
            'div.price',
            'div.price-main',
            'div.product-pane ul.price'
        ]
        
        for selector in price_selectors:
            price_element = soup.select_one(selector)
            if price_element:
                # Extract price text and clean it
                price_text = price_element.text.strip()
                # Remove non-numeric characters except decimal point
                price_numeric = re.sub(r'[^\d.]', '', price_text)
                if price_numeric:
                    try:
                        return float(price_numeric)
                    except ValueError:
                        continue
        
        # Another approach: check for price in structured data
        script_tags = soup.find_all('script', {'type': 'application/ld+json'})
        for script in script_tags:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and 'offers' in data:
                    price = data['offers'].get('price')
                    if price:
                        return float(price)
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        
        # Check for price in meta tags
        meta_price = soup.find('meta', {'property': 'product:price:amount'})
        if meta_price and meta_price.get('content'):
            try:
                return float(meta_price.get('content'))
            except ValueError:
                pass
                
        return None
    except Exception as e:
        logger.error(f"Error extracting Newegg price: {e}")
        return None

def extract_newegg_availability(soup):
    """Extract product availability from Newegg product page"""
    try:
        # Look for common availability indicators
        out_of_stock_indicators = [
            'OUT OF STOCK',
            'SOLD OUT',
            'Currently Sold Out',
            'Out of Stock',
            'This item is currently out of stock'
        ]
        
        # Check the page text for out-of-stock indicators
        page_text = soup.text.upper()
        for indicator in out_of_stock_indicators:
            if indicator.upper() in page_text:
                return False
        
        # Check for add to cart button
        add_to_cart_buttons = soup.select('button.btn-primary, button.atnBtn, button.btn-add-to-cart')
        if add_to_cart_buttons:
            # Check if any buttons are disabled
            for button in add_to_cart_buttons:
                if 'disabled' not in button.attrs:
                    return True
        
        # Check for stock status elements
        stock_elements = soup.select('.stock-info, .product-inventory, .product-stock')
        for elem in stock_elements:
            elem_text = elem.text.upper()
            if 'IN STOCK' in elem_text:
                return True
            if any(indicator in elem_text for indicator in out_of_stock_indicators):
                return False
        
        # Check structured data for availability
        script_tags = soup.find_all('script', {'type': 'application/ld+json'})
        for script in script_tags:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and 'offers' in data:
                    availability = data['offers'].get('availability', '')
                    if 'outofstock' in availability.lower():
                        return False
                    if 'instock' in availability.lower():
                        return True
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
                
        # Default to available if no clear indicators found
        return True
    except Exception as e:
        logger.error(f"Error extracting Newegg availability: {e}")
        return False

def extract_newegg_image(soup):
    """Extract product image URL from Newegg product page"""
    try:
        # Try various selectors for product images
        image_selectors = [
            'img#landingImage',
            'img.mainSlide',
            'img.product_image',
            'div.objImages img',
            '.product-view-img-original img',
            '.product-gallery img',
            '.product-gallery-top img',
            '.product-image img',
            '.gallery-img-mobile img'
        ]
        
        for selector in image_selectors:
            img_element = soup.select_one(selector)
            if img_element and img_element.get('src'):
                return img_element['src']
                
        # Try meta tags
        meta_img = soup.find('meta', {'property': 'og:image'})
        if meta_img and meta_img.get('content'):
            return meta_img.get('content')
            
        # General approach: find first large image
        for img in soup.find_all('img'):
            src = img.get('src', '')
            # Skip icons, tiny images, logos
            if ('icon' not in src.lower() and 
                'logo' not in src.lower() and
                'thumb' not in src.lower() and
                src.endswith(('.jpg', '.jpeg', '.png', '.webp'))):
                return src
                
        return ""
    except Exception as e:
        logger.error(f"Error extracting Newegg image: {e}")
        return ""

def extract_bestbuy_product_info(soup, url):
    """Extract product information from Best Buy product page"""
    # Basic implementation for Best Buy
    return {
        'name': "Best Buy Product",
        'price': 0.0,
        'available': False,
        'image_url': "",
        'url': url
    }

def extract_amazon_product_info(soup, url):
    """Extract product information from Amazon product page"""
    # Basic implementation for Amazon
    return {
        'name': "Amazon Product",
        'price': 0.0,
        'available': False,
        'image_url': "",
        'url': url
    }

if __name__ == "__main__":
    # Run the direct HTTP test
    success = run_direct_http_test()
    
    if success:
        logger.info("Direct HTTP test completed successfully! Product is available.")
        sys.exit(0)
    else:
        logger.error("Direct HTTP test failed or product is not available")
        sys.exit(1) 