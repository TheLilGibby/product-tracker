#!/usr/bin/env python3
"""
Simple test script to verify the Newegg HTTP fallback implementation
"""

import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

def test_fallback():
    """Test the Newegg HTTP fallback implementation"""
    try:
        # Import necessary modules
        logger.info("Importing required modules")
        from app import create_app
        from app.models.product import Product
        from app.scrapers import get_scraper
        
        # Create Flask app
        logger.info("Creating Flask app")
        app = create_app()
        
        with app.app_context():
            # Get a product from the database
            logger.info("Fetching product from database")
            product = Product.query.get(10)
            
            if not product:
                logger.error("No product found with ID 10")
                return False
                
            logger.info(f"Found product: {product.name}")
            logger.info(f"URL: {product.url}")
            
            # Get the appropriate scraper
            logger.info("Getting scraper for Newegg")
            scraper = get_scraper('newegg')
            logger.info(f"Scraper type: {type(scraper).__name__}")
            
            # Check if it's our enhanced scraper
            if type(scraper).__name__ != 'NeweggScraperWithFallback':
                logger.error("Not using the enhanced scraper with HTTP fallback!")
                return False
                
            logger.info("Successfully initialized the enhanced Newegg scraper with HTTP fallback")
            
            # Try to scrape the product
            if product.url and 'newegg.com' in product.url.lower():
                logger.info(f"Attempting to scrape Newegg product: {product.url}")
                try:
                    product_info = scraper.scrape_product(product.url)
                    logger.info("Scrape successful!")
                    logger.info(f"Product name: {product_info.get('name', 'N/A')}")
                    logger.info(f"Price: {product_info.get('price', 'N/A')}")
                    logger.info(f"Available: {product_info.get('available', 'N/A')}")
                except Exception as e:
                    logger.error(f"Error scraping product: {str(e)}")
                    return False
            
            return True
            
    except Exception as e:
        logger.error(f"Error in test_fallback: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    logger.info("Starting HTTP fallback test")
    success = test_fallback()
    
    if success:
        logger.info("Test completed successfully!")
        sys.exit(0)
    else:
        logger.error("Test failed")
        sys.exit(1) 