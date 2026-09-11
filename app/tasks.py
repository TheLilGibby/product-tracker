import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app
from app import db
from app.models.product import Product, PriceHistory
from app.scrapers import get_scraper, detect_store_type
from app.notifications import send_product_alert, notify_cart_success
import urllib.parse
import threading
import atexit

# Set up logger
logger = logging.getLogger(__name__)

# Global scheduler reference
scheduler = None

# Lock for check_all_products to prevent concurrent execution
check_lock = threading.Lock()

_BAD_SCRAPE_NAMES = {
    'unknown product',
    'robot or human?',
    'robot or human',
    'access denied',
    'sorry, you have been blocked',
    'attention required',
    'attention required!',
}


def usable_scraped_name(name):
    """Return a real product name, or None if the scrape hit a wall/placeholder."""
    if not name:
        return None
    cleaned = str(name).strip()
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower in _BAD_SCRAPE_NAMES:
        return None
    if any(token in lower for token in ('captcha', 'verify you', 'are you a robot', 'you have been blocked')):
        return None
    return cleaned

def check_all_products():
    """
    Check all products in the database for updates.
    Updates price and availability information.
    """
    # Use a lock to prevent concurrent execution
    if not check_lock.acquire(blocking=False):
        logger.warning("Skipping check_all_products, already running")
        return
        
    try:
        logger.info(f"Starting scheduled check of all products at {datetime.utcnow()}")
        
        from flask import current_app
        
        # Get app context
        if not current_app:
            # If no current app context, create one
            from app import create_app
            app = create_app()
            ctx = app.app_context()
            ctx.push()
            logger.info("Created new app context for scheduled task")
        else:
            app = current_app
            ctx = None
            logger.info("Using existing app context for scheduled task")
        
        try:
            # Products switched off from the dashboard are skipped, not deleted
            products = Product.query.filter(Product.tracking_enabled == True).all()
            logger.info(f"Found {len(products)} products to check")
            
            for product in products:
                try:
                    # Get store type from URL domain
                    store_type = detect_store_type(product.url)
                    
                    if not store_type:
                        logger.error(f"Could not determine store type for URL: {product.url}")
                        continue
                    
                    # Get appropriate scraper
                    try:
                        scraper = get_scraper(store_type)
                    except ValueError as e:
                        logger.error(f"Error getting scraper for {store_type}: {str(e)}")
                        continue
                    except Exception as e:
                        logger.error(f"Unexpected error getting scraper for {store_type}: {str(e)}")
                        continue
                    
                    # Scrape product data
                    try:
                        if store_type == 'test':
                            # TestScraper uses get_product_info instead of scrape_product
                            product_data = scraper.get_product_info(url=product.url)
                        else:
                            product_data = scraper.scrape_product(product.url)
                            
                        if not product_data:
                            logger.error(f"Failed to retrieve data for product {product.id}")
                            if not product.image_url and store_type == 'bestbuy':
                                from app.scrapers.bestbuy_scraper import BestBuyScraper
                                fallback = BestBuyScraper.image_url_from_url(product.url)
                                if fallback:
                                    product.image_url = fallback
                                    db.session.commit()
                            continue
                    except Exception as e:
                        logger.error(f"Error scraping product {product.id}: {str(e)}")
                        continue
                    
                    # Update product with new data
                    old_price = product.current_price
                    old_availability = product.available
                    
                    scraped_name = usable_scraped_name(product_data.get('name'))
                    if scraped_name:
                        product.name = scraped_name
                        product.image_url = product_data.get('image_url') or product.image_url
                    product.current_price = product_data.get('price') or product.current_price
                    product.available = product_data.get('available', False)
                    product.last_checked = datetime.utcnow()
                    product.record_stock_check()
                    
                    # Record price history if price changed
                    if product.current_price is not None and product.current_price != old_price:
                        history = PriceHistory(
                            product_id=product.id,
                            price=product.current_price,
                            timestamp=datetime.utcnow()
                        )
                        db.session.add(history)
                        logger.info(f"Price changed for product {product.id}: {old_price} -> {product.current_price}")
                    
                    # Send notifications to every configured channel (Discord webhook, Telegram)
                    # Price drop notification
                    if (product.notify_on_price_drop and 
                        product.current_price is not None and 
                        old_price is not None and 
                        product.current_price < old_price):
                        send_product_alert(product, old_price=old_price, is_availability_alert=False)
                        logger.info(f"Price drop notification for product {product.id}: {product.current_price} -> {old_price}")
                    
                    # Availability notification
                    if (product.notify_on_availability and 
                        product.available and 
                        not old_availability):
                        send_product_alert(product, old_price=old_price, is_availability_alert=True)
                        logger.info(f"Availability notification for product {product.id}: {product.current_price} -> {old_price}")
                    
                    # Commit changes
                    db.session.commit()
                    logger.info(f"Updated product {product.id}: {product.name}")
                    
                except Exception as e:
                    logger.error(f"Error updating product {product.id}: {str(e)}", exc_info=True)
                    db.session.rollback()
        except Exception as e:
            logger.error(f"Error in check_all_products: {str(e)}", exc_info=True)
            db.session.rollback()
        finally:
            # Release app context if we created one
            if ctx:
                ctx.pop()
                logger.info("Released app context for scheduled task")
    finally:
        # Always release the lock, even if an exception occurred
        check_lock.release()
        logger.info("Finished scheduled check of all products")

