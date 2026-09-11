"""
MCP server for the Product Tracker.

Exposes the tracker as MCP tools so any MCP client - Claude Code, Claude
Desktop, or a bot bridged to a Telegram/Discord channel - can add and inspect
tracked products. Products added this way are announced to whichever alert
channels the app has configured, so "add it from the channel" round-trips.

It talks to the running Flask app over HTTP rather than importing it, so the
same server works against a local `python run.py` or the Docker container.

Run:
    python mcp_server.py

Environment (read from the process, falling back to the project's .env):
    PRODUCT_TRACKER_URL      base URL of the Flask app (default http://localhost:5000)
    API_TOKEN                sent as X-API-Token; must match the app's API_TOKEN if set
    PRODUCT_TRACKER_TIMEOUT  seconds to wait on scraping calls (default 180)
"""

import os
import sys

import requests
from dotenv import load_dotenv

# Read the same .env the Flask app uses, so API_TOKEN and PRODUCT_TRACKER_URL stay
# in sync without duplicating the secret into .mcp.json (which is committed).
# Real environment variables still win.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=False)

try:
    # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - SDK 1.x fallback
    from mcp.server.fastmcp import FastMCP as MCPServer


BASE_URL = os.environ.get('PRODUCT_TRACKER_URL', 'http://localhost:5000').rstrip('/')
API_TOKEN = os.environ.get('API_TOKEN', '')

# Selenium-backed stores (Newegg, Adorama) can take well over a minute to scrape
SCRAPE_TIMEOUT = float(os.environ.get('PRODUCT_TRACKER_TIMEOUT', '180'))
READ_TIMEOUT = 30.0

mcp = MCPServer(
    name='product-tracker',
    instructions=(
        'Tools for a self-hosted price/stock tracker. add_product starts tracking a '
        'retailer product URL and announces it to the configured Telegram/Discord '
        'channels; list_products and get_product read current state; check_product '
        'forces a re-scrape; send_channel_message posts free text to the channel. '
        'Supported retailers: Amazon, Walmart, Newegg, Best Buy, Microcenter, '
        'B&H, Adorama.'
    ),
)


def _headers():
    return {'X-API-Token': API_TOKEN} if API_TOKEN else {}


def _request(method, path, timeout, **kwargs):
    """
    Call the tracker API and normalize both transport and HTTP errors into a
    dict, so a tool never raises at the client and the agent sees the reason.
    """
    url = f"{BASE_URL}{path}"
    try:
        response = requests.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
    except requests.exceptions.ConnectionError:
        return {
            'ok': False,
            'error': f"Could not reach the tracker at {BASE_URL}. Is the app running?",
        }
    except requests.exceptions.Timeout:
        return {
            'ok': False,
            'error': f"Timed out after {timeout:.0f}s calling {path}. Scraping "
                     f"Newegg/Adorama is slow; raise PRODUCT_TRACKER_TIMEOUT if this persists.",
        }
    except Exception as e:
        return {'ok': False, 'error': f"Request to {path} failed: {e}"}

    try:
        payload = response.json()
    except ValueError:
        return {
            'ok': False,
            'error': f"{response.status_code} from {path} with a non-JSON body: {response.text[:200]}",
        }

    if not response.ok:
        payload = dict(payload) if isinstance(payload, dict) else {'body': payload}
        payload['ok'] = False
        payload.setdefault('error', f"{response.status_code} from {path}")
        payload['status_code'] = response.status_code
        return payload

    payload = dict(payload) if isinstance(payload, dict) else {'result': payload}
    payload['ok'] = True
    return payload


def _price(value):
    return f"${value:.2f}" if isinstance(value, (int, float)) else "unknown"


@mcp.tool()
def add_product(
    url: str,
    target_price: float | None = None,
    store_type: str | None = None,
    name: str | None = None,
    auto_cart: bool = False,
    auto_cart_quantity: int = 1,
    discord_webhook_url: str | None = None,
    notify_on_price_drop: bool = True,
    notify_on_availability: bool = True,
    notify_on_cart: bool = True,
    scrape: bool = True,
    announce: bool = True,
) -> dict:
    """
    Start tracking a retailer product URL.

    Scrapes the product immediately (unless scrape=False) and posts a "Now
    Tracking" announcement to the configured alert channels (unless
    announce=False). Alerts for later price drops and restocks are then sent
    automatically by the tracker's scheduler.

    Args:
        url: Product page URL on a supported retailer.
        target_price: Notify (and auto-cart, if enabled) at or below this price.
        store_type: Override store detection, e.g. 'newegg'. Normally auto-detected.
        name: Override the product name instead of using the scraped one.
        auto_cart: Automatically add to the retailer's cart when it hits the target or restocks.
        auto_cart_quantity: Quantity to auto-cart.
        discord_webhook_url: Per-product Discord webhook, in addition to the global Telegram channel.
        notify_on_price_drop: Send alerts when the price falls.
        notify_on_availability: Send alerts when it comes back in stock.
        notify_on_cart: Send alerts when it is successfully added to the retailer cart.
        scrape: Fetch price/availability right away.
        announce: Post the "Now Tracking" message to the channels.
    """
    body = {
        'url': url,
        'target_price': target_price,
        'store_type': store_type,
        'name': name,
        'auto_cart_enabled': auto_cart,
        'auto_cart_quantity': auto_cart_quantity,
        'discord_webhook_url': discord_webhook_url,
        'notify_on_price_drop': notify_on_price_drop,
        'notify_on_availability': notify_on_availability,
        'notify_on_cart': notify_on_cart,
        'scrape': scrape,
        'announce': announce,
    }
    body = {k: v for k, v in body.items() if v is not None}

    result = _request('POST', '/api/products', SCRAPE_TIMEOUT, json=body)

    if not result.get('ok'):
        if result.get('status_code') == 409 and result.get('product'):
            existing = result['product']
            result['summary'] = (
                f"Already tracking this URL as #{existing['id']} ({existing['name']})."
            )
        else:
            result['summary'] = f"Could not add product: {result.get('error')}"
        return result

    product = result.get('product', {})
    stock = 'in stock' if product.get('available') else 'out of stock'
    summary = (
        f"Tracking #{product.get('id')}: {product.get('name')} "
        f"({product.get('store')}) at {_price(product.get('current_price'))}, {stock}."
    )
    if product.get('target_price') is not None:
        summary += f" Target {_price(product['target_price'])}."
    if not result.get('scraped'):
        summary += f" Initial scrape did not succeed ({result.get('scrape_message')}); the scheduler will retry."

    channels = [name for name, sent in (result.get('announced') or {}).items() if sent]
    summary += f" Announced to: {', '.join(channels)}." if channels else " No channel announcement was sent."

    result['summary'] = summary
    return result


