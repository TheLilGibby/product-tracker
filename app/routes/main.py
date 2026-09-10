from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app, session
from app import db
from app.models.product import Product, PriceHistory
from app.scrapers import get_scraper, add_to_cart
from app.tasks import check_all_products
from datetime import datetime, timedelta
import logging
import re
import urllib.parse
import traceback
import pytz
import os
import time
import random
import functools
import concurrent.futures
from pytz import all_timezones
from sqlalchemy import desc
from werkzeug.utils import secure_filename
import json
import markdown
import uuid
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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
    """Inject timezone and time format data for all templates."""
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
    
    # Get current time format preference
    current_time_format = session.get('time_format', current_app.config.get('TIME_FORMAT', '24h'))
    
    return {
        'timezones': all_timezones,
        'current_timezone': current_timezone,
        'current_time_format': current_time_format
    }

@main_bp.app_context_processor
def utility_functions():
    """Add utility functions to the template context."""
    
    def format_datetime(dt, timezone='UTC', time_format=None):
        """Format datetime in the specified timezone.
        
        Args:
            dt: The datetime object to format
            timezone: The timezone to convert to (default: UTC)
            time_format: Override the time format ('24h' or '12h'), or None to use session/app config
        """
        if dt is None:
            return "N/A"
            
        # Ensure dt has timezone info (assume UTC if naive)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
            
        # Convert to target timezone
        target_tz = pytz.timezone(timezone)
        localized_dt = dt.astimezone(target_tz)
        
        # Use specified format, session preference, or config default (in that order)
        format_pref = time_format or session.get('time_format', current_app.config['TIME_FORMAT'])
        
        # Format datetime based on preference
        if format_pref == '12h':
            return localized_dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')
        else:  # 24h format (default)
            return localized_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
        
    return {'format_datetime': format_datetime}

@main_bp.route('/')
def index():
    """Home page showing all tracked products."""
    # Query all products and order by availability (True first, then False)
    products = Product.query.order_by(Product.available.desc()).all()
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
    
    # Get auto-cart settings
    auto_cart_enabled = 'auto_cart_enabled' in request.form
    auto_cart_quantity = request.form.get('auto_cart_quantity', '1')
    auto_cart_quantity = int(auto_cart_quantity) if auto_cart_quantity.isdigit() and int(auto_cart_quantity) > 0 else 1
    
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
            
            # First try the N82E format
            id_match = re.search(r'N82E(\d+)', url)
            if id_match:
                product_id = 'N82E' + id_match.group(1)
                product_name = f"Newegg Product {product_id}"
            else:
                # Try the /p/XXX-XXX-XXXXX format
                id_match = re.search(r'/p/([A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,5})', url)
                if id_match:
                    product_id = id_match.group(1)
                    product_name = f"Newegg Product {product_id}"
                else:
                    # Fallback to just using the last segment of the URL
                    segments = url.split('/')
                    if len(segments) > 1:
                        product_id = segments[-1] if segments[-1] else segments[-2]
                        product_name = f"Newegg Product {product_id}"
            
            if product_id:
                # Create a basic product entry
                product = Product(
                    name=product_name,
                    url=url,
                    image_url=None,
                    current_price=None,
                    target_price=float(target_price) if target_price else None,
                    available=False,
                    discord_webhook_url=discord_webhook if discord_webhook else None,
                    notify_on_price_drop=notify_price,
                    notify_on_availability=notify_availability,
                    auto_cart_enabled=auto_cart_enabled,
                    auto_cart_quantity=auto_cart_quantity
                )
                
                # Save to database
                db.session.add(product)
                db.session.commit()
                
                # Update the product immediately
                try:
                    # Initialize the Newegg scraper
                    from app.scrapers.newegg_scraper import NeweggScraper
                    scraper = NeweggScraper()
                    
                    # Scrape the product data
                    product_data = scraper.scrape_product(url)
                    
                    if product_data:
                        # Update product details
                        scraped_name = product_data.get('name')
                        if scraped_name and scraped_name != "Unknown Product":
                            product.name = scraped_name
                        product.current_price = product_data.get('price')
                        product.available = product_data.get('available', False)
                        product.image_url = product_data.get('image_url')
                        product.last_checked = datetime.utcnow()
                        
                        # Add price history if we have a price
                        if product.current_price:
                            history = PriceHistory(
                                product_id=product.id,
                                price=product.current_price,
                                timestamp=datetime.utcnow()
                            )
                            db.session.add(history)
                        
                        # Save updates
                        db.session.commit()
                        flash('Product added and updated successfully!', 'success')
                    else:
                        flash('Product added. We will attempt to fetch the current price and availability in the background.', 'success')
                except Exception as e:
                    logger.error(f"Error updating Newegg product: {str(e)}")
                    flash('Product added with basic information. We will attempt to update it later.', 'warning')
                
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
                notify_on_availability=notify_availability,
                auto_cart_enabled=auto_cart_enabled,
                auto_cart_quantity=auto_cart_quantity
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
                notify_on_availability=notify_availability,
                auto_cart_enabled=auto_cart_enabled,
                auto_cart_quantity=auto_cart_quantity
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
        notify_on_availability=notify_availability,
        auto_cart_enabled=auto_cart_enabled,
        auto_cart_quantity=auto_cart_quantity
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
        'test-store.example.com': 'test',
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
    
    # Update product details. Only accept a real name; scrapers return
    # "Unknown Product" when extraction fails
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
            return redirect(url_for('main.settings'))
            
        # Convert to integers with defaults
        minutes = int(minutes) if minutes.isdigit() else 0
        seconds = int(seconds) if seconds.isdigit() else 0
        
        # Ensure at least 10 seconds total interval
        total_seconds = (minutes * 60) + seconds
        if total_seconds < 10:
            flash('Check interval must be at least 10 seconds', 'danger')
            return redirect(url_for('main.settings'))
        
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
        
    return redirect(url_for('main.settings'))

