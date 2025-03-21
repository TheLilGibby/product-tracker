"""
Final integration test for the enhanced Newegg scraper with HTTP fallback.
This script:
1. Copies the modified scrapers/__init__.py file to Docker
2. Runs a test with the HTTP fallback solution
"""

import os
import sys
import logging
import traceback
import subprocess
import time
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_command(cmd):
    """Run a shell command and return output"""
    logger.info(f"Running command: {cmd}")
    try:
        result = subprocess.run(cmd, shell=True, check=True, text=True, 
                               capture_output=True)
        logger.info(f"Command output: {result.stdout}")
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}")
        logger.error(f"Error output: {e.stderr}")
        raise

def run_integration_test():
    """Run the full integration test"""
    try:
        logger.info("Starting final integration test")
        
        # Step 1: Make sure we have the modified scrapers/__init__.py
        if not os.path.exists('modified_scrapers_init.py'):
            logger.error("modified_scrapers_init.py not found!")
            return False
            
        # Step 2: Create a temporary directory in Docker
        logger.info("Creating temporary directory in Docker")
        run_command("docker exec app-tracker mkdir -p /tmp/integration_test")
        
        # Step 3: Copy the modified __init__.py to Docker
        logger.info("Copying modified __init__.py to Docker")
        run_command("docker cp modified_scrapers_init.py app-tracker:/tmp/integration_test/__init__.py")
        
        # Step 4: Copy the HTTP handler to Docker
        if os.path.exists('direct_http_newegg_cart.py'):
            logger.info("Copying HTTP handler to Docker")
            run_command("docker cp direct_http_newegg_cart.py app-tracker:/tmp/integration_test/")
        else:
            logger.warning("direct_http_newegg_cart.py not found, skipping")
        
        # Step 5: Copy the enhanced scraper to Docker
        if os.path.exists('newegg_scraper_with_http_fallback.py'):
            logger.info("Copying enhanced scraper to Docker")
            run_command("docker cp newegg_scraper_with_http_fallback.py app-tracker:/tmp/integration_test/")
        else:
            logger.warning("newegg_scraper_with_http_fallback.py not found, skipping")
            
        # Step 6: Create integration test script in Docker
        logger.info("Creating integration test script in Docker")
        integration_script = """
import os
import sys
import logging
import traceback
from datetime import datetime
import shutil

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('integration_test')

def run():
    try:
        logger.info("Starting integration test")
        
        # Step 1: Back up original modules
        logger.info("Backing up original modules")
        app_scrapers_dir = '/app/app/scrapers'
        
        if os.path.exists(f"{app_scrapers_dir}/__init__.py.bak"):
            logger.info("Backup already exists, skipping backup")
        else:
            shutil.copy(f"{app_scrapers_dir}/__init__.py", f"{app_scrapers_dir}/__init__.py.bak")
        
        # Step 2: Copy our modified modules
        logger.info("Copying modified modules")
        
        # Copy __init__.py
        shutil.copy("/tmp/integration_test/__init__.py", f"{app_scrapers_dir}/__init__.py")
        
        # Copy HTTP handler if it exists
        if os.path.exists("/tmp/integration_test/direct_http_newegg_cart.py"):
            shutil.copy("/tmp/integration_test/direct_http_newegg_cart.py", 
                        f"{app_scrapers_dir}/direct_http_newegg_cart.py")
        
        # Copy enhanced scraper if it exists
        if os.path.exists("/tmp/integration_test/newegg_scraper_with_http_fallback.py"):
            shutil.copy("/tmp/integration_test/newegg_scraper_with_http_fallback.py", 
                        f"{app_scrapers_dir}/newegg_scraper_with_http_fallback.py")
        
        # Step 3: Import the necessary modules
        logger.info("Importing app modules")
        from app import create_app, db
        from app.models.product import Product
        
        # Step 4: Create app context
        app = create_app()
        with app.app_context():
            # Step 5: Get product ID 10
            product = Product.query.get(10)
            
            if not product:
                logger.error("Product with ID 10 not found")
                return False
            
            logger.info(f"Product details - ID: {product.id}, Name: {product.name}")
            logger.info(f"URL: {product.url}")
            logger.info(f"Auto-cart enabled: {product.auto_cart_enabled}")
            
            # Step 6: Determine store type
            store_type = None
            if "newegg.com" in product.url:
                store_type = "newegg"
            elif "amazon.com" in product.url:
                store_type = "amazon"
            elif "bestbuy.com" in product.url:
                store_type = "bestbuy"
            
            if not store_type:
                logger.error(f"Cannot determine store type for URL: {product.url}")
                return False
            
            logger.info(f"Detected store type: {store_type}")
            
            # Step 7: Import the add_to_cart function
            logger.info("Importing add_to_cart function")
            from app.scrapers import add_to_cart
            
            # Step 8: Try adding to cart
            try:
                logger.info(f"Adding product to cart: {product.url}")
                quantity = product.auto_cart_quantity or 1
                
                result = add_to_cart(store_type, product.url, quantity)
                
                logger.info(f"Result - Success: {result.get('success')}")
                logger.info(f"Message: {result.get('message')}")
                
                if result.get('cart_url'):
                    logger.info(f"Cart URL: {result.get('cart_url')}")
                
                # Update product
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = result.get('message')
                db.session.commit()
                
                return result.get('success', False)
            except Exception as e:
                logger.error(f"Error adding to cart: {str(e)}")
                logger.error(traceback.format_exc())
                return False
    
    except Exception as e:
        logger.error(f"Integration test failed: {str(e)}")
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    success = run()
    if success:
        logger.info("Integration test succeeded!")
        sys.exit(0)
    else:
        logger.error("Integration test failed!")
        sys.exit(1)
"""
        
        # Write the integration script to a file
        with open('temp_integration_script.py', 'w') as f:
            f.write(integration_script)
            
        # Copy it to Docker
        run_command("docker cp temp_integration_script.py app-tracker:/tmp/integration_test/run.py")
        
        # Clean up local temp file
        os.remove('temp_integration_script.py')
        
        # Step 7: Run the integration test in Docker
        logger.info("Running integration test in Docker")
        result = run_command("docker exec app-tracker python3 /tmp/integration_test/run.py")
        
        logger.info("Integration test complete")
        return "Integration test succeeded" in result
        
    except Exception as e:
        logger.error(f"Error in integration test: {str(e)}")
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    # Run the integration test
    success = run_integration_test()
    
    if success:
        logger.info("Final integration test completed successfully!")
        sys.exit(0)
    else:
        logger.error("Final integration test failed")
        sys.exit(1) 