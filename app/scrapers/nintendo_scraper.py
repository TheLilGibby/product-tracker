"""
Scraper for the My Nintendo Store (nintendo.com/us/store/products/...).

Nintendo serves a complete Next.js page to a plain HTTP request - no bot wall,
no rendering needed - so this is a requests + BeautifulSoup scraper with no
browser path at all.

Two things about this page are worth knowing before changing anything here.

Availability is isSalableQty, not the availability[] labels
-----------------------------------------------------------
A product node carries availability: ["Coming soon"] and prePurchase: true for
both an OPEN pre-order and one whose allocation is gone. The two states differ
only in isSalableQty. Observed live on 2026-09-10:

    sku 129088  availability ["Coming soon"]  prePurchase true  isSalableQty TRUE
    sku 121642  availability ["Coming soon"]  prePurchase true  isSalableQty FALSE

129088 could be pre-ordered that minute; 121642 (the Zelda 40th console) could
not. Keying on "Coming soon" or prePurchase would report all three Zelda items
as available on every check, from now until release.

The page describes ~30 products, not one
----------------------------------------
props.pageProps.initialApolloState is a normalised Apollo cache holding the
product plus its cross-sells and up-sells - 30 to 33 Product entries per page,
of which 14 to 22 are salable. Taking "the first product node", or "the first
salable node", would report the console as in stock because some unrelated
carrying case is. The node is therefore looked up by the exact cache key built
from the SKU in the URL, and that SKU is cross-checked against linkedData.
"""

import json
import logging
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from app.scrapers.common import DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page

logger = logging.getLogger('app.scrapers.nintendo')

# A store URL ends .../products/<slug>-<sku>/ - the SKU is the trailing number
NINTENDO_SKU_RE = re.compile(r'-(\d+)$')

# Apollo stores prices under a key with the query arguments baked into it, so
# the literal string matters.
PRICES_KEY = 'prices({"personalized":false})'

# schema.org availability values that mean "you can order this now"
ORDERABLE_SCHEMA_AVAILABILITY = (
    'https://schema.org/InStock',
    'https://schema.org/PreOrder',
    'https://schema.org/LimitedAvailability',
    'https://schema.org/BackOrder',
)