@main_bp.route('/update-timezone', methods=['POST'])
def update_timezone():
    """Update the preferred timezone setting."""
    try:
        # Get timezone from form
        timezone = request.form.get('timezone', 'UTC')
        
        # Validate timezone (simple check if it exists in pytz)
        if timezone not in pytz.all_timezones:
            flash(f'Invalid timezone: {timezone}', 'danger')
            return redirect(url_for('main.settings'))
        
        # Update app configuration
        current_app.config['DEFAULT_TIMEZONE'] = timezone
        
        # Set timezone in session for current user
        session['timezone'] = timezone
        
        flash(f'Timezone updated to {timezone}', 'success')
            
    except Exception as e:
        logger.error(f"Error updating timezone: {str(e)}")
        flash(f'Error updating timezone: {str(e)}', 'danger')
        
    return redirect(url_for('main.settings'))

@main_bp.route('/toggle-time-format', methods=['POST'])
def toggle_time_format():
    """Toggle between 24h and 12h time format."""
    # Get current format or default to 24h
    current_format = session.get('time_format', current_app.config['TIME_FORMAT'])
    
    # Toggle the format
    new_format = '12h' if current_format == '24h' else '24h'
    
    # Save to session
    session['time_format'] = new_format
    
    # Inform the user
    format_name = '12-hour (AM/PM)' if new_format == '12h' else '24-hour (military)'
    flash(f'Time format updated to {format_name}', 'success')
    
    # Redirect to settings page
    return redirect(url_for('main.settings'))

@main_bp.route('/product/<int:product_id>/add-to-cart', methods=['POST'])
def add_product_to_cart(product_id):
    """Add a product to the shopping cart on its retailer site."""
    product = Product.query.get_or_404(product_id)
    
    try:
        # Get quantity from form
        quantity = request.form.get('quantity', '1')
        quantity = int(quantity) if quantity.isdigit() and int(quantity) > 0 else 1
        
        # Get store type from URL
        from urllib.parse import urlparse
        domain = urlparse(product.url).netloc.lower()
        
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
            'test-store.example.com': 'test',
        }
        
        store_type = None
        for d, s in domain_to_store.items():
            if d in domain:
                store_type = s
                break
        
        if not store_type:
            flash('Could not determine store type from URL', 'danger')
            return redirect(url_for('main.product_detail', product_id=product.id))
        
        # Add to cart using the appropriate scraper
        result = add_to_cart(store_type, product.url, quantity)
        
        # Update product with cart attempt results
        product.last_cart_attempt = datetime.utcnow()
        product.last_cart_status = result.get('message', 'Unknown status')
        db.session.commit()
        
        if result.get('success'):
            # Update the cart count in the session
            update_cart_count()
            
            flash(f"Product added to cart successfully! {result.get('message', '')}", 'success')
            
            # Redirect to cart URL if available
            cart_url = result.get('cart_url')
            if cart_url:
                return redirect(cart_url)
            
            # If no cart URL, redirect to the cart successes page
            return redirect(url_for('main.cart_successes'))
        else:
            flash(f"Failed to add product to cart: {result.get('message', 'Unknown error')}", 'danger')
        
        # If we don't have a cart URL or the operation failed, redirect back to product detail
        return redirect(url_for('main.product_detail', product_id=product.id, cart_result=json.dumps(result)))
        
    except Exception as e:
        logger.error(f"Error adding product to cart: {str(e)}", exc_info=True)
        flash(f'Error adding product to cart: {str(e)}', 'danger')
        return redirect(url_for('main.product_detail', product_id=product.id))

