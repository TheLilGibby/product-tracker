#!/usr/bin/env python3
"""
Test script for auto-cart functionality with HTTP fallback
"""

import logging
import sys
import os
import time

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

def test_auto_cart():
    """Test the Newegg HTTP fallback for auto-cart"""
    try:
        logger.info("Starting auto-cart test with HTTP fallback")
        
        # Import necessary modules
        from app import create_app, db
        from app.models.product import Product
        from app.scrapers import add_to_cart
        
        # Create app context
        app = create_app()
        
        with app.app_context():
            # Get product with ID 10
            product = Product.query.get(10)
            
            if not product:
                logger.error("No product found with ID 10")
                return False
                
            logger.info(f"Product: {product.name}")
            logger.info(f"URL: {product.url}")
            logger.info(f"Current auto-cart status: {product.last_cart_status}")
            
            # Determine store type from URL
            store_type = None
            if 'newegg.com' in product.url.lower():
                store_type = 'newegg'
            elif 'amazon.com' in product.url.lower():
                store_type = 'amazon'
            elif 'bestbuy.com' in product.url.lower():
                store_type = 'bestbuy'
            else:
                logger.error(f"Unknown store type for URL: {product.url}")
                return False
                
            logger.info(f"Detected store type: {store_type}")
            
            # Get quantity from product settings
            quantity = product.auto_cart_quantity or 1
            logger.info(f"Auto-cart quantity: {quantity}")
            
            # Try to add to cart
            logger.info(f"Adding product to cart: {product.name}")
            
            try:
                # Call the add_to_cart function
                result = add_to_cart(store_type, product.url, quantity)
                
                # Log result
                logger.info("Auto-cart result:")
                logger.info(f"Success: {result.get('success', False)}")
                logger.info(f"Message: {result.get('message', 'N/A')}")
                if 'cart_url' in result and result['cart_url']:
                    logger.info(f"Cart URL: {result['cart_url']}")
                    
                # Update product status
                product.last_cart_attempt = time.time()
                product.last_cart_status = result.get('message', 'Unknown status')
                db.session.commit()
                
                logger.info(f"Updated database with new cart status: {product.last_cart_status}")
                
                return result.get('success', False)
                
            except Exception as e:
                logger.error(f"Error adding to cart: {str(e)}")
                import traceback
                logger.error(traceback.format_exc())
                return False
        
    except Exception as e:
        logger.error(f"Error in test_auto_cart: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    success = test_auto_cart()
    
    if success:
        logger.info("Auto-cart test completed successfully!")
        sys.exit(0)
    else:
        logger.error("Auto-cart test failed")
        sys.exit(1) 