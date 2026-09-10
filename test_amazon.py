"""
Amazon availability checks.

    python test_amazon.py            # offline: fixtures only, no network
    python test_amazon.py --live     # also scrape the live URLs below

The fixtures are trimmed from real Amazon product pages (saved 2026-09-10) and
keep the markup that actually decides the answer: which buy box Amazon rendered,
what #availability / #outOfStock say, and where the add-to-cart button sits.

They exist because AmazonScraper reported the Nintendo Switch 2 Zelda 40th
Anniversary console as available while Amazon was not selling it. That page says
"Currently unavailable. We do not know when or if this item will be back in
stock." - and the old check was `'in stock' in text`, which the tail of that
sentence satisfies. The pre-order fixtures are here so the fix cannot be "call
everything unavailable": a pre-order is a drop we want to be alerted about.

Exit code is non-zero if any check fails.
"""
import argparse
import logging
import sys

from bs4 import BeautifulSoup

from app.scrapers.amazon_scraper import AmazonScraper

# Real URLs behind the fixtures, for --live. Availability moves, so a live run
# reports what it sees and only fails on a scrape that returns nothing.
LIVE_URLS = [
    'https://www.amazon.com/dp/B0HJ6F8L6V',  # Zelda 40th console - not sold by Amazon
    'https://www.amazon.com/dp/B09B8V1LZ3',  # Echo Dot - normally in stock
]

# ---------------------------------------------------------------- fixtures

# Echo Dot: a plain in-stock offer. Both buttons live under the qualified buy box.
IN_STOCK = """
<div id="desktop_qualifiedBuyBox" class="a-section">
  <div id="availability" class="a-section a-spacing-base">
    <span class="a-size-medium a-color-success">In Stock</span>
  </div>
  <div id="addToCart_feature_div">
    <span class="a-button a-button-primary">
      <span class="a-button-inner">
        <input id="add-to-cart-button" class="a-button-input" name="submit.add-to-cart"
               title="Add to Shopping Cart" type="submit" value="Add to cart">
      </span>
    </span>
  </div>
  <div id="buyNow_feature_div">
    <span class="a-button a-button-primary">
      <span class="a-button-inner">
        <input id="buy-now-button" class="a-button-input" name="submit.buy-now"
               title="Buy Now" type="submit">
      </span>
    </span>
  </div>
</div>
"""

# Halloween: The Game - a pre-order. Qualified buy box, but no add-to-cart: the
# only control is Buy Now, and the availability text never says "in stock".
PRE_ORDER = """
<div id="desktop_qualifiedBuyBox" class="a-section">
  <div id="availability" class="a-section a-spacing-base">
    <span class="a-size-medium a-color-success">This item will be released on October 6, 2026.</span>
    <span class="a-size-base">Pre-order now.</span>
  </div>
  <div id="buyNow_feature_div">
    <span class="a-button a-button-primary">
      <span class="a-button-inner">
        <input id="buy-now-button" class="a-button-input" name="submit.buy-now"
               title="Buy Now" type="submit">
      </span>
    </span>
  </div>
</div>
"""

# Zelda 40th console, "Currently unavailable" render. No buy box, no button, and
# an availability sentence whose last three words are "back in stock".
OUT_OF_STOCK = """
<div id="outOfStockBuyBox_feature_div" class="a-section">
  <div id="availability" class="a-section a-spacing-base">
    <div id="all-offers-display" class="a-section">
      <span class="a-color-base a-text-bold">Currently unavailable.</span>
      <br>We do not know when or if this item will be back in stock.
    </div>
  </div>
  <div id="outOfStock" class="a-box">
    <div class="a-box-inner">
      <span class="a-color-base a-text-bold">Currently unavailable.</span>
      <br>We do not know when or if this item will be back in stock.
    </div>
  </div>
</div>
"""

# The same console on a different render: the buy box is the unqualified one -
# Amazon has no offer of its own, only "See All Buying Options" - and
# #availability is empty, so there is no wording to go on at all.
UNQUALIFIED_BUY_BOX = """
<div id="unqualifiedBuyBox_feature_div" class="a-section">
  <div id="availability" class="a-section a-spacing-base"></div>
  <span class="a-button a-button-primary">
    <span class="a-button-inner">
      <a href="/gp/offer-listing/B0HJ6F8L6V" class="a-button-text">See All Buying Options</a>
    </span>
  </span>
</div>
"""

