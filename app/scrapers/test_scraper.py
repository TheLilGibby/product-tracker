"""
Test scraper for simulating various auto-cart scenarios.
This scraper doesn't actually access any websites but simulates
different responses for testing purposes.
"""

import logging
import time
import random
import base64
import io
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from app.scrapers.base_scraper import BaseScraper

# Set up logger
logger = logging.getLogger('app.scrapers.test_scraper')

class TestScraper(BaseScraper):
    """
    A mock scraper for testing auto-cart functionality with different scenarios.
    
    This scraper interprets URL parameters to simulate different product conditions:
    - scenario=success: Product is available and can be added to cart (80% success rate)
    - scenario=outofstock: Product is out of stock
    - scenario=noprice: Product has no price
    - scenario=fail: Adding to cart will explicitly fail
    - scenario=timeout: Operation will time out
    
    Example URL: https://test-store.example.com/product/uuid?scenario=success&price=299.99&name=Test+Product
    """
    
    def __init__(self):
        """Initialize the test scraper without requiring a URL at instantiation."""
        logger.debug("Initializing TestScraper")
        self.url = None
        self.parsed_url = None
        self.query_params = {}
        self.scenario = 'success'
        self.product_name = 'Test Product'
        self.price_str = None
        self.price = random.uniform(99.99, 599.99)
    
    def _parse_url(self, url):
        """Parse the URL and extract parameters when a URL is provided."""
        self.url = url
        self.parsed_url = urlparse(url)
        self.query_params = parse_qs(self.parsed_url.query)
        
        # Extract parameters
        self.scenario = self._get_param('scenario', 'success')
        self.product_name = self._get_param('name', 'Test Product')
        self.price_str = self._get_param('price', None)
        
        try:
            self.price = float(self.price_str) if self.price_str else random.uniform(99.99, 599.99)
        except (ValueError, TypeError):
            self.price = random.uniform(99.99, 599.99)
    
    def _get_param(self, param_name, default=None):
        """Helper to get a parameter from the URL query string."""
        if param_name in self.query_params:
            return self.query_params[param_name][0]
        return default
    
    def get_product_info(self, url=None):
        """
        Scrape the product information based on the scenario.
        This is a simulation - no actual scraping occurs.
        
        Args:
            url: The product URL to scrape (optional, will use previously set URL if None)
        """
        # Parse the URL if provided
        if url:
            self._parse_url(url)
        elif not self.url:
            logger.error("No URL provided to TestScraper.get_product_info")
            return None
        
        # Base product info
        product_info = {
            'name': self.product_name,
            'image_url': self._generate_test_image_url(),
            'availability': False,
            'price': None,
            'currency': 'USD',
            'last_checked': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Apply scenario-specific modifications
        if self.scenario == 'success':
            product_info['availability'] = True
            product_info['price'] = round(self.price, 2)
        
        elif self.scenario == 'outofstock':
            product_info['availability'] = False
            product_info['price'] = round(self.price, 2)
        
        elif self.scenario == 'noprice':
            product_info['availability'] = True
            product_info['price'] = None
        
        elif self.scenario == 'fail':
            product_info['availability'] = True
            product_info['price'] = round(self.price, 2)
        
        elif self.scenario == 'timeout':
            # Simulate a timeout by sleeping
            time.sleep(3)
            product_info['availability'] = True
            product_info['price'] = round(self.price, 2)
        
        return product_info
    
    def add_to_cart(self, url=None, quantity=1):
        """
        Simulate adding the product to cart based on the scenario.
        
        Args:
            url: The product URL (optional, will use previously set URL if None)
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: A dictionary with cart status information
        """
        # Parse the URL if provided
        if url:
            self._parse_url(url)
        elif not self.url:
            logger.error("No URL provided to TestScraper.add_to_cart")
            return {
                'success': False,
                'message': "No URL provided",
                'cart_url': None,
                'screenshot': None
            }
        
        # Simulate some processing time
        time.sleep(random.uniform(0.5, 1.5))
        
        # Generate base64 screenshot for the result
        screenshot = self._generate_cart_screenshot()
        
        # Handle based on scenario
        if self.scenario == 'success':
            # 80% success rate
            if random.random() < 0.8:
                return {
                    'success': True,
                    'message': f"Successfully added {quantity} item(s) to cart",
                    'cart_url': "https://test-store.example.com/cart",
                    'screenshot': screenshot
                }
            else:
                return {
                    'success': False,
                    'message': "Random failure occurred while adding to cart",
                    'cart_url': None,
                    'screenshot': screenshot
                }
        
        elif self.scenario == 'outofstock':
            return {
                'success': False,
                'message': "Product is out of stock",
                'cart_url': None,
                'screenshot': screenshot
            }
        
        elif self.scenario == 'noprice':
            return {
                'success': False,
                'message': "Cannot add to cart: product has no price",
                'cart_url': None,
                'screenshot': screenshot
            }
        
        elif self.scenario == 'fail':
            return {
                'success': False,
                'message': "Failed to add to cart: store rejected the request",
                'cart_url': None,
                'screenshot': screenshot
            }
        
        elif self.scenario == 'timeout':
            # Simulate a long timeout
            time.sleep(5)
            return {
                'success': False,
                'message': "Operation timed out",
                'cart_url': None,
                'screenshot': screenshot
            }
        
        # Default case
        return {
            'success': False,
            'message': "Unknown scenario",
            'cart_url': None,
            'screenshot': screenshot
        }
    
    def _generate_test_image_url(self):
        """Generate a fake image URL for testing."""
        return f"https://via.placeholder.com/300/cccccc/666666?text=Test+Product+{self.scenario}"
    
    def _generate_cart_screenshot(self):
        """
        Generate a mock screenshot of the cart status.
        Returns a base64 encoded PNG image.
        """
        # Create a simple image
        width, height = 800, 400
        image = Image.new('RGB', (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(image)
        
        # Draw a border
        draw.rectangle([(0, 0), (width-1, height-1)], outline=(200, 200, 200))
        
        # Add a title
        draw.rectangle([(0, 0), (width, 50)], fill=(51, 51, 51))
        draw.text((20, 15), "Test Store - Cart Status", fill=(255, 255, 255))
        
        # Draw the message based on scenario
        if self.scenario == 'success' and random.random() < 0.8:
            status_color = (46, 204, 113)  # Green
            status_text = "✓ Product added to cart successfully!"
        else:
            status_color = (231, 76, 60)  # Red
            
            if self.scenario == 'outofstock':
                status_text = "✗ Error: Product is out of stock"
            elif self.scenario == 'noprice':
                status_text = "✗ Error: Product has no price"
            elif self.scenario == 'fail':
                status_text = "✗ Error: Store rejected the request"
            elif self.scenario == 'timeout':
                status_text = "✗ Error: Operation timed out"
            else:
                status_text = "✗ Error: Failed to add to cart"
        
        # Draw status box
        draw.rectangle([(50, 100), (width-50, 180)], fill=status_color)
        draw.text((70, 125), status_text, fill=(255, 255, 255))
        
        # Draw product details
        draw.text((70, 200), f"Product: {self.product_name}", fill=(0, 0, 0))
        
        if self.price_str:
            draw.text((70, 230), f"Price: ${self.price}", fill=(0, 0, 0))
        else:
            draw.text((70, 230), "Price: Not available", fill=(0, 0, 0))
        
        draw.text((70, 260), f"Scenario: {self.scenario}", fill=(0, 0, 0))
        draw.text((70, 290), f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", fill=(0, 0, 0))
        
        # Convert to PNG and encode in base64
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        img_str = base64.b64encode(buffer.getvalue()).decode()
        
        return f"data:image/png;base64,{img_str}" 