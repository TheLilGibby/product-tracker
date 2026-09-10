"""
Capture a screenshot of the running dashboard and optionally send it to Telegram.

Usage:
    python test_snapshot.py                                  # save snapshot.png of http://127.0.0.1:5000/
    python test_snapshot.py --send                           # also post it to the Telegram channel
    python test_snapshot.py --url http://localhost:5000/product/1 --out product1.png

Inside Docker (the app must be running):
    docker compose exec app-tracker python test_snapshot.py --send

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / PUBLIC_URL from .env or the environment.
"""
import argparse
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from app.snapshot import capture_dashboard, build_caption, get_dashboard_url  # noqa: E402
from app.notifications.telegram import TelegramNotifier  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='Dashboard snapshot test')
    parser.add_argument('--url', default=None, help=f'Page to capture (default: {get_dashboard_url()})')
    parser.add_argument('--out', default='snapshot.png', help='Where to write the PNG (default: snapshot.png)')
    parser.add_argument('--send', action='store_true', help='Also post the image to the Telegram channel')
    args = parser.parse_args()

    url = args.url or get_dashboard_url()
    print(f"Capturing {url} ...")
    started = time.time()
    png = capture_dashboard(url)
    if not png:
        print("FAILED: no screenshot produced (see app.log). Is the app running at that URL and Chrome installed?")
        return 1

    with open(args.out, 'wb') as fh:
        fh.write(png)
    print(f"Saved {len(png)} bytes to {args.out} in {time.time() - started:.1f}s")

    if not args.send:
        return 0

    if not TelegramNotifier.is_configured():
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); skipping send.")
        return 1

    print("Sending to Telegram ...")
    if TelegramNotifier.send_photo_file(png, build_caption()):
        print("Sent!")
        return 0
    print("FAILED: Telegram rejected the photo (see app.log).")
    return 1


if __name__ == '__main__':
    sys.exit(main())
