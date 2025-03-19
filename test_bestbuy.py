from app.scrapers.bestbuy_scraper import BestBuyScraper

# Test URL
url = "https://www.bestbuy.com/site/intel-arc-b580-limited-edition-graphics-card-multi/6613053.p?skuId=6613053"

# Create scraper
scraper = BestBuyScraper()

# Test scraping
print("Testing BestBuy scraper...")
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