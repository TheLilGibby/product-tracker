"""
Standalone checks for detect_store_type() in app/scrapers/__init__.py.

Usage:
    python test_store_detection.py

Pure string handling: no network, no Chrome, no database, no app context.
Exit code is non-zero if any case fails.
"""

import sys

from app.scrapers import (HIDDEN_STORES, STORE_DOMAINS, STORE_LABELS, detect_store_type,
                          get_scraper, store_choices, supported_stores)

# (url, expected store key, note)
CASES = [
    # --- ordinary product URLs, with and without www ---
    ("https://www.amazon.com/dp/B0CV7GF3XY", 'amazon', "www host"),
    ("https://amazon.com/dp/B0CV7GF3XY", 'amazon', "bare apex host"),
    ("https://www.walmart.com/ip/12345", 'walmart', ""),
    ("https://www.newegg.com/p/N82E16819113829", 'newegg', ""),
    ("https://www.microcenter.com/product/123/x", 'microcenter', ""),
    ("https://www.bestbuy.com/site/x/6691852.p", 'bestbuy', ""),
    ("https://www.bhphotovideo.com/c/product/1", 'bh', ""),
    ("https://www.adorama.com/x.html", 'adorama', ""),
    ("https://www.target.com/p/-/A-94300075", 'target', "registered ahead of TargetScraper"),
    ("https://test-store.example.com/product/abc", 'test', "/add-test-product mints these"),

    # --- subdomains are still the same retailer ---
    ("https://smile.amazon.com/dp/B0CV7GF3XY", 'amazon', "real subdomain"),
    ("https://deep.nest.bestbuy.com/x", 'bestbuy', "multi-level subdomain"),

    # --- host normalisation ---
    ("https://WWW.AMAZON.COM/dp/X", 'amazon', "uppercase host"),
    ("https://www.amazon.com./dp/X", 'amazon', "trailing DNS root dot"),
    ("https://www.amazon.com:8443/dp/X", 'amazon', "explicit port"),

    # --- the two bypasses this module exists to close ---
    ("https://amazon.com.evil.example/dp/X", None, "SSRF: suffix-glued lookalike host"),
    ("https://amazon.com@evil.example/dp/X", None, "SSRF: userinfo before the real host"),

    # --- further hostile shapes in the same family ---
    ("https://evil.example/?next=https://www.amazon.com/dp/X", None, "target domain only in the query"),
    ("https://notamazon.com/dp/X", None, "prefix-glued lookalike host"),
    ("https://amazon.com.br/dp/X", None, "different registrable domain"),
    ("https://bestbuy.com.evil.example@127.0.0.1/x", None, "userinfo plus lookalike, resolves to loopback"),
    ("https://user:pw@walmart.com.evil.example/x", None, "userinfo with a password"),
    ("http://169.254.169.254/latest/meta-data/", None, "cloud metadata endpoint"),
    ("http://localhost:5000/admin", None, "loopback by name"),

    # --- inputs that must not raise ---
    (None, None, "None"),
    ("", None, "empty string"),
    ("not a url", None, "unparseable"),
    ("/relative/path", None, "relative path, no host"),
    ("file:///C:/Windows/win.ini", None, "file scheme, no host"),
    ("https://", None, "scheme only"),
    ("https://[oops/x", None, "malformed IPv6 literal (urlparse raises)"),
]


def main():
    failures = 0

    print("detect_store_type:")
    for url, expected, note in CASES:
        try:
            got = detect_store_type(url)
        except Exception as e:  # a bad URL must never propagate to the caller
            got = f"raised {type(e).__name__}: {e}"
        ok = got == expected
        failures += 0 if ok else 1
        shown = url if url is None else (url[:58] + '...' if len(url) > 58 else url)
        print(f"  [{'ok' if ok else 'FAIL'}] {shown!r} -> {got!r}"
              + (f"  ({note})" if note else "")
              + ("" if ok else f"   EXPECTED {expected!r}"))

    # Every registered domain must map to a key get_scraper() can build, otherwise
    # the scheduler picks a store and then dies on the next line. 'target' is the
    # known exception until feature/target-scraper lands.
    print("\nSTORE_DOMAINS keys are constructible by get_scraper:")
    pending = {'target'}
    for store in supported_stores():
        try:
            get_scraper(store)
            state, ok = 'ok', True
        except ValueError:
            state, ok = 'no scraper registered', store in pending
        except Exception as e:
            state, ok = f"{type(e).__name__}: {e}", False
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {store}: {state}"
              + (" (expected, arrives with TargetScraper)" if store in pending and state != 'ok' else ""))

    # A domain registered twice under different keys would make detection order-dependent.
    print("\nSTORE_DOMAINS is unambiguous:")
    dupes = [d for d in STORE_DOMAINS if list(STORE_DOMAINS).count(d) > 1]
    ok = not dupes
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] {len(STORE_DOMAINS)} domains -> {len(supported_stores())} store keys"
          + (f"; duplicates: {dupes}" if dupes else ""))

    # The add-product form is built from store_choices(), so a store registered above
    # has to arrive there labelled, in order, and without the test store tagging along.
    print("\nstore_choices() drives the add-product form:")
    choices = store_choices()
    values = [value for value, _ in choices]
    labels = [label for _, label in choices]
    for desc, ok in (
        ("every offered value is a real store key", set(values) <= set(supported_stores())),
        ("hidden stores are not offered", not set(values) & set(HIDDEN_STORES)),
        ("every visible store is offered", set(values) == set(supported_stores()) - set(HIDDEN_STORES)),
        ("no duplicate values", len(values) == len(set(values))),
        ("every store key has a written label", set(supported_stores()) <= set(STORE_LABELS)),
        ("labels read A to Z", [l.lower() for l in labels] == sorted(l.lower() for l in labels)),
    ):
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {desc}")
    print(f"       offering: {', '.join(labels)}")

    print(f"\n{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
