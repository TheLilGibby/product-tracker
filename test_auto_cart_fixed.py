"""
Test auto-cart functionality with improved chromedriver handling
"""

import os
import sys
import logging
from datetime import datetime
import traceback
import subprocess

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def ensure_chromedriver():
    """Make sure chromedriver is properly installed before proceeding"""
    try:
        # Check if chromedriver exists in the expected location
        chromedriver_path = os.path.expanduser("~/.local/share/undetected_chromedriver/undetected/chromedriver-linux64/chromedriver")
        
        if not os.path.exists(chromedriver_path):
            logger.info("Chromedriver not found. Installing it now...")
            
            # Run the shell script to install chromedriver
            shell_script = "/app/install_chromedriver_in_docker.sh"
            
            if os.path.exists(shell_script):
                logger.info("Running chromedriver installation script...")
                result = subprocess.run(["bash", shell_script], check=True)
                
                if result.returncode == 0:
                    logger.info("Chromedriver installed successfully!")
                    return True
                else:
                    logger.error("Failed to install chromedriver using shell script")
                    return False
            else:
                # Try the Python script as fallback
                logger.info("Shell script not found. Trying Python installation script...")
                try:
                    from fix_chromedriver_alternative import install_chromedriver_py, create_symlinks, fix_chromedriver_directly
                    
                    if install_chromedriver_py() and create_symlinks():
                        logger.info("Successfully installed chromedriver with chromedriver-py")
                        return True
                    
                    if fix_chromedriver_directly():
                        logger.info("Successfully installed chromedriver with direct installation")
                        return True
                    
                    logger.error("Failed to install chromedriver with Python script")
                    return False
                except ImportError:
                    logger.error("Failed to import fix_chromedriver_alternative")
                    return False
        else:
            logger.info(f"Chromedriver already exists at {chromedriver_path}")
            return True
    except Exception as e:
        logger.error(f"Error ensuring chromedriver: {e}")
        return False

def run_auto_cart_test():
    """Run the actual auto-cart test"""
    try:
        logger.info("Starting auto-cart test")
        
        # Import necessary modules
        from app import create_app, db
        from app.models.products import Product
        import undetected_chromedriver as uc
        from selenium.webdriver.chrome.options import Options
        
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
            logger.info(f"Auto-cart enabled: {product.auto_cart}")
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
            
            # Initialize Chrome options
            options = Options()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            
            logger.info("Initializing Chrome driver")
            
            # Initialize the WebDriver using undetected_chromedriver
            driver = uc.Chrome(options=options)
            
            try:
                # Import the appropriate scraper
                if store_type == "newegg":
                    from app.scrapers.newegg_scraper import NeweggScraper
                    scraper = NeweggScraper(driver)
                elif store_type == "bestbuy":
                    from app.scrapers.bestbuy_scraper import BestBuyScraper
                    scraper = BestBuyScraper(driver)
                elif store_type == "amazon":
                    from app.scrapers.amazon_scraper import AmazonScraper
                    scraper = AmazonScraper(driver)
                else:
                    logger.error(f"No scraper available for store type: {store_type}")
                    return False
                
                # Set the URL for the scraper
                scraper.url = product.url
                
                # Try to add the product to the cart
                logger.info(f"Attempting to add product to cart: {product.name}")
                result = scraper.add_to_cart()
                
                # Log the result
                logger.info(f"Add to cart result - Success: {result['success']}")
                logger.info(f"Message: {result['message']}")
                logger.info(f"Cart URL: {result.get('cart_url', 'N/A')}")
                logger.info(f"Screenshot: {result.get('screenshot', 'N/A')}")
                
                # Update the product's last cart attempt and status
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = result['message']
                
                # Save changes to the database
                db.session.commit()
                
                return result['success']
            
            finally:
                # Close the driver
                driver.quit()
                logger.info("Chrome driver closed")
    
    except Exception as e:
        logger.error(f"Error in run_auto_cart_test: {e}")
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    # First ensure chromedriver is installed
    if ensure_chromedriver():
        # Then run the auto-cart test
        success = run_auto_cart_test()
        
        if success:
            logger.info("Auto-cart test completed successfully!")
            sys.exit(0)
        else:
            logger.error("Auto-cart test failed")
            sys.exit(1)
    else:
        logger.error("Failed to ensure chromedriver is installed")
        sys.exit(1) 