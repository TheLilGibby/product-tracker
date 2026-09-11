"""
JSON API for the product tracker.

This is the surface the MCP server (mcp_server.py) drives, so that agents and
chat bots can add and inspect tracked products without going through the HTML
forms. Everything here speaks JSON and never redirects or flashes.

Auth: if API_TOKEN is set in the environment, every mutating endpoint requires a
matching X-API-Token header. When it is unset the API is open, which matches the
rest of the app's posture and keeps local/Docker use frictionless.
"""

import functools
import html
import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request

from app import db
from app.models.product import Product
from app.notifications import send_product_alert
from app.notifications.telegram import TelegramNotifier
from app.scrapers import detect_store_type, supported_stores
from app.scrapers.bestbuy_scraper import BestBuyScraper
from app.tasks import refresh_product

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')


def require_token(view):
    """Enforce X-API-Token when API_TOKEN is configured; no-op when it isn't."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        expected = current_app.config.get('API_TOKEN')
        if expected and request.headers.get('X-API-Token') != expected:
            return jsonify({'error': 'Invalid or missing X-API-Token'}), 401
        return view(*args, **kwargs)
    return wrapped


def product_to_dict(product, include_history=False):
    """Serialize a Product for API responses."""
    data = {
        'id': product.id,
        'name': product.name,
        'url': product.url,
        'store': product.store,
        'store_label': product.store_label,
        'image_url': product.image_url or (
            BestBuyScraper.image_url_from_url(product.url)
            if hasattr(BestBuyScraper, 'image_url_from_url')
            and 'bestbuy.com' in (product.url or '').lower() else None
        ),
        'current_price': product.current_price,
        'target_price': product.target_price,
        'available': product.available,
        'last_checked': product.last_checked.isoformat() + 'Z' if product.last_checked else None,
        'created_at': product.created_at.isoformat() + 'Z' if product.created_at else None,
        'notify_on_price_drop': product.notify_on_price_drop,
        'notify_on_availability': product.notify_on_availability,
        'notify_on_cart': product.notify_on_cart,
        'discord_webhook_configured': bool(product.discord_webhook_url),
        'auto_cart_enabled': product.auto_cart_enabled,
        'tracking_enabled': product.tracking_enabled,
        'auto_cart_quantity': product.auto_cart_quantity,
        'last_cart_status': product.last_cart_status,
    }
    if include_history:
        data['price_history'] = [
            {'price': h.price, 'timestamp': h.timestamp.isoformat() + 'Z'}
            for h in product.price_histories
        ]
        # One entry per logged stock change, plus the listing's first
        # observation, oldest first, with what it cost at that moment.
        data['availability_history'] = [
            {'available': h.available, 'price': h.price, 'timestamp': h.timestamp.isoformat() + 'Z'}
            for h in sorted(product.availability_histories, key=lambda h: (h.timestamp, h.id))
        ]
    return data


def name_from_url(url, store_type=None):
    """
    Build a readable placeholder name from a product URL.

    Used when a product is added before (or instead of) a successful scrape, so
    the row is never nameless.
    """
    if store_type == 'newegg':
        match = re.search(r'N82E\d+', url)
        if match:
            return f"Newegg Product {match.group(0)}"
        match = re.search(r'/p/([A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,5})', url)
        if match:
            return f"Newegg Product {match.group(1)}"

    path = urlparse(url).path.rstrip('/')
    parts = [p for p in path.split('/') if p and p != '-']
    slug = parts[-1] if parts else ''
    # Target (and similar) put the item id last: /p/{name}/-/A-123456
    if re.fullmatch(r'A-\d+', slug, re.IGNORECASE) and len(parts) >= 2:
        slug = parts[-2]
    slug = re.sub(r'\.(html?|aspx?|php)$', '', slug, flags=re.IGNORECASE)
    # GameStop (and similar) put a numeric SKU last: /products/{name}/{id}.html
    if re.fullmatch(r'\d+', slug) and len(parts) >= 2:
        slug = re.sub(r'\.(html?|aspx?|php)$', '', parts[-2], flags=re.IGNORECASE)
    slug = re.sub(r'[-_+]+', ' ', slug).strip()
    slug = re.sub(r'\b8482\b', '', slug).strip()

    if not slug:
        return f"{(store_type or 'Tracked').title()} Product"

    # Long retailer slugs are noisy in a channel message; keep the leading words
    words = slug.split()[:12]
    pretty = ' '.join(w.capitalize() if w.islower() else w for w in words)
    return pretty[:200]


def parse_bool(value, default=False):
    """Accept JSON booleans as well as the usual string spellings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


@api_bp.route('/stores', methods=['GET'])
def list_stores():
    """List the store keys this tracker can scrape."""
    return jsonify({'stores': supported_stores()})


