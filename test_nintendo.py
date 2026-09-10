"""
Scrape the Zelda 40th Anniversary items from the My Nintendo Store.

    python test_nintendo.py

No browser, no profile, no bot wall - Nintendo serves the whole page to a plain
HTTP request, so this runs anywhere and needs no one-time setup.

What to look for
----------------
Availability comes from isSalableQty, NOT from the "Coming soon" label. All
three items below currently read availability ["Coming soon"] with prePurchase
true while isSalableQty is false: the pre-order is announced but closed. A
scraper keying on the label would report all three as available forever.

The last URL is a control: sku 129088 is a different product whose pre-order IS
open, with the same "Coming soon" label. If the console ever prints
available=True, check 129088 still prints available=True too - if BOTH flipped,
suspect a parse change rather than a real drop.
"""

import sys

from app.scrapers.nintendo_scraper import NintendoScraper

# Product names contain (tm) / en-dashes; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "https://www.nintendo.com/us/store/products/"

# The primary target is the console; the Pro Controller is second.
URLS = [
    ("console", BASE + "nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/"),
    ("controller", BASE + "nintendo-switch-2-pro-controller-display-stand-the-legend-of-zelda-40th-anniversary-edition-127076/"),
    ("case", BASE + "nintendo-switch-2-carrying-case-screen-protector-the-legend-of-zelda-40th-anniversary-edition-127073/"),
    # Control: an OPEN pre-order carrying the same "Coming soon" label.
    # Nintendo Switch 2 + Nintendo Switch Sports Resort bundle.
    ("control (open pre-order)", BASE + "nintendo-switch-2-nintendo-switch-sports-resort-bundle-129088/"),
]

scraper = NintendoScraper()

print("Testing Nintendo scraper...")
for label, url in URLS:
    print(f"\n--- {label} ---")
    print(f"URL: {url}")
    print(f"SKU: {NintendoScraper.extract_sku(url)}")
    try:
        data = scraper.scrape_product(url)
        if data:
            print("Success! Product data:")
            print(f"Name: {data.get('name')}")
            print(f"Price: ${data.get('price')}")
            print(f"Available: {data.get('available')}")
            print(f"Image URL: {data.get('image_url')}")
        else:
            print("No product data (None) - a block page, a moved product, or a bad URL.")
            print("None means 'no answer'; it must never be recorded as out of stock.")
    except Exception as e:
        print(f"Error: {str(e)}")
        print(f"Type: {type(e).__name__}")
