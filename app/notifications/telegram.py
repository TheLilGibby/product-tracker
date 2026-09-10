import html
import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

# Telegram caps photo captions at 1024 characters; longer alerts fall back to a text message
TELEGRAM_CAPTION_LIMIT = 1024


def get_telegram_settings():
    """
    Return (bot_token, chat_id) for the global alert channel.

    Reads Flask config when an app context is active (routes, scheduler jobs),
    otherwise falls back to environment variables (CLI scripts).
    """
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            return (
                current_app.config.get('TELEGRAM_BOT_TOKEN') or None,
                current_app.config.get('TELEGRAM_CHAT_ID') or None,
            )
    except RuntimeError:
        pass
    return (
        os.environ.get('TELEGRAM_BOT_TOKEN') or None,
        os.environ.get('TELEGRAM_CHAT_ID') or None,
    )


class TelegramNotifier:
    """Send notifications to a Telegram channel (or group/DM) via the Bot API"""

    @staticmethod
    def is_configured():
        """True when both a bot token and a chat id are available"""
        bot_token, chat_id = get_telegram_settings()
        return bool(bot_token and chat_id)

    @staticmethod
    def send_message(text, image_url=None, bot_token=None, chat_id=None):
        """
        Send an HTML-formatted message to the Telegram channel.

        Args:
            text: Message body. Telegram HTML parse mode - escape user content with html.escape()
            image_url: Optional image; sent via sendPhoto with the text as caption
            bot_token: Override the configured bot token
            chat_id: Override the configured chat id (e.g. -1001234567890 or @channelusername)

        Returns:
            Boolean indicating success or failure
        """
        default_token, default_chat = get_telegram_settings()
        bot_token = bot_token or default_token
        chat_id = chat_id or default_chat

        if not bot_token or not chat_id:
            logger.debug("Telegram not configured; skipping notification")
            return False

        base_url = f"{TELEGRAM_API_BASE}/bot{bot_token}"

        try:
            if image_url and len(text) <= TELEGRAM_CAPTION_LIMIT:
                response = requests.post(
                    f"{base_url}/sendPhoto",
                    json={
                        "chat_id": chat_id,
                        "photo": image_url,
                        "caption": text,
                        "parse_mode": "HTML",
                    },
                    timeout=15,
                )
                if response.ok and response.json().get("ok"):
                    return True
                # Retailer image URLs are often hotlink-protected; fall back to plain text
                logger.warning(
                    f"Telegram sendPhoto failed ({response.status_code}): {response.text[:200]}; falling back to text"
                )

            response = requests.post(
                f"{base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if response.ok and response.json().get("ok"):
                return True

            logger.error(f"Telegram sendMessage failed ({response.status_code}): {response.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"Error sending Telegram notification: {e}")
            return False

    @staticmethod
    def send_notification(product_name, product_url, current_price, old_price=None,
                          is_availability_alert=False, is_auto_cart=False, cart_url=None, image_url=None,
                          bot_token=None, chat_id=None):
        """
        Send a product alert to Telegram. Mirrors DiscordNotifier.send_notification.

        Args:
            product_name: Name of the product
            product_url: URL to the product
            current_price: Current price of the product
            old_price: Previous price (for price drop alerts)
            is_availability_alert: Whether this is an availability alert
            is_auto_cart: Whether this is an auto-cart notification
            cart_url: URL to the cart (for auto-cart notifications)
            image_url: URL to product image
            bot_token / chat_id: Optional overrides for the configured channel

        Returns:
            Boolean indicating success or failure
        """
        name = html.escape(product_name or "")

        if is_auto_cart:
            title = "🛒 <b>Auto-Cart Success!</b>"
        elif is_availability_alert:
            title = "🔔 <b>Product Available!</b>"
        else:
            title = "🔔 <b>Price Drop Alert!</b>"

        lines = [title, f"<b>{name}</b>", ""]

        if is_auto_cart:
            if current_price is not None:
                lines.append(f"Current Price: ${current_price:.2f}")
            lines.append("Status: Product has been automatically added to cart!")
            if cart_url:
                lines.append(f'<a href="{html.escape(cart_url, quote=True)}">View cart</a>')
        elif is_availability_alert:
            lines.append("Status: Product is now in stock!")
            if current_price is not None:
                lines.append(f"Current Price: ${current_price:.2f}")
        else:
            # Price drop alert
            lines.append(f"Current Price: ${current_price:.2f}")
            if old_price:
                lines.append(f"Previous Price: ${old_price:.2f}")
                savings = old_price - current_price
                percent_savings = (savings / old_price) * 100
                lines.append(f"You Save: ${savings:.2f} ({percent_savings:.1f}%)")

        lines += ["", f'<a href="{html.escape(product_url, quote=True)}">View product</a>']

        return TelegramNotifier.send_message(
            "\n".join(lines), image_url=image_url, bot_token=bot_token, chat_id=chat_id
        )
