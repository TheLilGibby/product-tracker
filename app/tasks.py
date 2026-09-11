import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app
from app import db
from app.models.product import AvailabilityHistory, Product, PriceHistory
from app.scrapers import detect_store_type, get_scraper, is_by_design_refusal
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

# --------------------------------------------------------------------------
# Per-store backoff
#
# The retailers with bot protection answer a burst of failed scrapes by
# blocking harder and for longer: Best Buy's Akamai starts serving block pages
# to every request, Target puts up the PX press-and-hold. Retrying that store
# on every check cycle is what turns a minute of rate limiting into hours of
# block pages, so a store that keeps failing is left alone for a while.
#
# Defaults: two consecutive failures buy 15 minutes of quiet, and each further
# failure doubles that up to an hour.
DEFAULT_STORE_BACKOFF_FAILURES = 2
DEFAULT_STORE_BACKOFF_MINUTES = 15
STORE_BACKOFF_MAX_MINUTES = 60

# store_type -> consecutive failed scrapes, and the UTC time it may be tried
# again. Only touched from check_all_products, which check_lock serializes.
_store_failures = {}
_store_retry_at = {}


def _backoff_settings():
    """
    (failures before backing off, first backoff in minutes) from app config.

    Falls back to the defaults outside an application context so the helpers
    below stay usable from a script or a test.
    """
    config = _config()
    return (int(config.get('STORE_BACKOFF_FAILURES', DEFAULT_STORE_BACKOFF_FAILURES)),
            float(config.get('STORE_BACKOFF_MINUTES', DEFAULT_STORE_BACKOFF_MINUTES)))


def _config():
    """current_app.config, or an empty mapping outside an app context."""
    try:
        return current_app.config
    except RuntimeError:
        return {}


def store_is_backed_off(store_type, now=None):
    """
    True when this store failed too often recently and should be skipped.

    Logs one INFO the first time a store comes due again, so the log says why
    a store went quiet and when it came back.
    """
    retry_at = _store_retry_at.get(store_type)
    if retry_at is None:
        return False

    now = now or datetime.utcnow()
    if now < retry_at:
        return True

    del _store_retry_at[store_type]
    logger.info(f"Retrying {store_type} after backoff "
                f"({_store_failures.get(store_type, 0)} consecutive failures so far)")
    return False


def record_store_failure(store_type, reason, now=None):
    """
    Count a failed scrape and, once there have been enough, put the store on ice.

    Returns the number of consecutive failures. The WARNING fires only when the
    store actually enters backoff, so it is one line per cycle at worst.
    """
    failures = _store_failures.get(store_type, 0) + 1
    _store_failures[store_type] = failures
    threshold, base_minutes = _backoff_settings()

    if failures < threshold:
        logger.debug(f"{store_type} scrape failed ({reason}); "
                     f"{failures} of {threshold} before backing off")
        return failures

    cap = float(_config().get('STORE_BACKOFF_MAX_MINUTES', STORE_BACKOFF_MAX_MINUTES))
    minutes = min(base_minutes * (2 ** (failures - threshold)), cap)
    _store_retry_at[store_type] = (now or datetime.utcnow()) + timedelta(minutes=minutes)
    logger.warning(f"{store_type} has failed {failures} scrapes in a row ({reason}); "
                   f"skipping its products for {minutes:g} minutes")
    return failures


def record_store_success(store_type):
    """Clear a store's failure history after a scrape that actually worked."""
    if _store_failures.pop(store_type, None):
        logger.info(f"{store_type} is answering again; backoff cleared")
    _store_retry_at.pop(store_type, None)


def reset_store_backoff():
    """Forget every store's backoff state. For tests and manual recovery."""
    _store_failures.clear()
    _store_retry_at.clear()


# --------------------------------------------------------------------------
# Per-store check intervals
#
# The scheduler runs on one global interval, but not every store can take it.
# A store listed in STORE_CHECK_INTERVALS is only scraped once its own interval
# has passed; everything else keeps the global cadence.
def store_check_interval(store_type):
    """
    Minutes between checks for this store, or None when it has no setting of
    its own and just follows the scheduler's global interval.
    """
    intervals = _config().get('STORE_CHECK_INTERVALS') or {}
    try:
        return float(intervals[store_type])
    except (KeyError, TypeError, ValueError):
        return None


def store_check_is_due(store_type, last_checked, now=None):
    """
    False only when a store has an interval of its own and was checked inside it.

    A store with no setting is left to the scheduler's cadence rather than
    gated here. Gating it on the global interval would read the same on a
    scheduled run and break the "Update All Products" button, which calls
    check_all_products directly and must still refresh what the user asked for.
    """
    interval = store_check_interval(store_type)
    if interval is None or interval <= 0 or last_checked is None:
        return True

    # The grace keeps a store whose interval matches the scheduler's own from
    # slipping a whole cycle when a run starts a moment early.
    grace = min(60.0, interval * 60.0 * 0.1)
    elapsed = ((now or datetime.utcnow()) - last_checked).total_seconds()
    return elapsed >= (interval * 60.0) - grace


