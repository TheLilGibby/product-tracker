import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app
from app import db
from app.models.product import Product, PriceHistory
from app.scrapers import get_scraper
from app.notifications.discord import DiscordNotifier
import urllib.parse
import threading

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
            
            # Map domains to store types
            domain_to_store = {
                'amazon.com': 'amazon',
                'www.amazon.com': 'amazon',
                'walmart.com': 'walmart',
                'www.walmart.com': 'walmart',
                'newegg.com': 'newegg',
                'www.newegg.com': 'newegg',
                'microcenter.com': 'microcenter',
                'www.microcenter.com': 'microcenter',
                'bestbuy.com': 'bestbuy',
                'www.bestbuy.com': 'bestbuy',
                'bhphotovideo.com': 'bh',
                'www.bhphotovideo.com': 'bh',
            }
            
            for product in products:
                try:
                    # Get store type from URL domain
                    domain = urllib.parse.urlparse(product.url).netloc.lower()
                    store_type = None
                    for d, s in domain_to_store.items():
                        if d in domain:
                            store_type = s
                            break
                    
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
                    
                    product.name = product_data.get('name') or product.name
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
                    
                    # Send notifications if configured
                    if product.discord_webhook_url:
                        # Price drop notification
                        if (product.notify_on_price_drop and 
                            product.current_price is not None and 
                            old_price is not None and 
                            product.current_price < old_price):
                            DiscordNotifier.send_notification(
                                product.discord_webhook_url,
                                product.name,
                                product.url,
                                product.current_price,
                                old_price,
                                is_availability_alert=False,
                                image_url=product.image_url
                            )
                            logger.info(f"Price drop notification for product {product.id}: {product.current_price} -> {old_price}")
                        
                        # Availability notification
                        if (product.notify_on_availability and 
                            product.available and 
                            not old_availability):
                            DiscordNotifier.send_notification(
                                product.discord_webhook_url,
                                product.name,
                                product.url,
                                product.current_price,
                                old_price,
                                is_availability_alert=True,
                                image_url=product.image_url
                            )
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
    Initialize the scheduler with the app context
    
    Args:
        app: Flask application instance
    """
    global scheduler
    
    # Shutdown any existing scheduler to prevent multiple instances
    try:
        if scheduler and scheduler.running:
            logger.info("Shutting down existing scheduler")
            scheduler.shutdown(wait=False)
    except Exception as e:
        logger.warning(f"Error shutting down existing scheduler: {str(e)}")
    
    with app.app_context():
        # Create a new scheduler
        scheduler = BackgroundScheduler()
        
        # Get interval from config in seconds
        # Convert the property to a direct integer value
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 15)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 0)
        check_interval_seconds = (minutes * 60) + seconds
        
        # Add job to check products at regular intervals
        scheduler.add_job(
            check_all_products,
            IntervalTrigger(seconds=check_interval_seconds),
            id='check_products',
            replace_existing=True,
            max_instances=1  # Ensure only one instance of the job runs at a time
        )
        
        # Start the scheduler
        scheduler.start()
        
        # Log the interval in a human-readable format
        minutes = check_interval_seconds // 60
        seconds = check_interval_seconds % 60
        if seconds == 0:
            logger.info(f"Scheduler initialized with check interval of {minutes} minutes")
        else:
            logger.info(f"Scheduler initialized with check interval of {minutes} minutes and {seconds} seconds")
        
        return scheduler 