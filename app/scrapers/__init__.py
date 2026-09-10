"""
Scraper module initialization.
Provides direct access to store-specific scrapers based on store type.
"""

import inspect
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
        'newegg': NeweggScraper,
        'microcenter': MicrocenterScraper,
        'bestbuy': BestBuyScraper,
        'bh': BHScraper,
        'test': TestScraper,
        'adorama': AdoramaScraper,
        'target': TargetScraper,
    }
    
    if store_type not in scrapers:
        logger.error(f"Unsupported store type: {store_type}")
        raise ValueError(f"Unsupported store type: {store_type}")
    
    scraper_class = scrapers[store_type]
    logger.debug(f"Using {scraper_class.__name__} for {store_type}")
    return scraper_class()

# Two invariants every scraper's add_to_cart must hold. Both have now been
# rediscovered independently in four scrapers, so they are written down here
# rather than found a fifth time.
#
# 1. Never click a buy button you cannot attribute to the item being carted.
#    Retailers server-render live, working add-to-cart buttons for RECOMMENDED
#    products on the product page itself, so "the first match in the document"
#    clicks a stranger's item. Scope by the item's own id - in the button, or in
#    a container around it - and skip any control naming a foreign id. Two
#    shapes in use: by id in Target (addToCartButtonOrTextIdFor<tcin>) and Best
#    Buy (pdp- / carousel- by sku), by container in Walmart (its buy-box
#    selectors) and Nintendo (the div before [data-drawer-id="add-to-cart-
#    drawer"], cross-checked against [data-ps-sku]).
#
# 2. Never report cart success on the absence of evidence. "The words 'cart is
#    empty' are missing" and "a toast appeared on the product page" are both
#    satisfied by a bot wall, and by a click that silently did nothing. Load the
#    cart page and require the item's own id/sku/ASIN to be PRESENT on it, and
#    check that page with detect_block_page(expect_product=False) - an empty
#    cart is legitimately small and names no product, so the default heuristic
#    would report it as a wall.
#
# Together they fail closed in both directions: an add that did not happen is
# never reported as a success, and a wrong item can never be the thing that
# makes the check pass. Note that (1) is what keeps (2) honest - without it, a
# carousel click puts a real product in a real cart, and the failure that
# follows names the wrong cause.
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
        # Scrapers whose add_to_cart takes a url get it directly (test, amazon,
        # bestbuy); the rest read it off the instance, so pre-scrape to set
        # current_product_url first (newegg, target, adorama).
        if 'url' in inspect.signature(scraper.add_to_cart).parameters:
            logger.debug(f"Passing product URL directly to {store_type} scraper")
            return scraper.add_to_cart(url=url, quantity=quantity)

        if hasattr(scraper, 'scrape_product'):
            logger.debug(f"Setting product URL for {store_type} scraper")
            try:
                # Just scrape basic product info to set the URL
                scraper.scrape_product(url)
            except Exception as e:
                logger.warning(f"Error pre-scraping product for {store_type}: {str(e)}")
                # Set the URL directly as fallback
                if hasattr(scraper, 'current_product_url'):
                    scraper.current_product_url = url

        return scraper.add_to_cart(quantity=quantity)
    except Exception as e:
        logger.error(f"Error adding product to cart: {str(e)}", exc_info=True)
        return {
            'success': False,
            'message': f"Error: {str(e)}",
            'cart_url': None,
            'screenshot': None
        } 