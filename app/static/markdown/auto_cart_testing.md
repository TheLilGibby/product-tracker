# Testing Auto-Cart Functionality

The TRACKER_ application includes a special testing mode for the auto-cart feature, which allows you to simulate various scenarios without having to interact with real retailer websites.

## Test Page Overview

The Test Auto-Cart page provides a form where you can create test products with different attributes and behaviors:

- Create products that are in stock or out of stock
- Simulate different price scenarios
- Test products with or without prices
- Simulate checkout failures and timeouts

## How to Use the Test Feature

1. Navigate to the **Test Auto-Cart** page from the main navigation menu
2. Fill out the test product form:
   - Set a product name
   - Choose a price (or leave blank for a random price)
   - Select a test scenario
   - Configure your target price (optional)
   - Set auto-cart and notification preferences
3. Submit the form to add the test product to your tracking list
4. Use the main product list to interact with your test product:
   - Click "Update" to refresh the product data
   - Click "Add to Cart" to manually trigger the cart functionality
   - Wait for automatic cart attempts if you've enabled auto-cart

## Available Test Scenarios

### Success (Default)
- Product is available and in stock
- Has a valid price
- Can be added to cart with 80% success rate

### Out of Stock
- Product exists but is currently unavailable
- Price is visible
- Cannot be added to cart

### No Price
- Product is available
- Price is not visible or not set
- Cannot be added to cart due to missing price

### Failure
- Product exists and is available
- Has a valid price
- Will always fail when trying to add to cart

### Timeout
- Simulates slow responses
- Operations will take longer than usual
- Eventually fails with a timeout error

## Testing Auto-Cart Logic

To test your auto-cart criteria:

1. Create a test product with the "Success" scenario
2. Set a target price slightly higher than the product's price
3. Enable auto-cart
4. The system should automatically attempt to add the product to cart

## Example Use Cases

- **Testing Price Drops**: Create a product, then update its URL parameter to change the price below your target
- **Testing Availability Changes**: Create an "Out of Stock" product, then change it to "Success" to simulate an item coming back in stock
- **Testing Error Handling**: Create products with different error scenarios to ensure your notifications work correctly

## Notes

- Test products use a special "test" store type that is handled by the test_scraper
- Test products create simulated screenshots showing the result of cart operations
- All test products use placeholder images
- The auto-cart success rate (80%) simulates real-world variability in checkout processes 