# Amazon keeps rendering a full add-to-cart form inside the unqualified buy box
# on listings it has no offer for. Counting that button is the whole bug.
HIDDEN_BUTTON_IN_UNQUALIFIED_BOX = """
<div id="unqualifiedBuyBox_feature_div" class="a-section">
  <div id="availability" class="a-section a-spacing-base"></div>
  <div class="aok-hidden">
    <input id="add-to-cart-button" class="a-button-input" name="submit.add-to-cart" type="submit">
  </div>
</div>
"""

# A button Amazon has switched off - the class sits on the wrapper span, not the
# input, so has_attr('disabled') alone does not always see it.
DISABLED_BUTTON = """
<div id="desktop_qualifiedBuyBox" class="a-section">
  <div id="availability" class="a-section a-spacing-base">
    <span class="a-size-medium">Select a size to see availability</span>
  </div>
  <span class="a-button a-button-disabled">
    <span class="a-button-inner">
      <input id="add-to-cart-button" class="a-button-input" type="submit" disabled>
    </span>
  </span>
</div>
"""

# Low stock still counts as orderable, and there is no button in this fragment,
# so it has to come out of the text.
LOW_STOCK_TEXT_ONLY = """
<div id="availability" class="a-section a-spacing-base">
  <span class="a-size-medium a-color-price">Only 2 left in stock - order soon.</span>
</div>
"""

TEMPORARILY_OUT_OF_STOCK_TEXT_ONLY = """
<div id="availability" class="a-section a-spacing-base">
  <span class="a-size-medium a-color-price">Temporarily out of stock.</span>
</div>
"""

# (fixture, expected availability, what the page is)
CASES = [
    (IN_STOCK, True, 'in stock, add-to-cart under the qualified buy box'),
    (PRE_ORDER, True, 'pre-order: qualified buy box, Buy Now only'),
    (OUT_OF_STOCK, False, 'Currently unavailable ... back in stock'),
    (UNQUALIFIED_BUY_BOX, False, 'unqualified buy box, no Amazon offer'),
    (HIDDEN_BUTTON_IN_UNQUALIFIED_BOX, False, 'hidden add-to-cart inside the unqualified buy box'),
    (DISABLED_BUTTON, False, 'add-to-cart present but disabled'),
    (LOW_STOCK_TEXT_ONLY, True, 'text only: Only 2 left in stock'),
    (TEMPORARILY_OUT_OF_STOCK_TEXT_ONLY, False, 'text only: Temporarily out of stock'),
]


def run_fixture_checks():
    """extract_availability against every fixture. Returns the failure count."""
    scraper = AmazonScraper()
    failures = 0
    for html, expected, description in CASES:
        got = scraper.extract_availability(BeautifulSoup(html, 'html.parser'))
        ok = got is expected
        failures += 0 if ok else 1
        status = 'ok' if ok else 'FAIL'
        print(f"  [{status}] {description}: expected {expected}, got {got}")
    return failures


def run_live_checks():
    """Scrape the real pages. Only a scrape that returns nothing is a failure."""
    scraper = AmazonScraper()
    failures = 0
    for url in LIVE_URLS:
        result = scraper.scrape_product(url)
        if not result:
            failures += 1
            print(f"  [FAIL] {url} -> no result (blocked, or the page changed shape)")
            continue
        print(f"  [ok] {url}")
        print(f"       {result['name']!r}")
        print(f"       price={result['price']} available={result['available']}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--live', action='store_true', help="also scrape the live product pages")
    parser.add_argument('-v', '--verbose', action='store_true', help="show scraper DEBUG logging")
    args = parser.parse_args()

    # force=True: importing the app installs handlers of its own, and without
    # this the scraper's DEBUG chatter drowns the check output.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format='%(levelname)s %(name)s %(message)s', force=True)
    logging.getLogger('app.scrapers.amazon').setLevel(
        logging.DEBUG if args.verbose else logging.WARNING)

    print("Availability fixtures:")
    failures = run_fixture_checks()
    if args.live:
        print("")
        print("Live pages:")
        failures += run_live_checks()

    print("")
    print(f"{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