def init_scheduler(app):
    """
    Initialize the APScheduler.
    
    Args:
        app: Flask application instance
    """
    logger.info("Initializing scheduler")
    
    with app.app_context():
        # Get interval from app config (minutes and seconds)
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 0)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 10)
        
        # Convert to total seconds
        interval_seconds = (minutes * 60) + seconds
        
        # Ensure minimum interval of 10 seconds
        if interval_seconds < 10:
            logger.warning("Check interval too low, setting to 10 seconds minimum")
            interval_seconds = 10
            
        logger.info(f"Scheduler will run every {interval_seconds} seconds")
        
        if hasattr(app, 'scheduler'):
            logger.info("Removing existing scheduler jobs")
            app.scheduler.remove_all_jobs()
            app.scheduler.shutdown()
            
        # Create a scheduler
        app.scheduler = BackgroundScheduler()
        
        # Add job to check products on the interval
        app.scheduler.add_job(
            func=lambda: check_all_products_with_context(app),
            trigger='interval',
            seconds=interval_seconds,
            id='check_products',
            name='Check all products',
            replace_existing=True
        )
        
        # Add job to check for auto-cart opportunities (runs every minute)
        app.scheduler.add_job(
            func=lambda: check_auto_cart_opportunities_with_context(app),
            trigger='interval',
            seconds=60,
            id='check_auto_cart',
            name='Check auto cart opportunities',
            replace_existing=True
        )
        
        # Start the scheduler
        app.scheduler.start()
        logger.info("Scheduler started")
        
        # Register a function to shut down the scheduler when the app exits
        atexit.register(lambda: app.scheduler.shutdown() if hasattr(app, 'scheduler') else None)

def check_all_products_with_context(app):
    """
    Run check_all_products in the application context.
    
    Args:
        app: Flask application instance
    """
    with app.app_context():
        try:
            check_all_products()
        except Exception as e:
            logger.error(f"Error in product check: {str(e)}", exc_info=True)

def check_auto_cart_opportunities_with_context(app):
    """
    Run check_auto_cart_opportunities in the application context.
    
    Args:
        app: Flask application instance
    """
    with app.app_context():
        try:
            check_auto_cart_opportunities()
        except Exception as e:
            logger.error(f"Error in auto cart opportunity check: {str(e)}", exc_info=True)

