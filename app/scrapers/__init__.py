"""
Scraper module initialization.
Provides direct access to store-specific scrapers based on store type.
"""

import logging
from urllib.parse import urlparse

from app.scrapers.base_scraper import BaseScraper
from app.scrapers.amazon_scraper import AmazonScraper
from app.scrapers.walmart_scraper import WalmartScraper
from app.scrapers.newegg_scraper import NeweggScraper
from app.scrapers.microcenter_scraper import MicrocenterScraper
from app.scrapers.bestbuy_scraper import BestBuyScraper
from app.scrapers.bh_scraper import BHScraper
from app.scrapers.test_scraper import TestScraper
from app.scrapers.adorama_scraper import AdoramaScraper

# Set up logging
logger = logging.getLogger('app.scrapers')

# Registered hostname -> store key. Single source of truth for turning a product
# URL into the key get_scraper() understands; app/tasks.py and app/routes/main.py
# both go through detect_store_type() rather than keeping their own copies.
#
# 'test-store.example.com' is not a real retailer. It stays registered because
# /add-test-product mints URLs on that host and the scheduler has to route them
# to TestScraper.
STORE_DOMAINS = {
    'amazon.com': 'amazon',
    'walmart.com': 'walmart',
    'newegg.com': 'newegg',
    'microcenter.com': 'microcenter',
    'bestbuy.com': 'bestbuy',
    'bhphotovideo.com': 'bh',
    'adorama.com': 'adorama',
    # TargetScraper arrives on feature/target-scraper; registering the domain
    # early is harmless because get_scraper() raises for a key it has no class for.
    'target.com': 'target',
    'test-store.example.com': 'test',
}


def detect_store_type(url):
    """
    Determine which store a product URL belongs to.

    Matching is on the parsed hostname and is exact or a true subdomain: a URL is
    'amazon' only when its host is amazon.com or something.amazon.com. Substring
    matching was used here before, which let an attacker-controlled host pose as a
    supported store and get fetched, because both

        https://amazon.com.evil.example/p/1
        https://amazon.com@evil.example/p/1

    contain 'amazon.com' while resolving to evil.example. urlparse().hostname is
    used rather than .netloc because it already strips userinfo and the port.

    Args:
        url: The product URL

    Returns:
        The store key (e.g. 'newegg'), or None when no registered store matches.
    """
    if not url:
        return None

    try:
        host = urlparse(url).hostname
    except ValueError:
        # urlparse raises on a malformed IPv6 literal or a non-numeric port.
        logger.debug(f"Could not parse a hostname out of URL: {url!r}")
        return None

    if not host:
        return None

    # A trailing dot is the DNS root label and addresses the same host.
    host = host.lower().rstrip('.')

    for domain, store in STORE_DOMAINS.items():
        if host == domain or host.endswith('.' + domain):
            return store

    logger.debug(f"No registered store matched host: {host}")
    return None


def supported_stores():
    """Return the sorted store keys detect_store_type() can return."""
    return sorted(set(STORE_DOMAINS.values()))


# How each store key is written for a human. A key with no entry falls back to a
# title-cased version of itself, so registering a domain above is enough to make a
# new store appear in the add-product form without touching the template.
STORE_LABELS = {
    'amazon': 'Amazon',
    'walmart': 'Walmart',
    'newegg': 'Newegg',
    'microcenter': 'Micro Center',
    'bestbuy': 'Best Buy',
    'bh': 'B&H Photo Video',
    'adorama': 'Adorama',
    'target': 'Target',
    'test': 'Test Store',
}

# Real stores only. The test store is reachable through /add-test-product and has
# no business in the store picker.
HIDDEN_STORES = frozenset({'test'})


def store_choices():
    """
    Return [(store key, human label)] for the stores a person can pick, A to Z by label.

    The add-product form and its supported-sites hint are both built from this, so
    adding a retailer means editing STORE_DOMAINS and STORE_LABELS and nothing else.
    """
    choices = [(store, STORE_LABELS.get(store, store.title()))
               for store in supported_stores() if store not in HIDDEN_STORES]
    return sorted(choices, key=lambda choice: choice[1].lower())

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