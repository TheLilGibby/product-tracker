"""
Test auto-cart functionality with pre-installed chromedriver
"""

import os
import sys
import logging
from datetime import datetime
import traceback

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_auto_cart_test():
    """Run the auto-cart test with pre-installed chromedriver"""
    try:
        logger.info("Starting auto-cart test")
        
        # Set environment variables to use our installed chromedriver
        root_dir = os.path.expanduser("~")
        chromedriver_path = os.path.join(root_dir, ".local", "share", "undetected_chromedriver", 
                                        "undetected", "chromedriver-linux64", "chromedriver")
        
        logger.info(f"Using chromedriver at: {chromedriver_path}")
        os.environ["CHROMEDRIVER_PATH"] = chromedriver_path
        
        # Set debug log levels for Selenium and undetected_chromedriver
        selenium_logger = logging.getLogger('selenium')
        selenium_logger.setLevel(logging.DEBUG)
        uc_logger = logging.getLogger('undetected_chromedriver')
        uc_logger.setLevel(logging.DEBUG)
        
        # Enable verbose logging for Chrome
        os.environ["UC_LOG_LEVEL"] = "DEBUG"
        
        # Import necessary modules
        from app import create_app, db
        from app.models.product import Product
        from app.scrapers import add_to_cart
        
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
            
            # Use the add_to_cart function from scrapers module which creates the appropriate scraper internally
            logger.info(f"Attempting to add product to cart: {product.name}")
            try:
                # Add special Chrome options to avoid bot detection
                os.environ["UC_CHROME_ARGS"] = "--headless=new --no-sandbox --disable-dev-shm-usage --disable-blink-features=AutomationControlled"
                
                # Get quantity from product settings
                quantity = product.auto_cart_quantity or 1
                
                # Call the add_to_cart function which handles creating the scraper
                result = add_to_cart(store_type, product.url, quantity)
                
                # Log the result
                logger.info(f"Add to cart result - Success: {result['success']}")
                logger.info(f"Message: {result['message']}")
                
                if 'cart_url' in result:
                    logger.info(f"Cart URL: {result['cart_url']}")
                else:
                    logger.info("Cart URL not returned")
                    
                if 'screenshot' in result:
                    logger.info(f"Screenshot: {result['screenshot']}")
                else:
                    logger.info("Screenshot not returned")
                
                # Update the product's last cart attempt and status
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = result['message']
                
                # Save changes to the database
                db.session.commit()
                
                return result['success']
                
            except Exception as e:
                logger.error(f"Error adding to cart: {str(e)}")
                logger.error(traceback.format_exc())
                return False
    
    except Exception as e:
        logger.error(f"Error in run_auto_cart_test: {e}")
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    # Run the auto-cart test
    success = run_auto_cart_test()
    
    if success:
        logger.info("Auto-cart test completed successfully!")
        sys.exit(0)
    else:
        logger.error("Auto-cart test failed")
        sys.exit(1) 