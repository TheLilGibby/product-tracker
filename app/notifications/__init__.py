# Import notification classes to make them available
from app.notifications.discord import DiscordNotifier
from app.notifications.telegram import TelegramNotifier


def send_product_alert(product, old_price=None, is_availability_alert=False, is_auto_cart=False, cart_url=None):
    """
    Fan a product alert out to every configured channel.

    Discord is per-product (the product's webhook URL); Telegram is a global channel
    configured via TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Callers are responsible for
    checking the product's notify_on_* preferences before calling.

    Returns:
        Dict of channel name -> success boolean for each channel that was attempted
    """
    results = {}
    alert = dict(
        product_name=product.name,
        product_url=product.url,
        current_price=product.current_price,
        old_price=old_price,
        is_availability_alert=is_availability_alert,
        is_auto_cart=is_auto_cart,
        cart_url=cart_url,
        image_url=product.image_url,
    )

    if product.discord_webhook_url:
        results['discord'] = DiscordNotifier.send_notification(product.discord_webhook_url, **alert)

    if TelegramNotifier.is_configured():
        results['telegram'] = TelegramNotifier.send_notification(**alert)

    return results
