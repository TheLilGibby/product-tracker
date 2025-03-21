from app import create_app
from app.models.product import Product

app = create_app()
with app.app_context():
    product = Product.query.get(10)
    if product:
        print(f"ID: {product.id}")
        print(f"Name: {product.name}")
        print(f"URL: {product.url}")
        print(f"Auto-Cart Enabled: {product.auto_cart_enabled}")
        print(f"Last Cart Status: {product.last_cart_status}")
    else:
        print("Product ID 10 not found") 