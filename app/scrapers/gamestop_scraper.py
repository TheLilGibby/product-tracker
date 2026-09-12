"""
Scraper for GameStop product pages (gamestop.com/.../products/<slug>/<id>.html).

GameStop is Salesforce Commerce Cloud behind Cloudflare, and the Cloudflare edge
decides on more than the User-Agent. Measured against the Zelda 40th console page
on 2026-09-10:

    plain requests, full browser header set   403,     5,019 bytes, "Attention Required!"
    undetected-chromedriver, --headless=new   200,     4,801 bytes, "Attention Required!"
    undetected-chromedriver, visible window   200,   558,727 bytes, the real product page

So headless is not merely slower or flakier - it is reliably blocked.
GAMESTOP_HEADLESS therefore defaults to OFF, which means a scheduled check opens
a real Chrome window on the user's desktop. That is a deliberate, documented
trade: a headless run returns None on every poll, which is worse than a visible
one.

The pasted session (GAMESTOP_COOKIES)
-------------------------------------
There is one way to make a plain request work, and it is the row above that
says the edge decides on more than the User-Agent: carry a clearance the user
already holds. Settings -> GameStop cookies takes the Cookie header from their
own browser, and while cf_clearance in it is good, a check is one HTTP request
and no window opens at all. That is the whole prize here - not speed, but a
scheduled poll that does not put a Chrome window on someone's desktop every few
minutes.

Two things make a paste fail in a way that looks like having none:

  * cf_clearance is issued to ONE User-Agent and refused to every other, so the
    browser's UA has to be sent with it. That is what GAMESTOP_USER_AGENT is,
    and the settings page asks for it beside the cookies.
  * It is issued to one IP as well, so a session pasted from another machine or
    behind a different exit will not work here.

A refusal is remembered for the run (app.scrapers.common) rather than retried
every cycle, and the browser path stays exactly as it was underneath - the HTTP
path only ever returns None or a real page. Nothing here solves a challenge:
the user passes it in their own browser, and this carries the result.

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

import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.common.exceptions import WebDriverException
from urllib.parse import urlparse

from app.scrapers.common import (DEFAULT_HEADERS, REQUEST_TIMEOUT, detect_block_page,
                                 detect_chrome_major, profile_lock, ProfileBusyError,
                                 apply_cookie_header, cookie_header_is_rejected,
                                 cookie_header_jar, cookie_header_names, cookie_setting,
                                 note_cookie_header_rejected, forget_cookie_header_rejection,
                                 note_session_probe, session_probe_get,
                                 session_probe_wait_seconds)

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

# The cookies that decide whether the Cloudflare edge answers. Named for the help
# text only - a paste is sent whole, because a session is more than the parts we
# can name here.
#
#   cf_clearance  the clearance token, issued once the edge is satisfied. This is
#                 the one that turns a 403 into a page.
#   __cf_bm       the bot-management cookie minted alongside it; short-lived.
#   _abck, bm_sz  Akamai's, set on the same pages. Carried, not required.
GAMESTOP_SESSION_COOKIES = ('cf_clearance', '__cf_bm')


def load_gamestop_cookies():
    """The pasted GameStop session as a Cookie header, or ''."""
    return cookie_setting('GAMESTOP_COOKIES')


def load_gamestop_user_agent(configured_only=False):
    """
    The User-Agent to send with the pasted session.

    cf_clearance is issued to one User-Agent and is refused to any other, so a
    paste from the user's own browser has to be sent with that browser's UA or
    it is dead on arrival - and it fails as a 403, which looks exactly like
    having no session at all. The settings page asks for it next to the cookies
    for that reason. Falling back to our default UA is the honest default: it
    is what the request would have sent anyway. ``configured_only`` drops that
    fallback, for the settings page, which should show the box empty rather
    than show our UA as though the user had typed it.
    """
    configured = cookie_setting('GAMESTOP_USER_AGENT')
    if configured or configured_only:
        return configured
    return DEFAULT_HEADERS['User-Agent']


# What the settings page asks for when no GameStop product is tracked yet. A
# product page rather than the homepage on purpose: the edge is stricter about
# the pages worth scraping, so a homepage 200 would prove less than it looks.
SESSION_PROBE_URL = ('https://www.gamestop.com/consoles-hardware/nintendo-switch-2/products/'
                     'nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition/'
                     '20037854.html')


def test_gamestop_session(url=None):
    """
    Fetch one page with the saved session and report what the edge did.

    The settings page's Test button. One request, status line and block-page
    classification only; the body is measured and discarded, never rendered.

    Returns {'ok', 'level', 'message', 'status'}, where ``level`` is the flash
    category. Cloudflare has three answers worth telling apart and they are
    easily confused: a 403, a 200 that is really the challenge page, and a 200
    that is really the product.

    A success forgets an earlier refusal so a renewed session resumes at once. A
    failure records none - see test_target_session for why.
    """
    header = load_gamestop_cookies()
    if not header:
        return {'ok': False, 'level': 'warning', 'status': None,
                'message': 'No GameStop session is saved, so there is nothing to test.'}

    waiting = session_probe_wait_seconds('gamestop')
    if waiting:
        return {'ok': False, 'level': 'warning', 'status': None,
                'message': f'Just tested. GameStop can be tested again in {waiting} seconds.'}

    jar = cookie_header_jar(header, 'gamestop.com')
    if jar is None:
        return {'ok': False, 'level': 'error', 'status': None,
                'message': 'That header holds no usable cookie. Copy the whole '
                           'cookie: line from the Network tab, not one cell.'}

    url = url or SESSION_PROBE_URL
    headers = dict(DEFAULT_HEADERS)
    headers['User-Agent'] = load_gamestop_user_agent()
    ua_pasted = bool(cookie_setting('GAMESTOP_USER_AGENT'))

    note_session_probe('gamestop')
    response, error = session_probe_get(url, headers=headers, cookies=jar)
    if error:
        logger.info(f"GameStop session test could not be made ({len(jar)} cookies sent): {error}")
        return {'ok': False, 'level': 'error', 'status': None,
                'message': f'GameStop could not be reached. {error}'}

    status = response.status_code
    blocked = detect_block_page(response.text) if status == 200 else None
    # Logged without the session: the names say everything a reader needs, and a
    # cookie in a log file is a cookie in a log file.
    logger.info(f"GameStop session test: HTTP {status}"
                f"{' (block page: ' + blocked + ')' if blocked else ''} "
                f"({', '.join(cookie_header_names(header))})")

    if status == 200 and not blocked:
        forget_cookie_header_rejection(header)
        return {'ok': True, 'level': 'success', 'status': status,
                'message': f'GameStop answered 200 with your session ({len(response.text):,} '
                           'bytes of product page). Checks will use plain HTTP and open '
                           'no browser window.'}

    # Both remaining failures have the same two causes, and the User-Agent one
    # is the one people do not think of - so it is named first when it is
    # missing, and mentioned second when it is not.
    if ua_pasted:
        why = ("That usually means the clearance has expired, or this machine's "
               'IP address has changed since it was issued - Cloudflare ties it to '
               'both. Load a gamestop.com product page again and copy a fresh '
               'cookie header.')
    else:
        why = ('No User-Agent is saved, which is the most likely cause: Cloudflare '
               'issues a clearance to one browser and refuses it to every other. '
               'Copy the user-agent: line from the same request and save it above.')

    if status == 200 and blocked:
        return {'ok': False, 'level': 'error', 'status': status,
                'message': f'GameStop answered 200 but served its challenge page '
                           f'({blocked}), which is a refusal wearing a success. {why}'}
    return {'ok': False, 'level': 'error', 'status': status,
            'message': f'GameStop refused it ({status}). {why}'}



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

        # With a session the user pasted, the edge answers a plain request and a
        # check costs no Chrome window at all. Without one this is skipped
        # entirely rather than spending a guaranteed 403 on every poll: the
        # measurements in the module docstring are what a bare request gets.
        html = self._fetch_via_requests(url, product_id)
        if html is None:
            html = self._fetch_rendered_html(url, product_id)
        if html is None:
            return None

        return self.extract_from_html(BeautifulSoup(html, 'html.parser'), product_id)

    def _fetch_via_requests(self, url, product_id):
        """
        Fetch the product page over plain HTTP, carrying the pasted session.

        Returns the HTML, or None for every other outcome - no session, a wall,
        a transport error - which always means "ask the browser", never that the
        product is unavailable.
        """
        header = load_gamestop_cookies()
        if not header:
            return None
        if cookie_header_is_rejected(header):
            logger.debug("Skipping the GameStop HTTP path: this session was already refused")
            return None
        jar = cookie_header_jar(header, 'gamestop.com')
        if jar is None:
            logger.warning("The pasted GameStop session holds no usable cookie; using the browser")
            return None

        headers = dict(self.headers)
        headers['User-Agent'] = load_gamestop_user_agent()
        logger.info(f"Fetching GameStop product {product_id} over HTTP with the pasted session "
                    f"({', '.join(cookie_header_names(header))})")
        try:
            response = requests.get(url, headers=headers, cookies=jar,
                                    timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.warning(f"GameStop HTTP request failed: {str(e)}; using the browser")
            return None

        if response.status_code != 200:
            logger.warning(f"GameStop answered HTTP {response.status_code} to the pasted "
                           "session; using the browser")
            # 403 is the edge refusing this client. The usual causes are an
            # expired clearance and a User-Agent that is not the one the
            # clearance was issued to, and neither gets better by retrying every
            # cycle, so the paste is set aside until a fresh one is saved.
            if response.status_code in (401, 403):
                note_cookie_header_rejected(header, 'GameStop')
            return None

        block_reason = detect_block_page(response.text)
        if block_reason:
            # A 200 carrying "Attention Required!" is the wall too - the edge
            # serves its challenge page with a 200 once JS is expected to run.
            logger.warning(f"GameStop served a block page to the pasted session "
                           f"({block_reason}); using the browser")
            note_cookie_header_rejected(header, 'GameStop')
            return None

        logger.info(f"GameStop answered the pasted session, {len(response.text)} bytes, "
                    "no browser needed")
        return response.text

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
                self._apply_gamestop_cookies(driver, url)
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

    def _apply_gamestop_cookies(self, driver, url):
        """
        Carry the pasted session into the browser, and reload onto it.

        Worth doing even though this path has its own persistent profile: the
        profile's own clearance is what a headless run never gets and a flagged
        profile stops being given, and the user's is a real one. The reload is
        not optional - cookies added after a page has loaded do not apply to it,
        so without it the page in hand is still the one the edge served before
        they arrived, challenge and all.

        Does nothing when no session is configured, which is the default.
        """
        header = load_gamestop_cookies()
        if not header or cookie_header_is_rejected(header):
            return 0
        applied = apply_cookie_header(
            driver, header, ('.gamestop.com', 'www.gamestop.com', 'gamestop.com'), 'GameStop')
        if applied:
            driver.get(url)
        return applied

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
