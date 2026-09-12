"""
Shared helpers for the requests-based scraper paths: default headers, the
request timeout, bot-wall detection and pre-order text matching.
"""

import json
import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager

try:                      # Windows
    import msvcrt
    fcntl = None
except ImportError:       # POSIX
    msvcrt = None
    import fcntl

# Set up logging
logger = logging.getLogger('app.scrapers.common')

# Seconds to wait for a retailer response. Without a timeout a hung request
# holds check_lock in app/tasks.py for the rest of the process lifetime.
REQUEST_TIMEOUT = 20

# Headers that look like a current desktop Chrome. Retailers serve stripped or
# blocked pages to the ancient Chrome/91 UA the scrapers used to send.
CHROME_VERSION = '130'
DEFAULT_HEADERS = {
    'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Phrases that appear in the <title> of bot walls / access-denied interstitials
BLOCK_PAGE_TITLE_MARKERS = (
    'robot or human',      # Walmart
    'access denied',       # Akamai (Best Buy, others)
    'robot check',         # Amazon
    'px-captcha',
    'automated access',
    'attention required',  # Cloudflare ("Attention Required! | Cloudflare")
    'just a moment',       # Cloudflare interstitial while its JS challenge runs
)

# Phrases specific enough to trust anywhere in the body. "access denied" is
# deliberately not here: it shows up in inline JS on perfectly good pages, and
# the bare string "px-captcha" appears in PerimeterX CSS (#px-captcha-modal) on
# every Target page - only the rendered challenge element counts.
BLOCK_PAGE_BODY_MARKERS = (
    'robot or human',
    'id="px-captcha"',     # PerimeterX challenge container
    "id='px-captcha'",
    'automated access',    # Amazon "To discuss automated access to Amazon data..."
    'robot check',
    # Cloudflare's block page container. Note there is deliberately NO marker for
    # /cdn-cgi/challenge-platform/ here: that script is served on every page behind
    # Cloudflare, healthy ones included, and matching it flagged all three real
    # GameStop product pages as blocked. The <title> markers catch the actual wall.
    'cf-error-details',
)

# Any of these means we are looking at a real product page, so a small body is
# not by itself a sign of a block page
PRODUCT_PAGE_MARKERS = (
    'og:title',
    'application/ld+json',
    'add to cart',
    'add-to-cart',
    'itemprop="price"',
    'producttitle',
)

# Bodies smaller than this with no product markers are almost always a challenge page
MIN_PRODUCT_PAGE_BYTES = 5000

# Only the head and start of the body are needed; keeps the scan cheap on big pages
_SCAN_LIMIT = 200000

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_PREORDER_RE = re.compile(r'pre[\s-]?order', re.IGNORECASE)


def detect_block_page(html, expect_product=True):
    """
    Decide whether a fetched page is a bot wall rather than a product page.

    Args:
        html: Raw response body
        expect_product: True when the page being checked should be a product
            page. Pass False for a page that is legitimately small and mentions
            no product - a cart page with nothing in it is exactly that - so the
            size heuristic below does not report it as a wall. Everything else
            still applies: a cart served as a challenge interstitial is still
            caught by the title and body markers, which is the whole reason to
            keep calling this on the cart at all.

    Returns:
        A short reason string if the page looks blocked, otherwise None
    """
    if not html:
        return 'empty response'

    sample = html[:_SCAN_LIMIT].lower()

    title_match = _TITLE_RE.search(sample)
    title = title_match.group(1).strip() if title_match else ''
    for marker in BLOCK_PAGE_TITLE_MARKERS:
        if marker in title:
            return f'title contains "{marker}"'

    for marker in BLOCK_PAGE_BODY_MARKERS:
        if marker in sample:
            return f'body contains "{marker}"'

    # Only meaningful when a product was expected. On a cart page this rule
    # fires on a perfectly good empty cart, and the trade is deliberate: with
    # expect_product=False a wall that is BOTH tiny AND carries none of the
    # markers above now reads as "nothing in the cart" instead. The action is
    # the same either way (the add failed, do not go on), while the false wall
    # actively sends you to fix the wrong thing.
    if expect_product and len(html) < MIN_PRODUCT_PAGE_BYTES \
            and not any(marker in sample for marker in PRODUCT_PAGE_MARKERS):
        return f'body is {len(html)} bytes with no product markers'

    return None


def is_preorder_text(text):
    """True if the text says the item is a pre-order ("pre-order", "preorder" or "pre order")"""
    return bool(text) and bool(_PREORDER_RE.search(text))


def detect_chrome_major():
    """
    Major version of the installed Chrome, for undetected-chromedriver's
    `version_main`. Without it uc downloads the newest driver, which refuses to
    start against an older browser ("This version of ChromeDriver only supports
    Chrome version N"). CHROME_MAJOR_VERSION in the environment overrides
    detection; None lets undetected-chromedriver decide.
    """
    override = os.environ.get('CHROME_MAJOR_VERSION', '')
    if override.isdigit():
        return int(override)

    for exe in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        try:
            output = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r'(\d+)\.\d+\.\d+', output or '')
        if match:
            return int(match.group(1))

    # Windows installs keep a <version> directory next to chrome.exe
    try:
        import undetected_chromedriver as uc
        exe = uc.find_chrome_executable()
        if exe:
            for entry in os.listdir(os.path.dirname(exe)):
                match = re.fullmatch(r'(\d+)\.\d+\.\d+\.\d+', entry)
                if match:
                    return int(match.group(1))
    except Exception as e:
        logger.debug(f"Could not detect Chrome version from install directory: {str(e)}")
    return None


