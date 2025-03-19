from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app, session
from app import db
from app.models.product import Product, PriceHistory
from app.scrapers import get_scraper
from app.tasks import check_all_products
from datetime import datetime
import logging
import re
import urllib.parse
import traceback
import pytz

# Import all scrapers directly
from app.scrapers.bestbuy_scraper import BestBuyScraper
from app.scrapers.amazon_scraper import AmazonScraper
from app.scrapers.walmart_scraper import WalmartScraper
from app.scrapers.newegg_scraper import NeweggScraper
from app.scrapers.microcenter_scraper import MicrocenterScraper
from app.scrapers.bh_scraper import BHScraper

# Set up logger
logger = logging.getLogger(__name__)

# Create blueprint
main_bp = Blueprint('main', __name__)

# Add context processors for template variables
@main_bp.app_context_processor
def inject_now():
    return {'now': datetime.utcnow()}

@main_bp.app_context_processor
def inject_timezone_data():
    """Inject timezone data for all templates."""
    # Get common timezone groups first
    common_timezones = [
        'UTC',
        'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
        'Europe/London', 'Europe/Paris', 'Europe/Berlin',
        'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Singapore',
        'Australia/Sydney',
    ]
    
    # Then add all other timezones
    all_timezones = common_timezones + sorted([tz for tz in pytz.all_timezones if tz not in common_timezones])
    
    # Get current timezone from session or config
    current_timezone = session.get('timezone', current_app.config.get('DEFAULT_TIMEZONE', 'UTC'))
    
    return {
        'timezones': all_timezones,
        'current_timezone': current_timezone
    }

@main_bp.app_context_processor
def utility_functions():
    """Add utility functions to the template context."""
    
    def format_datetime(dt, timezone='UTC'):
        """Format datetime in the specified timezone."""
        if dt is None:
            return "N/A"
            
        # Ensure dt has timezone info (assume UTC if naive)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
            
        # Convert to target timezone
        target_tz = pytz.timezone(timezone)
        localized_dt = dt.astimezone(target_tz)
        
        # Format datetime
        return localized_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
        
    return {'format_datetime': format_datetime}

@main_bp.route('/')
def index():
    """Home page showing all tracked products."""
    products = Product.query.all()
    return render_template('index.html', products=products)