def record_availability(product, now):
    """
    Log a listing's stock state when it differs from the last one logged.

    The comparison is against the listing's newest AvailabilityHistory row, not
    the value check_all_products just overwrote. That way a listing's first
    check logs its starting state, and a change that "Update Now" saw first
    (it sets product.available without logging) is still logged on the next
    scheduled check instead of being lost.
    """
    available = bool(product.available)
    last = (AvailabilityHistory.query
            .filter_by(product_id=product.id)
            .order_by(AvailabilityHistory.timestamp.desc(), AvailabilityHistory.id.desc())
            .first())
    if last is not None and last.available == available:
        return
    db.session.add(AvailabilityHistory(product_id=product.id, timestamp=now,
                                       available=available, price=product.current_price))
    if last is not None:
        logger.info(f"Stock changed for product {product.id}: "
                    f"{'in stock' if available else 'out of stock'}")


# Names for the dashboard. A store missing from here falls back to its key.
STORE_LABELS = {
    'amazon': 'Amazon',
    'walmart': 'Walmart',
    'newegg': 'Newegg',
    'microcenter': 'Micro Center',
    'bestbuy': 'Best Buy',
    'bh': 'B&H',
    'test': 'Test store',
}


def _minutes_until(moment, now):
    """Whole minutes from now until moment, rounded up."""
    return int(-(-(moment - now).total_seconds() // 60))


def get_store_backoff_state(now=None):
    """
    One row per store the scheduler is currently having trouble with.

    A store that is answering normally holds no state at all, so an empty list
    means every store is fine. Each row is:

        {'store_type', 'label', 'backed_off', 'failures',
         'retry_at' (naive UTC, or None), 'minutes_remaining'}

    Read without check_lock - the scheduler thread may be writing while a
    request reads. The dict copies below are single C-level operations, and a
    row that is a few seconds stale only affects what the page says.
    """
    now = now or datetime.utcnow()
    failures = dict(_store_failures)
    retry_times = dict(_store_retry_at)

    rows = []
    for store_type in sorted(set(failures) | set(retry_times)):
        retry_at = retry_times.get(store_type)
        backed_off = retry_at is not None and now < retry_at
        rows.append({
            'store_type': store_type,
            'label': STORE_LABELS.get(store_type, store_type.title()),
            'backed_off': backed_off,
            'failures': failures.get(store_type, 0),
            'retry_at': retry_at if backed_off else None,
            # Rounded up, so a store due in 40 seconds reads "1 min", not "0 min".
            'minutes_remaining': (_minutes_until(retry_at, now) if backed_off else 0),
        })
    return rows
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
                    # Store detection lives in app/scrapers; see STORE_DOMAINS there.
                    store_type = detect_store_type(product.url)
                    
                    if not store_type:
                        logger.error(f"Could not determine store type for URL: {product.url}")
                        continue
                    
                    # Leave a blocked or rate-limited store alone. The product
                    # keeps its stored data and its last_checked, so the page
                    # still shows when the figures were last known good.
                    if store_is_backed_off(store_type):
                        logger.debug(f"Skipping product {product.id}: {store_type} is backing off")
                        continue

                    # Stores that cannot take the global cadence wait for their
                    # own interval, again keeping their stored data untouched.
                    if not store_check_is_due(store_type, product.last_checked):
                        logger.debug(f"Skipping product {product.id}: {store_type} was checked "
                                     f"less than {store_check_interval(store_type):g} minutes ago")
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
                            record_store_failure(store_type, 'scrape returned no data')
                            continue
                    except Exception as e:
                        logger.error(f"Error scraping product {product.id}: {str(e)}")
                        record_store_failure(store_type, f"scrape raised {type(e).__name__}")
                        continue
                    
                    record_store_success(store_type)

                    # Update product with new data
                    old_price = product.current_price
                    old_availability = product.available
                    
                    # Only accept a real name; scrapers return "Unknown Product" or a
                    # bot-wall title when extraction fails, and a wall has no image.
                    scraped_name = usable_scraped_name(product_data.get('name'))
                    if scraped_name:
                        product.name = scraped_name
                        product.image_url = product_data.get('image_url') or product.image_url
                    product.current_price = product_data.get('price') or product.current_price
                    product.available = product_data.get('available', False)
                    product.last_checked = datetime.utcnow()
                    record_availability(product, product.last_checked)

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
                previous_status = product.last_cart_status
                result = add_to_cart(store_type, product.url, quantity)
                message = result.get('message', 'Unknown status')

                # Update product with cart attempt results. This is what arms
                # the cooldown, and it runs for a refusal exactly as it does for
                # a success - a scraper that declines to click has still had its
                # turn, and retrying it a minute later would only decline again.
                product.last_cart_attempt = datetime.utcnow()
                product.last_cart_status = message
                db.session.commit()

                if result.get('success'):
                    logger.info(f"Successfully added product {product.id} to cart")
                    had_successful_cart = True

                    # Send notification about auto-cart success
                    notify_cart_success(product, cart_url=result.get('cart_url'))
                elif is_by_design_refusal(message):
                    # Not a failure: the scraper looked at the page and correctly
                    # declined. Said once at INFO, and dropped to DEBUG while the
                    # answer stays the same, so a product that will refuse for
                    # weeks does not file a warning every cooldown for weeks.
                    if message == previous_status:
                        logger.debug(f"Product {product.id} still not cartable: {message}")
                    else:
                        logger.info(f"Leaving product {product.id} alone: {message}")
                else:
                    logger.warning(f"Failed to add product {product.id} to cart: {message}")
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