class NintendoScraper:
    """Scraper for My Nintendo Store product pages"""

    def __init__(self):
        """Initialize the Nintendo scraper."""
        logger.debug("Initializing NintendoScraper")
        self.headers = dict(DEFAULT_HEADERS)
        # Parity with the browser scrapers; nothing here needs it
        self.current_product_url = None

    @staticmethod
    def extract_sku(url):
        """
        Return the SKU from a My Nintendo Store URL, or None.

        The SKU is the trailing numeric segment of the product slug:
            /us/store/products/nintendo-switch-2-...-40th-anniversary-edition-121642/
        Query strings and fragments are ignored, so a tracked URL carrying
        campaign parameters still resolves.
        """
        try:
            path = urlparse(url or '').path
        except ValueError:
            logger.debug(f"Could not parse a path out of URL: {url!r}")
            return None
        match = NINTENDO_SKU_RE.search(path.rstrip('/'))
        return match.group(1) if match else None

    def scrape_product(self, url):
        """
        Scrape product information from a My Nintendo Store URL.

        Args:
            url: The product URL to scrape

        Returns:
            dict with name, price, available and image_url, or None if the page
            could not be read as a product page. None means "no answer" - the
            caller must not read it as "not available".
        """
        self.current_product_url = url

        sku = self.extract_sku(url)
        if not sku:
            logger.error(f"No SKU in Nintendo URL, cannot identify the product: {url}")
            return None

        logger.info(f"Scraping Nintendo product {sku}: {url}")
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.error(f"Request to Nintendo failed: {str(e)}")
            return None

        if response.status_code != 200:
            logger.error(f"Nintendo returned HTTP {response.status_code} for {url}")
            return None

        block_reason = detect_block_page(response.text)
        if block_reason:
            logger.warning(f"Nintendo served a block page ({block_reason}); reporting no result")
            return None

        soup = BeautifulSoup(response.text, 'html.parser')
        return self.extract_from_next_data(soup, sku)

    def extract_from_next_data(self, soup, sku):
        """
        Pull the product out of <script id="__NEXT_DATA__">.

        Args:
            soup: the parsed page
            sku: the SKU from the URL - the node is looked up by this and by
                 nothing else, because the page describes ~30 products

        Returns:
            dict, or None if this page has no node for that SKU
        """
        try:
            script = soup.find('script', id='__NEXT_DATA__')
            if not script or not script.string:
                logger.debug("No __NEXT_DATA__ script on page")
                return None

            page_props = json.loads(script.string).get('props', {}).get('pageProps', {})
            apollo = page_props.get('initialApolloState') or {}

            # The Apollo cache key is the literal string Nintendo writes, quotes
            # and all: Product:{"sku":"121642"}
            node = apollo.get('Product:{"sku":"%s"}' % sku)
            if not node:
                others = [key for key in apollo if key.startswith('Product:')]
                logger.error(f"No Apollo product node for SKU {sku} "
                             f"({len(others)} other products are on the page); "
                             "the URL may point at a product that has moved")
                return None

            linked = self._linked_data(page_props)
            # If linkedData names a different SKU we are reading the wrong page -
            # a redirect to a replacement product, say. Say so rather than
            # reporting on a product nobody asked about.
            linked_sku = str((linked or {}).get('sku') or '')
            if linked_sku and linked_sku != sku:
                logger.error(f"URL asks for SKU {sku} but the page describes {linked_sku}; "
                             "refusing to report on a different product")
                return None

            name = (node.get('name') or (linked or {}).get('name') or '').strip()
            if not name:
                logger.debug(f"Apollo node for {sku} has no name; not a usable product page")
                return None

            available = self._is_orderable(node, linked)

            price = None
            prices = node.get(PRICES_KEY) or {}
            for key in ('finalPrice', 'regularPrice'):
                value = prices.get(key)
                if isinstance(value, (int, float)):
                    price = float(value)
                    break
            if price is None:
                price = self._price_from_offers((linked or {}).get('offers'))

            image_url = (linked or {}).get('image')

            logger.info(f"Nintendo {sku}: {name[:60]} ${price} available={available}")
            return {
                'name': name,
                'price': price,
                'available': available,
                'image_url': image_url
            }
        except Exception as e:
            logger.error(f"Error extracting Nintendo product from __NEXT_DATA__: {str(e)}")
            return None

    @staticmethod
    def _is_orderable(node, linked=None):
        """
        Whether this product can actually be ordered right now.

        isSalableQty is the gate. It is the one field that separates an open
        pre-order from a closed one - see the module docstring. availability[]
        and prePurchase describe what the product IS, not whether Nintendo will
        take an order for it, so they are logged and otherwise ignored.

        linkedData's schema.org availability is consulted only to log a
        disagreement: it is a static per-page annotation, so it never overrides
        isSalableQty.
        """
        salable = node.get('isSalableQty')
        available = salable is True

        logger.debug(f"Nintendo {node.get('sku')}: isSalableQty={salable!r} "
                     f"availability={node.get('availability') or []} "
                     f"prePurchase={node.get('prePurchase')!r} -> available={available}")

        schema_availability = None
        if linked:
            offers = linked.get('offers') or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                schema_availability = offers.get('availability')
        if schema_availability:
            schema_orderable = schema_availability in ORDERABLE_SCHEMA_AVAILABILITY
            if schema_orderable != available:
                logger.warning(f"Nintendo {node.get('sku')}: isSalableQty says available="
                               f"{available} but linkedData says {schema_availability}; "
                               "trusting isSalableQty")
        return available

    @staticmethod
    def _linked_data(page_props):
        """Return the page's JSON-LD Product dict, or None. Nintendo sends it as a list."""
        linked = page_props.get('linkedData')
        if isinstance(linked, list):
            for entry in linked:
                if isinstance(entry, dict) and entry.get('@type') == 'Product':
                    return entry
            return linked[0] if linked and isinstance(linked[0], dict) else None
        return linked if isinstance(linked, dict) else None

    @staticmethod
    def _price_from_offers(offers):
        """Pull a float price out of a JSON-LD offers object or list, or None"""
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            return None
        try:
            return float(str(offers.get('price')).replace(',', ''))
        except (TypeError, ValueError):
            return None