@main_bp.route('/product/<int:product_id>/update-auto-cart', methods=['POST'])
def update_auto_cart_settings(product_id):
    """Update auto cart settings for a product."""
    product = Product.query.get_or_404(product_id)
    
    try:
        # Update auto cart settings
        product.auto_cart_enabled = 'auto_cart_enabled' in request.form
        
        # Get auto cart quantity
        quantity = request.form.get('auto_cart_quantity', '1')
        if quantity.isdigit() and int(quantity) > 0:
            product.auto_cart_quantity = int(quantity)
        else:
            product.auto_cart_quantity = 1
        
        # Save changes
        db.session.commit()
        
        flash('Auto cart settings updated successfully', 'success')
    except Exception as e:
        flash(f'Error updating auto cart settings: {str(e)}', 'danger')
    
    return redirect(url_for('main.product_detail', product_id=product.id))

@main_bp.route('/cart-successes')
def cart_successes():
    """Display all successful auto-cart operations with links to complete purchases."""
    # Get products that have been successfully added to cart in the last 24 hours
    past_24h = datetime.utcnow() - timedelta(hours=24)
    
    # Find all products with a last_cart_attempt within 24 hours that were successful
    # We determine success by checking if "success" is in the last_cart_status
    products_in_cart = Product.query.filter(
        Product.last_cart_attempt >= past_24h,
        Product.last_cart_status.like('%success%')
    ).order_by(Product.last_cart_attempt.desc()).all()
    
    # Update the cart count in the session
    update_cart_count()
    
    return render_template('cart_successes.html', products=products_in_cart)

@main_bp.route('/api/cart-count')
def get_cart_count():
    """API endpoint to get the current cart success count."""
    count = update_cart_count()
    return jsonify({'count': count})

@main_bp.route('/wiki')
def wiki():
    """Display the wiki page with markdown content."""
    try:
        with open(os.path.join(current_app.static_folder, 'markdown', 'wiki.md'), 'r') as f:
            content = f.read()
        html_content = markdown.markdown(content)
        return render_template('wiki.html', html_content=html_content)
    except Exception as e:
        logging.error(f"Error loading wiki: {str(e)}")
        return render_template('wiki.html', html_content="<p>Error loading wiki content.</p>")

@main_bp.route('/settings')
def settings():
    """Display settings page"""
    import pytz
    timezones = pytz.all_timezones
    return render_template('settings.html', timezones=timezones, os=os)

@main_bp.route('/update-newegg-cookies', methods=['POST'])
def update_newegg_cookies():
    """Update Newegg cookies for auto-cart functionality"""
    if request.method == 'POST':
        cookies = request.form.get('newegg_cookies', '')
        
        try:
            # Save the cookies to environment variable for the current process
            os.environ['NEWEGG_COOKIES'] = cookies
            
            # Write to .env file for persistence across restarts
            env_path = os.path.join(os.getcwd(), '.env')
            
            # Read existing .env file or create new one
            env_lines = []
            if os.path.exists(env_path):
                with open(env_path, 'r') as f:
                    env_lines = f.readlines()
            
            # Update or add NEWEGG_COOKIES line
            cookie_line_found = False
            for i, line in enumerate(env_lines):
                if line.startswith('NEWEGG_COOKIES='):
                    env_lines[i] = f'NEWEGG_COOKIES={cookies}\n'
                    cookie_line_found = True
                    break
            
            if not cookie_line_found:
                env_lines.append(f'NEWEGG_COOKIES={cookies}\n')
            
            # Write back to .env file
            with open(env_path, 'w') as f:
                f.writelines(env_lines)
            
            flash('Newegg cookies updated successfully.', 'success')
        except Exception as e:
            flash(f'Error updating Newegg cookies: {str(e)}', 'error')
        
        return redirect(url_for('main.settings'))

@main_bp.route('/auto-cart-testing')
def auto_cart_testing():
    """Display auto-cart testing documentation page"""
    # Load Markdown file
    markdown_path = os.path.join(current_app.static_folder, 'markdown', 'auto_cart_testing.md')
    
    with open(markdown_path, 'r') as f:
        md_content = f.read()
    
    # Convert Markdown to HTML
    import markdown
    html_content = markdown.markdown(md_content)
    
    return render_template('wiki.html', html_content=html_content, title="Auto-Cart Testing")

