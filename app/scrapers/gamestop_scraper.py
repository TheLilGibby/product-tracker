"""
GameStop scraper.

JSON-LD has name/price/image, but GameStop's Offer.availability is often
InStock even when the buy box says Not Available and the add-to-cart button
is disabled. HTTP requests are frequently 403'd by Akamai, so a Chrome
fallback reads the rendered page the same way Target does.
"""

import logging
import os
import time
from contextlib import ExitStack
import requests
from bs4 import BeautifulSoup
from app.scrapers.common import (
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    detect_block_page,
    detect_chrome_major,
    first_json_ld_product,
    json_ld_image,
    json_ld_price,
    profile_lock,
    ProfileBusyError,
)

logger = logging.getLogger('app.scrapers.gamestop')


class GameStopScraper:
    """Scraper for GameStop product pages: HTTP first, Chrome if Akamai blocks."""

    def __init__(self):
        logger.debug("Initializing GameStopScraper")
        self.headers = dict(DEFAULT_HEADERS)
        self.headers['Referer'] = 'https://www.gamestop.com/'
        self.profile_dir = os.path.join(os.path.expanduser("~"), ".chrome_profiles", "gamestop_profile")
        os.makedirs(self.profile_dir, exist_ok=True)
        self.current_product_url = None

    @staticmethod
    def image_url_from_url(url):
        """GameStop CDN ids are not the URL SKU, so there is no URL-only fallback."""
        return None

    def scrape_product(self, url):
        """
        Scrape product information from a GameStop URL.

        Returns:
            dict with name/price/available/image_url, or None on failure.
        """
        self.current_product_url = url
        try:
            product_data = self.scrape_via_requests(url)
            if product_data:
                return product_data
        except Exception as e:
            logger.error(f"Error scraping GameStop via HTTP: {str(e)}")

        try:
            product_data = self.scrape_via_browser(url)
            if product_data:
                return product_data
        except Exception as e:
            logger.error(f"Error scraping GameStop via browser: {str(e)}")

        logger.warning(f"Could not extract GameStop product data for {url}")
        return None

    def scrape_via_requests(self, url):
        response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            logger.warning(f"GameStop HTTP {response.status_code} for {url}")
            return None
        blocked = detect_block_page(response.text)
        if blocked:
            logger.warning(f"GameStop page looks blocked ({blocked})")
            return None
        return self.extract_from_html(response.text)

    def scrape_via_browser(self, url):
        import undetected_chromedriver as uc
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        logger.info(f"Attempting browser scrape for GameStop {url}")
        driver = None
        html = None
        profile = ExitStack()
        try:
            profile.enter_context(profile_lock(self.profile_dir, timeout=60))
        except ProfileBusyError as e:
            logger.warning(f"Skipping GameStop browser scrape: {str(e)}")
            return None
        try:
            options = uc.ChromeOptions()
            options.add_argument(f'--user-data-dir={self.profile_dir}')
            options.add_argument('--profile-directory=Default')
            options.add_argument('--headless=new')
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--no-sandbox')
            options.add_argument('--window-size=1366,768')
            try:
                driver = uc.Chrome(options=options, version_main=detect_chrome_major())
            except WebDriverException as e:
                logger.warning(f"Could not start Chrome for GameStop: {str(e)}")
                return None
            driver.set_page_load_timeout(45)
            driver.get(url)
            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        '.product-detail, script[type="application/ld+json"]',
                    ))
                )
            except TimeoutException:
                logger.warning("Timed out waiting for GameStop markup; parsing whatever rendered")
            time.sleep(3)
            html = driver.page_source
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            profile.close()

        if not html:
            return None
        return self.extract_from_html(html)

    def extract_from_html(self, html):
        blocked = detect_block_page(html)
        if blocked:
            logger.warning(f"GameStop HTML looks blocked ({blocked})")
            return None
        soup = BeautifulSoup(html, 'html.parser')
        product = first_json_ld_product(soup)
        name = ((product or {}).get('name') or '').strip()
        if not name:
            h1 = soup.find('h1')
            name = h1.get_text(strip=True) if h1 else ''
        if not name:
            logger.warning("Could not find GameStop product name")
            return None

        price = json_ld_price(product) if product else None
        if price is None:
            price_el = soup.select_one('.sales .value, .price .sales, [class*="sales"] .value')
            if price_el:
                import re
                match = re.search(r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', price_el.get_text())
                if match:
                    price = float(match.group(0).replace(',', ''))

        available = self.extract_availability(soup)
        image_url = json_ld_image(product) if product else None
        if not image_url:
            meta = soup.find('meta', {'property': 'og:image'})
            if meta and meta.get('content'):
                image_url = meta['content'].strip()

        data = {
            'name': name,
            'price': price,
            'available': available,
            'image_url': image_url,
        }
        logger.info(
            f"GameStop extracted: name={data['name']}, price={data['price']}, "
            f"available={data['available']}"
        )
        return data

    def extract_availability(self, soup):
        """Prefer the buy box over JSON-LD, which often claims InStock when it is not."""
        try:
            box = soup.select_one('.availability.product-availability, .product-availability')
            if box is not None and box.has_attr('data-available'):
                flag = (box.get('data-available') or '').strip().lower()
                if flag in ('true', 'false'):
                    logger.debug(f"GameStop data-available={flag}")
                    return flag == 'true'

            btn = soup.select_one('button.js-add-to-cart, button.add-to-cart, .js-add-to-cart')
            if btn:
                classes = ' '.join(btn.get('class') or []).lower()
                if btn.has_attr('disabled') or 'disabled' in classes:
                    logger.debug("GameStop add-to-cart is disabled")
                    return False
                return True

            text = soup.get_text(' ', strip=True).lower()
            if 'not available' in text or 'out of stock' in text or 'sold out' in text:
                logger.debug("GameStop page text says not available")
                return False
            return False
        except Exception as e:
            logger.error(f"Error extracting GameStop availability: {str(e)}")
            return False
