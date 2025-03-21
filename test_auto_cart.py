from app import create_app
from app.models.product import Product
from app.scrapers import add_to_cart
from urllib.parse import urlparse
from datetime import datetime

app = create_app()
with app.app_context():
    # Get product ID 10
    product = Product.query.get(10)
    if product:
        print(f"Testing auto-cart for product: {product.name}")
        print(f"URL: {product.url}")
        print(f"Auto-Cart Enabled: {product.auto_cart_enabled}")
        print(f"Current Status: {product.last_cart_status}")
        
        # Determine store type from URL
        domain = urlparse(product.url).netloc.lower()
        
        # Map domains to store types
        domain_to_store = {
            'amazon.com': 'amazon',
            'www.amazon.com': 'amazon',
            'walmart.com': 'walmart',
            'www.walmart.com': 'walmart',
            'newegg.com': 'newegg',
            'www.newegg.com': 'newegg',
            'microcenter.com': 'microcenter',
            'www.microcenter.com': 'microcenter',
            'bestbuy.com': 'bestbuy',
            'www.bestbuy.com': 'bestbuy',
            'bhphotovideo.com': 'bh',
            'www.bhphotovideo.com': 'bh',
            'test-store.example.com': 'test',
        }
        
        store_type = None
        for d, s in domain_to_store.items():
            if d in domain:
                store_type = s
                break
        
        if not store_type:
            print(f"Could not determine store type for URL: {product.url}")
        else:
            # Try to add to cart
            print(f"Attempting to add product to cart using {store_type} scraper...")
            
            # Get quantity from product settings
            quantity = product.auto_cart_quantity or 1
            
            try:
                result = add_to_cart(store_type, product.url, quantity)
                
                # Print the result
                print("\nAuto-Cart Result:")
                print(f"Success: {result.get('success', False)}")
                print(f"Message: {result.get('message', 'No message')}")
                print(f"Cart URL: {result.get('cart_url', 'N/A')}")
                print(f"Screenshot Available: {'Yes' if result.get('screenshot') else 'No'}")
                
                # Update product with results
                product.last_cart_attempt = datetime.now()
                product.last_cart_status = result.get('message', 'Unknown status')
                from app import db
                db.session.commit()
                
                print("\nProduct updated with cart attempt results")
            except Exception as e:
                print(f"Error: {str(e)}")
    else:
        print("Product ID 10 not found") 