# --------------------------------------------------------------- chrome profile
# Chrome refuses to run two instances against one --user-data-dir. When the
# background scheduler is mid-scrape and a user clicks auto-cart (or two
# scrapers overlap), the loser dies at launch with "session not created:
# ... from chrome not reachable" long before any page loads. That is easily
# misread as a bot wall, so the paths that share a profile serialize here.
PROFILE_LOCK_TIMEOUT = 60
PROFILE_LOCK_POLL = 0.5


class ProfileBusyError(RuntimeError):
    """Raised when another process holds the Chrome profile for too long."""


@contextmanager
def profile_lock(profile_dir, timeout=PROFILE_LOCK_TIMEOUT):
    """
    Hold an exclusive cross-process lock on a Chrome user-data-dir.

    Args:
        profile_dir: the --user-data-dir being shared
        timeout: seconds to wait for the current holder to finish. Pass None to
            wait indefinitely, which the interactive --login helper does.

    Raises:
        ProfileBusyError: if the lock is still held after `timeout`
    """
    # Beside the profile, not inside it: Chrome rewrites its own directory, and
    # every process sharing the profile must agree on one lock path.
    profile_dir = os.path.abspath(profile_dir)
    os.makedirs(os.path.dirname(profile_dir), exist_ok=True)
    lock_path = profile_dir + '.lock'
    handle = open(lock_path, 'a+b')
    deadline = None if timeout is None else time.monotonic() + timeout
    waited = False
    try:
        while True:
            try:
                _lock_exclusive(handle)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ProfileBusyError(
                        f"another process has held the Chrome profile {profile_dir} for over "
                        f"{timeout}s; retry once the running check finishes")
                waited = True
                time.sleep(PROFILE_LOCK_POLL)
        if waited:
            logger.info(f"Waited for another process to release the Chrome profile {profile_dir}")
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


def _lock_exclusive(handle):
    """Take a non-blocking exclusive lock, raising OSError if it is already held."""
    if msvcrt is not None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        logger.debug(f"Could not release the Chrome profile lock: {str(e)}")


# ---------------------------------------------------------------- cookie import
# A retailer can flag a Chrome profile so hard that its press-and-hold challenge
# loops forever - webdriver-controlled Chrome rarely clears one once flagged. The
# way out is not to solve the challenge but to carry the session the user already
# has: they sign in and verify in their ordinary browser, export that site's
# cookies, and we load them into the persistent profile. Nothing here defeats or
# automates a challenge; it moves a user's own session between their own profiles.

