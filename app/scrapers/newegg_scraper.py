import re
import logging
import time
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup
from app.scrapers.common import CHROME_VERSION, DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page, is_preorder_text, detect_chrome_major
import platform
import os
import random
import json
import shutil
from app.captcha import TwoCaptcha
import requests
from flask import current_app

# Set up logging
logger = logging.getLogger('app.scrapers.newegg')

class NeweggScraper:
    """Scraper specifically for Newegg products using undetected-chromedriver"""
    
    def __init__(self):
        """Initialize the Newegg scraper with undetected-chromedriver."""
        logger.debug("Initializing NeweggScraper with undetected-chromedriver")
        
        # Set up persistent profile directory
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "newegg_profile")
        os.makedirs(self.profile_dir, exist_ok=True)
        
        # Initialize 2captcha solver with the API key from environment variables
        api_key = os.environ.get('TWOCAPTCHA_API_KEY')
        if not api_key:
            logger.warning("No 2captcha API key found in environment variables, fallback to default")
            api_key = '34c6d7576786cffd62e7483403ff6fd1'  # Fallback for backward compatibility
            
        self.solver = TwoCaptcha(api_key)
        logger.info("2captcha solver initialized with API key")
        
        # Get proxy list (you'll need to set up your proxy provider)
        self.proxies = self.get_proxies()
        
        # Store the most recent product URL for use in add_to_cart
        self.current_product_url = None
    
    def get_proxies(self):
        """Get a list of proxies from your provider"""
        try:
            # Replace with your proxy provider's API
            proxy_url = os.getenv('PROXY_API_URL')
            if not proxy_url:
                logger.warning("No proxy API URL configured")
                return []
                
            response = requests.get(proxy_url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                # Format depends on your proxy provider
                return response.json()
            else:
                logger.error(f"Failed to get proxies: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Error getting proxies: {str(e)}")
            return []
    
    def _get_chrome_options(self):
        """Get fresh ChromeOptions (to avoid reuse error)"""
        # Configure Chrome options
        options = uc.ChromeOptions()
        
        # Use persistent profile
        options.add_argument(f'--user-data-dir={self.profile_dir}')
        options.add_argument('--profile-directory=Default')
        
        # Add more realistic browser parameters
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        options.add_argument('--start-maximized')
        options.add_argument('--disable-gpu')
        options.add_argument('--enable-javascript')
        
        # Add WebGL and canvas support
        options.add_argument('--use-gl=desktop')
        options.add_argument('--enable-webgl')
        options.add_argument('--canvas-msaa-sample-count=2')
        options.add_argument('--ignore-certificate-errors')
        
        # Add privacy settings
        options.add_argument('--disable-web-security')
        options.add_argument('--allow-running-insecure-content')
        
        # Random viewport size from common resolutions
        viewports = [
            (1920, 1080),
            (1366, 768),
            (1536, 864),
            (1440, 900),
            (1280, 720)
        ]
        viewport = random.choice(viewports)
        options.add_argument(f'--window-size={viewport[0]},{viewport[1]}')
        
        # Random user agent from recent Chrome versions
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ]
        self.user_agent = random.choice(user_agents)
        options.add_argument(f'--user-agent={self.user_agent}')
        
        # Add touch events support
        options.add_argument('--touch-events=enabled')
        
        # Add fonts for better fingerprinting
        options.add_argument('--font-render-hinting=medium')
        
        return options

    def detect_captcha_type(self, driver):
        """Detect the type of CAPTCHA present on the page"""
        try:
            # Check if we're on the areyouahuman page
            current_url = driver.current_url
            if "areyouahuman" in current_url:
                # This is directly identifiable as Newegg's bot protection
                logger.info("Detected Newegg's areyouahuman protection page")
                
                # Check page content for specific CAPTCHA signatures
                page_source = driver.page_source.lower()
                
                # Check for Arkose Labs / FunCaptcha
                if any(term in page_source for term in ["arkoselabs", "funcaptcha", "arkose", "challenge"]):
                    return "arkose"
                
                # Check for iframe which might contain a CAPTCHA
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                if iframes:
                    # Try to switch to each iframe to check for CAPTCHA elements
                    original_window = driver.current_window_handle
                    for iframe in iframes:
                        try:
                            driver.switch_to.frame(iframe)
                            
                            # Check for Arkose Labs
                            if "arkose" in driver.page_source.lower() or "funcaptcha" in driver.page_source.lower():
                                driver.switch_to.window(original_window)
                                return "arkose"
                                
                            # Switch back to main content
                            driver.switch_to.window(original_window)
                        except:
                            # If we can't switch to the iframe, continue to the next one
                            driver.switch_to.window(original_window)
                            continue
                
                # If we're on the areyouahuman page, assume it's an Arkose challenge
                # even if we can't detect the specific type
                return "arkose"
            
            # If not on areyouahuman, check for other CAPTCHA types
            # Check for reCAPTCHA v2
            if driver.find_elements(By.CSS_SELECTOR, ".g-recaptcha"):
                return "recaptcha_v2"
            
            # Check for reCAPTCHA v3
            if driver.find_elements(By.CSS_SELECTOR, ".grecaptcha-badge"):
                return "recaptcha_v3"
            
            # Check for hCaptcha
            if driver.find_elements(By.CSS_SELECTOR, ".h-captcha"):
                return "hcaptcha"
            
            # Check for basic image CAPTCHA
            if driver.find_elements(By.CSS_SELECTOR, "#captcha-box img"):
                return "image"
            
            return None
            
        except Exception as e:
            logger.error(f"Error detecting CAPTCHA type: {str(e)}")
            return None

    def solve_captcha(self, driver):
        """Solve CAPTCHA using appropriate method based on type"""
        try:
            captcha_type = self.detect_captcha_type(driver)
            if not captcha_type:
                logger.info("No CAPTCHA detected")
                return True
                
            if not self.solver:
                logger.error("No 2captcha API key configured")
                return False
            
            logger.info(f"Detected CAPTCHA type: {captcha_type}")
            
            if captcha_type == "recaptcha_v2":
                return self.solve_recaptcha_v2(driver)
            elif captcha_type == "recaptcha_v3":
                return self.solve_recaptcha_v3(driver)
            elif captcha_type == "arkose":
                return self.solve_arkose(driver)
            elif captcha_type == "hcaptcha":
                return self.solve_hcaptcha(driver)
            elif captcha_type == "image":
                return self.solve_image_captcha(driver)
            
            logger.error(f"Unsupported CAPTCHA type: {captcha_type}")
            return False
            
        except Exception as e:
            logger.error(f"Error solving CAPTCHA: {str(e)}")
            return False

    def solve_recaptcha_v2(self, driver):
        """Solve reCAPTCHA v2"""
        try:
            # Get the site key
            site_key = driver.find_element(By.CSS_SELECTOR, ".g-recaptcha").get_attribute("data-sitekey")
            if not site_key:
                logger.error("Could not find reCAPTCHA site key")
                return False
            
            # Get the page URL
            page_url = driver.current_url
            
            # Send to 2captcha
            result = self.solver.recaptcha(
                sitekey=site_key,
                url=page_url,
                version="v2",
                enterprise=0
            )
            
            if not result or not result.get("code"):
                logger.error("Failed to solve reCAPTCHA")
                return False
            
            # Input the solution
            driver.execute_script(
                f'document.getElementById("g-recaptcha-response").innerHTML = "{result["code"]}";'
            )
            
            # Submit the form
            submit_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            submit_button.click()
            
            time.sleep(3)
            return True
            
        except Exception as e:
            logger.error(f"Error solving reCAPTCHA v2: {str(e)}")
            return False

    def solve_recaptcha_v3(self, driver):
        """Solve reCAPTCHA v3"""
        try:
            # Look for reCAPTCHA v3 in the page source
            page_source = driver.page_source
            
            # Try to find the site key in the reCAPTCHA script tag
            import re
            site_key_match = re.search(r'google\.com/recaptcha/api\.js\?render=([A-Za-z0-9_\-]+)', page_source)
            
            if site_key_match:
                site_key = site_key_match.group(1)
            else:
                # If we can't find it directly, try to look for it in other ways
                site_key = "6LdAn3UUAAAAAKt8pKdAdZf83OwfA2QhtacSvywE"  # Default Newegg reCAPTCHA key
            
            logger.info(f"Found reCAPTCHA v3 site key: {site_key}")
            
            # Get the page URL
            page_url = driver.current_url
            
            # Send to 2captcha
            logger.info(f"Sending reCAPTCHA v3 task to 2captcha: {site_key} on {page_url}")
            result = self.solver.recaptcha(
                sitekey=site_key,
                url=page_url,
                version="v3",
                action="verify"  # Common action in reCAPTCHA v3
            )
            
            if not result or not result.get("code"):
                logger.error("Failed to solve reCAPTCHA v3")
                return False
            
            logger.info(f"Got reCAPTCHA v3 solution from 2captcha: {result.get('code')[:30]}...")
            
            # Try to inject the token via JavaScript
            try:
                # Method 1: Set the g-recaptcha-response textarea
                driver.execute_script(
                    f'document.getElementById("g-recaptcha-response").innerHTML = "{result["code"]}";'
                )
                
                # Method 2: If there's a grecaptcha object, use it to set the response
                driver.execute_script(f'''
                    try {{
                        if (typeof grecaptcha !== 'undefined') {{
                            grecaptcha.enterprise.execute = function() {{
                                return Promise.resolve("{result["code"]}");
                            }};
                        }}
                    }} catch (e) {{
                        console.error("Error setting up grecaptcha mock:", e);
                    }}
                ''')
                
                # Try to find and submit any form
                driver.execute_script('''
                    try {
                        const forms = document.querySelectorAll('form');
                        if (forms.length > 0) {
                            forms[0].submit();
                        }
                    } catch (e) {
                        console.error("Error submitting form:", e);
                    }
                ''')
                
                logger.info("Injected reCAPTCHA v3 solution and attempted form submission")
                time.sleep(3)
                return True
                
            except Exception as e:
                logger.error(f"Error submitting reCAPTCHA v3 solution: {str(e)}")
                return False
            
        except Exception as e:
            logger.error(f"Error solving reCAPTCHA v3: {str(e)}")
            return False

    def solve_arkose(self, driver):
        """Solve Arkose Labs CAPTCHA"""
        try:
            import re  # Import re at the function level to fix the scope issue
            logger.info("Attempting to solve Arkose Labs CAPTCHA")
            
            # Finding the publickey is tricky for Arkose
            # Newegg usually uses a standard publickey for their Arkose implementation
            # Default to a common Newegg publickey if not found
            public_key = None
            
            # Try to find publickey in the URL
            if "public_key" in driver.current_url:
                match = re.search(r'public_key=([^&]+)', driver.current_url)
                if match:
                    public_key = match.group(1)
                    logger.info(f"Found Arkose public_key in URL: {public_key}")
            
            # Try to find in page scripts - looking specifically for reCAPTCHA keys
            if not public_key:
                page_source = driver.page_source
                # Check for reCAPTCHA script
                recaptcha_match = re.search(r'google\.com/recaptcha/api\.js\?render=([A-Za-z0-9_\-]+)', page_source)
                if recaptcha_match:
                    public_key = recaptcha_match.group(1)
                    logger.info(f"Found reCAPTCHA public_key in script: {public_key}")
                
            # Try to find in page scripts - look for standard arkose format
            if not public_key:
                for script in driver.find_elements(By.TAG_NAME, "script"):
                    script_content = script.get_attribute("innerHTML") or ""
                    if "ark.pk" in script_content or "arkose" in script_content:
                        # Try to extract the publickey
                        match = re.search(r'["\']pk["\']\s*:\s*["\']([A-F0-9-]+)["\']', script_content)
                        if match:
                            public_key = match.group(1)
                            logger.info(f"Found Arkose public_key in script: {public_key}")
                            break
            
            # If we still don't have a publickey, check for data-pkey attribute in any element
            if not public_key:
                for elem in driver.find_elements(By.CSS_SELECTOR, "[data-pkey]"):
                    public_key = elem.get_attribute("data-pkey")
                    if public_key:
                        logger.info(f"Found Arkose public_key in data-pkey attribute: {public_key}")
                        break
            
            # If we still don't have the key, use the correct Newegg reCAPTCHA key
            if not public_key:
                # This is the reCAPTCHA site key found in Newegg's page source
                public_key = "6LdAn3UUAAAAAKt8pKdAdZf83OwfA2QhtacSvywE"
                logger.info(f"Using Newegg's reCAPTCHA public_key: {public_key}")
            
            # Get the page URL
            page_url = driver.current_url
            
            logger.info(f"Submitting CAPTCHA to 2captcha: {public_key} on {page_url}")
            
            # Determine if it's reCAPTCHA or FunCaptcha based on the key format
            if re.match(r'^[0-9A-Za-z_\-]{40}$', public_key):
                # This is likely a reCAPTCHA key
                logger.info("Detected reCAPTCHA format, using reCAPTCHA solver")
                result = self.solver.recaptcha(
                    sitekey=public_key,
                    url=page_url,
                    version="v3",
                    action="verify"
                )
            else:
                # Assume it's FunCaptcha/Arkose
                logger.info("Using FunCaptcha/Arkose solver")
                result = self.solver.funcaptcha(
                    sitekey=public_key,
                    url=page_url,
                    data={
                        "type": "funcaptcha",
                        "api_server": "https://client-api.arkoselabs.com",
                        "user_agent": self.user_agent
                    }
                )
            
            if not result or not result.get("code"):
                logger.error("Failed to solve CAPTCHA")
                return False
            
            logger.info(f"Got CAPTCHA solution from 2captcha: {result.get('code')[:30]}...")
            
            # Try multiple submission methods since the right one can vary
            success = False
            
            # Method 1: Try reCAPTCHA submission
            try:
                driver.execute_script(
                    f'document.getElementById("g-recaptcha-response").innerHTML = "{result["code"]}";'
                )
                
                # Submit any form found
                driver.execute_script("document.querySelector('form').submit();")
                logger.info("Submitted reCAPTCHA solution via g-recaptcha-response")
                time.sleep(3)
                success = True
            except Exception as e1:
                logger.info(f"reCAPTCHA submission method failed: {str(e1)}")
            
            # If first method fails, try alternative submission methods
            if not success:
                try:
                    # Method 2: Try finding and clicking submit buttons
                    submit_buttons = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], .btn-primary")
                    for button in submit_buttons:
                        if button.is_displayed() and button.is_enabled():
                            logger.info("Clicking submit button after solving CAPTCHA")
                            button.click()
                            time.sleep(3)
                            success = True
                            break
                except Exception as e2:
                    logger.info(f"Button click method failed: {str(e2)}")
            
            # Method 3: Try to find clickable elements that might bypass protection
            if not success:
                try:
                    clickable_elements = driver.find_elements(By.CSS_SELECTOR, ".btn, button, a[href='#']")
                    for elem in clickable_elements:
                        try:
                            if elem.is_displayed() and elem.is_enabled():
                                elem_text = elem.text.lower()
                                if any(keyword in elem_text for keyword in ["continue", "verify", "submit", "proceed"]):
                                    logger.info(f"Clicking element with text: {elem_text}")
                                    elem.click()
                                    time.sleep(3)
                                    success = True
                                    break
                        except:
                            continue
                except Exception as e3:
                    logger.info(f"Clickable elements method failed: {str(e3)}")
            
            # Method 4: Try injecting solution via postMessage as a last resort
            if not success:
                try:
                    driver.execute_script(
                        f'parent.postMessage(JSON.stringify({{"token": "{result["code"]}"}}), "*");'
                    )
                    logger.info("Sent CAPTCHA solution via postMessage")
                    time.sleep(3)
                    success = True
                except Exception as e4:
                    logger.error(f"postMessage method failed: {str(e4)}")
            
            return success
        
        except Exception as e:
            logger.error(f"Error solving CAPTCHA: {str(e)}")
            return False

    def solve_image_captcha(self, driver):
        """Solve basic image CAPTCHA"""
        try:
            # Find CAPTCHA image
            img_element = driver.find_element(By.CSS_SELECTOR, "#captcha-box img")
            if not img_element:
                logger.error("Could not find CAPTCHA image")
                return False
            
            # Get image URL
            img_url = img_element.get_attribute("src")
            
            # Send to 2captcha
            result = self.solver.normal(img_url)
            
            if not result or not result.get("code"):
                logger.error("Failed to solve image CAPTCHA")
                return False
            
            # Input the solution
            input_field = driver.find_element(By.ID, "captcha-input")
            submit_button = driver.find_element(By.ID, "captcha-submit")
            
            input_field.send_keys(result["code"])
            time.sleep(random.uniform(0.5, 1.5))
            submit_button.click()
            
            time.sleep(3)
            return True
            
        except Exception as e:
            logger.error(f"Error solving image CAPTCHA: {str(e)}")
            return False
    
    def handle_bot_protection(self, driver):
        """Handle bot protection page"""
        try:
            # Check if we're on the bot protection page
            if "areyouahuman" in driver.current_url or "human" in driver.page_source.lower():
                logger.info("Detected bot protection page, attempting to solve...")
                
                # Try to solve CAPTCHA if present
                if self.solve_captcha(driver):
                    logger.info("Successfully solved CAPTCHA")
                    return True
                
                # If no CAPTCHA found, try clicking through (some pages just need interaction)
                try:
                    buttons = driver.find_elements(By.TAG_NAME, "button")
                    for button in buttons:
                        if "continue" in button.text.lower() or "verify" in button.text.lower():
                            button.click()
                            time.sleep(3)
                            return True
                except:
                    pass
                
                logger.error("Failed to handle bot protection")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error handling bot protection: {str(e)}")
            return False
    
    def inject_stealth_js(self, driver):
        """Inject JavaScript to mask automation fingerprints"""
        js_scripts = [
            # Mask webdriver presence
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            """,
            # Add missing chrome properties
            """
            window.chrome = {
                app: {
                    isInstalled: false,
                    InstallState: {
                        DISABLED: 'disabled',
                        INSTALLED: 'installed',
                        NOT_INSTALLED: 'not_installed'
                    },
                    RunningState: {
                        CANNOT_RUN: 'cannot_run',
                        READY_TO_RUN: 'ready_to_run',
                        RUNNING: 'running'
                    }
                },
                runtime: {
                    OnInstalledReason: {
                        CHROME_UPDATE: 'chrome_update',
                        INSTALL: 'install',
                        SHARED_MODULE_UPDATE: 'shared_module_update',
                        UPDATE: 'update'
                    },
                    OnRestartRequiredReason: {
                        APP_UPDATE: 'app_update',
                        OS_UPDATE: 'os_update',
                        PERIODIC: 'periodic'
                    },
                    PlatformArch: {
                        ARM: 'arm',
                        ARM64: 'arm64',
                        MIPS: 'mips',
                        MIPS64: 'mips64',
                        X86_32: 'x86-32',
                        X86_64: 'x86-64'
                    },
                    PlatformNaclArch: {
                        ARM: 'arm',
                        MIPS: 'mips',
                        MIPS64: 'mips64',
                        X86_32: 'x86-32',
                        X86_64: 'x86-64'
                    },
                    PlatformOs: {
                        ANDROID: 'android',
                        CROS: 'cros',
                        LINUX: 'linux',
                        MAC: 'mac',
                        OPENBSD: 'openbsd',
                        WIN: 'win'
                    },
                    RequestUpdateCheckStatus: {
                        NO_UPDATE: 'no_update',
                        THROTTLED: 'throttled',
                        UPDATE_AVAILABLE: 'update_available'
                    }
                }
            };
            """,
            # Add missing navigator properties
            """
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    {
                        0: {type: "application/x-google-chrome-pdf", suffixes: "pdf", description: "Portable Document Format"},
                        description: "Portable Document Format",
                        filename: "internal-pdf-viewer",
                        length: 1,
                        name: "Chrome PDF Plugin"
                    },
                    {
                        0: {type: "application/pdf", suffixes: "pdf", description: "Portable Document Format"},
                        description: "Portable Document Format",
                        filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
                        length: 1,
                        name: "Chrome PDF Viewer"
                    }
                ]
            });
            """,
            # Add WebGL fingerprint
            """
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) {
                    return 'Intel Inc.';
                }
                if (parameter === 37446) {
                    return 'Intel(R) Iris(TM) Graphics 6100';
                }
                return getParameter.apply(this, arguments);
            };
            """
        ]
        
        for script in js_scripts:
            try:
                driver.execute_script(script)
            except Exception as e:
                logger.warning(f"Failed to inject script: {str(e)}")
    
    def simulate_human_behavior(self, driver):
        """Simulate human-like behavior"""
        try:
            # Random mouse movements
            driver.execute_script("""
                (() => {
                    const events = [];
                    let lastX = 0;
                    let lastY = 0;
                    
                    // Generate natural-looking mouse movement path
                    for(let i = 0; i < 10; i++) {
                        const x = Math.random() * window.innerWidth;
                        const y = Math.random() * window.innerHeight;
                        
                        // Calculate intermediate points for smooth movement
                        const points = 5;
                        for(let j = 0; j < points; j++) {
                            const ix = lastX + (x - lastX) * (j / points);
                            const iy = lastY + (y - lastY) * (j / points);
                            events.push({
                                type: 'mousemove',
                                x: ix,
                                y: iy,
                                delay: Math.random() * 100
                            });
                        }
                        
                        lastX = x;
                        lastY = y;
                    }
                    
                    // Execute movements with delays
                    events.forEach((evt, index) => {
                        setTimeout(() => {
                            const event = new MouseEvent(evt.type, {
                                view: window,
                                bubbles: true,
                                cancelable: true,
                                clientX: evt.x,
                                clientY: evt.y
                            });
                            document.dispatchEvent(event);
                        }, evt.delay * index);
                    });
                })();
            """)
            
            # Random scrolling
            scroll_points = random.randint(3, 6)
            for _ in range(scroll_points):
                scroll_amount = random.randint(100, 800)
                driver.execute_script(f"window.scrollBy(0, {scroll_amount})")
                time.sleep(0.5 + random.random())
                
                # Sometimes scroll back up
                if random.random() < 0.3:
                    driver.execute_script(f"window.scrollBy(0, -{scroll_amount//2})")
                    time.sleep(0.3 + random.random())
            
            # Random pauses
            time.sleep(1 + random.random() * 2)
            
        except Exception as e:
            logger.warning(f"Failed to simulate human behavior: {str(e)}")
    
    def scrape_product(self, url):
        """Scrape product information from Newegg URL using undetected-chromedriver"""
        # Store the URL for potential add_to_cart operations
        self.current_product_url = url
        
        max_retries = 2
        current_retry = 0
        
        # Extract product ID from URL for fallback
        product_id = None
        
        # First try the N82E format
        id_match = re.search(r'N82E(\d+)', url)
        if id_match:
            product_id = 'N82E' + id_match.group(1)
        else:
            # Try the /p/XXX-XXX-XXXXX format
            id_match = re.search(r'/p/([A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,5})', url)
            if id_match:
                product_id = id_match.group(1)
            else:
                # Fallback to just using the last segment of the URL
                segments = url.split('/')
                if len(segments) > 1:
                    product_id = segments[-1] if segments[-1] else segments[-2]
        
        if product_id:
            logger.info(f"Extracted product ID: {product_id}")
            # Store product ID for add_to_cart
            self.current_product_id = product_id
        
        # Try Selenium first
        while current_retry < max_retries:
            driver = None
            try:
                # Get fresh ChromeOptions to avoid reuse error
                options = self._get_chrome_options()
                
                # Add page load strategy for faster loading
                options.page_load_strategy = 'eager'
                
                # Create a new browser instance
                driver = uc.Chrome(options=options, version_main=detect_chrome_major())
                
                logger.debug(f"Accessing URL: {url}")
                
                # Use minimal stealth JS
                driver.execute_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false
                    });
                """)
                
                # Reduce delay - we need efficiency over stealth
                time.sleep(1)
                
                # Navigate to URL with timeout
                driver.set_page_load_timeout(30)  # 30 second timeout for page load
                driver.get(url)
                
                # Quick check for bot protection
                if "areyouahuman" in driver.current_url:
                    logger.info(f"Encountered Newegg bot protection - will try fallback")
                    # Don't waste time on captcha handling, just try the next strategy
                    current_retry += 1
                    continue
                
                # Minimal waiting - we want to scrape fast
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Get the page source
                page_source = driver.page_source
                soup = BeautifulSoup(page_source, 'html.parser')
                
                # Extract product details
                name = self.extract_name(soup, driver)
                price = self.extract_price(soup, driver)
                available = self.extract_availability(soup, driver)
                image_url = self.extract_image_url(soup, driver)
                
                # If we got basic info, return it
                if name and name != "Unknown Product":
                    return {
                        'name': name,
                        'price': price,
                        'available': available,
                        'image_url': image_url
                    }
                
                # If we reached here, basic extraction failed
                current_retry += 1
                
            except Exception as e:
                logger.error(f"Error scraping Newegg product: {str(e)}")
                current_retry += 1
            finally:
                # Make sure we close the driver to free resources
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
        
        # If all retries failed, use simplified HTTP-based scraping as a fallback
        try:
            logger.info(f"Using HTTP-based fallback scraper for {url}")
            product_data = self.scrape_via_requests(url, product_id)
            if product_data:
                return product_data
        except Exception as e:
            logger.error(f"Error using fallback scraper: {str(e)}")

        # Nothing usable was extracted. Return None so callers keep the stored
        # name/price/availability instead of overwriting them with placeholders.
        logger.warning(f"Could not extract Newegg product data for {url}; leaving stored data unchanged")
        return None
    
    def scrape_via_requests(self, url, product_id=None):
        """Simple HTTP-based scraping as a fallback when Selenium fails."""
        logger.info(f"Attempting to scrape Newegg product via direct HTTP request: {url}")
        
        headers = {
            'User-Agent': DEFAULT_HEADERS['User-Agent'],
            'Accept': DEFAULT_HEADERS['Accept'],
            'Accept-Language': DEFAULT_HEADERS['Accept-Language'],
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://www.newegg.com/',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': f'"Not A(Brand";v="99", "Google Chrome";v="{CHROME_VERSION}", "Chromium";v="{CHROME_VERSION}"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Upgrade-Insecure-Requests': '1'
        }
        
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            block_reason = detect_block_page(response.text)
            if block_reason:
                logger.warning(f"Newegg returned a block page for {url} (HTTP {response.status_code}): {block_reason}")
                return None
            if response.status_code != 200:
                logger.warning(f"Failed to get page, status code: {response.status_code}")
                return None
            
            # Parse the HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract product info without using Selenium
            name = self.extract_name_from_html(soup)
            price = self.extract_price_from_html(soup)
            available = self.extract_availability_from_html(soup)
            image_url = self.extract_image_url_from_html(soup)
            
            logger.info(f"HTTP fallback scraper extracted: name={name}, price={price}, available={available}")
            
            if not name or name == "Unknown Product":
                # No product markup at all - most likely a challenge page we did not recognise
                logger.warning(f"HTTP fallback found no product name for {url}; not returning placeholder data")
                return None

            return {
                'name': name,
                'price': price,
                'available': available,
                'image_url': image_url
            }
        except Exception as e:
            logger.error(f"Error in HTTP-based scraper: {str(e)}")
            return None

    def extract_name_from_html(self, soup):
        """Extract product name from HTML without Selenium."""
        try:
            # Try to find the product title in various locations
            selectors = [
                'h1.product-title',
                '[data-selenium="product-title"]',
                '.product-title',
                '.page-title'
            ]
            
            for selector in selectors:
                element = soup.select_one(selector)
                if element and element.text.strip():
                    return element.text.strip()
            
            # Try to get from meta tags
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                return meta_title.get('content').strip()
            
            # Try to find structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'name' in data:
                            return data['name']
                except:
                    continue
                
            return "Unknown Product"
        except Exception as e:
            logger.error(f"Error extracting name from HTML: {str(e)}")
            return "Unknown Product"

    def extract_price_from_html(self, soup):
        """Extract product price from HTML without Selenium."""
        try:
            # Try various price selectors
            selectors = [
                '.price-current',
                '.price-current strong',
                '.product-price',
                '.product-price strong',
                '[data-selenium="salePrice"]',
                '[data-selenium="itemPrice"]',
                '.price'
            ]
            
            for selector in selectors:
                element = soup.select_one(selector)
                if element and element.text.strip():
                    # Extract dollar amount
                    price_text = element.text.strip()
                    price_match = re.search(r'(\$)?(\d+,?\d*\.?\d*)', price_text)
                    if price_match:
                        price_str = price_match.group(2).replace(',', '')
                        try:
                            return float(price_str)
                        except ValueError:
                            continue
            
            # Try to find structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'price' in offers:
                                return float(offers['price'])
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'price' in offer:
                                        return float(offer['price'])
                except:
                    continue
                
            # Try meta tags
            meta_price = soup.find('meta', property='og:price:amount')
            if meta_price and meta_price.get('content'):
                try:
                    return float(meta_price.get('content'))
                except:
                    pass
                
            return None
        except Exception as e:
            logger.error(f"Error extracting price from HTML: {str(e)}")
            return None

    def extract_availability_from_html(self, soup):
        """Extract product availability from HTML without Selenium."""
        try:
            # Check for "Add to cart" button (enabled)
            add_to_cart_buttons = soup.select('button.btn-primary')
            for button in add_to_cart_buttons:
                if button.get('disabled') is None and ('add to cart' in button.text.lower() or 'add' in button.text.lower() or is_preorder_text(button.text)):
                    return True
            
            # Look for in-stock text
            for element in soup.select('.product-inventory, [data-selenium="inStock"]'):
                text = element.text.lower()
                if is_preorder_text(text):
                    return True
                if 'in stock' in text and 'out of stock' not in text:
                    return True
                
            # Look for out-of-stock indicators
            out_of_stock_elements = soup.find_all(string=lambda text: text and ('out of stock' in text.lower() or 'OUT OF STOCK' in text))
            if out_of_stock_elements:
                return False
            
            # Find price elements
            price_elements = soup.select('.price, .price-current')
            for element in price_elements:
                if element and '$' in element.text:
                    # If a price is displayed, it's likely in stock
                    return True
                
            # Try to find structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'availability' in offers:
                                return 'InStock' in offers['availability']
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'availability' in offer:
                                        if 'InStock' in offer['availability']:
                                            return True
                except:
                    continue
                
            # If we can't determine, default to False
            return False
        except Exception as e:
            logger.error(f"Error extracting availability from HTML: {str(e)}")
            return False

    def extract_image_url_from_html(self, soup):
        """Extract product image URL from HTML without Selenium."""
        try:
            # Try various image selectors
            selectors = [
                '.product-view-img-original',
                '[data-selenium="product-image"]',
                '.product-gallery img',
                '.swiper-zoom-container img',
                '.product-view-img-container img',
                '#product_preview_img',
                'div.swiper-slide.swiper-slide-active img',
                'img.mainSlide'
            ]
            
            for selector in selectors:
                element = soup.select_one(selector)
                if element and element.get('src'):
                    return element.get('src')
            
            # Try meta tags
            meta_image = soup.find('meta', property='og:image')
            if meta_image and meta_image.get('content'):
                return meta_image.get('content')
            
            # Try media gallery
            for media in soup.select('[data-slide-index="0"] img'):
                if media.get('src'):
                    return media.get('src')
                
            # Try structured data
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    if script and script.string:
                        data = json.loads(script.string)
                        if isinstance(data, dict) and 'image' in data:
                            if isinstance(data['image'], str):
                                return data['image']
                            elif isinstance(data['image'], list) and len(data['image']) > 0:
                                return data['image'][0]
                except:
                    continue
                
            # Last resort - find any image that seems to be a product image
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if any(term in src.lower() for term in ['product', 'gallery', 'large']):
                    return src
                
            return None
        except Exception as e:
            logger.error(f"Error extracting image URL from HTML: {str(e)}")
            return None

    def extract_name(self, soup, driver):
        """Extract product name using Selenium"""
        logger.debug("NeweggScraper: Extracting name")
        try:
            # Try multiple selectors for product name
            selectors = [
                (By.CLASS_NAME, "product-title"),
                (By.CSS_SELECTOR, "h1.product-title"),
                (By.CSS_SELECTOR, "[data-selenium='product-title']"),
                (By.TAG_NAME, "h1")
            ]
            
            for by, selector in selectors:
                try:
                    element = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((by, selector))
                    )
                    name = element.text.strip()
                    if name:
                        logger.debug(f"Found name: {name}")
                        return name
                except:
                    continue
            
            # Try structured data
            try:
                script = soup.find('script', {'type': 'application/ld+json'})
                if script and script.string:
                    import json
                    data = json.loads(script.string)
                    if isinstance(data, dict) and 'name' in data:
                        name = data['name']
                        logger.debug(f"Found name from structured data: {name}")
                        return name
            except:
                pass
            
            logger.warning("Could not find product name")
            return "Unknown Product"
        except Exception as e:
            logger.error(f"Error extracting name: {str(e)}")
            return "Unknown Product"
    
    def extract_price(self, soup, driver):
        """Extract product price using Selenium"""
        logger.debug("NeweggScraper: Extracting price")
        try:
            # Try multiple price selectors
            price_selectors = [
                (By.CSS_SELECTOR, ".price-current strong"),
                (By.CSS_SELECTOR, ".price-current"),
                (By.CSS_SELECTOR, ".product-price strong"),
                (By.CSS_SELECTOR, ".product-price"),
                (By.CSS_SELECTOR, ".price"),
                (By.CSS_SELECTOR, "[data-selenium='salePrice']"),
                (By.CSS_SELECTOR, "[data-selenium='itemPrice']"),
                (By.XPATH, "//span[contains(@class, 'price') or contains(@class, 'price-current')]"),
                (By.XPATH, "//li[contains(@class, 'price-current')]/strong")
            ]
            
            for by, selector in price_selectors:
                try:
                    element = driver.find_element(by, selector)
                    price_text = element.text.strip()
                    
                    # If price element is found but empty, try getting text from child elements
                    if not price_text:
                        child_elements = element.find_elements(By.XPATH, ".//*")
                        for child in child_elements:
                            if child.text and '$' in child.text:
                                price_text = child.text
                                break
                    
                    if price_text:
                        # Extract dollar amount
                        import re
                        price_match = re.search(r'(\$)?(\d+,?\d*\.?\d*)', price_text)
                        if price_match:
                            price_str = price_match.group(2).replace(',', '')
                            try:
                                price = float(price_str)
                                logger.debug(f"Found price: ${price}")
                                return price
                            except ValueError:
                                logger.warning(f"Could not convert price string to float: {price_str}")
                                continue
                except:
                    continue
            
            # Try structured data
            try:
                structured_data = driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for data_element in structured_data:
                    try:
                        data = json.loads(data_element.get_attribute('textContent'))
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'price' in offers:
                                try:
                                    price = float(offers['price'])
                                    logger.debug(f"Found price from structured data: ${price}")
                                    return price
                                except (ValueError, TypeError):
                                    pass
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'price' in offer:
                                        try:
                                            price = float(offer['price'])
                                            logger.debug(f"Found price from structured data offers list: ${price}")
                                            return price
                                        except (ValueError, TypeError):
                                            pass
                    except:
                        pass
            except:
                pass
            
            logger.warning("Could not extract price from page")
            return None
        except Exception as e:
            logger.error(f"Error extracting price: {str(e)}")
            return None
    
    def extract_availability(self, soup, driver):
        """Extract product availability using Selenium"""
        logger.debug("NeweggScraper: Extracting availability")
        try:
            # Try multiple availability indicators
            
            # Check "Add to cart" button availability
            add_to_cart_selectors = [
                (By.CSS_SELECTOR, "button.btn-primary:not([disabled])"),
                (By.XPATH, "//button[contains(text(), 'ADD TO CART') and not(@disabled)]"),
                (By.XPATH, "//button[contains(@class, 'btn-primary') and not(@disabled)]"),
                (By.XPATH, "//button[contains(text(), 'Add to cart') and not(@disabled)]")
            ]
            
            for by, selector in add_to_cart_selectors:
                try:
                    element = driver.find_element(by, selector)
                    if element and element.is_displayed():
                        logger.debug("Found enabled Add to Cart button - product is in stock")
                        return True
                except:
                    continue
            
            # Check for explicit in-stock text
            stock_text_selectors = [
                (By.CSS_SELECTOR, ".product-inventory"),
                (By.CSS_SELECTOR, "[data-selenium='inStock']"),
                (By.XPATH, "//*[contains(text(), 'In Stock')]")
            ]
            
            for by, selector in stock_text_selectors:
                try:
                    element = driver.find_element(by, selector)
                    if element:
                        text = element.text.lower()
                        if "in stock" in text and "out of stock" not in text:
                            logger.debug(f"Found in-stock text: {text}")
                            return True
                except:
                    continue
            
            # Pre-order buttons and stock text count as available
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, "button.btn-primary, .product-inventory"):
                    if element.is_displayed() and element.is_enabled() and is_preorder_text(element.text):
                        logger.debug("Found pre-order indicator - treating as available")
                        return True
            except:
                pass

            # Check for out-of-stock indicators
            out_of_stock_selectors = [
                (By.XPATH, "//*[contains(text(), 'OUT OF STOCK')]"),
                (By.XPATH, "//*[contains(text(), 'Out of Stock')]"),
                (By.XPATH, "//*[contains(text(), 'out of stock')]"),
                (By.XPATH, "//button[contains(@class, 'btn-primary') and @disabled]")
            ]
            
            for by, selector in out_of_stock_selectors:
                try:
                    element = driver.find_element(by, selector)
                    if element and element.is_displayed():
                        logger.debug("Found out-of-stock indicator")
                        return False
                except:
                    continue
            
            # Try structured data
            try:
                structured_data = driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for data_element in structured_data:
                    try:
                        data = json.loads(data_element.get_attribute('textContent'))
                        if isinstance(data, dict) and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'availability' in offers:
                                available = 'InStock' in offers['availability']
                                logger.debug(f"Found availability from structured data: {available}")
                                return available
                            elif isinstance(offers, list) and len(offers) > 0:
                                for offer in offers:
                                    if isinstance(offer, dict) and 'availability' in offer:
                                        if 'InStock' in offer['availability']:
                                            return True
                    except:
                        pass
            except:
                pass
            
            # If we can find a current price, the product is likely in stock
            try:
                price_elements = driver.find_elements(By.CSS_SELECTOR, ".price")
                for element in price_elements:
                    if element.is_displayed() and '$' in element.text:
                        logger.debug("Found price display - assuming product is in stock")
                        return True
            except:
                pass
            
            logger.warning("Could not determine product availability - defaulting to False")
            return False
        except Exception as e:
            logger.error(f"Error extracting availability: {str(e)}")
            return False
    
    def extract_image_url(self, soup, driver):
        """Extract product image URL using Selenium"""
        logger.debug("NeweggScraper: Extracting image URL")
        try:
            # Try multiple image selectors
            selectors = [
                (By.CLASS_NAME, "product-view-img-original"),
                (By.CSS_SELECTOR, "[data-selenium='product-image']"),
                (By.CSS_SELECTOR, ".product-gallery img"),
                (By.CSS_SELECTOR, ".swiper-zoom-container img"),
                (By.CSS_SELECTOR, ".product-view-img-container img"),
                (By.CSS_SELECTOR, "#product_preview_img"),
                (By.CSS_SELECTOR, "div.swiper-slide.swiper-slide-active img"),
                (By.CSS_SELECTOR, "img.mainSlide"),
                (By.XPATH, "//div[contains(@class, 'swiper-slide') and contains(@class, 'swiper-slide-active')]//img")
            ]
            
            for by, selector in selectors:
                try:
                    element = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((by, selector))
                    )
                    image_url = element.get_attribute("src")
                    if image_url:
                        logger.debug(f"Found image URL: {image_url}")
                        return image_url
                except:
                    continue
            
            # Try json-ld structured data for images
            try:
                structured_data = driver.find_elements(By.XPATH, "//script[@type='application/ld+json']")
                for data_element in structured_data:
                    try:
                        data = json.loads(data_element.get_attribute('textContent'))
                        if isinstance(data, dict) and 'image' in data:
                            if isinstance(data['image'], str):
                                return data['image']
                            elif isinstance(data['image'], list) and len(data['image']) > 0:
                                return data['image'][0]
                    except:
                        pass
            except:
                pass
                
            # Last resort: look for any image with product in the src
            try:
                all_images = driver.find_elements(By.TAG_NAME, "img")
                for img in all_images:
                    src = img.get_attribute("src")
                    if src and ("product" in src.lower() or "gallery" in src.lower() or "large" in src.lower()):
                        return src
            except:
                pass
            
            logger.warning("Could not find product image URL")
            return None
        except Exception as e:
            logger.error(f"Error extracting image URL: {str(e)}")
            return None

    def solve_hcaptcha(self, driver):
        """Solve hCaptcha"""
        try:
            # Get the site key
            site_key = driver.find_element(By.CSS_SELECTOR, ".h-captcha").get_attribute("data-sitekey")
            if not site_key:
                logger.error("Could not find hCaptcha site key")
                return False
            
            # Get the page URL
            page_url = driver.current_url
            
            # Send to 2captcha
            logger.info(f"Sending hCaptcha task to 2captcha: {site_key} on {page_url}")
            result = self.solver.hcaptcha(
                sitekey=site_key,
                url=page_url
            )
            
            if not result or not result.get("code"):
                logger.error("Failed to solve hCaptcha")
                return False
            
            logger.info(f"Got hCaptcha solution from 2captcha: {result.get('code')[:30]}...")
            
            # Try to inject the token via JavaScript
            try:
                driver.execute_script(
                    f'document.querySelector("[name=\'h-captcha-response\']").innerHTML = "{result["code"]}";'
                )
                
                # Try to find and click submit button
                submit_buttons = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
                for button in submit_buttons:
                    if button.is_displayed() and button.is_enabled():
                        button.click()
                        time.sleep(3)
                        return True
                
                # If no button found, try submitting form
                driver.execute_script("document.querySelector('form').submit();")
                time.sleep(3)
                return True
                
            except Exception as e:
                logger.error(f"Error submitting hCaptcha solution: {str(e)}")
                return False
            
        except Exception as e:
            logger.error(f"Error solving hCaptcha: {str(e)}")
            return False

    def add_to_cart(self, quantity=1):
        """
        Add a product to Newegg cart.
        
        Args:
            quantity: Quantity to add to cart (default: 1)
            
        Returns:
            dict: A dictionary with cart status information
                {
                    'success': True/False,
                    'message': str,
                    'cart_url': str,
                    'screenshot': str (base64)
                }
        """
        logger.info(f"Attempting to add product to Newegg cart with quantity {quantity}")
        
        if not hasattr(self, 'current_product_url') or not self.current_product_url:
            logger.error("No product URL available. Use scrape_product first.")
            return {
                'success': False,
                'message': "No product URL available. Please scrape the product first.",
                'cart_url': None,
                'screenshot': None
            }
        
        url = self.current_product_url
        driver = None
        
        try:
            # Get fresh ChromeOptions to avoid reuse error
            options = self._get_chrome_options()
            
            # Create browser instance
            driver = uc.Chrome(options=options, version_main=detect_chrome_major())
            
            # Step 1: Load cookies from environment if available
            newegg_cookies = os.environ.get('NEWEGG_COOKIES')
            
            # Step 2: Navigate to Newegg homepage first to set cookies
            driver.get('https://www.newegg.com/')
            
            # Apply user cookies if available
            if newegg_cookies:
                try:
                    logger.info("Applying user-provided Newegg cookies")
                    # Parse the cookie string
                    if isinstance(newegg_cookies, str):
                        # Basic cookie format parsing
                        cookie_pairs = newegg_cookies.split(';')
                        
                        for pair in cookie_pairs:
                            pair = pair.strip()
                            if not pair:
                                continue
                            
                            # Handle cookies without values (just names)
                            if '=' in pair:
                                name, value = pair.split('=', 1)
                            else:
                                name, value = pair, ''
                            
                            # Add cookie to driver
                            try:
                                cookie_dict = {
                                    'name': name.strip(),
                                    'value': value.strip(),
                                    'domain': '.newegg.com',
                                    'path': '/'
                                }
                                driver.add_cookie(cookie_dict)
                                logger.debug(f"Added cookie: {name}")
                            except Exception as e:
                                logger.warning(f"Failed to add cookie {name}: {str(e)}")
                except Exception as e:
                    logger.warning(f"Error applying cookies: {str(e)}")
            else:
                logger.warning("No Newegg cookies provided. Cart functionality may be limited.")
            
            # Refresh page after setting cookies
            driver.refresh()
            time.sleep(1)
            
            # Step 3: Navigate to product page
            logger.info(f"Navigating to product page: {url}")
            driver.get(url)
            
            # Handle any CAPTCHA or bot protection that might appear
            try:
                self.handle_bot_protection(driver)
            except Exception as e:
                logger.warning(f"Error handling bot protection: {str(e)}")
            
            # Step 4: Wait for page to load and check if product is available
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body"))
            )
            
            # Step 5: Check if product is available
            try:
                # Look for typical out-of-stock indicators
                out_of_stock_elements = driver.find_elements(By.XPATH, "//div[contains(text(), 'OUT OF STOCK') or contains(@class, 'product-inventory') and contains(text(), 'Out of Stock')]")
                
                if out_of_stock_elements:
                    screenshot = self._take_screenshot(driver)
                    return {
                        'success': False,
                        'message': "Product is out of stock",
                        'cart_url': None,
                        'screenshot': screenshot
                    }
            except Exception as e:
                logger.warning(f"Error checking stock status: {str(e)}")
            
            # Step 6: Find and click the Add to Cart button 
            try:
                # First try the standard Add to Cart button
                add_to_cart_selectors = [
                    "//button[contains(text(), 'ADD TO CART')]",
                    "//button[contains(@class, 'btn-primary') and contains(text(), 'Add to cart')]",
                    "//button[contains(@id, 'ProductBuy')]",
                    "//button[contains(@class, 'btn-buy')]"
                ]
                
                add_button = None
                for selector in add_to_cart_selectors:
                    elements = driver.find_elements(By.XPATH, selector)
                    if elements:
                        add_button = elements[0]
                        break
                
                if not add_button:
                    screenshot = self._take_screenshot(driver)
                    return {
                        'success': False,
                        'message': "Could not find Add to Cart button",
                        'cart_url': None,
                        'screenshot': screenshot
                    }
                
                # Click the Add to Cart button
                logger.info("Clicking Add to Cart button")
                
                # Scroll to the button to make it visible
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", add_button)
                time.sleep(0.5)
                
                # Try regular click first
                try:
                    add_button.click()
                except Exception as e:
                    logger.warning(f"Regular click failed: {str(e)}, trying JavaScript click")
                    driver.execute_script("arguments[0].click();", add_button)
                
                # Wait a bit for the cart to update
                time.sleep(2)
                
                # Check if we need to handle a popup
                try:
                    # Look for common popups that might appear after adding to cart
                    popup_close_buttons = driver.find_elements(By.XPATH, "//button[contains(@class, 'close') or contains(@class, 'btn-close')]")
                    if popup_close_buttons:
                        popup_close_buttons[0].click()
                        time.sleep(1)
                except Exception as e:
                    logger.warning(f"Error handling popup: {str(e)}")
                
                # Step 7: Verify that product was added to cart
                # Go to cart page
                driver.get("https://secure.newegg.com/shop/cart")
                time.sleep(2)
                
                # Take a screenshot of the cart
                screenshot = self._take_screenshot(driver)
                
                # Check if the cart has items
                cart_empty = len(driver.find_elements(By.XPATH, "//div[contains(text(), 'Your Shopping Cart is empty')]")) > 0
                
                if cart_empty:
                    return {
                        'success': False,
                        'message': "Failed to add product to cart",
                        'cart_url': driver.current_url,
                        'screenshot': screenshot
                    }
                
                return {
                    'success': True,
                    'message': "Product added to cart successfully",
                    'cart_url': driver.current_url,
                    'screenshot': screenshot
                }
                
            except Exception as e:
                logger.error(f"Error adding to cart: {str(e)}")
                if driver:
                    screenshot = self._take_screenshot(driver)
                else:
                    screenshot = None
                    
                return {
                    'success': False,
                    'message': f"Error adding to cart: {str(e)}",
                    'cart_url': None,
                    'screenshot': screenshot
                }
        except Exception as e:
            logger.error(f"Unexpected error in add_to_cart: {str(e)}")
            if driver:
                try:
                    screenshot = self._take_screenshot(driver)
                except:
                    screenshot = None
            else:
                screenshot = None
                
            return {
                'success': False,
                'message': f"Unexpected error: {str(e)}",
                'cart_url': None,
                'screenshot': screenshot
            }
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass

    def _take_screenshot(self, driver):
        """Take a screenshot and return as base64 string"""
        try:
            import base64
            screenshot = driver.get_screenshot_as_base64()
            return screenshot
        except Exception as e:
            logger.error(f"Error taking screenshot: {str(e)}")
            return None