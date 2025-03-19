"""
Scraper module initialization.
Provides direct access to store-specific scrapers based on store type.
"""

import logging
# Removing base_scraper import as requested
from app.scrapers.amazon_scraper import AmazonScraper
from app.scrapers.walmart_scraper import WalmartScraper
from app.scrapers.newegg_scraper import NeweggScraper
from app.scrapers.microcenter_scraper import MicrocenterScraper
from app.scrapers.bestbuy_scraper import BestBuyScraper
from app.scrapers.bh_scraper import BHScraper

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
        'bh': BHScraper
    }
    
    if store_type not in scrapers:
        logger.error(f"Unsupported store type: {store_type}")
        raise ValueError(f"Unsupported store type: {store_type}")
    
    scraper_class = scrapers[store_type]
    logger.debug(f"Using {scraper_class.__name__} for {store_type}")
    return scraper_class() 