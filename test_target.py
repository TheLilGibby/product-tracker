import sys

from app.scrapers.target_scraper import TargetScraper

# Product names contain ™ / –; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Test URLs (Nintendo Switch 2 Zelda 40th Anniversary drop items)
urls = [
    "https://www.target.com/p/nintendo-8482-switch-2-the-legend-of-zelda-40th-anniversary-edition-console-system/-/A-1013322047",
    "https://www.target.com/p/nintendo-8482-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition/-/A-1013213521",
]

# Create scraper
scraper = TargetScraper()

# Test scraping
print("Testing Target scraper...")
for url in urls:
    print(f"\nURL: {url}")
    print(f"TCIN: {TargetScraper.extract_tcin(url)}")
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