def parse_cookie_file(path, domain_suffix, now=None):
    """
    Read cookies for one site from a Netscape cookies.txt or a JSON export.

    Netscape format is what the "Get cookies.txt LOCALLY" extension writes: seven
    tab-separated fields, `# ` comments, and a `#HttpOnly_` prefix on the domain of
    http-only entries. The JSON form is an array of
    {name, value, domain, path, expires, secure, httpOnly}, which is what most
    other exporters produce.

    Args:
        path: the exported cookie file
        domain_suffix: keep only cookies for this site, e.g. 'target.com'
        now: epoch seconds used to drop expired cookies (for tests)

    Returns:
        a list of dicts shaped for selenium's driver.add_cookie()

    Raises:
        ValueError: if the file parses to no usable cookie for the domain
    """
    now = time.time() if now is None else now
    raw = open(path, 'r', encoding='utf-8-sig', errors='replace').read().strip()
    if not raw:
        raise ValueError(f"{path} is empty")

    entries = _parse_json_cookies(raw) if raw[0] in '[{' else _parse_netscape_cookies(raw)

    cookies, skipped_domain, skipped_expired = [], 0, 0
    for entry in entries:
        domain = (entry.get('domain') or '').lstrip('.')
        if not (domain == domain_suffix or domain.endswith('.' + domain_suffix)):
            skipped_domain += 1
            continue
        expires = entry.get('expires')
        # 0/None is a session cookie, which has no expiry to check
        if expires and float(expires) <= now:
            skipped_expired += 1
            continue
        cookie = {
            'name': entry['name'],
            'value': entry.get('value') or '',
            # Keep the leading dot off: Chrome infers the host-only flag, and a
            # dotted domain is rejected by some chromedriver versions.
            'domain': domain,
            'path': entry.get('path') or '/',
            'secure': bool(entry.get('secure')),
        }
        if expires:
            cookie['expiry'] = int(float(expires))
        if entry.get('httpOnly'):
            cookie['httpOnly'] = True
        cookies.append(cookie)

    logger.debug(f"Parsed {len(cookies)} cookies for {domain_suffix} from {path} "
                 f"({skipped_domain} other domains, {skipped_expired} expired)")
    if not cookies:
        raise ValueError(
            f"{path} holds no unexpired cookies for {domain_suffix} "
            f"({skipped_domain} were for other domains, {skipped_expired} had expired). "
            "Export again while signed in to that site.")
    return cookies


def _parse_netscape_cookies(raw):
    """Yield raw cookie dicts from Netscape cookies.txt text"""
    entries = []
    for line in raw.splitlines():
        line = line.rstrip('\n')
        http_only = False
        if line.startswith('#HttpOnly_'):
            http_only = True
            line = line[len('#HttpOnly_'):]
        elif line.startswith('#') or not line.strip():
            continue
        fields = line.split('\t')
        if len(fields) < 7:
            # Some exporters pad with spaces instead of tabs
            fields = line.split()
            if len(fields) < 7:
                continue
        domain, _include_sub, path, secure, expires, name, value = fields[:7]
        entries.append({
            'domain': domain,
            'path': path,
            'secure': secure.upper() == 'TRUE',
            'expires': _as_epoch(expires),
            'name': name,
            'value': value,
            'httpOnly': http_only,
        })
    return entries