@main_bp.route('/api/products')
def get_products_json():
    """API endpoint to fetch the latest product data in JSON format."""
    try:
        # Query all products and order by availability (True first, then False)
        products = Product.query.order_by(Product.available.desc()).all()
        
        # Convert to list of dictionaries
        products_data = []
        for product in products:
            # Format the last_checked time according to user's preferred timezone and format
            timezone = session.get('timezone', current_app.config.get('DEFAULT_TIMEZONE', 'UTC'))
            time_format = session.get('time_format', current_app.config.get('TIME_FORMAT', '24h'))
            
            # Ensure dt has timezone info (assume UTC if naive)
            last_checked = product.last_checked
            if last_checked.tzinfo is None:
                last_checked = pytz.utc.localize(last_checked)
                
            # Convert to target timezone
            target_tz = pytz.timezone(timezone)
            localized_dt = last_checked.astimezone(target_tz)
            
            # Format datetime based on preference
            if time_format == '12h':
                formatted_time = localized_dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')
            else:  # 24h format (default)
                formatted_time = localized_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
            
            # Build the product data dictionary
            product_data = {
                'id': product.id,
                'name': product.name,
                'image_url': product.image_url,
                'current_price': product.current_price,
                'target_price': product.target_price,
                'available': product.available,
                'last_checked': formatted_time
            }
            products_data.append(product_data)
        
        return jsonify({'products': products_data})
    except Exception as e:
        logger.error(f"Error fetching products JSON: {str(e)}")
        return jsonify({'error': str(e)}), 500

@main_bp.route('/test-auto-cart')
def test_auto_cart():
    """Display the test auto-cart page."""
    return render_template('test_auto_cart.html')

@main_bp.route('/add-test-product', methods=['POST'])
def add_test_product():
    """Add a test product for auto-cart testing."""
    product_name = request.form.get('product_name', 'Test Product')
    
    # Generate a test URL with parameters encoding the test scenario
    scenario = request.form.get('scenario', 'success')
    price = request.form.get('price', '')
    if not price:
        price = round(random.uniform(99.99, 599.99), 2)
    
    # Use test-store.example.com as the domain to ensure it can be identified as 'test' type
    # This aligns with how the application determines store type from domain
    test_url = f"https://test-store.example.com/product/{uuid.uuid4()}?"
    params = {
        'scenario': scenario,
        'price': price,
        'name': product_name
    }
    
    # Construct URL with parameters
    parsed = urlparse(test_url)
    query = parse_qs(parsed.query)
    query.update(params)
    parsed = parsed._replace(query=urlencode(query, doseq=True))
    product_url = urlunparse(parsed)
    
    # Get auto-cart settings
    auto_cart_enabled = 'auto_cart_enabled' in request.form
    auto_cart_quantity = request.form.get('auto_cart_quantity', 1, type=int)
    target_price = request.form.get('target_price', None, type=float)
    
    # Get notification settings
    notify_price = 'notify_price' in request.form
    notify_availability = 'notify_availability' in request.form
    discord_webhook = request.form.get('discord_webhook', '')
    
    # Create the product in the database using the correct field names
    product = Product(
        name=product_name,
        url=product_url,
        current_price=float(price) if price else None,
        target_price=target_price,
        auto_cart_enabled=auto_cart_enabled,
        auto_cart_quantity=auto_cart_quantity,
        notify_on_price_drop=notify_price,
        notify_on_availability=notify_availability,
        created_at=datetime.now()
    )
    
    if discord_webhook:
        product.discord_webhook_url = discord_webhook
    
    db.session.add(product)
    db.session.commit()
    
    flash(f'Test product "{product_name}" added for scenario: {scenario}', 'success')
    return redirect(url_for('main.index'))

@main_bp.route('/test-cart', methods=['GET', 'POST'])
def test_cart():
    """Test the auto-cart functionality with the test scraper."""
    test_result = None
    
    if request.method == 'POST':
        test_url = request.form.get('test_url')
        quantity = int(request.form.get('quantity', 1))
        
        if not test_url:
            flash('Please provide a test URL', 'warning')
            return redirect(url_for('main.test_cart'))
        
        # Use the test scraper to attempt adding to cart
        try:
            from app.scrapers import add_to_cart
            result = add_to_cart('test', test_url, quantity)
            test_result = result
            
            if result.get('success'):
                flash('Test product was successfully added to cart!', 'success')
            else:
                flash(f'Failed to add test product to cart: {result.get("message")}', 'warning')
                
        except Exception as e:
            logger.error(f"Error testing auto-cart functionality: {str(e)}", exc_info=True)
            flash(f'Error: {str(e)}', 'danger')
    
    return render_template('test_cart.html', test_result=test_result)

# Add this before the cart_successes route
def update_cart_count():
    """Update the cart count in the session based on successful cart additions in the last 24 hours."""
    past_24h = datetime.utcnow() - timedelta(hours=24)
    
    # Find all products with a last_cart_attempt within 24 hours that were successful
    count = Product.query.filter(
        Product.last_cart_attempt >= past_24h,
        Product.last_cart_status.like('%success%')
    ).count()
    
    # Store the count in the session
    session['cart_count'] = count
    return count 