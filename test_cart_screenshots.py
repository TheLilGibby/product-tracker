"""
Check the cart-attempt screenshot store.

    python test_cart_screenshots.py

Touches no network, no retailer and no database: it builds a bare Flask app on a
temporary instance folder, so the tracker's own instance/ is never written to.
"""
import base64
import os
import shutil
import sys
import tempfile

from flask import Flask

from app.cart_screenshots import (FILENAME_RE, KEEP_PER_PRODUCT, SCREENSHOT_DIR,
                                  save_cart_screenshot, screenshot_path)

# The smallest valid PNG: an 1x1 image. The module does not parse the image, but
# using a real one keeps the round-trip honest.
PNG = base64.b64decode(
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
PNG_B64 = base64.b64encode(PNG).decode()

failures = []


def check(condition, description):
    print(f"  {'ok  ' if condition else 'FAIL'}  {description}")
    if not condition:
        failures.append(description)


def main():
    instance = tempfile.mkdtemp(prefix='cart-screenshots-')
    app = Flask(__name__, instance_path=instance)
    directory = os.path.join(instance, SCREENSHOT_DIR)
    try:
        with app.app_context():
            print("A screenshot is written and comes back byte for byte")
            name = save_cart_screenshot(8, PNG_B64)
            check(bool(name), "save_cart_screenshot returns a name")
            check(bool(name and FILENAME_RE.match(name)), f"the name is the expected shape: {name}")
            path = screenshot_path(name)
            check(bool(path), "screenshot_path finds the file it just wrote")
            if path:
                with open(path, 'rb') as handle:
                    check(handle.read() == PNG, "the file holds exactly the bytes that went in")

            print("Nothing usable means None, never an exception")
            check(save_cart_screenshot(8, None) is None, "no screenshot at all -> None")
            check(save_cart_screenshot(8, '') is None, "an empty string -> None")
            check(save_cart_screenshot(8, 'not base64 !!') is None, "junk that is not base64 -> None")
            check(save_cart_screenshot(8, base64.b64encode(b'').decode()) is None,
                  "base64 of nothing -> None")

            print("A name out of the database is checked, not trusted")
            for bad in (None, '', 'cart_8.png', '../../.env', 'cart_8_20260911T120000_000000.png.exe',
                        os.path.join('..', 'product_tracker.db'), '/etc/passwd',
                        'cart_8_20260911T120000_000000.png/../../secrets'):
                check(screenshot_path(bad) is None, f"refused: {bad!r}")

            print("Saves in the same clock tick still get their own file")
            # The timestamp alone was not enough: %f is microseconds but the
            # clock ticks about once a millisecond, and a burst this size
            # collided twice before the name carried a token. Kept deliberately
            # tight so it would fail again if the token ever went away.
            burst = [save_cart_screenshot(9, PNG_B64) for _ in range(60)]
            check(all(burst), "all 60 saves in a burst returned a name")
            check(len(set(burst)) == len(burst),
                  f"all 60 names are distinct (got {len(set(burst))})")

            print("Old attempts are pruned, and only this product's")
            other = save_cart_screenshot(80, PNG_B64)
            kept = [save_cart_screenshot(8, PNG_B64) for _ in range(KEEP_PER_PRODUCT + 3)]
            mine = sorted(n for n in os.listdir(directory) if n.startswith('cart_8_'))
            check(len(mine) == KEEP_PER_PRODUCT,
                  f"product 8 keeps {KEEP_PER_PRODUCT} of its {KEEP_PER_PRODUCT + 4} screenshots "
                  f"(has {len(mine)})")
            check(len(set(kept)) == len(kept), "each of those saves got its own filename")
            check(mine == sorted(kept[-KEEP_PER_PRODUCT:]), "the ones kept are the newest")
            check(screenshot_path(kept[-1]) is not None, "the newest is still servable")
            check(bool(other) and os.path.isfile(os.path.join(directory, other)),
                  "product 80's screenshot was left alone by product 8's pruning")
    finally:
        shutil.rmtree(instance, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for description in failures:
            print(f"  - {description}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
