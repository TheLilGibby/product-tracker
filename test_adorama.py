import logging
import sys
from app.scrapers.adorama_scraper import AdoramaScraper

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

def test_adorama_scraper():
    # Test URL from the user's request
    url = "https://www.adorama.com/ifjx1006.html"
    
    print(f"Testing Adorama scraper with URL: {url}")
    scraper = AdoramaScraper()
    
    # Test product scraping
    print("\nScraping product information...")
    product_info = scraper.scrape_product(url)
    
    if product_info:
        print("\nProduct Information:")
        print(f"Name: {product_info.get('name', 'N/A')}")
        print(f"Price: ${product_info.get('price', 'N/A')}")
        print(f"Available: {product_info.get('available', 'N/A')}")
        print(f"Image URL: {product_info.get('image_url', 'N/A')}")
    else:
        print("Failed to scrape product information")
    
    # Uncomment to test add to cart feature
    # print("\nTesting add to cart...")
    # cart_result = scraper.add_to_cart(quantity=1)
    # print(f"Add to cart success: {cart_result.get('success', False)}")
    # print(f"Message: {cart_result.get('message', 'N/A')}")
    # print(f"Cart URL: {cart_result.get('cart_url', 'N/A')}")
    # if cart_result.get('screenshot'):
    #     print("Screenshot was captured")

if __name__ == "__main__":
    test_adorama_scraper() 