def _parse_json_cookies(raw):
    """Yield raw cookie dicts from a JSON cookie export"""
    data = json.loads(raw)
    if isinstance(data, dict):
        # Some tools wrap the array, e.g. {"cookies": [...]}
        for key in ('cookies', 'Cookies', 'data'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError("JSON cookie file is an object with no cookie array in it")
    entries = []
    for item in data:
        if not isinstance(item, dict) or not item.get('name'):
            continue
        entries.append({
            'domain': item.get('domain') or item.get('Domain') or '',
            'path': item.get('path') or '/',
            'secure': bool(item.get('secure')),
            # expirationDate is what the Chrome extension APIs call it
            'expires': _as_epoch(item.get('expires', item.get('expirationDate'))),
            'name': item['name'],
            'value': item.get('value', ''),
            'httpOnly': bool(item.get('httpOnly')),
        })
    return entries


def _as_epoch(value):
    """Cookie expiries arrive as ints, float strings or empty; normalise to a number or None"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def import_cookies_txt(driver, path, domain_suffix):
    """
    Load a user's exported cookies for one site into the driver's profile.

    The driver must already have that site loaded: add_cookie() writes into the
    current document's cookie store and rejects anything for another domain.

    Returns:
        (added, rejected) counts. Individual rejections are logged, not raised -
        retailers ship cookies chromedriver dislikes, and the session ones that
        matter usually still land.
    """
    cookies = parse_cookie_file(path, domain_suffix)
    added = rejected = 0
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
            added += 1
        except Exception as e:
            rejected += 1
            logger.debug(f"Chrome rejected the cookie {cookie['name']}: {str(e)}")
    logger.info(f"Imported {added}/{len(cookies)} {domain_suffix} cookies from {path}")
    return added, rejected


# ------------------------------------------------------------- cookie jar cache
# Importing cookies fixes the BROWSER path, but the browser is the expensive part:
# a Chrome launch per check, a profile lock held for its duration, and on Target a
# fresh chance of a press-and-hold every time. The same session works over plain
# HTTP - Redsky answers a client that carries a valid _px3 - so the import also
# writes the cookies to a small JSON cache that the requests path can load. When
# the cache is good, tracking never opens a browser at all and the profile is only
# touched for cart attempts.
#
# The file is a live login. It is written 0600, callers put it outside the repo
# (beside the Chrome profile whose session it is), and nothing here logs a cookie
# value.

# Cookies whose absence means the cheap path will not work. _px3 is the PerimeterX
# clearance token: without it Redsky answers 403 with a captchaRelativeURL.
CRITICAL_COOKIE_NAMES = ('_px3',)


def save_cookie_jar(path, cookies, critical_names=CRITICAL_COOKIE_NAMES):
    """
    Persist cookies for reuse by a requests-based path.

    Args:
        path: destination JSON file; parent directories are created
        cookies: selenium-shaped cookie dicts (driver.get_cookies())
        critical_names: names to report on, so the caller can tell the user the
                        cheap path will not work before they delete their export

    Returns:
        (written, missing_critical) - the number of cookies stored and the tuple
        of critical names that were not among them.
    """
    stored = []
    for cookie in cookies or []:
        name = cookie.get('name')
        if not name:
            continue
        stored.append({
            'name': name,
            'value': cookie.get('value') or '',
            # Kept verbatim, leading dot and all. parse_cookie_file strips the
            # dot because chromedriver dislikes it, but http.cookiejar needs it:
            # ".target.com" is sent to redsky.target.com while a dotless
            # "target.com" is host-only and would strand _px3 on the wrong host.
            'domain': cookie.get('domain') or '',
            'path': cookie.get('path') or '/',
            'expires': _as_epoch(cookie.get('expiry') if 'expiry' in cookie else cookie.get('expires')),
            'secure': bool(cookie.get('secure')),
            'httpOnly': bool(cookie.get('httpOnly')),
        })

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    payload = {'saved_at': int(time.time()), 'stale': False, 'cookies': stored}
    # Create it 0600 before anything is written, so the session is never briefly
    # world-readable. os.open's mode is ignored on Windows, where the file
    # inherits the directory's ACL instead - the chmod below is the portable half.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug(f"Could not chmod {path}: {str(e)}")

    missing = tuple(name for name in critical_names
                    if not any(c['name'] == name for c in stored))
    logger.info(f"Saved {len(stored)} cookies to {path}"
                + (f" (missing {', '.join(missing)})" if missing else ""))
    return len(stored), missing


def load_cookie_jar(path, now=None):
    """
    Read back a saved cookie jar, dropping anything that has expired.

    Returns:
        a list of cookie dicts, or None when there is nothing usable - the file
        is absent, unreadable, marked stale, or every cookie in it has expired.
        None is the signal to use the browser path; it is never an error.
    """
    now = time.time() if now is None else now
    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except FileNotFoundError:
        logger.debug(f"No cookie jar at {path}")
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read the cookie jar at {path}: {str(e)}")
        return None

    if payload.get('stale'):
        logger.debug(f"Cookie jar at {path} is marked stale; using the browser path")
        return None

    live, expired = [], 0
    for cookie in payload.get('cookies') or []:
        expires = _as_epoch(cookie.get('expires'))
        if expires and expires <= now:
            expired += 1
            continue
        live.append(cookie)

    if not live:
        logger.info(f"Every cookie in {path} has expired; re-import to restore the cheap path")
        return None
    logger.debug(f"Loaded {len(live)} cookies from {path} ({expired} expired)")
    return live


def mark_cookie_jar_stale(path):
    """
    Flag a saved jar as no longer working, so the next check goes straight to the
    browser instead of spending a request on a cookie the retailer has retired.

    The cookies are flagged rather than deleted: replacing them costs the user a
    manual export, and a jar that failed once is worth being able to look at. A
    fresh import overwrites the file and clears the flag.

    Returns:
        True if the file was flagged, False if there was nothing to flag (already
        stale, missing, or unreadable) - so the caller can log it exactly once.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return False
    if payload.get('stale'):
        return False

    payload['stale'] = True
    payload['stale_at'] = int(time.time())
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning(f"Could not mark the cookie jar at {path} stale: {str(e)}")
        return False
    logger.warning(f"Marked the cookie jar at {path} stale; re-import to restore the cheap path")
    return True


# --------------------------------------------------------- pasted cookie headers
# The other half of the cookie story above. A jar is exported from the browser as
# a file; a header is the one line a person can copy out of DevTools ("Copy value"
# on the Cookie request header) and paste into the settings page, which is the only
# route that works for someone who will not install a cookie-export extension.
#
# Same principle either way, and it is worth restating because it is what keeps
# this on the right side of the line: nothing here solves, weakens or automates a
# challenge. The user passes a challenge themselves, in their own browser, and
# hands us the session it produced.

def cookie_setting(name, default=''):
    """
    Read a cookie-header setting, from the environment or the Flask config.

    The environment wins because that is what the settings page writes when it
    saves one: .env for the next boot, os.environ for this process. The config
    lookup is the fallback for a deployment that configures Flask directly, and
    it is skipped outside an app context - scrapers also run on the scheduler.
    """
    value = (os.environ.get(name) or '').strip()
    if value:
        return value
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            value = (current_app.config.get(name) or '').strip()
            if value:
                return value
    except (ImportError, RuntimeError):
        pass
    return default


def clean_cookie_value(cookie_name, value):
    """Strip table copy/paste extras: wrapping quotes and a leading name=."""
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].strip()
    prefix = cookie_name + '='
    if value.lower().startswith(prefix.lower()):
        value = value[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1].strip()
    return value


