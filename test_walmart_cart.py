"""
Live check of WalmartScraper.add_to_cart (cart only - never checkout).

    python test_walmart_cart.py                      # headless, uses ~/.chrome_profiles/walmart_profile
    WALMART_HEADLESS=0 python test_walmart_cart.py   # visible window
    python test_walmart_cart.py --login              # ONE-TIME SETUP, see below

One-time setup (--login)
------------------------
Walmart serves a PerimeterX "Robot or human? / Press & hold" wall to fresh
automated profiles - a brand new headless profile gets it on the very first
product page - and gates the cart behind a sign-in. Neither is solved in code:
`--login` opens the persistent profile in a VISIBLE window so you can sign in
and clear the challenge yourself. The cookies stay in
~/.chrome_profiles/walmart_profile and later headless runs reuse them.

The app must run as the SAME OS user, since it reads that same profile directory.
The lock is held for the whole session, so a scheduled check cannot open Chrome
on this profile while you are signing in.

Expected: the Pro Controller below fails cleanly with "the pre-order is not open"
WITHOUT launching a browser - the buy-box state is read over plain HTTP first.
Screenshots, when a browser did run, are written to walmart_cart_<item_id>.png.
"""
import base64
import os
import sys

CONTROLLER_URL = ("https://www.walmart.com/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-"
                  "Anniversary-Edition/20954470204")
ACCOUNT_URL = "https://www.walmart.com/account"

# --login must show a window whatever WALMART_HEADLESS says; set before the scraper reads it
LOGIN_MODE = '--login' in sys.argv
if LOGIN_MODE:
    os.environ['WALMART_HEADLESS'] = '0'

from app.scrapers.walmart_scraper import WalmartScraper, WALMART_CART_URL  # noqa: E402
from app.scrapers.common import profile_lock  # noqa: E402

# Product names contain ™ / –; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def login():
    """Open the persistent profile in a visible window for a one-time sign-in / verification."""
    scraper = WalmartScraper()
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    # Wait indefinitely: a scheduled check may hold the profile, and the human
    # here needs it for as long as signing in takes.
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with profile_lock(scraper.profile_dir, timeout=None):
        _login_session(scraper)


def _login_session(scraper):
    driver = scraper._start_driver()
    try:
        driver.set_page_load_timeout(60)
        driver.get(ACCOUNT_URL)
        print("\nSign in and complete any verification in the window, then press Enter here...")
        input()

        # Visit the product page and the cart once so the profile carries the cleared cookies
        for url in (CONTROLLER_URL, WALMART_CART_URL):
            print(f"Visiting {url}")
            try:
                driver.get(url)
            except Exception as e:
                print(f"  (navigation error: {e})")
            challenge = scraper._challenge_present(driver)
            if challenge:
                print(f"  Still showing a verification challenge ({challenge}) - complete it in the window, "
                      "then press Enter...")
                input()
        print("\nDone. The profile now holds the session; headless runs will reuse it.")
        print("Run `python test_walmart_cart.py` to test add-to-cart.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def check(url):
    item_id = WalmartScraper.extract_item_id(url)
    print(f"\nURL: {url}")
    scraper = WalmartScraper()

    # add_to_cart needs current_product_url, so pre-scrape like app.scrapers.add_to_cart does
    info = scraper.scrape_product(url)
    print(f"Scrape: {info and info.get('name')} | price={info and info.get('price')} "
          f"| available={info and info.get('available')}")

    try:
        result = scraper.add_to_cart(quantity=1)
        print(f"Success: {result.get('success')}")
        print(f"Message: {result.get('message')}")
        print(f"Cart URL: {result.get('cart_url')}")
        if result.get('screenshot'):
            path = f"walmart_cart_{item_id}.png"
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
        print("Testing Walmart add_to_cart...")
        check(CONTROLLER_URL)
