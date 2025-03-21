"""
Enhanced Newegg scraper with HTTP fallback capabilities.
This scraper inherits from the original NeweggScraper and adds HTTP fallback functionality.
"""

import logging
import traceback
from datetime import datetime

# Import the original Newegg scraper
from app.scrapers.newegg_scraper import NeweggScraper
from app.scrapers.direct_http_newegg_cart import NeweggHttpCartHandler

# Import config settings
try:
    from app.scrapers.newegg_fallback_config import (
        ENABLE_HTTP_FALLBACK,
        ALWAYS_USE_HTTP_FALLBACK,
        WEBDRIVER_TIMEOUT,
        DEBUG_HTTP_FALLBACK
    )
except ImportError:
    # Default values if config file is not available
    ENABLE_HTTP_FALLBACK = True
    ALWAYS_USE_HTTP_FALLBACK = False
    WEBDRIVER_TIMEOUT = 30
    DEBUG_HTTP_FALLBACK = True

# Set up logger
logger = logging.getLogger('app.scrapers.newegg_with_fallback')

class NeweggScraperWithFallback(NeweggScraper):
    """
    Enhanced Newegg scraper with HTTP fallback capabilities.
    Attempts to use the original WebDriver-based scraper first, then falls back to HTTP methods if that fails.
    """
    
    def __init__(self):
        """Initialize the enhanced scraper with both WebDriver and HTTP capabilities"""
        # Initialize the parent class (WebDriver-based scraper)
        super().__init__()
        
        # Initialize HTTP handler for fallback
        self.http_handler = NeweggHttpCartHandler()
        
        # Track if we're in fallback mode
        self.using_fallback = ALWAYS_USE_HTTP_FALLBACK
        
        logger.info("Initialized Newegg scraper with HTTP fallback")
    
    def scrape_product(self, url):
        """
        Scrape product information, falling back to HTTP if WebDriver fails
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        # Store the URL for future use (e.g., cart operations)
        self.current_product_url = url
        
        # Skip WebDriver if always using HTTP fallback
        if ALWAYS_USE_HTTP_FALLBACK:
            logger.info(f"Always using HTTP fallback for scraping")
            self.using_fallback = True
            return self.http_handler.get_product_info(url)
        
        # Try WebDriver approach first
        logger.info(f"Attempting to scrape {url} with WebDriver")
        try:
            # Call the parent class method (WebDriver-based)
            return super().scrape_product(url)
        except Exception as e:
            # If WebDriver fails and fallback is enabled, try HTTP approach
            if ENABLE_HTTP_FALLBACK:
                logger.info(f"Using HTTP-based fallback scraper for {url}")
                self.using_fallback = True
                
                if DEBUG_HTTP_FALLBACK:
                    logger.error(f"WebDriver error: {str(e)}")
                    logger.error(traceback.format_exc())
                
                # Use HTTP handler
                return self.http_handler.get_product_info(url)
            else:
                # Re-raise the exception if fallback is disabled
                logger.error(f"WebDriver scraping failed and HTTP fallback is disabled")
                raise
    
    def add_to_cart(self, quantity=1):
        """
        Add a product to the cart, falling back to HTTP if WebDriver fails
        
        Args:
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: A dictionary with cart status information
            {
                'success': True/False,  # Whether the operation succeeded
                'message': str,         # Message about the result
                'cart_url': str,        # URL to the cart (if successful)
                'screenshot': str,      # Base64 screenshot of the cart (if available)
            }
        """
        if not self.current_product_url:
            message = "No product URL set. Call scrape_product first."
            logger.error(message)
            return {
                'success': False,
                'message': message,
                'cart_url': None,
                'screenshot': None
            }
        
        logger.info(f"Attempting to add {self.current_product_url} to cart (quantity: {quantity})")
        
        # Skip WebDriver if always using HTTP fallback or if we already used fallback for scraping
        if ALWAYS_USE_HTTP_FALLBACK or self.using_fallback:
            logger.info(f"Using HTTP-based cart handler")
            return self.http_handler.add_to_cart(self.current_product_url, quantity)
        
        # Try WebDriver approach first
        logger.info(f"Attempting to add to cart with WebDriver")
        try:
            # Call the parent class method (WebDriver-based)
            return super().add_to_cart(quantity)
        except Exception as e:
            # If WebDriver fails and fallback is enabled, try HTTP approach
            if ENABLE_HTTP_FALLBACK:
                logger.info(f"Using HTTP-based fallback for cart operation")
                self.using_fallback = True
                
                if DEBUG_HTTP_FALLBACK:
                    logger.error(f"WebDriver cart error: {str(e)}")
                    logger.error(traceback.format_exc())
                
                # Use HTTP handler
                return self.http_handler.add_to_cart(self.current_product_url, quantity)
            else:
                # Re-raise the exception if fallback is disabled
                logger.error(f"WebDriver cart operation failed and HTTP fallback is disabled")
                raise 