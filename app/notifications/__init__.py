# Import notification classes to make them available
import logging

from app.notifications.discord import DiscordNotifier
from app.notifications.telegram import TelegramNotifier
from app.public_urls import tracker_product_url

logger = logging.getLogger(__name__)


def notify_cart_success(product, cart_url=None):
    """
    Ping Telegram/Discord when a product actually lands in the cart.

    Independent of notify_on_availability: that flag is the "this product
    exists / is in stock" ping. This one only fires after a successful add.
    """
    if not getattr(product, 'notify_on_cart', True):
        return {}
    try:
        return send_product_alert(product, is_auto_cart=True, cart_url=cart_url)
    except Exception as exc:
        logger.error(f"Error sending cart-success alert for product {product.id}: {exc}", exc_info=True)
        return {}


def send_product_alert(product, old_price=None, is_availability_alert=False, is_auto_cart=False, cart_url=None,
                       is_new_tracking=False):
    """
    Fan a product alert out to every configured channel.

    Discord is per-product (the product's webhook URL); Telegram is a global channel
    configured via TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Callers are responsible for
    checking the product's notify_on_* preferences before calling.

    Set is_new_tracking to announce a product that was just added to the tracker
    (used by the JSON API / MCP server).

    Returns:
        Dict of channel name -> success boolean for each channel that was attempted
    """
    results = {}
    alert = dict(
        product_name=product.name,
        product_url=product.url,
        tracker_url=tracker_product_url(product.id),
        current_price=product.current_price,
        old_price=old_price,
        is_availability_alert=is_availability_alert,
        is_auto_cart=is_auto_cart,
        cart_url=cart_url,
        image_url=product.image_url,
    )

    if is_new_tracking:
        alert['is_new_tracking'] = True
        alert['available'] = product.available

    if product.discord_webhook_url:
        results['discord'] = DiscordNotifier.send_notification(product.discord_webhook_url, **alert)

    if TelegramNotifier.is_configured():
        telegram_alert = dict(alert)
        if is_new_tracking:
            telegram_alert['target_price'] = product.target_price
        results['telegram'] = TelegramNotifier.send_notification(**telegram_alert)

    return results
