"""
Base scraper module that defines the interface for all scrapers.
This serves as the parent class for all scrapers in the system.
"""

import logging

# Set up logger
logger = logging.getLogger('app.scrapers.base')

class BaseScraper:
    """
    Base class for all scrapers. Defines the common interface that all scrapers should implement.
    
    Other scrapers should inherit from this class and override the necessary methods.
    """
    
    def __init__(self, url, *args, **kwargs):
        """
        Initialize the base scraper.
        
        Args:
            url: The product URL to scrape
            *args: Variable length argument list
            **kwargs: Arbitrary keyword arguments
        """
        self.url = url
        logger.debug(f"Initializing base scraper for URL: {url}")
    
    def get_product_info(self):
        """
        Retrieve product information from the store.
        This method should be overridden by subclasses.
        
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        raise NotImplementedError("Subclasses must implement get_product_info()")
    
    def scrape_product(self, url):
        """
        Alias for get_product_info for compatibility with application code.
        By default, this calls get_product_info with the provided URL.
        
        Args:
            url: The product URL to scrape
            
        Returns:
            dict: Product information including name, price, availability, and image URL
        """
        # By default, just pass the URL to get_product_info if it's implemented
        if hasattr(self, 'get_product_info'):
            return self.get_product_info(url)
        else:
            raise NotImplementedError("Subclasses must implement get_product_info() or scrape_product()")
    
    def add_to_cart(self, quantity=1):
        """
        Add a product to the cart.
        This method should be overridden by subclasses.
        
        Args:
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            tuple: (success, message, screenshot)
                - success: Boolean indicating success or failure
                - message: String message about the result
                - screenshot: Base64-encoded screenshot of the cart (if available)
        """
        raise NotImplementedError("Subclasses must implement add_to_cart()") 