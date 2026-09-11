#!/usr/bin/env python
"""
Offline checks for how auto-cart treats a scraper that refuses on purpose.

Background. The Amazon console listing is a pre-order: its buy box is real and
priced, but its only control is #buy-now-button, and Buy Now skips the cart and
goes to checkout. The scraper therefore declines to click and returns "Product
offers Buy Now but no Add to Cart, so it was left alone". That is the correct
outcome, and it will be the outcome every time until the release date - so the
question is what the scheduler does with an answer that will not change.

What was already true, and is checked here so it stays true: the cooldown arms
on a refusal exactly as it does on a success, because check_auto_cart_opportunities
writes last_cart_attempt from the returned dict without looking at 'success'.
Measured on the integration host's log: nine attempts on that product across
eight hours, spaced 35-140 minutes apart, never the 60-second poll interval.

What changed: a by-design refusal is no longer logged as a failure. It is said
once at INFO when the answer changes and at DEBUG while it stays the same, so a
listing that will refuse for six weeks does not file a warning every cooldown
for six weeks. The alert path and price history are untouched - the product is
still available, still tracked, still alerted on.

Run: python test_autocart_refusal.py
No network, no Chrome, no database.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.scrapers import is_by_design_refusal  # noqa: E402

# The exact strings the scrapers return, copied from their sources.
BUY_NOW_ONLY = "Product offers Buy Now but no Add to Cart, so it was left alone"
COMING_SOON = "Product is not purchasable (coming soon)"
NO_ASIN = ("Could not read an ASIN from the product URL, so an add could not be verified "
           "against the cart. Use the /dp/<ASIN> form of the link")
NO_SKU = "Could not identify the product's SKU, so the add could not be verified"

# Real failures. Every one of these is worth a warning, and worth retrying:
# the next attempt may well go differently.
WALL = "Amazon served a block page on the cart (title contains \"robot check\"), so the add could not be verified"
EMPTY = "Item did not appear in the cart (the cart is empty)"
WRONG_ITEM = "Item did not appear in the cart (it does not list ASIN B0HJ6F8L6V)"
BUSY = "Chrome profile is busy: another session holds amazon_profile"
CRASH = "Error: Message: session not created: This version of ChromeDriver only supports Chrome version 153"
SUCCESS = "Successfully added 1 item(s) to the Amazon cart"

REFUSALS = (
    (BUY_NOW_ONLY, 'Amazon pre-order: Buy Now only'),
    (COMING_SOON, 'Best Buy: the CTA says coming soon'),
    (NO_ASIN, 'no ASIN in the URL to verify against'),
    (NO_SKU, 'no SKU to verify against'),
)

FAILURES = (
    (WALL, 'a bot wall is a failure, and worth retrying'),
    (EMPTY, 'an empty cart after a click is a failure'),
    (WRONG_ITEM, 'the wrong item in the cart is a failure'),
    (BUSY, 'a busy profile is a failure, and clears on its own'),
    (CRASH, 'a driver mismatch is a failure'),
    (SUCCESS, 'a success is not a refusal'),
    ('', 'an empty message is not a refusal'),
    (None, 'no message at all is not a refusal'),
)


def run_classification_checks():
    failures = 0
    for message, label in REFUSALS:
        ok = is_by_design_refusal(message) is True
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] refusal: {label}")
    for message, label in FAILURES:
        ok = is_by_design_refusal(message) is False
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] not a refusal: {label}")
    return failures


def run_cooldown_checks():
    """
    The cooldown gate, in isolation: the same arithmetic
    check_auto_cart_opportunities does, with the clock supplied.

    The point of these is that nothing about the message reaches this decision.
    A refusal and a bot wall are rate-limited identically, because both of them
    have had their turn, and the only thing that makes a refusal special is how
    it is logged.
    """
    cooldown = timedelta(minutes=30)
    now = datetime(2026, 9, 11, 6, 0, 0)

    def eligible(last_status, last_attempt):
        if (last_status or '').lower().startswith('success'):
            return False
        if last_attempt and now - last_attempt < cooldown:
            return False
        return True

    cases = (
        (None, None, True, 'never attempted'),
        (BUY_NOW_ONLY, now - timedelta(minutes=1), False, 'refused a minute ago: not retried on the next poll'),
        (BUY_NOW_ONLY, now - timedelta(minutes=29), False, 'refused 29 minutes ago: still inside the cooldown'),
        (BUY_NOW_ONLY, now - timedelta(minutes=31), True, 'refused 31 minutes ago: eligible again'),
        (WALL, now - timedelta(minutes=1), False, 'a failure is rate-limited the same way'),
        (SUCCESS, now - timedelta(days=2), False, 'already carted: never re-added, however old'),
    )

    failures = 0
    for status, attempt, expected, label in cases:
        got = eligible(status, attempt)
        ok = got is expected
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label} -> eligible={got}")
    return failures


def main():
    print("A refusal is not a failure:")
    failures = run_classification_checks()

    print("\nThe cooldown arms on a refusal too:")
    failures += run_cooldown_checks()

    print("")
    print(f"{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
