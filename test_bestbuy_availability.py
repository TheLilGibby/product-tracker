#!/usr/bin/env python
"""
Offline checks for what Best Buy availability is allowed to be read from.

The invariant: availability is the product's own call-to-action, and nothing
else. The CTA is the one element on the page that knows whether the item can be
bought, it names the SKU it belongs to, and it is what the cart path clicks - so
it is the only thing whose answer the cart path can be expected to agree with.

The contradiction these checks pin down was found in the running integration
tracker: the Best Buy console row alerted as available, and the auto-cart job
then refused the click with "Product is not purchasable (coming soon)". Two
saved captures explain it exactly. Both carry

    "availability": "https://schema.org/InStock"

in the Product JSON-LD, and both render

    <button data-testid="pdp-coming-soon-6691852" disabled>Coming Soon</button>

as the page's own CTA - SKUs 6691852 and 6691849, metadata and page disagreeing
in the same capture. The JSON-LD is simply wrong for anything unreleased, which
is the entire class of product this tracker exists for.

Two failure paths led to that True, and both are checked below:

  1. The CTA had not rendered yet. _load_product_page's wait is satisfied by the
     JSON-LD, which is in the server's first response, while the CTA is client-
     rendered a moment later - so the scrape could read a CTA-less page even
     though the page was perfectly healthy.
  2. Having found no CTA, extract_availability fell back to the JSON-LD and
     believed its InStock.

Run: python test_bestbuy_availability.py
No network, no Chrome, no Best Buy request - the markup below is distilled from
the saved captures.
"""

import logging
import os
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.scrapers.bestbuy_scraper import BestBuyScraper  # noqa: E402

CONSOLE_SKU = '6691852'
OTHER_SKU = '6691849'


def _jsonld(sku, availability):
    return (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Nintendo Switch 2",'
        '"sku":"%s","offers":{"@type":"Offer","price":519.99,"priceCurrency":"USD",'
        '"availability":"%s","itemCondition":"https://schema.org/NewCondition"}}'
        '</script>' % (sku, availability)
    )


def _cta(state, sku, label, disabled=True):
    return '<button data-testid="pdp-%s-%s"%s>%s</button>' % (
        state, sku, ' disabled' if disabled else '', label)


# What the page actually says, in the two saved captures.
COMING_SOON = _jsonld(CONSOLE_SKU, 'https://schema.org/InStock') + _cta('coming-soon', CONSOLE_SKU, 'Coming Soon')
# The same page one paint earlier: the metadata is there, the CTA is not.
PRE_HYDRATION = _jsonld(CONSOLE_SKU, 'https://schema.org/InStock')
# A real, buyable listing.
BUYABLE = (_jsonld(CONSOLE_SKU, 'https://schema.org/InStock')
           + _cta('add-to-cart', CONSOLE_SKU, 'Add to Cart', disabled=False))
# A pre-order is buyable: the order is placed now and the cart accepts it.
PRE_ORDER = (_jsonld(CONSOLE_SKU, 'https://schema.org/InStock')
             + _cta('pre-order', CONSOLE_SKU, 'Pre-Order', disabled=False))
# Sold out, with the metadata still insisting otherwise.
SOLD_OUT = _jsonld(OTHER_SKU, 'https://schema.org/InStock') + _cta('sold-out', OTHER_SKU, 'Sold Out')
# The honest metadata case, which is still allowed to say no.
JSONLD_OUT_OF_STOCK = _jsonld(CONSOLE_SKU, 'https://schema.org/OutOfStock')
# Neither one. The plain-HTTP fetch of a Best Buy pdp looks like this: the saved
# bestbuy_sample.html carries 0 pdp buttons and 0 Product JSON-LD scripts.
NOTHING = '<html><body><div id="shop">Best Buy</div></body></html>'

CASES = (
    (COMING_SOON, False, 'coming soon on the page, InStock in the metadata: the page wins'),
    (SOLD_OUT, False, 'sold out on the page, InStock in the metadata: the page wins'),
    (PRE_HYDRATION, False, 'metadata alone never reports available'),
    (JSONLD_OUT_OF_STOCK, False, 'metadata may still say no'),
    (NOTHING, False, 'no CTA and no metadata is not available'),
    (BUYABLE, True, 'an enabled Add to Cart is what available means'),
    (PRE_ORDER, True, 'an enabled Pre-Order is available: the order is placed now'),
)


def run_checks():
    scraper = BestBuyScraper.__new__(BestBuyScraper)
    failures = 0

    for markup, expected, label in CASES:
        got = scraper.extract_availability_from_html(BeautifulSoup(markup, 'html.parser'))
        ok = got is expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> {got} (expected {expected})")

    # The same answers through the browser entry point, with no driver to fall
    # back to. extract_availability and extract_availability_from_html must not
    # disagree: one feeds the alert and the other feeds the HTTP path, and a
    # product that is available down one and not the other is the whole bug.
    print("")
    for markup, expected, label in CASES:
        soup = BeautifulSoup(markup, 'html.parser')
        got = scraper.extract_availability(soup, None)
        ok = got is expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] browser path agrees: {label} -> {got}")

    return failures


def main():
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    print("Best Buy availability comes from the CTA, not the metadata:")
    failures = run_checks()
    print("")
    print(f"{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
