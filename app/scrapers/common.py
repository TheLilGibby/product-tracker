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


def detect_block_page(html):
    """
    Decide whether a fetched page is a bot wall rather than a product page.

    Args:
        html: Raw response body

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

    if len(html) < MIN_PRODUCT_PAGE_BYTES and not any(marker in sample for marker in PRODUCT_PAGE_MARKERS):
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
