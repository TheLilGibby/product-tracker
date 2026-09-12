"""
Keep the screenshot a cart attempt took, so a failure can be looked at afterwards.

Scrapers already grab one while the driver is still alive and return it inline as
base64 (the cart result contract in app/scrapers/__init__.py). Until now that
image only ever survived as far as the redirect after a manual "Add to Cart"
click, and auto-cart dropped it on the floor entirely - which is precisely the
attempt nobody is sitting there watching. A Nintendo pre-purchase click that ends
with an empty cart leaves nothing to look at but one line of status text.

This writes the image to a file and hands back the name to store on the product.
Files live in instance/cart_screenshots: instance/ is already gitignored, and it
is outside the static route, so these are reachable only through the product's
own screenshot endpoint and never by guessing a URL under /static.

Nothing here is allowed to fail a cart attempt. A screenshot that cannot be
decoded or written is logged and becomes None; the add itself is unaffected.
"""
import base64
import binascii
import logging
import os
import re
import uuid
from datetime import datetime

from flask import current_app

logger = logging.getLogger(__name__)

# Under app.instance_path
SCREENSHOT_DIR = 'cart_screenshots'

# Attempts kept per product. Enough to compare a failure against the one before
# it without letting a product that retries every cooldown grow without end.
KEEP_PER_PRODUCT = 5

# The exact shape save_cart_screenshot() writes, and the only shape
# screenshot_path() will serve back. The timestamp leads so the names sort
# chronologically as text, which is what _prune() relies on.
#
# The trailing token is not decoration. %f says microseconds but the clock
# behind it ticks about once a millisecond on Windows, so two saves in the same
# tick produced the same name and the second quietly overwrote the first -
# measured here at 2 collisions in 60 back-to-back saves. Real attempts are
# minutes apart and would never have hit it, which is precisely why it would
# have sat there unnoticed. The token makes the name unique by construction
# instead of by luck.
TOKEN_LENGTH = 6
NAME_FORMAT = 'cart_{product_id}_{stamp}_{token}.png'
STAMP_FORMAT = '%Y%m%dT%H%M%S_%f'
FILENAME_RE = re.compile(r'^cart_\d+_\d{8}T\d{6}_\d{6}_[0-9a-f]{%d}\.png$' % TOKEN_LENGTH)


def screenshot_dir():
    """Absolute path of the screenshot directory, created if it is not there yet."""
    path = os.path.join(current_app.instance_path, SCREENSHOT_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def save_cart_screenshot(product_id, screenshot):
    """
    Write one cart attempt's screenshot; return its filename, or None.

    `screenshot` is the base64 string off a cart result. None is ordinary and
    not an error - the HTTP-only refusal paths never launch a browser, so they
    have no image to give.
    """
    if not screenshot:
        return None
    try:
        image = base64.b64decode(screenshot, validate=True)
    except (binascii.Error, ValueError) as e:
        logger.debug(f"Cart screenshot for product {product_id} was not valid base64: {e}")
        return None
    if not image:
        return None

    name = NAME_FORMAT.format(product_id=product_id,
                              stamp=datetime.utcnow().strftime(STAMP_FORMAT),
                              token=uuid.uuid4().hex[:TOKEN_LENGTH])
    try:
        directory = screenshot_dir()
        with open(os.path.join(directory, name), 'wb') as handle:
            handle.write(image)
    except Exception as e:
        logger.warning(f"Could not save the cart screenshot for product {product_id}: {e}")
        return None

    logger.info(f"Saved the cart attempt screenshot for product {product_id} as {name}")
    _prune(directory, product_id)
    return name


def screenshot_path(name):
    """
    Absolute path of a stored screenshot, or None when there is nothing to serve.

    The name arrives from the database, so it is checked rather than trusted:
    only the exact filename shape this module writes is accepted, which leaves
    no room for a separator, a traversal or an absolute path.
    """
    if not name or not FILENAME_RE.match(name):
        return None
    path = os.path.join(current_app.instance_path, SCREENSHOT_DIR, name)
    return path if os.path.isfile(path) else None


def _prune(directory, product_id):
    """Drop all but the newest KEEP_PER_PRODUCT screenshots for this product."""
    # cart_8_ cannot match cart_80_..., so one product never prunes another's.
    prefix = f'cart_{product_id}_'
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith(prefix) and name.endswith('.png'))
    except OSError as e:
        logger.debug(f"Could not list {directory} to prune screenshots: {e}")
        return
    for name in names[:-KEEP_PER_PRODUCT]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError as e:
            logger.debug(f"Could not remove the old cart screenshot {name}: {e}")