@main_bp.route('/product/add', methods=['GET', 'POST'])
def add_product():
    """Add a new product to track."""
    # GET request - just show the form
    if request.method == 'GET':
        return render_template('products/add.html')
    
    # POST request - process form submission
    url = request.form.get('url')
    store_type = request.form.get('scraper_type')
    target_price = request.form.get('target_price')
    discord_webhook = request.form.get('discord_webhook')
    notify_price = 'notify_price' in request.form
    notify_availability = 'notify_availability' in request.form
    
    # Validate URL and store type
    if not url:
        flash('Please provide a product URL', 'danger')
        return redirect(url_for('main.add_product'))
        
    if not store_type:
        flash('Please select a store/website type', 'danger')
        return redirect(url_for('main.add_product'))
        
    # Check if product already exists
    existing = Product.query.filter_by(url=url).first()
    if existing:
        flash('This product is already being tracked', 'warning')
        return redirect(url_for('main.product_detail', product_id=existing.id))
    
    # For Newegg products, use a different approach as they have strong anti-scraping
    if store_type == 'newegg':
        logger.info(f"Processing Newegg product: {url}")
        try:
            # Extract product ID from URL
            import re
            product_id = None
            id_match = re.search(r'N82E(\d+)', url)
            
            if id_match:
                product_id = 'N82E' + id_match.group(1)
                product_name = f"Newegg Product {product_id}"
                
                # Create a basic product entry without scraping
                product = Product(
                    name=product_name,
                    url=url,
                    image_url=None,
                    current_price=None,
                    target_price=float(target_price) if target_price else None,
                    available=False,
                    discord_webhook_url=discord_webhook if discord_webhook else None,
                    notify_on_price_drop=notify_price,
                    notify_on_availability=notify_availability
                )
                
                # Save to database
                db.session.add(product)
                db.session.commit()
                
                # Queue an asynchronous update task
                flash('Product added. We will attempt to fetch the current price and availability in the background.', 'success')
                return redirect(url_for('main.product_detail', product_id=product.id))
            
            # If we couldn't extract the ID, fall back to normal scraping
        except Exception as e:
            logger.error(f"Error pre-processing Newegg product: {str(e)}")
            # Continue with normal scraping
    
    # Initialize the scraper with error handling
    try:
        scraper = get_scraper(store_type)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('main.add_product'))
    except Exception as e:
        logger.error(f"Error initializing scraper: {str(e)}")
        logger.error(traceback.format_exc())
        flash(f'Error initializing scraper: {str(e)}', 'danger')
        return redirect(url_for('main.add_product'))
    
    # Scrape product data with error handling and timeout
    product_data = None
    
    try:
        # Add a timeout to avoid hanging the request
        import threading
        import queue
        
        # Function to run in a separate thread
        def scrape_with_timeout():
            try:
                result.put(scraper.scrape_product(url))
            except Exception as e:
                logger.error(f"Error in scraper thread: {str(e)}")
                result.put(None)
        
        # Create a queue for the result
        result = queue.Queue()
        
        # Start the scraper in a separate thread
        thread = threading.Thread(target=scrape_with_timeout)
        thread.daemon = True
        thread.start()
        
        # Wait for the result with a timeout (60 seconds)
        thread.join(timeout=60)
        
        # If the thread is still alive after the timeout, consider it a failure
        if thread.is_alive():
            logger.warning(f"Scraper thread timed out for {url}")
            
            # Create a basic product entry
            product = Product(
                name=f"{store_type.capitalize()} Product - {url.split('/')[-1]}",
                url=url,
                image_url=None,
                current_price=None,
                target_price=float(target_price) if target_price else None,
                available=False,
                discord_webhook_url=discord_webhook if discord_webhook else None,
                notify_on_price_drop=notify_price,
                notify_on_availability=notify_availability
            )
            
            # Save to database
            db.session.add(product)
            db.session.commit()
            
            flash('Product added with basic information. We will attempt to update it during the next check cycle.', 'warning')
            return redirect(url_for('main.product_detail', product_id=product.id))
        
        # Get the result from the queue
        product_data = result.get()
        
        if not product_data:
            flash('Failed to retrieve product information. Adding with basic details.', 'warning')
            
            # Create a basic product entry
            product = Product(
                name=f"{store_type.capitalize()} Product - {url.split('/')[-1]}",
                url=url,
                image_url=None,
                current_price=None,
                target_price=float(target_price) if target_price else None,
                available=False,
                discord_webhook_url=discord_webhook if discord_webhook else None,
                notify_on_price_drop=notify_price,
                notify_on_availability=notify_availability
            )
            
            # Save to database
            db.session.add(product)
            db.session.commit()
            
            flash('Product added with basic information. We will attempt to update it during the next check cycle.', 'warning')
            return redirect(url_for('main.product_detail', product_id=product.id))
    except Exception as e:
        logger.error(f"Error in scraper thread management: {str(e)}")
        logger.error(traceback.format_exc())
        flash(f'Error scraping product: {str(e)}', 'danger')
        return redirect(url_for('main.add_product'))
        
    # Create new product
    product = Product(
        name=product_data.get('name', 'Unknown Product'),
        url=url,
        image_url=product_data.get('image_url'),
        current_price=product_data.get('price'),
        target_price=float(target_price) if target_price else None,
        available=product_data.get('available', False),
        discord_webhook_url=discord_webhook if discord_webhook else None,
        notify_on_price_drop=notify_price,
        notify_on_availability=notify_availability
    )
    
    # Add price history
    if product.current_price:
        history = PriceHistory(
            price=product.current_price,
            timestamp=datetime.utcnow()
        )
        product.price_histories.append(history)
        
    # Save to database
    db.session.add(product)
    db.session.commit()
    
    flash('Product added successfully!', 'success')
    return redirect(url_for('main.product_detail', product_id=product.id))

@main_bp.route('/product/<int:product_id>')
def product_detail(product_id):
    """Product detail page."""
    product = Product.query.get_or_404(product_id)
    return render_template('products/detail.html', product=product)

@main_bp.route('/product/<int:product_id>/update')
def update_product(product_id):
    """Manually update a product."""
    product = Product.query.get_or_404(product_id)
    
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
    
    # Get store type from URL domain
    from urllib.parse import urlparse
    domain = urlparse(product.url).netloc.lower()
    store_type = None
    for d, s in domain_to_store.items():
        if d in domain:
            store_type = s
            break
    
    if not store_type:
        flash('Could not determine store type from URL', 'danger')
        return redirect(url_for('main.product_detail', product_id=product.id))
    
    # Initialize the scraper with error handling
    try:
        scraper = get_scraper(store_type)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('main.product_detail', product_id=product.id))
    except Exception as e:
        logger.error(f"Error initializing scraper: {str(e)}")
        logger.error(traceback.format_exc())
        flash(f'Error initializing scraper: {str(e)}', 'danger')
        return redirect(url_for('main.product_detail', product_id=product.id))
    
    # Scrape product data with error handling
    try:
        product_data = scraper.scrape_product(product.url)
        if not product_data:
            flash('Failed to retrieve product information', 'danger')
            return redirect(url_for('main.product_detail', product_id=product.id))
    except Exception as e:
        logger.error(f"Error scraping product from {product.url}: {str(e)}")
        logger.error(traceback.format_exc())
        flash(f'Error scraping product: {str(e)}', 'danger')
        return redirect(url_for('main.product_detail', product_id=product.id))
        
    # Update product with new data
    old_price = product.current_price
    old_availability = product.available
    
    # Update product details
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
    
    # Send notifications if configured
    if product.discord_webhook_url:
        from app.notifications.discord import DiscordNotifier
        
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
    
    # Commit changes
    db.session.commit()
    
    flash('Product updated successfully!', 'success')
    return redirect(url_for('main.product_detail', product_id=product.id))