@api_bp.route('/products', methods=['POST'])
@require_token
def create_product():
    """
    Start tracking a product.

    Body (JSON):
        url                     (required) product URL
        store_type              optional; auto-detected from the URL when omitted
        name                    optional; replaced by the scraped name when scrape succeeds
        target_price            optional float
        discord_webhook_url     optional per-product Discord webhook
        notify_on_price_drop    default true
        notify_on_availability  default true
        notify_on_cart          default true
        auto_cart_enabled       default false
        auto_cart_quantity      default 1
        scrape                  default true  - fetch price/availability immediately
        announce                default true  - post a "Now Tracking" alert to the channels
    """
    data = request.get_json(silent=True) or request.form or {}

    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'url is required'}), 400

    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return jsonify({'error': 'url must be an absolute http(s) URL'}), 400

    store_type = (data.get('store_type') or '').strip() or detect_store_type(url)
    if not store_type:
        return jsonify({
            'error': f"Could not determine the store for {url}",
            'supported_stores': supported_stores(),
        }), 400
    if store_type not in supported_stores():
        return jsonify({
            'error': f"Unsupported store type: {store_type}",
            'supported_stores': supported_stores(),
        }), 400

    # store_type naming a real store is not enough: an explicit store_type
    # skips detect_store_type entirely, so any http(s) URL would be accepted
    # and then fetched server-side by the scheduler on the next check. The
    # hostname has to actually belong to the store being claimed.
    detected = detect_store_type(url)
    if detected != store_type:
        return jsonify({
            'error': (
                f"URL does not belong to {store_type}"
                if detected is None else
                f"URL looks like {detected}, not {store_type}"
            ),
            'supported_stores': supported_stores(),
        }), 400

    existing = Product.query.filter_by(url=url).first()
    if existing:
        return jsonify({
            'error': 'This product is already being tracked',
            'product': product_to_dict(existing),
        }), 409

    target_price = data.get('target_price')
    try:
        target_price = float(target_price) if target_price not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'error': 'target_price must be a number'}), 400

    quantity = data.get('auto_cart_quantity', 1)
    try:
        quantity = max(1, int(quantity))
    except (TypeError, ValueError):
        quantity = 1

    product = Product(
        name=(data.get('name') or '').strip() or name_from_url(url, store_type),
        url=url,
        target_price=target_price,
        available=False,
        discord_webhook_url=(data.get('discord_webhook_url') or '').strip() or None,
        notify_on_price_drop=parse_bool(data.get('notify_on_price_drop'), True),
        notify_on_availability=parse_bool(data.get('notify_on_availability'), True),
        notify_on_cart=parse_bool(data.get('notify_on_cart'), True),
        auto_cart_enabled=parse_bool(data.get('auto_cart_enabled'), False),
        auto_cart_quantity=quantity,
        created_at=datetime.utcnow(),
    )

    try:
        db.session.add(product)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating product for {url}: {e}", exc_info=True)
        return jsonify({'error': f"Could not save product: {e}"}), 500

    logger.info(f"Product {product.id} added via API: {url}")

    # Initial scrape. A failure here is not fatal - the product stays tracked and
    # the scheduler will retry on its next pass.
    scrape_result = None
    if parse_bool(data.get('scrape'), True):
        scrape_result = refresh_product(product)
        if not scrape_result['success']:
            logger.warning(f"Initial scrape failed for product {product.id}: {scrape_result['message']}")

    announced = {}
    if parse_bool(data.get('announce'), True):
        try:
            announced = send_product_alert(product, is_new_tracking=True)
        except Exception as e:
            logger.error(f"Error announcing new product {product.id}: {e}", exc_info=True)

    return jsonify({
        'product': product_to_dict(product),
        'scraped': scrape_result['success'] if scrape_result else False,
        'scrape_message': scrape_result['message'] if scrape_result else 'Initial scrape skipped',
        'announced': announced,
    }), 201


@api_bp.route('/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Fetch one tracked product, including its price history."""
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': f'No product with id {product_id}'}), 404
    return jsonify({'product': product_to_dict(product, include_history=True)})


@api_bp.route('/products/<int:product_id>', methods=['DELETE'])
@require_token
def delete_product(product_id):
    """Stop tracking a product (also removes its price history)."""
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': f'No product with id {product_id}'}), 404

    name = product.name
    try:
        db.session.delete(product)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting product {product_id}: {e}", exc_info=True)
        return jsonify({'error': f"Could not delete product: {e}"}), 500

    return jsonify({'deleted': True, 'id': product_id, 'name': name})


@api_bp.route('/products/<int:product_id>/check', methods=['POST'])
@require_token
def check_product(product_id):
    """
    Re-scrape one product right now.

    Runs the same path as a scheduled check, so price history is recorded and
    price-drop / back-in-stock alerts fire if warranted.
    """
    product = Product.query.get(product_id)
    if not product:
        return jsonify({'error': f'No product with id {product_id}'}), 404

    result = refresh_product(product)
    response = {'product': product_to_dict(product)}
    response.update(result)
    return jsonify(response), (200 if result['success'] else 502)


@api_bp.route('/channels/send', methods=['POST'])
@require_token
def send_channel_message():
    """
    Post an arbitrary message to the Telegram alert channel.

    Body: {"message": "text", "image_url": "https://..." (optional)}
    """
    data = request.get_json(silent=True) or request.form or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'success': False, 'error': 'message is required'}), 400

    if not TelegramNotifier.is_configured():
        return jsonify({'success': False, 'error': 'Telegram is not configured'}), 503

    success = TelegramNotifier.send_message(html.escape(message), image_url=data.get('image_url'))
    return jsonify({'success': success}), (200 if success else 502)
