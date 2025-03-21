# Newegg HTTP Fallback Solution

## Overview

The HTTP fallback solution provides a robust alternative for product scraping and cart operations when browser automation fails in Docker environments. This document explains how to use, configure, and maintain the solution.

## Components

The solution consists of three key components:

1. **`direct_http_newegg_cart.py`**: A lightweight HTTP-based handler for Newegg product scraping and cart operations.
2. **`newegg_scraper_with_http_fallback.py`**: An enhanced version of the existing Newegg scraper with fallback capabilities.
3. **`newegg_fallback_config.py`**: Configuration settings to control fallback behavior.

## How It Works

The system follows this flow:

1. When a request for a Newegg scraper is made, the enhanced `NeweggScraperWithFallback` is returned.
2. For product scraping or cart operations, the system first attempts to use the traditional WebDriver approach.
3. If WebDriver fails (common in Docker), it automatically falls back to HTTP-based methods.
4. Product information is extracted and cart operations are performed using direct HTTP requests.
5. The database is updated with the results, maintaining full compatibility with the rest of the application.

## Configuration

The behavior of the HTTP fallback can be configured using environment variables or by updating the `newegg_fallback_config.py` file:

| Setting | Environment Variable | Default | Description |
|---------|---------------------|---------|-------------|
| Enable Fallback | N/A | `True` | Whether to enable HTTP fallback |
| Always Use HTTP | `ALWAYS_USE_HTTP_FALLBACK` | `false` | Skip WebDriver and always use HTTP |
| WebDriver Timeout | `WEBDRIVER_TIMEOUT` | `30` | Seconds before falling back to HTTP |
| User Agent | `HTTP_USER_AGENT` | Chrome UA | User agent for HTTP requests |
| Debug Mode | `DEBUG_HTTP_FALLBACK` | `true` | Log detailed information |
| Max Retries | `HTTP_MAX_RETRIES` | `3` | Maximum HTTP request retries |
| Retry Delay | `HTTP_RETRY_DELAY` | `2` | Seconds between retries |
| Debug Path | `HTTP_DEBUG_PATH` | `None` | Path to save HTML responses |

To update configuration at runtime, use the `update_config` function:

```python
from app.scrapers.newegg_fallback_config import update_config

update_config({
    'ALWAYS_USE_HTTP_FALLBACK': True,
    'WEBDRIVER_TIMEOUT': 15
})
```

## Docker Environment Setup

To optimize the HTTP fallback in Docker, add these environment variables to your `docker-compose.yml`:

```yaml
services:
  app-tracker:
    # Existing configuration...
    environment:
      # Existing environment variables...
      - ALWAYS_USE_HTTP_FALLBACK=true
      - WEBDRIVER_TIMEOUT=10
      - HTTP_MAX_RETRIES=5
```

## Maintenance

### Updating Selectors

If Newegg changes their website structure, you may need to update the selectors in `direct_http_newegg_cart.py`. Look for the following methods and update their CSS selectors:

- `_extract_name`
- `_extract_price`
- `_extract_availability`
- `_extract_image_url`
- `add_to_cart`

### Testing

Use the provided test scripts to verify the HTTP fallback is working correctly:

1. `test_fallback.py` - Verifies the basic fallback mechanism
2. `test_cart_fallback.py` - Tests cart operations with fallback
3. `verify_http_fallback.py` - Comprehensive verification of the entire solution

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| Selectors not working | Check if Newegg's website structure has changed and update selectors |
| HTTP requests blocked | Try changing the user agent in configuration |
| Cart operations failing | Check if Newegg's cart API endpoints have changed |
| Slow performance | Increase `WEBDRIVER_TIMEOUT` and `HTTP_MAX_RETRIES` |

### Logs to Check

- Enable debug mode to see detailed logs: `DEBUG_HTTP_FALLBACK=true`
- Look for these log messages to track fallback operation:
  - "Attempting to scrape with WebDriver"
  - "Using HTTP-based fallback scraper"
  - "HTTP fallback scraper extracted"

## Extending the Solution

To extend the HTTP fallback to other stores:

1. Create a similar HTTP handler for the store (e.g., `direct_http_amazon_cart.py`)
2. Create an enhanced scraper with fallback for the store
3. Update the `get_scraper` function in `app/scrapers/__init__.py`
4. Create configuration settings for the new store

## Future Improvements

- Add metrics/telemetry to track fallback usage
- Implement caching to improve performance
- Add more robust error handling and recovery
- Create an admin UI to configure fallback settings 