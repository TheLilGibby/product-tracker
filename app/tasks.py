import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app
from app import db
from app.models.product import Product, PriceHistory
from app.scrapers import detect_store_type, get_scraper
from app.notifications import send_product_alert
import urllib.parse
import threading
import atexit

# Set up logger
logger = logging.getLogger(__name__)

# Global scheduler reference
scheduler = None

# Lock for check_all_products to prevent concurrent execution
check_lock = threading.Lock()

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
            # Get all products from database
            products = Product.query.all()
            logger.info(f"Found {len(products)} products to check")
            
            for product in products:
                try:
                    # Store detection lives in app/scrapers; see STORE_DOMAINS there.
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
                            continue
                    except Exception as e:
                        logger.error(f"Error scraping product {product.id}: {str(e)}")
                        continue
                    
                    # Update product with new data
                    old_price = product.current_price
                    old_availability = product.available
                    
                    # Only accept a real name; scrapers return "Unknown Product" when extraction fails
                    scraped_name = product_data.get('name')
                    if scraped_name and scraped_name != "Unknown Product":
                        product.name = scraped_name
                    product.current_price = product_data.get('price') or product.current_price
                    product.available = product_data.get('available', False)
                    product.image_url = product_data.get('image_url') or product.image_url
                    product.last_checked = datetime.utcnow()
                    
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
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 15)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 0)
        
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
        
        # Optional third job: post a dashboard screenshot to Telegram on an interval (0 = off)
        snapshot_minutes = app.config.get('SNAPSHOT_INTERVAL_MINUTES', 0) or 0
        if snapshot_minutes > 0:
            app.scheduler.add_job(
                func=lambda: send_dashboard_snapshot_with_context(app),
                trigger='interval',
                minutes=snapshot_minutes,
                id='telegram_snapshot',
                name='Send dashboard snapshot to Telegram',
                replace_existing=True
            )
            logger.info(f"Dashboard snapshot to Telegram scheduled every {snapshot_minutes} minutes")
        
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

def send_dashboard_snapshot_with_context(app):
    """
    Run the Telegram dashboard snapshot in the application context.
    
    Args:
        app: Flask application instance
    """
    with app.app_context():
        try:
            from app.snapshot import send_dashboard_snapshot
            result = send_dashboard_snapshot()
            if not result.get('success'):
                logger.warning(f"Scheduled dashboard snapshot failed: {result.get('message')}")
        except Exception as e:
            logger.error(f"Error in scheduled dashboard snapshot: {str(e)}", exc_info=True)

def check_auto_cart_opportunities():
    """
    Check for products that meet auto-cart criteria and add them to cart automatically.
    """
    logger.info("Checking for auto-cart opportunities")
    
    # Query for products with auto_cart_enabled that are:
    # 1. Available and notify_on_availability is True, OR
    # 2. Below target price and notify_on_price_drop is True
    eligible_products = Product.query.filter(
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

    # Don't re-add the same item every cycle: skip products already carted
    # successfully, and rate-limit retries after a failed attempt.
    cooldown = timedelta(minutes=current_app.config.get('AUTO_CART_COOLDOWN_MINUTES', 30))
    now = datetime.utcnow()
    products_to_cart = []
    for product in eligible_products:
        status = (product.last_cart_status or '').lower()
        if status.startswith('success'):
            logger.debug(f"Skipping product {product.id}: already in cart ({product.last_cart_status})")
            continue
        if product.last_cart_attempt and now - product.last_cart_attempt < cooldown:
            logger.debug(f"Skipping product {product.id}: last cart attempt {product.last_cart_attempt} is within the {cooldown} cooldown")
            continue
        products_to_cart.append(product)
    eligible_products = products_to_cart

    logger.info(f"Found {len(eligible_products)} products eligible for auto-cart")

    from app.scrapers import add_to_cart
    
    # Track if we had any successful cart additions
    had_successful_cart = False
    
    for product in eligible_products:
        try:
            # Store detection lives in app/scrapers; see STORE_DOMAINS there.
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
                product.last_cart_attempt = datetime.utcnow()
                product.last_cart_status = result.get('message', 'Unknown status')
                db.session.commit()
                
                if result.get('success'):
                    logger.info(f"Successfully added product {product.id} to cart")
                    had_successful_cart = True
                    
                    # Send notification about auto-cart success
                    send_product_alert(product, is_auto_cart=True, cart_url=result.get('cart_url'))
                else:
                    logger.warning(f"Failed to add product {product.id} to cart: {result.get('message', 'Unknown error')}")
            except Exception as e:
                logger.error(f"Error adding product {product.id} to cart: {str(e)}", exc_info=True)
                product.last_cart_attempt = datetime.utcnow()
                product.last_cart_status = f"Error: {str(e)}"
                db.session.commit()
        except Exception as e:
            logger.error(f"Error processing auto-cart for product {product.id}: {str(e)}", exc_info=True)
    
    # This used to call update_cart_count(), which writes the badge count into the
    # Flask session. A session needs a request context, and the scheduler thread has
    # none, so every successful auto-cart logged "Working outside of request context"
    # and the write went nowhere. The badge is recomputed from the database by
    # GET /api/cart-count, which every page polls every 30s, so nothing is needed here.
    if had_successful_cart:
        logger.info("Auto-cart succeeded for at least one product; the nav cart badge "
                    "refreshes on the next /api/cart-count poll") 