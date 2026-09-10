"""
Standalone check for the undetected-chromedriver Best Buy scraper.

Usage:
    python test_bestbuy_browser.py                 # scrape the two target products
    python test_bestbuy_browser.py --headed        # force a visible Chrome window
    python test_bestbuy_browser.py --cart          # also try add_to_cart on the first buyable product
    python test_bestbuy_browser.py <url> [<url>]   # scrape custom URLs
    python test_bestbuy_browser.py --offline       # only run the HTML-parsing checks (no Chrome)

Requires Chrome installed locally. Hits bestbuy.com for everything except --offline.
Exit code is non-zero when a scrape returns None or violates the result contract.
"""

import argparse
import logging
import sys

from bs4 import BeautifulSoup

from app.scrapers.bestbuy_scraper import BestBuyScraper

# Importing app.scrapers runs the app package's DEBUG basicConfig; override it so
# selenium/urllib3 don't dump every page source to the console.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
for noisy in ('selenium', 'urllib3', 'uc', 'undetected_chromedriver'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

TARGETS = [
    # Carrying case - listed as pre-order / coming soon (releases Oct 29 2026)
    "https://www.bestbuy.com/product/nintendo-switch-2-carrying-case-and-screen-protector-the-legend-of-zelda-40th-anniversary-edition-multi/J7GSL57WCW/sku/6691852",
    # Pro Controller - reported sold out at Best Buy
    "https://www.bestbuy.com/product/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-multi/J7GSL57W27",
]

# Minimal HTML fixtures mirroring bestbuy.com's current product-page markup
FIXTURE_TEMPLATE = """
<html><head><title>{name} - Best Buy</title>
<script type="application/ld+json">{{"@type": "Product", "name": "{name}", "sku": "{sku}",
 "image": [{{"@type": "ImageObject", "url": "https://pisces.bbystatic.com/image2/BestBuy_US/images/products/x.jpg"}}],
 "offers": [{{"@type": "Offer", "price": {price}, "availability": "https://schema.org/{ld_avail}"}}]}}</script>
</head><body><h1>{name}</h1>
<div data-testid="price-block-customer-price">${price} <span>$</span><span>{price}</span></div>
<button data-testid="pdp-{state}-{sku}" {disabled}>{label}</button>
<button data-testid="carousel-add-to-cart-1111111">Add to cart</button>
</body></html>
"""

FIXTURES = [
    # (state, label, disabled attr, JSON-LD availability, expected available)
    ('add-to-cart', 'Add to Cart', '', 'InStock', True),
    ('pre-order', 'Pre-Order', '', 'PreOrder', True),
    ('coming-soon', 'Coming Soon', 'disabled', 'InStock', False),   # JSON-LD lies; button wins
    ('sold-out', 'Sold Out', 'disabled', 'OutOfStock', False),
    ('add-to-cart', 'Add to Cart', 'disabled', 'InStock', False),   # disabled CTA => not buyable
]

CHROME_ERROR_PAGE = '<html><head><title>www.bestbuy.com</title></head><body><div id="main-frame-error"></div></body></html>'
ACCESS_DENIED_PAGE = '<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>Reference #18.abc</body></html>'


def check_contract(result):
    """Raise AssertionError if a scrape result violates the scraper contract."""
    assert isinstance(result, dict), f"expected dict, got {type(result).__name__}"
    assert set(result) == {'name', 'price', 'available', 'image_url'}, f"unexpected keys: {sorted(result)}"
    assert isinstance(result['name'], str) and result['name'], "name must be a non-empty string"
    assert result['price'] is None or isinstance(result['price'], float), "price must be float or None"
    assert isinstance(result['available'], bool), "available must be a bool"
    assert result['image_url'] is None or result['image_url'].startswith('http'), "image_url must be a URL or None"


def run_offline_checks():
    """Exercise the HTML-only extraction path and bot-wall detection without Chrome."""
    scraper = BestBuyScraper.__new__(BestBuyScraper)  # skip __init__: no profile dir, no Chrome lookup
    failures = 0

    for state, label, disabled, ld_avail, expected in FIXTURES:
        html = FIXTURE_TEMPLATE.format(name="Fixture Product", sku="6691852", price="39.99",
                                       state=state, label=label, disabled=disabled, ld_avail=ld_avail)
        soup = BeautifulSoup(html, 'html.parser')
        got = scraper.extract_availability_from_html(soup)
        price = scraper.extract_price_from_html(soup)
        name = scraper.extract_name_from_html(soup)
        image = scraper.extract_image_url_from_html(soup)
        ok = got == expected and price == 39.99 and name == "Fixture Product" and image.startswith('https://pisces')
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] pdp-{state} ({label}, disabled={bool(disabled)}, ld={ld_avail}) "
              f"-> available={got} price={price}")

    for label, html, expected in (
        ('chrome network error page', CHROME_ERROR_PAGE, True),
        ('akamai access denied', ACCESS_DENIED_PAGE, True),
        ('real product markup', FIXTURE_TEMPLATE.format(name="X", sku="6691852", price="1.00", state='sold-out',
                                                        label='Sold Out', disabled='disabled', ld_avail='OutOfStock'), False),
    ):
        got = BestBuyScraper.is_blocked_html(html, BeautifulSoup(html, 'html.parser').title.string)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] is_blocked_html: {label} -> {got}")

    for url, expected in (
        (TARGETS[0], '6691852'),
        (TARGETS[1], None),
        ("https://www.bestbuy.com/site/some-product/6613053.p?skuId=6613053", '6613053'),
    ):
        got = BestBuyScraper.extract_sku(url)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] extract_sku({url[-40:]}) -> {got}")

    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('urls', nargs='*', help="product URLs (default: the two Zelda 40th Anniversary items)")
    parser.add_argument('--headed', action='store_true', help="force a visible Chrome window")
    parser.add_argument('--cart', action='store_true', help="also call add_to_cart() on the first buyable product")
    parser.add_argument('--offline', action='store_true', help="only run parser/fixture checks, no Chrome")
    args = parser.parse_args()

    print("Offline parser checks:")
    failures = run_offline_checks()
    if args.offline:
        print(f"\n{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
        return 1 if failures else 0

    urls = args.urls or TARGETS
    scraper = BestBuyScraper(headless=False if args.headed else None)
    print(f"\nChrome major={scraper.chrome_major} headless={scraper.headless} "
          f"scrape_headed_fallback={scraper.scrape_headed_fallback} cart_headed_fallback={scraper.cart_headed_fallback}")

    buyable_url = None
    for url in urls:
        print(f"\nScraping: {url}")
        result = scraper.scrape_product(url)
        if result is None:
            print("  FAIL: scrape_product returned None (bot wall or network failure)")
            failures += 1
            continue
        try:
            check_contract(result)
        except AssertionError as e:
            print(f"  FAIL: contract violation: {e}")
            failures += 1
        print(f"  name:      {result['name']}")
        print(f"  price:     {result['price']}")
        print(f"  available: {result['available']}")
        print(f"  image_url: {result['image_url']}")
        print(f"  sku:       {scraper.current_sku}")
        if result['available'] and buyable_url is None:
            buyable_url = url

    if args.cart:
        if buyable_url is None:
            print("\nadd_to_cart skipped: no buyable product among the targets")
        else:
            print(f"\nadd_to_cart on {buyable_url}")
            scraper.current_product_url = buyable_url
            cart = scraper.add_to_cart(quantity=1)
            print(f"  success:   {cart.get('success')}")
            print(f"  message:   {cart.get('message')}")
            print(f"  cart_url:  {cart.get('cart_url')}")
            print(f"  screenshot:{'captured' if cart.get('screenshot') else 'none'}")
            if not cart.get('success'):
                failures += 1

    print(f"\n{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
