"""
Test script to use the enhanced Newegg scraper with HTTP fallback.
This script integrates our HTTP fallback solution with the main app functionality.
"""

import os
import sys
import logging
import traceback
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add necessary environment variables
os.environ["CHROMEDRIVER_PATH"] = os.path.expanduser("~/.local/share/undetected_chromedriver/undetected/chromedriver-linux64/chromedriver")

def run_integrated_test():
    """Run the test with enhanced Newegg scraper integration"""
    try:
        logger.info("Starting integrated Newegg HTTP fallback test")
        
        # Import necessary modules
        from app import create_app, db
        from app.models.product import Product

        # Monkey patch the get_scraper function to use our enhanced scraper for Newegg
        import app.scrapers
        from app.scrapers.newegg_scraper_with_http_fallback import NeweggScraperWithFallback
        
        # Store the original get_scraper function
        original_get_scraper = app.scrapers.get_scraper
        
        # Define the patched function
        def patched_get_scraper(store_type):
            """Patched version of get_scraper that uses our enhanced Newegg scraper"""
            if store_type == 'newegg':
                logger.info("Using enhanced Newegg scraper with HTTP fallback")
                return NeweggScraperWithFallback()
            else:
                # Use the original function for other store types
                return original_get_scraper(store_type)
        
        # Apply the patch
        app.scrapers.get_scraper = patched_get_scraper
        
        # Initialize Flask app context
        app = create_app()
        with app.app_context():
            # Get product ID 10 from the database
            product = Product.query.get(10)
            
            if not product:
                logger.error("Product with ID 10 not found")
                return False
            
            logger.info(f"Product details - ID: {product.id}, Name: {product.name}")
            logger.info(f"URL: {product.url}")
            logger.info(f"Auto-cart enabled: {product.auto_cart_enabled}")
            logger.info(f"Last cart status: {product.last_cart_status}")
            
            # Determine the store type from the URL
            store_mapping = {
                "newegg.com": "newegg",
                "bestbuy.com": "bestbuy",
                "amazon.com": "amazon"
            }
            
            store_type = None
            for domain, store in store_mapping.items():
                if domain in product.url:
                    store_type = store
                    break
            
            if not store_type:
                logger.error(f"Unknown store type for URL: {product.url}")
                return False
            
            logger.info(f"Detected store type: {store_type}")
            
            # Get quantity from product settings
            quantity = product.auto_cart_quantity or 1
            
            # Use the add_to_cart function from scrapers module with our enhanced scraper
            logger.info(f"Attempting to add product to cart: {product.name} (Quantity: {quantity})")
            
            try:
                # Here we use the enhanced add_to_cart method that will fall back to HTTP if WebDriver fails
                from app.scrapers import add_to_cart
                result = add_to_cart(store_type, product.url, quantity)
                
                # Log the result
                logger.info(f"Add to cart result - Success: {result['success']}")
                logger.info(f"Message: {result['message']}")
                
                if 'cart_url' in result and result['cart_url']:
                    logger.info(f"Cart URL: {result['cart_url']}")
                
                # Update the product's last cart attempt and status
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = result['message']
                
                # Save changes to the database
                db.session.commit()
                logger.info("Updated product database record with cart attempt results")
                
                return result['success']
                
            except Exception as e:
                logger.error(f"Error adding to cart: {str(e)}")
                logger.error(traceback.format_exc())
                return False
    
    except Exception as e:
        logger.error(f"Error in run_integrated_test: {e}")
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    # Run the integrated test
    success = run_integrated_test()
    
    if success:
        logger.info("Integrated HTTP fallback test completed successfully!")
        sys.exit(0)
    else:
        logger.error("Integrated HTTP fallback test failed")
        sys.exit(1) 