def parse_cookie_header(cookie_header):
    """
    Split a Cookie header into (name, value) pairs.

    Malformed pairs are dropped rather than raising: a paste that picked up one
    stray fragment should still carry the rest of the session.
    """
    cookies = []
    for pair in (cookie_header or '').split(';'):
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        name, value = pair.split('=', 1)
        name = name.strip()
        if name:
            cookies.append((name, clean_cookie_value(name, value)))
    return cookies


def cookie_header_names(cookie_header):
    """The cookie names in a header, for logging a session without its values."""
    return [name for name, _value in parse_cookie_header(cookie_header)]


def cookie_header_jar(cookie_header, domain):
    """
    A requests cookie jar from a pasted header, scoped to one site.

    ``domain`` is stored with a leading dot so the cookies reach the retailer's
    API subdomain as well as www - Target's clearance token is set on
    .target.com and has to arrive at redsky.target.com or the request is refused
    exactly as an uncookied one is. A header carries no domains of its own, so
    unlike an exported jar there is nothing to scope per cookie; every pair in
    it came from the one site the user copied it from.

    Returns None when the header holds no usable pair, which is the same signal
    as having no header at all: use the browser.
    """
    import requests

    pairs = parse_cookie_header(cookie_header)
    if not pairs:
        return None
    domain = '.' + domain.lstrip('.')
    jar = requests.cookies.RequestsCookieJar()
    for name, value in pairs:
        try:
            jar.set_cookie(requests.cookies.create_cookie(
                name=name, value=value, domain=domain, path='/'))
        except Exception as e:
            logger.debug(f"Skipping pasted cookie {name!r}: {str(e)}")
    if not len(jar):
        return None
    return jar


def apply_cookie_header(driver, cookie_header, domains, label):
    """
    Inject a pasted session into a running browser.

    The browser has to already be on the site - selenium refuses a cookie for a
    domain the current page does not belong to - and the caller reloads
    afterwards, because cookies added after a page loaded do not apply to it.

    ``domains`` is tried in order per cookie: chromedriver rejects a domain that
    does not match the current page, and which spelling it accepts depends on
    where the browser currently is, so the first that takes is kept.

    Returns the number of cookies the browser accepted.
    """
    pairs = parse_cookie_header(cookie_header)
    if not pairs:
        return 0

    applied = 0
    for name, value in pairs:
        for domain in domains:
            try:
                driver.add_cookie({'name': name, 'value': value,
                                   'domain': domain, 'path': '/'})
                applied += 1
                break
            except Exception:
                continue
        else:
            logger.debug(f"Could not add {label} cookie {name}")
    if applied:
        logger.info(f"Applied {applied} pasted {label} cookie(s) to the browser")
    else:
        # Said at WARNING because the run continues either way, and a silent
        # zero here looks identical to never having configured a session.
        logger.warning(
            f"{label} cookies are configured but the browser accepted none of "
            "them; this page will load as an anonymous visitor"
        )
    return applied


