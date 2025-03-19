# Bot Protection Bypass Documentation

This document provides a comprehensive overview of the techniques implemented in our product tracker to bypass bot protection mechanisms on e-commerce sites, with a particular focus on Newegg.

## Table of Contents

1. [Introduction](#introduction)
2. [Challenges](#challenges)
3. [Solution Architecture](#solution-architecture)
4. [Implementation Details](#implementation-details)
5. [2captcha Integration](#2captcha-integration)
6. [Browser Fingerprinting](#browser-fingerprinting)
7. [Human Behavior Simulation](#human-behavior-simulation)
8. [Proxy Support](#proxy-support)
9. [Troubleshooting](#troubleshooting)
10. [Future Enhancements](#future-enhancements)

## Introduction

Modern e-commerce websites employ increasingly sophisticated bot protection mechanisms to prevent automated scraping of their content. Our product tracker needs to bypass these protections to monitor product prices and availability. This document outlines the approach we've taken to overcome these challenges.

## Challenges

The main challenges we face include:

- CAPTCHA challenges (Google reCAPTCHA v2/v3, hCaptcha, Arkose Labs)
- Browser fingerprinting detection
- Behavior analysis (detecting non-human browsing patterns)
- IP-based blocking
- JavaScript challenges

## Solution Architecture

Our solution employs a multi-layered approach:

1. **Undetected Chrome Driver**: Using a specialized Selenium wrapper that helps evade detection
2. **CAPTCHA Solving**: Integration with 2captcha API to solve various CAPTCHA challenges
3. **Human Behavior Simulation**: Mimicking human-like mouse movements, scrolling, and timing
4. **Browser Fingerprinting Protection**: Modifying browser properties to appear more human-like
5. **Proxy Support**: Optional IP rotation to prevent blocking

## Implementation Details

### Undetected ChromeDriver

We use the `undetected_chromedriver` library which:

- Patches the standard ChromeDriver to avoid detection
- Removes automation flags and indicators
- Uses a persistent browser profile to maintain cookies and session data

```python
import undetected_chromedriver as uc

options = self._get_chrome_options()
driver = uc.Chrome(options=options)
```

### Persistent Profile

We maintain a persistent Chrome profile to preserve cookies, cache, and browsing history, which makes the browser appear more legitimate:

```python
self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "newegg_profile")
os.makedirs(self.profile_dir, exist_ok=True)

# In chrome options:
options.add_argument(f'--user-data-dir={self.profile_dir}')
options.add_argument('--profile-directory=Default')
```

## 2captcha Integration

We use the 2captcha service to solve CAPTCHA challenges automatically.

### Setup

```python
from app.captcha import TwoCaptcha

# Initialize with your API key
self.solver = TwoCaptcha('YOUR_API_KEY')
```

### CAPTCHA Type Detection

The scraper can detect multiple types of CAPTCHAs and handle them appropriately:

```python
def detect_captcha_type(self, driver):
    """Detect the type of CAPTCHA present on the page"""
    page_source = driver.page_source.lower()
    current_url = driver.current_url.lower()
    
    if "recaptcha" in page_source and "google.com/recaptcha" in page_source:
        if "invisible" in page_source or "v3" in page_source:
            return "recaptcha_v3"
        else:
            return "recaptcha_v2"
    elif "hcaptcha" in page_source:
        return "hcaptcha"
    elif "arkoselabs" in page_source or "funcaptcha" in page_source:
        return "arkose"
    elif "areyouahuman" in current_url or "human" in current_url:
        return "image_captcha"
    
    return None
```

### CAPTCHA Solving Methods

We've implemented handlers for multiple CAPTCHA types:

1. **reCAPTCHA v2**
2. **reCAPTCHA v3**
3. **hCaptcha**
4. **Arkose Labs / FunCaptcha**
5. **Basic image CAPTCHAs**

Each CAPTCHA type has its own solving method that:
1. Extracts the necessary data (site key, etc.)
2. Sends the challenge to 2captcha
3. Waits for the solution
4. Injects the solution into the page
5. Submits the form or triggers necessary callbacks

## Browser Fingerprinting

To avoid detection based on browser fingerprinting, we implement:

### JavaScript Injection

We inject custom JavaScript to override browser properties that might reveal automation:

```python
def inject_stealth_js(self, driver):
    """Inject stealth JS to mask automation fingerprints"""
    # Override navigator properties
    js_script = """
    // Override the navigator properties
    Object.defineProperty(navigator, 'webdriver', {
        get: () => false
    });
    
    // ... additional property overrides ...
    
    // Override permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => {
        if (parameters.name === 'notifications') {
            return Promise.resolve({state: Notification.permission});
        }
        return originalQuery(parameters);
    };
    """
    
    driver.execute_script(js_script)
```

### Chrome Options

We configure Chrome with options that make it harder to detect:

```python
def _get_chrome_options(self):
    """Configure Chrome options to avoid detection"""
    options = webdriver.ChromeOptions()
    
    # Add arguments to avoid detection
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-browser-side-navigation")
    options.add_argument("--disable-gpu")
    
    # Add realistic window size
    options.add_argument(f"--window-size={random.randint(1050, 1200)},{random.randint(800, 900)}")
    
    # ... more options ...
    
    return options
```

## Human Behavior Simulation

We simulate human-like behavior to avoid behavior-based detection:

```python
def simulate_human_behavior(self, driver):
    """Simulates human-like behavior to avoid detection"""
    
    # Random wait time
    time.sleep(random.uniform(1, 3))
    
    # Get viewport size
    viewport_width = driver.execute_script("return window.innerWidth")
    viewport_height = driver.execute_script("return window.innerHeight")
    
    # Perform random mouse movements
    actions = webdriver.ActionChains(driver)
    for _ in range(random.randint(3, 7)):
        x = random.randint(0, viewport_width)
        y = random.randint(0, viewport_height)
        actions.move_by_offset(x, y)
        actions.pause(random.uniform(0.1, 0.5))
    actions.perform()
    
    # Random scrolling
    scroll_amount = random.randint(300, 700)
    driver.execute_script(f"window.scrollBy(0, {scroll_amount})")
    time.sleep(random.uniform(0.5, 1.5))
    
    # Scroll back up partially
    if random.random() > 0.5:
        up_amount = random.randint(100, scroll_amount)
        driver.execute_script(f"window.scrollBy(0, -{up_amount})")
        time.sleep(random.uniform(0.3, 0.7))
```

## Proxy Support

For sites that use IP-based blocking, we've added proxy support:

```python
def get_proxies(self):
    """Get a list of proxies to use"""
    proxy_api_url = os.environ.get('PROXY_API_URL')
    if not proxy_api_url:
        logger.warning("No proxy API URL provided, not using proxies")
        return None
        
    try:
        response = requests.get(proxy_api_url)
        response.raise_for_status()
        proxies = response.json()
        logger.info(f"Retrieved {len(proxies)} proxies")
        return proxies
    except Exception as e:
        logger.error(f"Error retrieving proxies: {str(e)}")
        return None
```

## Troubleshooting

Common issues and solutions:

### CAPTCHA Solving Fails

- Ensure your 2captcha API key is valid and has sufficient balance
- Check if the correct CAPTCHA type is being detected
- Verify that the site key is correctly extracted from the page

### Bot Protection Still Detected

1. Check browser logs for fingerprinting-related detection
2. Try using a proxy if IP is being blocked
3. Enhance human behavior simulation
4. Use a pre-warmed browser profile with legitimate browsing history

## Future Enhancements

Potential improvements to the system:

1. **Rotating User Agents**: Regularly change the browser user agent
2. **Machine Learning**: Train models to solve CAPTCHAs locally
3. **Enhanced Fingerprinting Protection**: More sophisticated browser property spoofing
4. **Distributed Scraping**: Use multiple machines with different profiles
5. **Browser Automation Alternatives**: Explore using Playwright or Puppeteer

## References

- [2captcha API Documentation](https://2captcha.com/2captcha-api)
- [Undetected ChromeDriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)
- [Selenium Documentation](https://selenium-python.readthedocs.io/) 