import sys

from app.scrapers.gamestop_scraper import GameStopScraper

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

urls = [
    "https://www.gamestop.com/consoles-hardware/nintendo-switch-2/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition/451607.html",
]

scraper = GameStopScraper()

print("Testing GameStop scraper...")
for url in urls:
    print(f"\nURL: {url}")
    print(f"PID: {GameStopScraper.extract_pid(url)}")
    try:
        data = scraper.scrape_product(url)
        if data:
            print("Success! Product data:")
            print(f"Name: {data.get('name')}")
            print(f"Price: ${data.get('price')}")
            print(f"Available: {data.get('available')}")
            print(f"Image URL: {data.get('image_url')}")
        else:
            print("Failed to get product data.")
    except Exception as e:
        print(f"Error: {str(e)}")
        print(f"Type: {type(e).__name__}")
