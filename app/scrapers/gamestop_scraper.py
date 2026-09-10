"""
Scraper for GameStop product pages (gamestop.com/.../products/<slug>/<id>.html).

GameStop is Salesforce Commerce Cloud behind Cloudflare, and the Cloudflare edge
decides on more than the User-Agent. Measured against the Zelda 40th console page
on 2026-09-10:

    plain requests, full browser header set   403,     5,019 bytes, "Attention Required!"
    undetected-chromedriver, --headless=new   200,     4,801 bytes, "Attention Required!"
    undetected-chromedriver, visible window   200,   558,727 bytes, the real product page

So there is no HTTP fallback path here, and headless is not merely slower or
flakier - it is reliably blocked. GAMESTOP_HEADLESS therefore defaults to OFF,
which means a scheduled check opens a real Chrome window on the user's desktop.
That is a deliberate, documented trade: a headless run returns None on every
poll, which is worse than a visible one.

Reading the buy box
-------------------
Three things on this page look like availability. Only one of them is:

    JSON-LD offers[].availability   WRONG - said InStock for the Pro Controller
                                    while its button was disabled and the page
                                    read "0 item(s) are available for Pre-Order"
    button.add-to-cart text         right, but it is prose ("Pre-Order", "Not
                                    Available", "Add to Cart")
    .product-availability           right, and it carries an explicit boolean in
      [data-available]              data-available="true"/"false"

data-available is the signal. The button's disabled attribute is read as a
cross-check and a disagreement is logged.

JSON-LD is still used, but only for the name and image: its offers carry a
DIFFERENT sku from the URL (the console URL ends 20037854.html while offers[0]
is sku 451607) because offers are per-condition variants - New, Pre-Owned - each
with its own product id.

Scoping to the right product
----------------------------
A product page carries 7 to 11 [data-pid] elements, and the Pro Controller page
carries a second .product-detail[data-pid] entirely - a bundle. Carousel entries
have their own live "ADD TO CART" buttons. Every selector below is therefore
scoped to the id from the URL; nothing here takes a first match.
"""

import logging
import json
import os
import re
import time
from contextlib import ExitStack

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.common.exceptions import WebDriverException
from urllib.parse import urlparse

from app.scrapers.common import (DEFAULT_HEADERS, detect_block_page, detect_chrome_major,
                                 profile_lock, ProfileBusyError)

logger = logging.getLogger('app.scrapers.gamestop')

# .../products/<slug>/<product id>.html
GAMESTOP_ID_RE = re.compile(r'/(\d+)\.html$')

PRICE_RE = re.compile(r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)')

# Button prose that means the product can be ordered. Read only as a cross-check
# on data-available; see the module docstring.
ORDERABLE_BUTTON_TEXTS = ('add to cart', 'pre-order', 'preorder')

# Seconds to let the SFCC page settle after load. The buy box is server-rendered,
# but Cloudflare's challenge script runs first on a cold profile.
PAGE_SETTLE_SECONDS = 8