def check_auto_cart_opportunities():
    """
    Check for products that meet auto-cart criteria and add them to cart automatically.
    """
    logger.info("Checking for auto-cart opportunities")
    
    # Query for products with auto_cart_enabled that are:
    # 1. Available and notify_on_availability is True, OR
    # 2. Below target price and notify_on_price_drop is True
    eligible_products = Product.query.filter(
        Product.tracking_enabled == True,
        Product.auto_cart_enabled == True,
        db.or_(
            db.and_(
                Product.available == True,
                Product.notify_on_availability == True
            ),
            db.and_(
                Product.current_price != None,
                Product.target_price != None,
                Product.current_price <= Product.target_price,
                Product.notify_on_price_drop == True
            )
        )
    ).all()
    
    logger.info(f"Found {len(eligible_products)} products eligible for auto-cart")
    
    from app.scrapers import add_to_cart
    
    # Track if we had any successful cart additions
    had_successful_cart = False
    
    for product in eligible_products:
        try:
            # Determine store type from URL
            store_type = detect_store_type(product.url)
            
            if not store_type:
                logger.warning(f"Could not determine store type for {product.url}")
                continue
                
            # Try to add to cart
            logger.info(f"Attempting to add product {product.id} ({product.name}) to cart")
            
            # Get quantity from product settings
            quantity = product.auto_cart_quantity or 1
            
            try:
                result = add_to_cart(store_type, product.url, quantity)
                
                # Update product with cart attempt results
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = result.get('message', 'Unknown status')
                db.session.commit()
                
                if result.get('success'):
                    logger.info(f"Successfully added product {product.id} to cart")
                    had_successful_cart = True
                    notify_cart_success(product, cart_url=result.get('cart_url'))
                else:
                    logger.warning(f"Failed to add product {product.id} to cart: {result.get('message', 'Unknown error')}")
            except Exception as e:
                logger.error(f"Error adding product {product.id} to cart: {str(e)}", exc_info=True)
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = f"Error: {str(e)}"
                db.session.commit()
        except Exception as e:
            logger.error(f"Error processing auto-cart for product {product.id}: {str(e)}", exc_info=True)
    
    # If we had any successful cart additions, update the cart count in the application context
    if had_successful_cart:
        try:
            # Import Flask to access the app
            from flask import current_app, session
            
            # Check if we're in an application context
            if current_app:
                with current_app.app_context():
                    # Update the cart count function
                    # This function would be imported from routes.main to avoid circular imports
                    from app.routes.main import update_cart_count
                    update_cart_count()
        except Exception as e:
            logger.error(f"Error updating cart count after auto-cart: {str(e)}", exc_info=True) 

def refresh_product(product):
    """
    Scrape a single product, persist any changes, and fire channel alerts.

    Shared by the JSON API (and therefore the MCP server) so a one-off check
    behaves exactly like a scheduled one.

    Args:
        product: A Product instance

    Returns:
        dict: {'success', 'message', 'price_changed', 'became_available',
               'old_price', 'new_price'}
    """
    store_type = detect_store_type(product.url)
    if not store_type:
        return {
            'success': False,
            'message': f"Could not determine store type for URL: {product.url}",
            'price_changed': False,
            'became_available': False,
            'old_price': product.current_price,
            'new_price': product.current_price,
        }

    try:
        scraper = get_scraper(store_type)
        if store_type == 'test':
            # TestScraper uses get_product_info instead of scrape_product
            product_data = scraper.get_product_info(url=product.url)
        else:
            product_data = scraper.scrape_product(product.url)
    except Exception as e:
        logger.error(f"Error scraping product {product.id}: {str(e)}", exc_info=True)
        return {
            'success': False,
            'message': f"Error scraping product: {str(e)}",
            'price_changed': False,
            'became_available': False,
            'old_price': product.current_price,
            'new_price': product.current_price,
        }

    if not product_data:
        if not product.image_url and detect_store_type(product.url) == 'bestbuy':
            from app.scrapers.bestbuy_scraper import BestBuyScraper
            fallback = BestBuyScraper.image_url_from_url(product.url)
            if fallback:
                product.image_url = fallback
                db.session.commit()
        return {
            'success': False,
            'message': 'Failed to retrieve product information',
            'price_changed': False,
            'became_available': False,
            'old_price': product.current_price,
            'new_price': product.current_price,
        }

    old_price = product.current_price
    old_availability = product.available

    scraped_name = usable_scraped_name(product_data.get('name'))
    if scraped_name:
        product.name = scraped_name
        product.image_url = product_data.get('image_url') or product.image_url
    product.current_price = product_data.get('price') or product.current_price
    product.available = product_data.get('available', False)
    product.last_checked = datetime.utcnow()
    product.record_stock_check()

    price_changed = product.current_price is not None and product.current_price != old_price
    if price_changed:
        db.session.add(PriceHistory(
            product_id=product.id,
            price=product.current_price,
            timestamp=datetime.utcnow()
        ))

    # Send notifications to every configured channel (Discord webhook, Telegram)
    if (product.notify_on_price_drop and
            product.current_price is not None and
            old_price is not None and
            product.current_price < old_price):
        send_product_alert(product, old_price=old_price, is_availability_alert=False)

    became_available = bool(product.available and not old_availability)
    if product.notify_on_availability and became_available:
        send_product_alert(product, old_price=old_price, is_availability_alert=True)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error saving product {product.id}: {str(e)}", exc_info=True)
        return {
            'success': False,
            'message': f"Error saving product: {str(e)}",
            'price_changed': False,
            'became_available': False,
            'old_price': old_price,
            'new_price': product.current_price,
        }

    return {
        'success': True,
        'message': 'Product refreshed',
        'price_changed': price_changed,
        'became_available': became_available,
        'old_price': old_price,
        'new_price': product.current_price,
    }
