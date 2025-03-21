"""
Scraper module initialization.
Provides direct access to store-specific scrapers based on store type.
"""

import logging
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.amazon_scraper import AmazonScraper
from app.scrapers.walmart_scraper import WalmartScraper
from app.scrapers.newegg_scraper_with_http_fallback import NeweggScraperWithFallback
from app.scrapers.microcenter_scraper import MicrocenterScraper
from app.scrapers.bestbuy_scraper import BestBuyScraper
from app.scrapers.bh_scraper import BHScraper
from app.scrapers.test_scraper import TestScraper

# Set up logging
logger = logging.getLogger('app.scrapers')

def get_scraper(store_type):
    """
    Get the appropriate scraper based on store type.
    
    Args:
        store_type: String identifier for the store ('amazon', 'walmart', etc.)
        
    Returns:
        An instance of the appropriate scraper class
    
    Raises:
        ValueError: If store_type is not supported
    """
    logger.debug(f"Creating scraper for store type: {store_type}")
    
    scrapers = {
        'amazon': AmazonScraper,
        'walmart': WalmartScraper,
        'newegg': NeweggScraperWithFallback,  # Using the enhanced Newegg scraper with HTTP fallback
        'microcenter': MicrocenterScraper,
        'bestbuy': BestBuyScraper,
        'bh': BHScraper,
        'test': TestScraper,
    }
    
    if store_type not in scrapers:
        logger.error(f"Unsupported store type: {store_type}")
        raise ValueError(f"Unsupported store type: {store_type}")
    
    scraper_class = scrapers[store_type]
    logger.debug(f"Using {scraper_class.__name__} for {store_type}")
    return scraper_class()

def add_to_cart(store_type, url, quantity=1):
    """
    Add a product to the cart for the specified store.
    
    Args:
        store_type: String identifier for the store ('amazon', 'walmart', etc.)
        url: The product URL
        quantity: Quantity to add to cart (default: 1)
        
    Returns:
        dict: A dictionary with cart status information
        {
            'success': True/False,  # Whether the operation succeeded
            'message': str,         # Message about the result
            'cart_url': str,        # URL to the cart (if successful)
            'screenshot': str,      # Base64 screenshot of the cart (if available)
        }
    
    Raises:
        ValueError: If store_type is not supported or doesn't implement add_to_cart
    """
    logger.debug(f"Attempting to add product to cart for {store_type}: {url}")
    
    scraper = get_scraper(store_type)
    
    if not hasattr(scraper, 'add_to_cart'):
        logger.error(f"Scraper {store_type} does not implement add_to_cart")
        raise ValueError(f"Auto cart functionality not supported for {store_type}")
    
    try:
        # For Newegg and other scrapers that need to store the URL first,
        # we need to call scrape_product to set the current_product_url
        if store_type == 'newegg' and hasattr(scraper, 'scrape_product'):
            logger.debug(f"Setting product URL for {store_type} scraper")
            try:
                # Just scrape basic product info to set the URL
                scraper.scrape_product(url)
            except Exception as e:
                logger.warning(f"Error pre-scraping product for {store_type}: {str(e)}")
                # Set the URL directly as fallback
                if hasattr(scraper, 'current_product_url'):
                    scraper.current_product_url = url
        
        # Pass the URL as the first parameter if this is the test scraper
        if store_type == 'test':
            result = scraper.add_to_cart(url=url, quantity=quantity)
        # For Amazon scraper which accepts URL directly
        elif store_type == 'amazon':
            result = scraper.add_to_cart(url=url, quantity=quantity)
        else:
            # For other scrapers, just pass the quantity (they look up the URL internally)
            result = scraper.add_to_cart(quantity=quantity)
        return result
    except Exception as e:
        logger.error(f"Error adding product to cart: {str(e)}", exc_info=True)
        return {
            'success': False,
            'message': f"Error: {str(e)}",
            'cart_url': None,
            'screenshot': None
        } 