@mcp.tool()
def list_products(available_only: bool = False, limit: int = 50) -> dict:
    """
    List tracked products with their current price and stock state.

    Args:
        available_only: Only return products currently in stock.
        limit: Maximum number of products to return.
    """
    result = _request('GET', '/api/products', READ_TIMEOUT)
    if not result.get('ok'):
        result['summary'] = f"Could not list products: {result.get('error')}"
        return result

    products = result.get('products', [])
    if available_only:
        products = [p for p in products if p.get('available')]
    truncated = len(products) > limit
    products = products[:limit]

    summary = f"{len(products)} product(s)" + (' in stock' if available_only else ' tracked')
    if truncated:
        summary += f" (truncated to {limit})"

    return {
        'ok': True,
        'products': products,
        'count': len(products),
        'truncated': truncated,
        'summary': summary,
    }


@mcp.tool()
def get_product(product_id: int) -> dict:
    """
    Fetch one tracked product, including its full price history.

    Args:
        product_id: The tracker's product id (from list_products).
    """
    result = _request('GET', f'/api/products/{product_id}', READ_TIMEOUT)
    if not result.get('ok'):
        result['summary'] = f"Could not fetch product {product_id}: {result.get('error')}"
        return result

    product = result.get('product', {})
    stock = 'in stock' if product.get('available') else 'out of stock'
    result['summary'] = (
        f"#{product.get('id')} {product.get('name')} - "
        f"{_price(product.get('current_price'))}, {stock}, "
        f"{len(product.get('price_history') or [])} price point(s) recorded."
    )
    return result


@mcp.tool()
def check_product(product_id: int) -> dict:
    """
    Re-scrape one product now instead of waiting for the next scheduled check.

    Records price history and fires price-drop / back-in-stock alerts if the
    product's notification settings warrant them. Slow for Newegg and Adorama,
    which drive a real browser.

    Args:
        product_id: The tracker's product id (from list_products).
    """
    result = _request('POST', f'/api/products/{product_id}/check', SCRAPE_TIMEOUT)
    if not result.get('ok'):
        result['summary'] = f"Check failed for product {product_id}: {result.get('error') or result.get('message')}"
        return result

    product = result.get('product', {})
    stock = 'in stock' if product.get('available') else 'out of stock'
    summary = f"#{product.get('id')} {product.get('name')} is {_price(product.get('current_price'))}, {stock}."
    if result.get('price_changed'):
        summary += f" Price changed from {_price(result.get('old_price'))}."
    if result.get('became_available'):
        summary += " Just came back in stock - alert sent."
    result['summary'] = summary
    return result


@mcp.tool()
def remove_product(product_id: int) -> dict:
    """
    Stop tracking a product and delete its price history. This cannot be undone.

    Args:
        product_id: The tracker's product id (from list_products).
    """
    result = _request('DELETE', f'/api/products/{product_id}', READ_TIMEOUT)
    if not result.get('ok'):
        result['summary'] = f"Could not remove product {product_id}: {result.get('error')}"
        return result
    result['summary'] = f"Stopped tracking #{result.get('id')} ({result.get('name')})."
    return result


@mcp.tool()
def send_channel_message(message: str, image_url: str | None = None) -> dict:
    """
    Post a message to the configured Telegram alert channel.

    Args:
        message: Text to post. Sent as plain text; HTML is escaped.
        image_url: Optional image to attach, with the message as the caption.
    """
    body = {'message': message}
    if image_url:
        body['image_url'] = image_url

    result = _request('POST', '/api/channels/send', READ_TIMEOUT, json=body)
    if not result.get('ok') or not result.get('success'):
        result['summary'] = f"Message not sent: {result.get('error', 'the channel rejected it')}"
        result['ok'] = False
        return result
    result['summary'] = 'Message posted to the Telegram channel.'
    return result


@mcp.tool()
def list_supported_stores() -> dict:
    """List the retailers this tracker can scrape, and confirm the app is reachable."""
    result = _request('GET', '/api/stores', READ_TIMEOUT)
    if not result.get('ok'):
        result['summary'] = f"Tracker unreachable at {BASE_URL}: {result.get('error')}"
        return result
    result['base_url'] = BASE_URL
    result['summary'] = f"Tracker at {BASE_URL} supports: {', '.join(result.get('stores', []))}."
    return result


if __name__ == '__main__':
    print(f"product-tracker MCP server -> {BASE_URL}", file=sys.stderr)
    mcp.run(transport='stdio')
