"""
Direct HTTP-based implementation for Newegg cart functionality.
This module provides a lightweight alternative to Selenium-based automation
for adding products to cart on Newegg.
"""

import requests
import logging
import re
import json
import traceback
from bs4 import BeautifulSoup

# Set up logger
logger = logging.getLogger('app.scrapers.newegg_http')

class NeweggHttpCartHandler:
    """
    A lightweight handler for adding products to Newegg cart via HTTP requests.
    """
    
    def __init__(self):
        """Initialize the Newegg HTTP cart handler."""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        logger.debug("Initialized NeweggHttpCartHandler")
    
    def get_product_info(self, url):
        """
        Get product information from Newegg using HTTP requests.
        
        Args:
            url: Product URL
            
        Returns:
            dict: Product information including name, price, availability, and image URL
                  or None if the request fails
        """
        try:
            logger.info(f"Getting product info for: {url}")
            response = self.session.get(url, timeout=30)
            
            if response.status_code != 200:
                logger.error(f"Failed to get product page: Status code {response.status_code}")
                return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract product name
            name = self._extract_name(soup)
            
            # Extract price
            price = self._extract_price(soup)
            
            # Extract availability
            available = self._extract_availability(soup)
            
            # Extract image URL
            image_url = self._extract_image_url(soup)
            
            logger.info(f"Extracted product info - Name: {name[:30]}..., Price: {price}, Available: {available}")
            
            return {
                'name': name,
                'price': price,
                'available': available,
                'image_url': image_url,
                'url': url
            }
            
        except Exception as e:
            logger.error(f"Error getting product info via HTTP: {e}")
            logger.debug(traceback.format_exc())
            return None
    
    def add_to_cart(self, url, quantity=1):
        """
        Add a product to cart using HTTP requests.
        
        Args:
            url: Product URL
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: Result of the operation with keys:
                - success: Boolean indicating if the operation succeeded
                - message: Status message
                - cart_url: URL to the cart (if available)
                - screenshot: Base64-encoded screenshot of the cart (if available)
        """
        result = {
            'success': False,
            'message': 'Failed to add to cart',
            'cart_url': None,
            'screenshot': None
        }
        
        try:
            # First, get the product page to extract necessary information
            logger.info(f"Getting Newegg product page: {url}")
            response = self.session.get(url)
            
            if response.status_code != 200:
                result['message'] = f"Failed to load product page: Status code {response.status_code}"
                return result
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Check if the product is available
            if not self._extract_availability(soup):
                result['message'] = "Product is not available for purchase"
                return result
            
            # Extract the item number using multiple methods
            item_number = self._extract_item_number(soup, url)
            
            if not item_number:
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
                    headers = self.session.headers.copy()
                    headers.update({
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'Referer': url
                    })
                    cart_response = self.session.post(cart_url, data=data, headers=headers, allow_redirects=True)
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
                    cart_response = self.session.post(cart_url, data=data, allow_redirects=True)
                
                logger.debug(f"Cart response URL: {cart_response.url}")
                logger.debug(f"Cart response status: {cart_response.status_code}")
                
                # Check if the add to cart was successful
                if cart_response.status_code == 200:
                    # Look for success indicators
                    if 'ShoppingCart.aspx' in cart_response.url or '/cart' in cart_response.url or '/shop/cart' in cart_response.url:
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
            logger.debug(traceback.format_exc())
            result['message'] = f"Exception: {str(e)}"
            return result
    
    def _extract_item_number(self, soup, url):
        """
        Extract the item number from the product page.
        
        Args:
            soup: BeautifulSoup object of the product page
            url: Product URL
            
        Returns:
            str: Item number or None if not found
        """
        item_number = None
        
        # Method 1: Extract from URL
        url_match = re.search(r'/p/([^/]+)(?:/|$)', url)
        if url_match:
            item_number = url_match.group(1)
            logger.debug(f"Extracted item number from URL: {item_number}")
            return item_number
            
        # Method 2: Look for product_id in script tags
        script_tags = soup.find_all('script')
        for script in script_tags:
            if script.string and 'product_id' in script.string:
                match = re.search(r'"product_id"\s*:\s*"([^"]+)"', script.string)
                if match:
                    item_number = match.group(1)
                    logger.debug(f"Extracted item number from product_id: {item_number}")
                    return item_number
        
        # Method 3: Look for hidden input fields
        hidden_inputs = soup.find_all('input', {'type': 'hidden'})
        for input_field in hidden_inputs:
            if input_field.get('id') in ['hiddenItemNumber', 'Item']:
                item_number = input_field.get('value')
                logger.debug(f"Extracted item number from hidden input: {item_number}")
                return item_number
        
        # Method 4: Look for structured data
        for script in soup.find_all('script', {'type': 'application/ld+json'}):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and 'sku' in data:
                    item_number = data['sku']
                    logger.debug(f"Extracted item number from structured data: {item_number}")
                    return item_number
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
                
        return None
    
    def _extract_name(self, soup):
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
    
    def _extract_price(self, soup):
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
    
    def _extract_availability(self, soup):
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
    
    def _extract_image_url(self, soup):
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

# Example usage:
if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Test with a sample Newegg product
    url = "https://www.newegg.com/yeston-geforce-rtx-3060-ti-rtx-3060ti/p/1FT-007N-00068"
    
    # Create the handler
    handler = NeweggHttpCartHandler()
    
    # Get product info
    product_info = handler.get_product_info(url)
    if product_info:
        print(f"Product: {product_info['name']}")
        print(f"Price: ${product_info['price']}")
        print(f"Available: {product_info['available']}")
        
        # Try to add to cart if available
        if product_info['available']:
            print("Attempting to add to cart...")
            result = handler.add_to_cart(url)
            print(f"Success: {result['success']}")
            print(f"Message: {result['message']}")
            if result['cart_url']:
                print(f"Cart URL: {result['cart_url']}")
    else:
        print("Failed to get product information") 