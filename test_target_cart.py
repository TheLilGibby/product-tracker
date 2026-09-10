"""
Live check of TargetScraper.add_to_cart (cart only - never checkout).

    python test_target_cart.py                     # headless, uses ~/.chrome_profiles/target_profile
    TARGET_HEADLESS=0 python test_target_cart.py   # visible window
    python test_target_cart.py --login             # ONE-TIME SETUP, see below

One-time setup (--login)
------------------------
Target gates the cart behind a sign-in, and intermittently serves a PerimeterX
"Quick verification / Press & hold" challenge to automated profiles. Neither is
solved in code: `--login` opens the persistent profile in a VISIBLE window so you
can sign in and complete the press-and-hold yourself. The resulting cookies stay
in ~/.chrome_profiles/target_profile and later headless runs reuse them.

The app must run as the SAME OS user, since it reads that same profile directory.

Expected once set up: the carrying case (pre-order open) lands in the cart; the
Pro Controller fails with "Preorders have sold out".
Screenshots are written to target_cart_<tcin>.png.
"""
import base64
import os
import sys

CASE_URL = ("https://www.target.com/p/nintendo-8482-switch-2-the-legend-of-zelda-40th-anniversary-edition-"
            "carrying-case-and-screen-protector/-/A-1013213522")
CONTROLLER_URL = ("https://www.target.com/p/nintendo-8482-switch-2-pro-controller-the-legend-of-zelda-40th-"
                  "anniversary-edition/-/A-1013213521")
ACCOUNT_URL = "https://www.target.com/account"

# --login must show a window whatever TARGET_HEADLESS says; set before the scraper reads it
LOGIN_MODE = '--login' in sys.argv
if LOGIN_MODE:
    os.environ['TARGET_HEADLESS'] = '0'

from app.scrapers.target_scraper import TargetScraper, TARGET_CART_URL  # noqa: E402
from app.scrapers.common import detect_chrome_major  # noqa: E402
import undetected_chromedriver as uc  # noqa: E402

# Product names contain ™ / –; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def login():
    """Open the persistent profile in a visible window for a one-time sign-in / verification."""
    scraper = TargetScraper()
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    driver = uc.Chrome(options=scraper._get_chrome_options(), version_main=detect_chrome_major())
    try:
        driver.set_page_load_timeout(60)
        driver.get(ACCOUNT_URL)
        print("\nSign in and complete any verification in the window, then press Enter here...")
        input()

        # Visit the product page and the cart once so the profile carries the cleared cookies
        for url in (CASE_URL, TARGET_CART_URL):
            print(f"Visiting {url}")
            try:
                driver.get(url)
            except Exception as e:
                print(f"  (navigation error: {e})")
            challenge = scraper._challenge_present(driver)
            if challenge:
                print(f"  Still showing a verification challenge ({challenge}) - complete it in the window, then press Enter...")
                input()
        print("\nDone. The profile now holds the session; headless runs will reuse it.")
        print("Run `python test_target_cart.py` to test add-to-cart.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def check(url):
    tcin = TargetScraper.extract_tcin(url)
    print(f"\nURL: {url}")
    scraper = TargetScraper()

    # add_to_cart needs current_product_url, so pre-scrape like app.scrapers.add_to_cart does
    info = scraper.scrape_product(url)
    print(f"Scrape: {info and info.get('name')} | price={info and info.get('price')} | available={info and info.get('available')}")

    try:
        result = scraper.add_to_cart(quantity=1)
        print(f"Success: {result.get('success')}")
        print(f"Message: {result.get('message')}")
        print(f"Cart URL: {result.get('cart_url')}")
        if result.get('screenshot'):
            path = f"target_cart_{tcin}.png"
            with open(path, 'wb') as f:
                f.write(base64.b64decode(result['screenshot']))
            print(f"Screenshot: {path}")
    except Exception as e:
        print(f"Error: {str(e)}")
        print(f"Type: {type(e).__name__}")


if __name__ == '__main__':
    if LOGIN_MODE:
        login()
    else:
        print("Testing Target add_to_cart...")
        for product_url in (CASE_URL, CONTROLLER_URL):
            check(product_url)
