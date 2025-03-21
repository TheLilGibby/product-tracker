"""
Modified Newegg scraper with HTTP fallback.
This module enhances the original Newegg scraper with a direct HTTP fallback
for when browser automation fails.
"""

import os
import sys
import logging
import traceback
from app.scrapers.newegg_scraper import NeweggScraper

# Import our HTTP handler
from app.scrapers.direct_http_newegg_cart import NeweggHttpCartHandler

# Set up logger
logger = logging.getLogger('app.scrapers.newegg_with_fallback')

class NeweggScraperWithFallback(NeweggScraper):
    """
    Enhanced Newegg scraper that falls back to HTTP requests when WebDriver fails.
    Inherits from the original NeweggScraper class but adds HTTP fallback functionality.
    """
    
    def __init__(self):
        """Initialize the scraper with both WebDriver and HTTP capabilities."""
        # Initialize the parent class
        try:
            super().__init__()
            self.webdriver_initialized = True
        except Exception as e:
            logger.warning(f"Failed to initialize WebDriver for Newegg: {str(e)}")
            self.webdriver_initialized = False
        
        # Initialize the HTTP handler as a fallback
        self.http_handler = NeweggHttpCartHandler()
        logger.info("Initialized Newegg scraper with HTTP fallback")
    
    def scrape_product(self, url):
        """
        Scrape product information, with fallback to HTTP if WebDriver fails.
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        # Store the URL for later use by add_to_cart
        self.current_product_url = url
        
        # Try with WebDriver first
        if self.webdriver_initialized:
            try:
                logger.info(f"Attempting to scrape {url} with WebDriver")
                return super().scrape_product(url)
            except Exception as e:
                logger.warning(f"WebDriver-based scraping failed: {str(e)}")
                logger.warning("Falling back to HTTP-based scraping")
        else:
            logger.info("WebDriver not initialized, using HTTP-based scraping directly")
        
        # Fall back to HTTP-based scraping
        try:
            product_info = self.http_handler.get_product_info(url)
            if product_info:
                return product_info
            else:
                raise Exception("HTTP-based scraping failed to retrieve product information")
        except Exception as e:
            logger.error(f"HTTP-based scraping failed: {str(e)}")
            # Re-raise the exception for the caller to handle
            raise
    
    def add_to_cart(self, quantity=1):
        """
        Add product to cart with fallback to HTTP if WebDriver fails.
        
        Args:
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: Result of the operation with keys:
                - success: Boolean indicating if the operation succeeded
                - message: Status message
                - cart_url: URL to the cart (if available)
                - screenshot: Base64-encoded screenshot of the cart (if available)
        """
        # Check if we have a product URL
        if not hasattr(self, 'current_product_url') or not self.current_product_url:
            return {
                'success': False,
                'message': 'No product URL set. Call scrape_product() first.',
                'cart_url': None,
                'screenshot': None
            }
        
        url = self.current_product_url
        logger.info(f"Attempting to add {url} to cart (quantity: {quantity})")
        
        # Try with WebDriver first if it's initialized
        if self.webdriver_initialized:
            try:
                logger.info("Attempting to add to cart with WebDriver")
                return super().add_to_cart(quantity=quantity)
            except Exception as e:
                logger.warning(f"WebDriver-based add to cart failed: {str(e)}")
                logger.warning("Falling back to HTTP-based add to cart")
        else:
            logger.info("WebDriver not initialized, using HTTP-based add to cart directly")
        
        # Fall back to HTTP-based add to cart
        try:
            logger.info(f"Attempting HTTP-based add to cart for {url}")
            result = self.http_handler.add_to_cart(url, quantity)
            
            # If we got a cart URL but no screenshot, include a message about it
            if result['success'] and not result['screenshot']:
                result['message'] += " (HTTP method used, no screenshot available)"
            
            return result
        except Exception as e:
            logger.error(f"HTTP-based add to cart failed: {str(e)}")
            return {
                'success': False,
                'message': f"All add to cart methods failed. Last error: {str(e)}",
                'cart_url': None,
                'screenshot': None
            }

# Example usage for testing
if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Test product URL
    url = "https://www.newegg.com/yeston-geforce-rtx-3060-ti-rtx-3060ti/p/1FT-007N-00068"
    
    # Create the scraper
    scraper = NeweggScraperWithFallback()
    
    # Scrape product info
    try:
        product_info = scraper.scrape_product(url)
        print(f"Product: {product_info['name']}")
        print(f"Price: ${product_info['price']}")
        print(f"Available: {product_info['available']}")
        
        # Try to add to cart if available
        if product_info['available']:
            print("Attempting to add to cart...")
            result = scraper.add_to_cart(1)
            print(f"Success: {result['success']}")
            print(f"Message: {result['message']}")
            if result['cart_url']:
                print(f"Cart URL: {result['cart_url']}")
    except Exception as e:
        print(f"Error: {str(e)}")
        traceback.print_exc() 