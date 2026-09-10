"""
Live check of TargetScraper.add_to_cart (cart only - never checkout).

    python test_target_cart.py                     # headless, uses ~/.chrome_profiles/target_profile
    TARGET_HEADLESS=0 python test_target_cart.py   # visible window
    python test_target_cart.py --login             # ONE-TIME SETUP, see below
    python test_target_cart.py --import-cookies cookies.txt   # if --login keeps looping

One-time setup (--login)
------------------------
Target gates the cart behind a sign-in, and intermittently serves a PerimeterX
"Quick verification / Press & hold" challenge to automated profiles. Neither is
solved in code: `--login` opens the persistent profile in a VISIBLE window so you
can sign in and complete the press-and-hold yourself. The resulting cookies stay
in ~/.chrome_profiles/target_profile and later headless runs reuse them.

If the challenge loops (--import-cookies)
-----------------------------------------
Once Target has flagged a profile, its press-and-hold can loop forever in a
webdriver-controlled window - a new Reference ID every attempt, no way through.
Rather than automate the challenge (never do that), carry over the session you
already have in your ordinary browser:

    1. In your normal Chrome, sign in to target.com and clear any verification.
    2. Export cookies for target.com - the "Get cookies.txt LOCALLY" extension
       writes the Netscape format this reads; a JSON export also works.
    3. python test_target_cart.py --import-cookies path/to/cookies.txt

That loads your own cookies into ~/.chrome_profiles/target_profile and reports
whether a challenge or sign-in wall is still in the way. Treat the file as a
password: it is your live session. Delete it afterwards.

It also writes data/target_cookies.json (0600), which the Redsky path reads. With
a valid _px3 in there, scheduled Target checks answer from one HTTP request and
open no browser at all - so the profile, and its risk of being flagged, is only
spent on cart attempts. PX tokens are short-lived; when it goes cold the checks
quietly return to the browser path and re-running --import-cookies restores it.

The app must run as the SAME OS user, since it reads that same profile directory.

Expected once set up: the carrying case (pre-order open) lands in the cart; the
Pro Controller fails with "Preorders have sold out".
Screenshots are written to target_cart_<tcin>.png.
"""
import base64
import os
import stat
import sys

CASE_URL = ("https://www.target.com/p/nintendo-8482-switch-2-the-legend-of-zelda-40th-anniversary-edition-"
            "carrying-case-and-screen-protector/-/A-1013213522")
CONTROLLER_URL = ("https://www.target.com/p/nintendo-8482-switch-2-pro-controller-the-legend-of-zelda-40th-"
                  "anniversary-edition/-/A-1013213521")
ACCOUNT_URL = "https://www.target.com/account"
HOME_URL = "https://www.target.com/"

# --login and --import-cookies must show a window whatever TARGET_HEADLESS says;
# set it before the scraper module reads the variable at import time
LOGIN_MODE = '--login' in sys.argv
IMPORT_MODE = '--import-cookies' in sys.argv
if LOGIN_MODE or IMPORT_MODE:
    os.environ['TARGET_HEADLESS'] = '0'

from app.scrapers.target_scraper import TargetScraper, TARGET_CART_URL, TARGET_COOKIE_JAR  # noqa: E402
from app.scrapers.common import profile_lock, import_cookies_txt, save_cookie_jar  # noqa: E402

# Product names contain ™ / –; keep printing on cp1252 Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def login():
    """Open the persistent profile in a visible window for a one-time sign-in / verification."""
    scraper = TargetScraper()
    print(f"Opening a visible Chrome window using profile: {scraper.profile_dir}")
    # Wait indefinitely: a scheduled scrape may hold the profile, and the human
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


def import_cookies(path):
    """
    Load a target.com cookie export from the user's ordinary browser into the
    persistent profile, then report whether the way to the cart is clear.

    This moves the user's own signed-in session between two of their own browser
    profiles. It does not solve, bypass or automate the press-and-hold challenge.
    """
    scraper = TargetScraper()
    print(f"Importing {path} into profile: {scraper.profile_dir}")
    print("Waiting for exclusive access to the profile (a running check may hold it)...")
    with profile_lock(scraper.profile_dir, timeout=None):
        driver = scraper._start_driver()
        try:
            driver.set_page_load_timeout(60)
            # add_cookie() writes into the current document's store, so the domain
            # has to be loaded before any cookie for it will be accepted.
            print("Opening https://www.target.com/ so the cookies have a home...")
            driver.get(HOME_URL)
            added, rejected = import_cookies_txt(driver, path, 'target.com')
            print(f"Added {added} cookies ({rejected} rejected by Chrome)")

            for url in (CASE_URL, TARGET_CART_URL):
                print(f"\nReloading {url}")
                try:
                    driver.get(url)
                except Exception as e:
                    print(f"  navigation error: {e}")
                    continue
                challenge = scraper._challenge_present(driver)
                signed_out = scraper._login_wall_present(driver)
                print(f"  challenge present: {challenge or 'no'}")
                print(f"  sign-in wall:      {'yes' if signed_out else 'no'}")
                if not challenge and not signed_out:
                    print("  clear")

            # Save the session for the requests path as well. These are the
            # cookies as they stand AFTER the reloads above, so any PX token
            # Target refreshed during them is the one that gets stored.
            _save_jar(driver)

            print("\nIf both pages are clear, headless runs will reuse this session.")
            print("Delete the cookie file now - it is a live login.")
        finally:
            try:
                driver.quit()
            except Exception:
                pass


def _save_jar(driver):
    """
    Persist the browser's target.com cookies where the requests path can find
    them, so tracking stops needing a browser at all while they are valid.
    """
    try:
        cookies = [c for c in driver.get_cookies()
                   if (c.get('domain') or '').lstrip('.').endswith('target.com')]
        written, missing = save_cookie_jar(TARGET_COOKIE_JAR, cookies)
    except Exception as e:
        print(f"\nCould not save the cookie jar: {e}")
        print("The profile still has the session; only the browser-free path is unavailable.")
        return

    print(f"\nSaved {written} cookies to {TARGET_COOKIE_JAR}")
    # The file is opened 0600. On Windows that mode is ignored and the file
    # inherits the ACL of data/ instead, so do not promise a mode we did not set.
    print(f"  permissions: {oct(stat.S_IMODE(os.stat(TARGET_COOKIE_JAR).st_mode))}"
          + (" (Windows applies the inherited ACL, not the mode)" if os.name == "nt" else ""))
    if missing:
        print(f"  WARNING: no {', '.join(missing)} among them.")
        print("  _px3 is the PerimeterX clearance token; without it Redsky will keep")
        print("  answering 403 and every check will fall back to opening Chrome.")
    else:
        print("  Redsky checks will use this and skip the browser entirely.")
    print("  It expires on its own; when it does, checks quietly go back to the")
    print("  browser path and you can re-run --import-cookies to restore it.")
    print("  This file is a live login - it is under data/, which git ignores.")


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


def _cookie_path():
    """The path after --import-cookies"""
    index = sys.argv.index('--import-cookies')
    if index + 1 >= len(sys.argv):
        sys.exit("Usage: python test_target_cart.py --import-cookies <cookies.txt>")
    return sys.argv[index + 1]


if __name__ == '__main__':
    if IMPORT_MODE:
        import_cookies(_cookie_path())
    elif LOGIN_MODE:
        login()
    else:
        print("Testing Target add_to_cart...")
        for product_url in (CASE_URL, CONTROLLER_URL):
            check(product_url)
