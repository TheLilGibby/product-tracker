"""
Scraper module initialization.
Provides direct access to store-specific scrapers based on store type.
"""

import logging
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.amazon_scraper import AmazonScraper
from app.scrapers.walmart_scraper import WalmartScraper
from app.scrapers.newegg_scraper import NeweggScraper
from app.scrapers.microcenter_scraper import MicrocenterScraper
from app.scrapers.bestbuy_scraper import BestBuyScraper
from app.scrapers.bh_scraper import BHScraper
from app.scrapers.test_scraper import TestScraper
from app.scrapers.adorama_scraper import AdoramaScraper
from app.scrapers.target_scraper import TargetScraper
from app.scrapers.gamestop_scraper import GameStopScraper
from app.scrapers.nintendo_scraper import NintendoScraper

# Set up logging
logger = logging.getLogger('app.scrapers')

# Map of hostname fragments -> store type. Single source of truth for turning a
# product URL into the store key that get_scraper() understands.
STORE_DOMAINS = {
    'amazon.com': 'amazon',
    'walmart.com': 'walmart',
    'newegg.com': 'newegg',
    'microcenter.com': 'microcenter',
    'bestbuy.com': 'bestbuy',
    'bhphotovideo.com': 'bh',
    'adorama.com': 'adorama',
    'target.com': 'target',
    'gamestop.com': 'gamestop',
    'nintendo.com': 'nintendo',
    'test-store.example.com': 'test',
}

# Human-readable labels for the store keys above. Keep in sync with the
# <select name="scraper_type"> options on the add-product form.
STORE_DISPLAY_NAMES = {
    'amazon': 'Amazon',
    'walmart': 'Walmart',
    'newegg': 'Newegg',
    'microcenter': 'Micro Center',
    'bestbuy': 'Best Buy',
    'bh': 'B&H',
    'adorama': 'Adorama',
    'target': 'Target',
    'gamestop': 'GameStop',
    'nintendo': 'Nintendo',
    'test': 'Test',
}


def detect_store_type(url):
    """
    Determine the store type for a product URL by matching its hostname.

    Args:
        url: The product URL

    Returns:
        The store key (e.g. 'newegg'), or None if no supported store matches
    """
    if not url:
        return None

    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()
    if not domain:
        return None

    for fragment, store in STORE_DOMAINS.items():
        if fragment in domain:
            return store

    logger.debug(f"No supported store matched domain: {domain}")
    return None


def supported_stores():
    """Return the sorted list of store keys get_scraper() accepts."""
    return sorted(set(STORE_DOMAINS.values()))


def store_display_name(store_type):
    """
    Human-readable store name for UI (e.g. 'bestbuy' -> 'Best Buy').

    Unknown or missing keys become 'Unknown'.
    """
    if not store_type:
        return 'Unknown'
    return STORE_DISPLAY_NAMES.get(store_type, store_type.replace('_', ' ').title())


def store_label_from_url(url):
    """
    Retailer name for a product URL.

    Uses STORE_DISPLAY_NAMES when the host is a supported store; otherwise falls
    back to a title-cased hostname (e.g. unknown-shop.example -> 'Unknown Shop').
    """
    store = detect_store_type(url)
    if store:
        return store_display_name(store)
    if not url:
        return 'Unknown'
    from urllib.parse import urlparse
    host = (urlparse(url).netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    name = host.split('.')[0] if host else ''
    return name.replace('-', ' ').title() if name else 'Unknown'

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
        'newegg': NeweggScraper,
        'microcenter': MicrocenterScraper,
        'bestbuy': BestBuyScraper,
        'bh': BHScraper,
        'test': TestScraper,
        'adorama': AdoramaScraper,
        'target': TargetScraper,
        'gamestop': GameStopScraper,
        'nintendo': NintendoScraper,
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
        # Newegg and Target read the URL off the instance, so pre-scrape
        # to set current_product_url first.
        if store_type in ('newegg', 'target', 'gamestop') and hasattr(scraper, 'scrape_product'):
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