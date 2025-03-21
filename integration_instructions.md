# HTTP Fallback Integration Instructions

This guide explains how to integrate the HTTP-based fallback solution for auto-cart functionality into the main application.

## Background

The regular Selenium/ChromeDriver-based approach for scraping product information and auto-cart functionality can sometimes fail in Docker environments due to issues with running Chrome in headless mode. The HTTP fallback solution provides a lightweight alternative that uses direct HTTP requests instead of browser automation.

## Files Added

1. `app/scrapers/direct_http_newegg_cart.py` - HTTP-based implementation for Newegg product scraping and cart addition
2. `app/scrapers/newegg_scraper_with_http_fallback.py` - Enhanced Newegg scraper that falls back to HTTP when WebDriver fails

## Integration Steps

### 1. Modify the scrapers module initialization

Modify `app/scrapers/__init__.py` to use the enhanced Newegg scraper:

```python
# Add this import
from app.scrapers.newegg_scraper_with_http_fallback import NeweggScraperWithFallback

def get_scraper(store_type):
    """
    Get the appropriate scraper based on store type.
    
    Args:
        store_type: String identifier for the store ('amazon', 'walmart', etc.)
        
    Returns:
        An instance of the appropriate scraper class
    
    Raises:
        ValueError: If store_type is not supported
    """
    logger.debug(f"Creating scraper for store type: {store_type}")
    
    scrapers = {
        'amazon': AmazonScraper,
        'walmart': WalmartScraper,
        # Use the enhanced Newegg scraper with HTTP fallback
        'newegg': NeweggScraperWithFallback,  # Changed this line
        'microcenter': MicrocenterScraper,
        'bestbuy': BestBuyScraper,
        'bh': BHScraper,
        'test': TestScraper,
    }
    
    # Rest of the function remains the same
    if store_type not in scrapers:
        logger.error(f"Unsupported store type: {store_type}")
        raise ValueError(f"Unsupported store type: {store_type}")
    
    scraper_class = scrapers[store_type]
    logger.debug(f"Using {scraper_class.__name__} for {store_type}")
    return scraper_class()
```

### 2. Add logging configuration for the new modules

Make sure appropriate logging is configured for the new modules. Add the following to your logging configuration:

```python
# In app/__init__.py or wherever you set up logging
# Set up logging for HTTP fallback
logging.getLogger('app.scrapers.newegg_http').setLevel(logging.DEBUG)
logging.getLogger('app.scrapers.newegg_with_fallback').setLevel(logging.DEBUG)
```

### 3. Update Docker dependencies

Make sure the Docker container has the necessary Python packages. Add the following to requirements.txt if they're not already included:

```
requests>=2.25.0
beautifulsoup4>=4.9.0
```

## Testing the Integration

After making these changes, you can test the integration by:

1. Restarting the Docker container
2. Monitoring the logs for HTTP fallback messages
3. Trying to add a product to cart from Newegg

If the WebDriver approach fails, you should see log messages about falling back to HTTP, and the product should still be added to the cart successfully.

## Limitations

The HTTP fallback approach has some limitations compared to the full WebDriver approach:

1. No screenshots of the cart are provided (the screenshot field in the result will be None)
2. Some advanced cart features may not be available (e.g., adding warranty protection)
3. It may be more susceptible to website structure changes

However, it provides a reliable fallback for basic auto-cart functionality when the WebDriver approach fails.

## Extending to Other Stores

This pattern can be extended to other stores as well. To do so:

1. Create an HTTP-based implementation for the store (similar to `direct_http_newegg_cart.py`)
2. Create an enhanced scraper with fallback (similar to `newegg_scraper_with_http_fallback.py`)
3. Update the `get_scraper` function to use the enhanced scraper for that store

## Troubleshooting

If you encounter issues with the HTTP fallback:

1. Check the logs for specific error messages
2. Verify that the website structure hasn't changed significantly
3. Update the CSS selectors in the HTTP-based implementation if necessary
4. Ensure proper network connectivity from the Docker container 