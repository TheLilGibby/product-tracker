"""
Scrape the Zelda 40th Anniversary items from GameStop.

    python test_gamestop.py

THIS OPENS A VISIBLE CHROME WINDOW, and that is not a bug.

Cloudflare decides on more than the User-Agent here. Measured against the
console page on 2026-09-10:

    plain requests, full browser headers      403,     5,019 bytes, "Attention Required!"
    undetected-chromedriver, --headless=new   200,     4,801 bytes, "Attention Required!"
    undetected-chromedriver, visible window   200,   558,727 bytes, the real page

There is no HTTP fallback and headless does not work, so GAMESTOP_HEADLESS
defaults to off. Set GAMESTOP_HEADLESS=1 only to reproduce the block.

The run takes the gamestop_profile lock for its whole session, so it will wait
if a scheduled check is already using that profile.

What to look for
----------------
The case is the useful control: its pre-order is OPEN (button "Pre-Order",
data-available="true") while the console and Pro Controller are closed ("Not
Available", data-available="false", "0 item(s) are available for Pre-Order").
If all three ever print the same thing, suspect a parse change before believing
a drop.

Note the JSON-LD on these pages is NOT trustworthy for availability: it claimed
InStock for the Pro Controller while its button was disabled. Availability comes
from data-available; JSON-LD supplies the name and image only.
"""

import sys

from app.scrapers.gamestop_scraper import GameStopScraper

# Product names contain (tm) / en-dashes; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "https://www.gamestop.com/"

# The primary target is the console; the Pro Controller is second.
URLS = [
    ("console", BASE + "consoles-hardware/nintendo-switch-2/products/"
                "nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition/20037854.html"),
    ("controller", BASE + "gaming-accessories/controllers/nintendo-switch-2/products/"
                   "nintendo-switch-2-pro-controller-the-legend-of-zelda---40th-anniversary-edition/20037855.html"),
    ("case (open pre-order)", BASE + "gaming-accessories/cases-stands/nintendo-switch-2/products/"
                              "the-legend-of-zelda-40th-anniversary-edition-nintendo-switch-2-carrying-case-"
                              "and-screen-protector/451628.html"),
]

scraper = GameStopScraper()

print("Testing GameStop scraper...")
print("A Chrome window will open - Cloudflare blocks headless Chrome on this site.\n")
for label, url in URLS:
    print(f"\n--- {label} ---")
    print(f"URL: {url}")
    print(f"Product id: {GameStopScraper.extract_product_id(url)}")
    try:
        data = scraper.scrape_product(url)
        if data:
            print("Success! Product data:")
            print(f"Name: {data.get('name')}")
            print(f"Price: ${data.get('price')}")
            print(f"Available: {data.get('available')}")
            print(f"Image URL: {data.get('image_url')}")
        else:
            print("No product data (None) - a Cloudflare wall, a busy profile, or a moved product.")
            print("None means 'no answer'; it must never be recorded as out of stock.")
    except Exception as e:
        print(f"Error: {str(e)}")
        print(f"Type: {type(e).__name__}")