class GameStopScraper:
    """Scraper for GameStop product pages"""

    def __init__(self):
        """Initialize the GameStop scraper."""
        logger.debug("Initializing GameStopScraper")
        self.headers = dict(DEFAULT_HEADERS)

        # Persistent profile so Cloudflare sees a returning browser
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "gamestop_profile")
        os.makedirs(self.profile_dir, exist_ok=True)

        self.current_product_url = None

    @staticmethod
    def extract_product_id(url):
        """
        Return the product id from a GameStop URL - the numeric filename, e.g.
        .../nintendo-switch-2-...-edition/20037854.html -> "20037854". None if
        the URL is not a product page.
        """
        try:
            path = urlparse(url or '').path
        except ValueError:
            logger.debug(f"Could not parse a path out of URL: {url!r}")
            return None
        match = GAMESTOP_ID_RE.search(path)
        return match.group(1) if match else None

    def scrape_product(self, url):
        """
        Scrape product information from a GameStop URL.

        Args:
            url: The product URL to scrape

        Returns:
            dict with name, price, available and image_url, or None if the page
            could not be read - a Cloudflare wall, a busy profile, or a page with
            no buy box for this id. None means "no answer", never "out of stock".
        """
        self.current_product_url = url

        product_id = self.extract_product_id(url)
        if not product_id:
            logger.error(f"No product id in GameStop URL, cannot identify the product: {url}")
            return None

        html = self._fetch_rendered_html(url, product_id)
        if html is None:
            return None

        return self.extract_from_html(BeautifulSoup(html, 'html.parser'), product_id)

    def _fetch_rendered_html(self, url, product_id):
        """
        Load the product page in Chrome and return its HTML, or None.

        The profile lock is held for the whole browser session: Chrome refuses to
        run two instances against one --user-data-dir, and the loser's launch
        error looks exactly like a bot wall.
        """
        logger.info(f"Fetching GameStop product {product_id} in a browser: {url}")
        with ExitStack() as stack:
            try:
                stack.enter_context(profile_lock(self.profile_dir))
            except ProfileBusyError as e:
                logger.error(f"Could not get the GameStop profile: {str(e)}")
                return None

            driver = None
            try:
                driver = self._start_driver()
                stack.callback(self._quit_quietly, driver)
                driver.set_page_load_timeout(60)
                driver.get(url)
                time.sleep(PAGE_SETTLE_SECONDS)
                html = driver.page_source
            except ProfileBusyError as e:
                logger.error(str(e))
                return None
            except WebDriverException as e:
                logger.error(f"Browser failed on the GameStop page: {str(e)}")
                return None

            block_reason = detect_block_page(html)
            if block_reason:
                logger.warning(
                    f"GameStop served a block page ({block_reason}). If this is a headless run, "
                    "that is expected - Cloudflare blocks headless Chrome here; unset "
                    "GAMESTOP_HEADLESS to use a visible window.")
                return None

            logger.debug(f"GameStop page rendered, {len(html)} bytes")
            return html

    def _start_driver(self):
        """
        Launch Chrome on the shared profile, translating the launch failure that
        profile contention produces into something the caller can report.

        The lock must already be held.
        """
        try:
            return uc.Chrome(options=self._get_chrome_options(), version_main=detect_chrome_major())
        except WebDriverException as e:
            message = str(e)
            if 'not reachable' in message or 'session not created' in message:
                raise ProfileBusyError(
                    f"Chrome could not start on the profile {self.profile_dir}. Another Chrome is most "
                    "likely using it - close any window opened from that profile and retry") from e
            raise

    def _get_chrome_options(self):
        """
        Get fresh ChromeOptions (reusing an options object raises in undetected-chromedriver).

        Note the headless default is OFF, the opposite of the other browser
        scrapers: Cloudflare serves headless Chrome a 4.8KB challenge page here
        every time. Set GAMESTOP_HEADLESS=1 only to reproduce that.
        """
        options = uc.ChromeOptions()
        options.add_argument(f'--user-data-dir={self.profile_dir}')
        options.add_argument('--profile-directory=Default')
        if os.environ.get('GAMESTOP_HEADLESS', '0') == '1':
            logger.warning("GAMESTOP_HEADLESS=1: Cloudflare blocks headless Chrome on gamestop.com, "
                           "so this run will almost certainly return no result")
            options.add_argument('--headless=new')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1366,768')
        options.add_argument('--lang=en-US')
        return options

    @staticmethod
    def _quit_quietly(driver):
        """Close the browser without letting a teardown error mask the real result"""
        try:
            driver.quit()
        except Exception as e:
            logger.debug(f"Ignoring error while closing Chrome: {str(e)}")

    def extract_from_html(self, soup, product_id):
        """
        Read the buy box for one product id out of a rendered GameStop page.

        Args:
            soup: the parsed page
            product_id: the id from the URL; every lookup is scoped to it

        Returns:
            dict, or None if the page has no buy box for that id
        """
        try:
            detail = self._product_detail(soup, product_id)
            if detail is None:
                logger.error(f"No .product-detail for GameStop id {product_id} on this page; "
                             "the URL may point at a product that has moved")
                return None

            available = self._is_orderable(soup, detail, product_id)
            if available is None:
                return None

            linked = self._linked_data(soup)
            name = self._extract_name(detail, soup, linked)
            if not name:
                logger.debug(f"No product name for GameStop id {product_id}; not a usable page")
                return None

            price = self._extract_price(detail, linked)
            image_url = self._extract_image(linked)

            logger.info(f"GameStop {product_id}: {name[:60]} ${price} available={available}")
            return {
                'name': name,
                'price': price,
                'available': available,
                'image_url': image_url
            }
        except Exception as e:
            logger.error(f"Error extracting GameStop product: {str(e)}")
            return None

    @staticmethod
    def _product_detail(soup, product_id):
        """
        The .product-detail element for this id.

        A page can carry several - the Pro Controller page ships a bundle
        alongside the product - so this matches on data-pid and never on
        position.
        """
        for element in soup.select('.product-detail[data-pid]'):
            if element.get('data-pid') == product_id:
                return element
        return None

    def _is_orderable(self, soup, detail, product_id):
        """
        Whether this product can be ordered right now.

        Returns True/False, or None when the page carries no availability signal
        for this id at all - which is a parse failure, not an out-of-stock.

        data-available on .product-availability is the source of truth. The
        add-to-cart button's disabled attribute is read as a cross-check.
        """
        availability = self._scoped(soup, detail, product_id, '.product-availability[data-available]')
        available = None
        if availability is not None:
            raw = (availability.get('data-available') or '').strip().lower()
            if raw in ('true', 'false'):
                available = raw == 'true'
                logger.debug(f"GameStop {product_id}: data-available={raw!r} "
                             f"msg={availability.get_text(' ', strip=True)[:60]!r}")

        button = self._scoped(soup, detail, product_id, 'button.add-to-cart')
        button_orderable = None
        if button is not None:
            text = button.get_text(strip=True).lower()
            button_orderable = (not button.has_attr('disabled')
                                and any(phrase in text for phrase in ORDERABLE_BUTTON_TEXTS))
            logger.debug(f"GameStop {product_id}: button text={text!r} "
                         f"disabled={button.has_attr('disabled')} -> {button_orderable}")

        if available is None:
            if button_orderable is None:
                logger.error(f"GameStop page has no availability signal for id {product_id} "
                             "(no data-available, no add-to-cart button); reporting no result")
                return None
            logger.warning(f"GameStop {product_id}: no data-available on the page, "
                           f"falling back to the button state ({button_orderable})")
            return button_orderable

        if button_orderable is not None and button_orderable != available:
            logger.warning(f"GameStop {product_id}: data-available says {available} but the "
                           f"add-to-cart button says {button_orderable}; trusting data-available")
        return available

    @staticmethod
    def _scoped(soup, detail, product_id, selector):
        """
        Find one element for this product: inside its .product-detail if possible,
        otherwise the one carrying a matching data-pid.

        Never falls back to "the first match on the page" - that is how a
        carousel's Add to Cart button ends up deciding whether the console is in
        stock.
        """
        match = detail.select_one(selector)
        if match is not None:
            return match
        for element in soup.select(selector):
            if element.get('data-pid') == product_id:
                return element
        return None

    @staticmethod
    def _linked_data(soup):
        """Return the page's JSON-LD Product dict, or None"""
        for tag in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(tag.string or '')
            except (ValueError, TypeError):
                continue
            for node in (data if isinstance(data, list) else [data]):
                if isinstance(node, dict) and node.get('@type') == 'Product':
                    return node
        return None

    @staticmethod
    def _extract_name(detail, soup, linked):
        """Product name, preferring the page's own heading over JSON-LD"""
        for selector in ('h1.product-name', '.product-name', 'h1'):
            element = detail.select_one(selector) or soup.select_one(selector)
            if element:
                name = element.get_text(strip=True)
                if name:
                    return name
        return ((linked or {}).get('name') or '').strip() or None

    def _extract_price(self, detail, linked):
        """
        Current price as a float, or None.

        The DOM price is scoped to this product's detail block; JSON-LD is the
        fallback, and its offers are per-condition, so the lowest New offer would
        be a Pre-Owned price if read carelessly - only offers[0] is used, which
        is the condition the page is showing.
        """
        for selector in ('.actual-price', '.price .sales', '.product-price .sales', '[itemprop="price"]'):
            element = detail.select_one(selector)
            if element is None:
                continue
            text = element.get('content') or element.get_text(strip=True)
            match = PRICE_RE.search(text or '')
            if match:
                try:
                    return float(match.group(1).replace(',', ''))
                except ValueError:
                    pass
            try:
                return float(str(text).replace(',', '').strip())
            except (TypeError, ValueError):
                continue

        offers = (linked or {}).get('offers')
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            try:
                return float(str(offers.get('price')).replace(',', ''))
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _extract_image(linked):
        """Product image URL from JSON-LD, or None"""
        image = (linked or {}).get('image')
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get('url')
        return image if isinstance(image, str) else None
