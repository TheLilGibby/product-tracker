# Product Tracker Wiki

## Overview

Product Tracker is a web application that allows you to monitor product prices and availability across multiple online retailers. It automatically checks for price drops and stock updates, notifies you via Discord when your criteria are met, and can even automatically add products to your cart.

## Key Features

### Price Tracking

- **Real-time Price Monitoring**: Track current prices from major retailers
- **Price History**: View historical price data to identify trends
- **Target Price Alerts**: Set target prices and receive notifications when prices drop
- **Availability Notifications**: Get alerted when out-of-stock items become available

### Supported Retailers

- Amazon
- Walmart
- Target
- GameStop
- Best Buy
- Newegg
- Microcenter
- B&H Photo Video
- Adorama

### Notifications

Product Tracker uses Discord webhooks to send notifications when:
- A product's price drops
- A product becomes available after being out of stock
- Auto-cart features successfully add products to your cart

### Automated Cart Features

One of the most powerful features of Product Tracker is the ability to automatically add products to your cart when they meet your criteria:

1. Enable auto-cart on a product's detail page
2. Set your desired quantity
3. When the product becomes available or drops below your target price, the system will automatically add it to your cart

This feature is particularly useful for limited-stock items or flash sales where speed is critical.

### Testing Auto-Cart Functionality

Product Tracker includes a dedicated test environment for auto-cart features:

1. Navigate to the "Test Auto-Cart" page from the main menu
2. Use the provided test URLs or create your own with custom parameters
3. Simulate various scenarios like successful purchases, out-of-stock items, or timeouts
4. View detailed results including cart screenshots

The test environment allows you to:
- Verify auto-cart functionality without interacting with real retailer websites
- Test different quantity settings
- Understand how the system handles various edge cases
- Preview what a successful cart addition looks like

**Test URL Parameters:**
- `success=true|false` - Force success or failure
- `stock=true|false` - Set product availability
- `price=X` - Set product price
- `timeout=true` - Simulate a timeout
- `name=ProductName` - Customize the product name

## How It Works

### Web Scraping

Product Tracker uses web scraping techniques to extract product information directly from retailer websites. For each supported retailer, we implement specialized scrapers that:

1. Extract product names, prices, and availability
2. Capture product images
3. Handle retailer-specific page structures and formats

### Scheduled Checking

The application uses a background scheduler to periodically check all tracked products:

1. By default, checks run every 10 seconds
2. The check interval can be customized in the application settings
3. Each run fetches the latest product data from the retailer websites

### Database Storage

Information is stored in an SQLite database, with key tables for:
- Products (current status, URL, target price, notification settings)
- Price history (records of all price changes over time)

### Browser Automation

For advanced features like auto-cart, Product Tracker uses browser automation with:
- Selenium WebDriver
- Undetected ChromeDriver (to bypass anti-bot measures)

### Docker Deployment

The application runs in a Docker container for:
- Consistent environment across different systems
- Easy deployment and updates
- Isolated execution of web scraping processes

## Getting Started

### Adding Your First Product

1. Click "Add Product" in the navigation menu
2. Enter the product URL from a supported retailer
3. Set a target price (optional)
4. Configure notification preferences
5. Click "Add Product" to begin tracking

### Setting Up Notifications

1. Create a Discord webhook in your Discord server
2. Copy the webhook URL
3. Paste it into the Discord Webhook field for products you want to monitor
4. Enable price drop and/or availability notifications

## Tips and Best Practices

- **Reasonable Check Intervals**: While you can set very short check intervals, most retailers have anti-bot measures that may block frequent requests
- **Target Prices**: Set realistic target prices to avoid missing good deals
- **Auto-Cart Settings**: Use auto-cart judiciously, and make sure to use it only for products you genuinely intend to purchase
- **Webhook Management**: Create separate Discord channels for different types of products to better organize your notifications

## Technical Specifications

- Built with Flask (Python web framework)
- SQLite database for data storage
- APScheduler for background tasks
- Selenium and Undetected ChromeDriver for browser automation
- Discord webhooks for notifications
- Docker for containerization

## Troubleshooting

- **Product Not Tracking**: Some retailers change their page structure frequently; please report any issues
- **Missing Prices**: Occasionally retailers may show prices only after adding items to cart or logging in
- **Auto-Cart Issues**: Auto-cart functionality may be affected by retailer website changes or anti-bot measures 