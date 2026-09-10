"""
Shared helpers for the requests-based scraper paths: default headers, the
request timeout, bot-wall detection and pre-order text matching.
"""

import logging
import os
import re
import subprocess

# Set up logging
logger = logging.getLogger('app.scrapers.common')

# Seconds to wait for a retailer response. Without a timeout a hung request
# holds check_lock in app/tasks.py for the rest of the process lifetime.
REQUEST_TIMEOUT = 20

# Headers that look like a current desktop Chrome. Retailers serve stripped or
# blocked pages to the ancient Chrome/91 UA the scrapers used to send.
CHROME_VERSION = '130'
DEFAULT_HEADERS = {
    'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Phrases that appear in the <title> of bot walls / access-denied interstitials
BLOCK_PAGE_TITLE_MARKERS = (
    'robot or human',      # Walmart
    'access denied',       # Akamai (Best Buy, others)
    'robot check',         # Amazon
    'px-captcha',
    'automated access',
)

# Phrases specific enough to trust anywhere in the body. "access denied" is
# deliberately not here: it shows up in inline JS on perfectly good pages, and
# the bare string "px-captcha" appears in PerimeterX CSS (#px-captcha-modal) on
# every Target page - only the rendered challenge element counts.
BLOCK_PAGE_BODY_MARKERS = (
    'robot or human',
    'id="px-captcha"',     # PerimeterX challenge container
    "id='px-captcha'",
    'automated access',    # Amazon "To discuss automated access to Amazon data..."
    'robot check',
)

# Any of these means we are looking at a real product page, so a small body is
# not by itself a sign of a block page
PRODUCT_PAGE_MARKERS = (
    'og:title',
    'application/ld+json',
    'add to cart',
    'add-to-cart',
    'itemprop="price"',
    'producttitle',
)

# Bodies smaller than this with no product markers are almost always a challenge page
MIN_PRODUCT_PAGE_BYTES = 5000

# Only the head and start of the body are needed; keeps the scan cheap on big pages
_SCAN_LIMIT = 200000

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_PREORDER_RE = re.compile(r'pre[\s-]?order', re.IGNORECASE)


def detect_block_page(html):
    """
    Decide whether a fetched page is a bot wall rather than a product page.

    Args:
        html: Raw response body

    Returns:
        A short reason string if the page looks blocked, otherwise None
    """
    if not html:
        return 'empty response'

    sample = html[:_SCAN_LIMIT].lower()

    title_match = _TITLE_RE.search(sample)
    title = title_match.group(1).strip() if title_match else ''
    for marker in BLOCK_PAGE_TITLE_MARKERS:
        if marker in title:
            return f'title contains "{marker}"'

    for marker in BLOCK_PAGE_BODY_MARKERS:
        if marker in sample:
            return f'body contains "{marker}"'

    if len(html) < MIN_PRODUCT_PAGE_BYTES and not any(marker in sample for marker in PRODUCT_PAGE_MARKERS):
        return f'body is {len(html)} bytes with no product markers'

    return None


def is_preorder_text(text):
    """True if the text says the item is a pre-order ("pre-order", "preorder" or "pre order")"""
    return bool(text) and bool(_PREORDER_RE.search(text))


def detect_chrome_major():
    """
    Major version of the installed Chrome, for undetected-chromedriver's
    `version_main`. Without it uc downloads the newest driver, which refuses to
    start against an older browser ("This version of ChromeDriver only supports
    Chrome version N"). CHROME_MAJOR_VERSION in the environment overrides
    detection; None lets undetected-chromedriver decide.
    """
    override = os.environ.get('CHROME_MAJOR_VERSION', '')
    if override.isdigit():
        return int(override)

    for exe in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        try:
            output = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r'(\d+)\.\d+\.\d+', output or '')
        if match:
            return int(match.group(1))

    # Windows installs keep a <version> directory next to chrome.exe
    try:
        import undetected_chromedriver as uc
        exe = uc.find_chrome_executable()
        if exe:
            for entry in os.listdir(os.path.dirname(exe)):
                match = re.fullmatch(r'(\d+)\.\d+\.\d+\.\d+', entry)
                if match:
                    return int(match.group(1))
    except Exception as e:
        logger.debug(f"Could not detect Chrome version from install directory: {str(e)}")
    return None
