"""
Send a test alert to the configured Telegram channel.

Usage:
    python test_telegram.py                 # sample price-drop alert
    python test_telegram.py "Hello channel" # custom message

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env / the environment.
"""
import html
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from app.notifications.telegram import TelegramNotifier, get_telegram_settings  # noqa: E402


def main():
    bot_token, chat_id = get_telegram_settings()
    if not bot_token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        return 1

    print(f"Sending test alert to chat {chat_id} ...")
    if len(sys.argv) > 1:
        ok = TelegramNotifier.send_message(html.escape(" ".join(sys.argv[1:])))
    else:
        ok = TelegramNotifier.send_notification(
            product_name="Test Product (Product Tracker)",
            product_url="https://example.com/product",
            tracker_url=os.environ.get("PRODUCT_TRACKER_PUBLIC_URL") or None,
            current_price=79.99,
            old_price=99.99,
        )

    print("Sent!" if ok else "FAILED - check the token, that the bot is a channel admin, and the log output above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