@main_bp.route('/update-all-products')
def update_all_products():
    """Manually trigger an update of all products."""
    try:
        # Use the scheduler function to check all products
        check_all_products()
        flash('All products updated successfully!', 'success')
    except Exception as e:
        logger.error(f"Error updating all products: {str(e)}")
        flash(f'Error updating products: {str(e)}', 'danger')
        
    return redirect(url_for('main.index'))

@main_bp.route('/product/<int:product_id>/delete', methods=['POST'])
def delete_product(product_id):
    """Delete a product."""
    product = Product.query.get_or_404(product_id)
    
    try:
        db.session.delete(product)
        db.session.commit()
        flash('Product deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting product: {str(e)}', 'danger')
        
    return redirect(url_for('main.index'))

@main_bp.route('/product/<int:product_id>/update-notifications', methods=['POST'])
def update_notification_settings(product_id):
    """Update notification settings for a product."""
    product = Product.query.get_or_404(product_id)
    
    try:
        # Update notification settings
        product.discord_webhook_url = request.form.get('discord_webhook')
        product.notify_on_price_drop = 'notify_price' in request.form
        product.notify_on_availability = 'notify_availability' in request.form
        
        # Save changes
        db.session.commit()
        flash('Notification settings updated successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating notification settings: {str(e)}', 'danger')
        
    return redirect(url_for('main.product_detail', product_id=product.id))

@main_bp.route('/update-check-interval', methods=['POST'])
def update_check_interval():
    """Update the product check interval settings."""
    try:
        # Get values from form
        minutes = request.form.get('check_interval_minutes', '')
        seconds = request.form.get('check_interval_seconds', '')
        
        # Validate input
        if not minutes and not seconds:
            flash('Please provide a valid interval in minutes or seconds', 'danger')
            return redirect(url_for('main.index'))
            
        # Convert to integers with defaults
        minutes = int(minutes) if minutes.isdigit() else 0
        seconds = int(seconds) if seconds.isdigit() else 0
        
        # Ensure at least 10 seconds total interval
        total_seconds = (minutes * 60) + seconds
        if total_seconds < 10:
            flash('Check interval must be at least 10 seconds', 'danger')
            return redirect(url_for('main.index'))
        
        # Update app configuration
        current_app.config['CHECK_INTERVAL_MINUTES'] = minutes
        current_app.config['CHECK_INTERVAL_SECONDS'] = seconds
        
        # Restart the scheduler with new interval
        from app.tasks import init_scheduler
        init_scheduler(current_app)
        
        # Format success message
        if seconds == 0:
            flash(f'Check interval updated to {minutes} minutes', 'success')
        elif minutes == 0:
            flash(f'Check interval updated to {seconds} seconds', 'success')
        else:
            flash(f'Check interval updated to {minutes} minutes and {seconds} seconds', 'success')
            
    except Exception as e:
        logger.error(f"Error updating check interval: {str(e)}")
        flash(f'Error updating check interval: {str(e)}', 'danger')
        
    return redirect(url_for('main.index'))

@main_bp.route('/update-timezone', methods=['POST'])
def update_timezone():
    """Update the preferred timezone setting."""
    try:
        # Get timezone from form
        timezone = request.form.get('timezone', 'UTC')
        
        # Validate timezone (simple check if it exists in pytz)
        if timezone not in pytz.all_timezones:
            flash(f'Invalid timezone: {timezone}', 'danger')
            return redirect(url_for('main.index'))
        
        # Update app configuration
        current_app.config['DEFAULT_TIMEZONE'] = timezone
        
        # Set timezone in session for current user
        session['timezone'] = timezone
        
        flash(f'Timezone updated to {timezone}', 'success')
            
    except Exception as e:
        logger.error(f"Error updating timezone: {str(e)}")
        flash(f'Error updating timezone: {str(e)}', 'danger')
        
    return redirect(url_for('main.index')) 