# A pasted session that the retailer has already refused this process. An
# exported jar gets flagged stale in its own file; a pasted header has no file
# to flag - rewriting .env to record a failure would be the app editing its own
# config behind the user's back - so the refusal is remembered here, keyed by a
# digest of the header. A fresh paste is a different digest and is tried again,
# and a restart forgets, which is the right default: the usual reason a paste
# stops working is that it expired, and the usual fix is a new one.
_REJECTED_COOKIE_HEADERS = {}


def _cookie_header_digest(cookie_header):
    import hashlib

    return hashlib.sha256((cookie_header or '').encode('utf-8')).hexdigest()[:16]


def note_cookie_header_rejected(cookie_header, label):
    """
    Record that the retailer refused this exact paste. Returns True the first
    time, so the caller can say so once instead of every cycle.
    """
    digest = _cookie_header_digest(cookie_header)
    if digest in _REJECTED_COOKIE_HEADERS:
        return False
    _REJECTED_COOKIE_HEADERS[digest] = label
    logger.warning(
        f"The pasted {label} session was refused; skipping it until a fresh one "
        "is saved on the settings page"
    )
    return True


def cookie_header_is_rejected(cookie_header):
    """Whether this paste has already been refused since the app started."""
    return _cookie_header_digest(cookie_header) in _REJECTED_COOKIE_HEADERS


def clear_rejected_cookie_headers():
    """Forget every refusal. For tests, and for a settings-page re-save."""
    _REJECTED_COOKIE_HEADERS.clear()


def forget_cookie_header_rejection(cookie_header):
    """
    Forget one refusal, for a session that has since been shown to work.

    Its use is the settings page's Test button: a paste set aside after a 403
    that now answers 200 - the clearance was renewed, the User-Agent corrected,
    the address changed back - should go straight back into service rather than
    wait for a restart to be tried again.
    """
    return _REJECTED_COOKIE_HEADERS.pop(_cookie_header_digest(cookie_header), None)


# How long a settings-page probe waits. Shorter than REQUEST_TIMEOUT because
# somebody is sitting on the page watching for the answer, and a retailer that
# has not replied in ten seconds has told us what we needed to know anyway.
SESSION_PROBE_TIMEOUT = 10


def session_probe_get(url, headers=None, cookies=None, params=None,
                      timeout=SESSION_PROBE_TIMEOUT):
    """
    One GET, made to find out whether a pasted session is still accepted.

    Returns (response, error): exactly one of the two is None. The error is a
    sentence for the person who pressed the button, not a traceback - at this
    point they are trying to tell an expired clearance from a typo, and the
    distinction between those and "your network is down" is the whole answer.

    Deliberately thin: it fetches and it does not judge. What a given status
    means differs per retailer (Redsky's 206 is a healthy session answering a
    question it did not like; a GameStop 200 can still be the wall), so the
    caller classifies.
    """
    import requests

    try:
        response = requests.get(url, headers=headers, cookies=cookies,
                                params=params, timeout=timeout)
    except requests.Timeout:
        return None, f'No answer within {timeout} seconds. Try again in a moment.'
    except requests.RequestException as e:
        return None, f'The request could not be made: {str(e)}'
    return response, None


# One probe per store per minute. The button is a request to a retailer that is
# already counting them, and the two ways it gets pressed twice - an impatient
# double-click, a browser re-POSTing on refresh - both spend a second request to
# learn what the first one just said. Per process, like the rejection memory
# above: this guards a retailer's patience, not a resource worth persisting.
SESSION_PROBE_COOLDOWN_SECONDS = 60
_LAST_SESSION_PROBE = {}


def session_probe_wait_seconds(store, now=None):
    """Seconds before ``store`` may be probed again; 0 when it may be now."""
    last = _LAST_SESSION_PROBE.get(store)
    if last is None:
        return 0
    elapsed = (now if now is not None else time.time()) - last
    return max(0, int(round(SESSION_PROBE_COOLDOWN_SECONDS - elapsed)))


def note_session_probe(store, now=None):
    """Record that a probe for ``store`` has just gone out."""
    _LAST_SESSION_PROBE[store] = now if now is not None else time.time()


def clear_session_probe_history():
    """Forget every cooldown. For tests."""
    _LAST_SESSION_PROBE.clear()
