"""
Nintendo Store scraper.

The official store server-renders schema.org JSON-LD with name, price,
availability, and image, so a plain HTTP fetch is enough.
"""

import logging
import requests
from bs4 import BeautifulSoup
from app.scrapers.common import (
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    detect_block_page,
    first_json_ld_product,
    json_ld_available,
    json_ld_image,
    json_ld_price,
)

logger = logging.getLogger('app.scrapers.nintendo')


class NintendoScraper:
    """Scraper for nintendo.com store product pages."""

    def __init__(self):
        logger.debug("Initializing NintendoScraper")
        self.headers = dict(DEFAULT_HEADERS)

    def scrape_product(self, url):
        """
        Scrape product information from a Nintendo Store URL.

        Returns:
            dict with name/price/available/image_url, or None on failure.
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            # Nintendo serves UTF-8; without this, requests can treat it as
            # latin-1 and turn ™ / en-dashes into replacement characters.
            response.encoding = 'utf-8'
            html = response.text
            blocked = detect_block_page(html)
            if blocked:
                logger.warning(f"Nintendo page looks blocked ({blocked})")
                return None
            return self.extract_from_html(html)
        except Exception as e:
            logger.error(f"Error scraping Nintendo product: {str(e)}")
            return None

    def extract_from_html(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        product = first_json_ld_product(soup)
        if not product:
            logger.warning("No JSON-LD Product on Nintendo page")
            return None

        name = (product.get('name') or '').strip()
        if not name:
            logger.warning("Nintendo JSON-LD Product has no name")
            return None

        available = json_ld_available(product)
        if available is None:
            page_text = soup.get_text(' ', strip=True).lower()
            available = not any(token in page_text for token in (
                'out of stock', 'sold out', 'currently unavailable',
            ))

        data = {
            'name': name,
            'price': json_ld_price(product),
            'available': bool(available),
            'image_url': json_ld_image(product),
        }
        logger.info(
            f"Nintendo extracted: name={data['name']}, price={data['price']}, "
            f"available={data['available']}"
        )
        return data
