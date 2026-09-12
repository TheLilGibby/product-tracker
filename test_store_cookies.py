#!/usr/bin/env python
"""
Offline checks for the pasted Target and GameStop sessions.

    python test_store_cookies.py

No network, no Chrome, no retailer, no database beyond an in-memory one. The
browser is a fake object that records what was asked of it, and every HTTP call
is intercepted before it leaves. Everything is written inside a temp directory.

Why these two stores. They are the two the tracker cannot read at all: Target's
Redsky API answers 403 with a captcha URL to any client that has not passed the
press-and-hold, and GameStop's Cloudflare edge refuses a plain request outright,
so both sit permanently in backoff. The fix is the same one Amazon already has -
the user passes the challenge themselves, in their own browser, and pastes the
session it produced into the settings page. Nothing here solves, weakens or
automates a challenge; it carries a session the user already earned.

The four things worth checking, and why each has bitten:

  * A Cookie header is a line of text, and the pastes people actually produce
    have quotes, a stray `name=name=value`, and fragments in them.
  * Scope. Target's clearance token is set on `.target.com` and has to arrive at
    `redsky.target.com`; a jar scoped to `www.target.com` looks perfectly
    healthy and silently leaves the one cookie that matters behind.
  * Reload. Cookies added to a browser after a page has loaded do not apply to
    that page. Without the reload the window still shows the anonymous page.
  * Refusal. A session that the retailer has rejected will be rejected again, so
    it is set aside rather than spent on a doomed request every cycle - and a
    403 has to blame the right session, pasted or exported, not guess.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# Nothing in this file may schedule a check or post to Telegram, and the .env
# write resolves its path from the working directory - so both are settled
# before the app is imported and can read either.
os.environ['SCHEDULER_ENABLED'] = '0'
os.environ['TELEGRAM_ALERTS_ENABLED'] = '0'
for name in ('TARGET_COOKIES', 'GAMESTOP_COOKIES', 'GAMESTOP_USER_AGENT'):
    os.environ.pop(name, None)

WORKDIR = tempfile.mkdtemp(prefix='store_cookies_')
os.chdir(WORKDIR)

import requests  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.scrapers.common import (  # noqa: E402
    DEFAULT_HEADERS, clear_rejected_cookie_headers, clear_session_probe_history,
    cookie_header_is_rejected, cookie_header_jar, cookie_header_names,
    cookie_setting, detect_block_page, note_cookie_header_rejected,
    parse_cookie_header,
)

FAILED = []

# A realistic Target paste: the clearance token, the visitor ids it was minted
# for, and the marketing cookies that come along with any real session.
TARGET_HEADER = ('visitorId=0192ABCD; _pxvid=01234567-89ab-cdef-0123-456789abcdef; '
                 'pxcts=fedcba98-7654-3210; _px3=px3token.value==; '
                 'TealeafAkaSid=ZzZz')
# A GameStop paste is shorter, because only one cookie in it means anything.
GAMESTOP_HEADER = 'cf_clearance=abc123.def-456; __cf_bm=xyz789; dwanonymous_0a1b=0000'
GAMESTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')

# The two walls, as they are actually served. The GameStop body is Cloudflare's
# interstitial, which arrives with a 200 once JS is expected to run; the Target
# body is what a Redsky 403 carries.
CLOUDFLARE_BODY = (
    '<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title>'
    '</head><body><h1>Please enable cookies.</h1></body></html>'
)
REDSKY_403_BODY = (
    '{"errors":[{"message":"Forbidden"}],'
    '"captchaRelativeURL":"/_sec/cp_challenge/px-captcha?..."}'
)
# A product page, as far as detect_block_page is concerned: it carries the
# markers a real one carries. That matters here - a small body with no product
# markers is itself a wall signal, and rightly so, since that is what a
# challenge page looks like.
PRODUCT_BODY = (
    '<!DOCTYPE html><html><head><title>Nintendo Switch 2 | GameStop</title>'
    '<meta property="og:title" content="Nintendo Switch 2">'
    '<script type="application/ld+json">{"@type":"Product","name":"Switch 2"}</script>'
    '</head><body><h1>Nintendo Switch 2</h1>'
    '<button class="add-to-cart">Add to Cart</button></body></html>'
)


def report(ok, what, detail=''):
    print(('  [ok] ' if ok else '  [FAIL] ') + what
          + (('   <- ' + str(detail)) if detail and not ok else ''))
    if not ok:
        FAILED.append(what)


class FakeResponse:
    """Just enough of requests.Response for the paths under test."""

    def __init__(self, status_code=200, text='', payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class FakeDriver:
    """
    A browser that records what was asked of it.

    ``accepts`` is the one domain spelling this fake will take, which is the
    behaviour that matters: chromedriver refuses a cookie whose domain does not
    match the page it is on, and which spelling it accepts depends on where the
    browser currently is.
    """

    def __init__(self, accepts=None):
        self.accepts = accepts
        self.cookies = []
        self.loads = []

    def get(self, url):
        self.loads.append(url)

    def add_cookie(self, cookie):
        if self.accepts is not None and cookie.get('domain') != self.accepts:
            raise Exception(f"invalid cookie domain: {cookie.get('domain')}")
        self.cookies.append(cookie)


class Intercepted:
    """Replace a module's requests.get and keep what it was called with."""

    def __init__(self, module, response):
        self.module = module
        self.response = response
        self.calls = []
        self._real = None

    def __enter__(self):
        self._real = self.module.requests.get

        def fake_get(*args, **kwargs):
            self.calls.append({'args': args, 'kwargs': kwargs})
            if isinstance(self.response, list):
                return self.response[min(len(self.calls), len(self.response)) - 1]
            return self.response

        self.module.requests.get = fake_get
        return self

    def __exit__(self, *exc):
        self.module.requests.get = self._real
        return False

    @property
    def sent_cookies(self):
        """The cookie names of the last call, whatever shape they arrived in."""
        if not self.calls:
            return []
        jar = self.calls[-1]['kwargs'].get('cookies')
        return [] if jar is None else sorted(cookie.name for cookie in jar)

    @property
    def sent_headers(self):
        return self.calls[-1]['kwargs'].get('headers') or {}


class Timeout:
    """A retailer that accepts the connection and then says nothing."""

    def __init__(self, module):
        self.module = module
        self.calls = []
        self._real = None

    def __enter__(self):
        self._real = self.module.requests.get

        def fake_get(*args, **kwargs):
            self.calls.append({'args': args, 'kwargs': kwargs})
            raise requests.Timeout('timed out')

        self.module.requests.get = fake_get
        return self

    def __exit__(self, *exc):
        self.module.requests.get = self._real
        return False


def sent_cookie_header(jar, url):
    """
    What requests would put on the wire for ``url``, given ``jar``.

    This is the only honest way to check scope: http.cookiejar, not our code,
    decides which cookies a host is allowed to see, and it decides it from the
    leading dot on the domain.
    """
    prepared = requests.Request('GET', url).prepare()
    return requests.cookies.get_cookie_header(jar, prepared) or ''


# ------------------------------------------------------------------- parsing

def check_header_parsing():
    print("A pasted Cookie header becomes name/value pairs")
    cases = [
        ('an ordinary header', 'a=1; b=2', [('a', '1'), ('b', '2')]),
        ('a value may contain = (base64 padding)', '_px3=tok.en==',
         [('_px3', 'tok.en==')]),
        ('missing spaces after the separator', 'a=1;b=2', [('a', '1'), ('b', '2')]),
        ('a trailing separator', 'a=1; ', [('a', '1')]),
        ('a fragment with no = is dropped, the rest survives', 'a=1; junk; b=2',
         [('a', '1'), ('b', '2')]),
        ('a name with no value is kept as empty', 'a=; b=2', [('a', ''), ('b', '2')]),
        ('quotes picked up from a DevTools cell', 'a="1"; b=2',
         [('a', '1'), ('b', '2')]),
        ('a value that repeats its own name', 'a=a=1', [('a', '1')]),
        ('nothing at all', '', []),
        ('None', None, []),
    ]
    for what, header, expected in cases:
        got = parse_cookie_header(header)
        report(got == expected, what, got)

    report(cookie_header_names(TARGET_HEADER) ==
           ['visitorId', '_pxvid', 'pxcts', '_px3', 'TealeafAkaSid'],
           'the names can be logged without the values')
    # The point of that helper: a log line naming a session must not be a log
    # line containing it.
    report('px3token' not in ' '.join(cookie_header_names(TARGET_HEADER)),
           'and no value leaks into that list')


def check_jar_scope():
    print("A pasted header becomes a jar the API subdomain can see")
    jar = cookie_header_jar(TARGET_HEADER, 'target.com')
    report(jar is not None and len(jar) == 5, 'every pair reaches the jar',
           None if jar is None else len(jar))

    redsky = sent_cookie_header(jar, 'https://redsky.target.com/redsky_aggregations/v1/web/x')
    report('_px3=px3token.value==' in redsky,
           'the clearance token is sent to redsky.target.com', redsky)
    www = sent_cookie_header(jar, 'https://www.target.com/p/A-94300072')
    report('_px3=' in www, 'and to www.target.com', www)
    elsewhere = sent_cookie_header(jar, 'https://www.example.com/')
    report(elsewhere == '', 'and to nobody else', elsewhere)

    # The bug this exists to prevent: scoped to the host it was copied from, the
    # jar looks healthy and the one cookie that matters never arrives.
    narrow = cookie_header_jar(TARGET_HEADER, 'www.target.com')
    report(sent_cookie_header(narrow, 'https://redsky.target.com/x') == '',
           'a www-scoped jar is exactly the trap: it reaches redsky with nothing')

    gs = cookie_header_jar(GAMESTOP_HEADER, 'gamestop.com')
    report('cf_clearance=abc123.def-456' in sent_cookie_header(
        gs, 'https://www.gamestop.com/products/x/20037854.html'),
        'the GameStop clearance is sent to www.gamestop.com')

    report(cookie_header_jar('', 'target.com') is None,
           'an empty header makes no jar, which reads as "use the browser"')
    report(cookie_header_jar('not-a-cookie', 'target.com') is None,
           'and neither does an unusable one')


def check_config_reading():
    print("The scrapers read the setting the settings page writes")
    from app.scrapers.gamestop_scraper import (load_gamestop_cookies,
                                               load_gamestop_user_agent)
    from app.scrapers.target_scraper import load_target_cookies

    report(load_target_cookies() == '', 'nothing configured is the default')
    report(load_gamestop_cookies() == '', 'for both stores')
    report(load_gamestop_user_agent() == DEFAULT_HEADERS['User-Agent'],
           'and the UA falls back to the one the request would have sent anyway')
    report(load_gamestop_user_agent(configured_only=True) == '',
           'while the settings page is shown an empty box, not our UA')

    os.environ['TARGET_COOKIES'] = TARGET_HEADER
    os.environ['GAMESTOP_COOKIES'] = GAMESTOP_HEADER
    os.environ['GAMESTOP_USER_AGENT'] = GAMESTOP_UA
    try:
        report(load_target_cookies() == TARGET_HEADER, 'the environment is read')
        report(load_gamestop_cookies() == GAMESTOP_HEADER, 'for both stores')
        report(load_gamestop_user_agent() == GAMESTOP_UA, 'and the pasted UA wins')
        # os.environ is what the settings page sets for the running process; the
        # Flask config is the fallback for a deployment that configures Flask
        # directly, and neither may be required to be present.
        os.environ.pop('TARGET_COOKIES')
        report(cookie_setting('TARGET_COOKIES', 'fallback') == 'fallback',
               'an unset name gives the default rather than raising')
    finally:
        for name in ('TARGET_COOKIES', 'GAMESTOP_COOKIES', 'GAMESTOP_USER_AGENT'):
            os.environ.pop(name, None)


# ------------------------------------------------------------- the .env write

def check_env_round_trip():
    """The settings page writes it; a restart has to read back the same string."""
    print("A pasted session survives a restart")
    from app import create_app

    app = create_app('testing')
    client = app.test_client()
    env_path = os.path.join(WORKDIR, '.env')
    if os.path.exists(env_path):
        os.remove(env_path)

    response = client.post('/update-target-cookies',
                           data={'target_cookies': TARGET_HEADER})
    report(response.status_code in (200, 302), 'the Target form posts',
           response.status_code)
    report(os.environ.get('TARGET_COOKIES') == TARGET_HEADER,
           'this process sees it immediately', os.environ.get('TARGET_COOKIES'))
    stored = dotenv_values(env_path)
    report(stored.get('TARGET_COOKIES') == TARGET_HEADER,
           'and dotenv reads back the identical string', stored.get('TARGET_COOKIES'))

    client.post('/update-gamestop-cookies',
                data={'gamestop_cookies': GAMESTOP_HEADER,
                      'gamestop_user_agent': GAMESTOP_UA})
    stored = dotenv_values(env_path)
    report(stored.get('GAMESTOP_COOKIES') == GAMESTOP_HEADER,
           'the GameStop header too', stored.get('GAMESTOP_COOKIES'))
    report(stored.get('GAMESTOP_USER_AGENT') == GAMESTOP_UA,
           'and the User-Agent it was issued to, which is half the credential',
           stored.get('GAMESTOP_USER_AGENT'))
    report(stored.get('TARGET_COOKIES') == TARGET_HEADER,
           'writing one store does not disturb the other')

    # A value .env cannot hold must be refused before anything is written,
    # rather than ending up live in the process for one run.
    before = os.environ.get('TARGET_COOKIES')
    client.post('/update-target-cookies',
                data={'target_cookies': "a=1\nAUTH_PASSWORD=hunter2"})
    report(os.environ.get('TARGET_COOKIES') == before,
           'a header with a line break in it is refused, not stored',
           os.environ.get('TARGET_COOKIES'))
    report(dotenv_values(env_path).get('AUTH_PASSWORD') is None,
           'so a paste cannot smuggle a second setting into .env')

    # Clearing is how a user revokes it from here, and it must be a real clear.
    client.post('/update-target-cookies',
                data={'target_cookies': TARGET_HEADER, 'clear_target_cookies': '1'})
    report(os.environ.get('TARGET_COOKIES') == '', 'clearing empties the process value',
           os.environ.get('TARGET_COOKIES'))
    report(dotenv_values(env_path).get('TARGET_COOKIES') == '',
           'and the file', dotenv_values(env_path).get('TARGET_COOKIES'))

    client.post('/update-gamestop-cookies', data={'clear_gamestop_cookies': '1'})
    report(dotenv_values(env_path).get('GAMESTOP_USER_AGENT') == '',
           'clearing GameStop drops the paired User-Agent with it')

    response = client.get('/settings')
    body = response.get_data(as_text=True)
    report(response.status_code == 200, 'the settings page still renders',
           response.status_code)
    report('update-target-cookies' in body and 'update-gamestop-cookies' in body,
           'with both forms on it')
    report('_px3' in body and 'cf_clearance' in body,
           'naming the cookie that actually matters for each store')

    for name in ('TARGET_COOKIES', 'GAMESTOP_COOKIES', 'GAMESTOP_USER_AGENT'):
        os.environ.pop(name, None)


# --------------------------------------------------------------- Target paths

def check_target_requests_path():
    print("Target: the session reaches the Redsky request")
    from app.scrapers import target_scraper
    from app.scrapers.target_scraper import TargetScraper

    clear_rejected_cookie_headers()
    scraper = TargetScraper()
    url = 'https://www.target.com/p/x/-/A-94300072'

    jar, source = TargetScraper._redsky_session()
    report(jar is None and source == '', 'with nothing configured there is no session',
           source)

    os.environ['TARGET_COOKIES'] = TARGET_HEADER
    try:
        jar, source = TargetScraper._redsky_session()
        report(source == 'pasted', 'a paste is the session', source)
        report(jar is not None and '_px3' in [c.name for c in jar],
               'and it carries the clearance token')

        payload = {'data': {'product': {'tcin': '94300072'}}}
        with Intercepted(target_scraper, FakeResponse(200, '{}', payload)) as sent:
            got = scraper._redsky_get('https://redsky.target.com/x', {'tcin': '1'},
                                      '94300072', url, 'availability')
            report(got == payload, 'a 200 comes back decoded')
            report(sent.sent_cookies == ['TealeafAkaSid', '_px3', '_pxvid',
                                         'pxcts', 'visitorId'],
                   'every pasted cookie went out with the request', sent.sent_cookies)
            report(sent.sent_headers.get('sec-fetch-site') == 'same-site',
                   'and the request looks like the page XHR it is imitating')

        # A 403 is the session being refused. It will be refused next cycle too.
        with Intercepted(target_scraper, FakeResponse(403, REDSKY_403_BODY)) as sent:
            got = scraper._redsky_get('https://redsky.target.com/x', {'tcin': '1'},
                                      '94300072', url, 'availability')
            report(got is None, 'a 403 is None - "ask the browser", not "unavailable"')
        report(cookie_header_is_rejected(TARGET_HEADER),
               'and the paste is remembered as refused')

        with Intercepted(target_scraper, FakeResponse(200, '{}', payload)) as sent:
            jar, source = TargetScraper._redsky_session()
            report(source == '' and jar is None,
                   'so the next check does not spend a request on it', source)
            scraper._redsky_get('https://redsky.target.com/x', {'tcin': '1'},
                                '94300072', url, 'availability')
            report(sent.sent_cookies == [], 'the request goes out uncookied instead',
                   sent.sent_cookies)

        # A fresh paste is a different digest: the user saving a new session is
        # exactly the event that should make us try again.
        os.environ['TARGET_COOKIES'] = TARGET_HEADER.replace('px3token', 'px3newtoken')
        jar, source = TargetScraper._redsky_session()
        report(source == 'pasted', 'a newly pasted session is tried again', source)
    finally:
        os.environ.pop('TARGET_COOKIES', None)
        clear_rejected_cookie_headers()


def check_target_browser_path():
    print("Target: the session reaches the browser, and the page is reloaded onto it")
    from app.scrapers.target_scraper import TargetScraper

    clear_rejected_cookie_headers()
    scraper = TargetScraper()
    url = 'https://www.target.com/p/x/-/A-94300072'

    driver = FakeDriver()
    report(scraper._apply_target_cookies(driver, url) == 0,
           'nothing configured, nothing injected')
    report(driver.loads == [], 'and no reload, because nothing changed')

    os.environ['TARGET_COOKIES'] = TARGET_HEADER
    try:
        driver = FakeDriver(accepts='.target.com')
        applied = scraper._apply_target_cookies(driver, url)
        report(applied == 5, 'every cookie is injected', applied)
        report([c['name'] for c in driver.cookies][0] == 'visitorId',
               'by name and value', driver.cookies[:1])
        report(all(c['domain'] == '.target.com' for c in driver.cookies),
               'scoped so redsky.target.com is in range too')
        report(driver.loads == [url],
               'and the page is reloaded exactly once - cookies added after a '
               'load do not apply to the page already on screen', driver.loads)

        # chromedriver's accepted domain spelling depends on where the browser
        # is, so the spellings are tried in order and the first that takes wins.
        driver = FakeDriver(accepts='www.target.com')
        applied = scraper._apply_target_cookies(driver, url)
        report(applied == 5, 'a driver that only takes the www spelling still works',
               applied)
        report(driver.loads == [url], 'and still gets its reload')

        driver = FakeDriver(accepts='nothing-matches')
        applied = scraper._apply_target_cookies(driver, url)
        report(applied == 0, 'a driver that refuses every spelling injects nothing',
               applied)
        report(driver.loads == [],
               'and is not reloaded, because that would only re-fetch the same '
               'anonymous page')
    finally:
        os.environ.pop('TARGET_COOKIES', None)
        clear_rejected_cookie_headers()


# ------------------------------------------------------------- GameStop paths

def check_gamestop_requests_path():
    print("GameStop: with a session, a check is an HTTP request and not a window")
    from app.scrapers import gamestop_scraper
    from app.scrapers.gamestop_scraper import GameStopScraper

    clear_rejected_cookie_headers()
    scraper = GameStopScraper()
    url = 'https://www.gamestop.com/products/x/20037854.html'

    with Intercepted(gamestop_scraper, FakeResponse(200, '<html></html>')) as sent:
        report(scraper._fetch_via_requests(url, '20037854') is None,
               'no session means no HTTP attempt at all')
        report(sent.calls == [], 'not even a doomed one', len(sent.calls))

    os.environ['GAMESTOP_COOKIES'] = GAMESTOP_HEADER
    os.environ['GAMESTOP_USER_AGENT'] = GAMESTOP_UA
    try:
        with Intercepted(gamestop_scraper, FakeResponse(200, PRODUCT_BODY)) as sent:
            got = scraper._fetch_via_requests(url, '20037854')
            report(got == PRODUCT_BODY, 'a 200 product page comes straight back', got)
            report(sent.sent_cookies == ['__cf_bm', 'cf_clearance', 'dwanonymous_0a1b'],
                   'the clearance went out with it', sent.sent_cookies)
            report(sent.sent_headers.get('User-Agent') == GAMESTOP_UA,
                   'sent with the User-Agent it was issued to - Cloudflare binds '
                   'a clearance to one browser, so our own UA would be a 403',
                   sent.sent_headers.get('User-Agent'))

        # The wall arrives two ways and both have to count as refused.
        with Intercepted(gamestop_scraper, FakeResponse(403, CLOUDFLARE_BODY)):
            report(scraper._fetch_via_requests(url, '20037854') is None,
                   'a 403 falls through to the browser')
        report(cookie_header_is_rejected(GAMESTOP_HEADER),
               'and the paste is set aside rather than retried every cycle')

        clear_rejected_cookie_headers()
        with Intercepted(gamestop_scraper, FakeResponse(200, CLOUDFLARE_BODY)):
            report(scraper._fetch_via_requests(url, '20037854') is None,
                   'a 200 carrying the interstitial is the wall too, and is not '
                   'mistaken for a product page')
        report(cookie_header_is_rejected(GAMESTOP_HEADER),
               'that one is remembered as refused as well')

        with Intercepted(gamestop_scraper, FakeResponse(200, PRODUCT_BODY)) as sent:
            report(scraper._fetch_via_requests(url, '20037854') is None,
                   'a refused session is skipped on the next check')
            report(sent.calls == [], 'without another request', len(sent.calls))
    finally:
        os.environ.pop('GAMESTOP_COOKIES', None)
        os.environ.pop('GAMESTOP_USER_AGENT', None)
        clear_rejected_cookie_headers()


def check_gamestop_browser_path():
    print("GameStop: the session reaches the browser too")
    from app.scrapers.gamestop_scraper import GameStopScraper

    clear_rejected_cookie_headers()
    scraper = GameStopScraper()
    url = 'https://www.gamestop.com/products/x/20037854.html'

    driver = FakeDriver(accepts='.gamestop.com')
    report(scraper._apply_gamestop_cookies(driver, url) == 0,
           'nothing configured, nothing injected')

    os.environ['GAMESTOP_COOKIES'] = GAMESTOP_HEADER
    try:
        driver = FakeDriver(accepts='.gamestop.com')
        applied = scraper._apply_gamestop_cookies(driver, url)
        report(applied == 3, 'the pasted cookies are injected', applied)
        report(driver.loads == [url], 'and the page is reloaded onto them',
               driver.loads)

        # A session the edge has already refused is not worth re-injecting.
        from app.scrapers.common import note_cookie_header_rejected
        note_cookie_header_rejected(GAMESTOP_HEADER, 'GameStop')
        driver = FakeDriver(accepts='.gamestop.com')
        report(scraper._apply_gamestop_cookies(driver, url) == 0,
               'a refused session is not injected either')
        report(driver.loads == [], 'and costs no reload')
    finally:
        os.environ.pop('GAMESTOP_COOKIES', None)
        clear_rejected_cookie_headers()


# ------------------------------------------------------------- the walls still

def check_block_pages_still_classify():
    """
    The existing detection has to keep working, cookies or no cookies.

    This is the regression that would hurt most quietly: if a wall stopped being
    recognised, a challenge page would be parsed as a product page and the
    tracker would report a console as out of stock because Cloudflare said so.
    """
    print("The 403 and Cloudflare bodies still read as blocked")
    report(detect_block_page(CLOUDFLARE_BODY) is not None,
           'Cloudflare\'s interstitial is a block page',
           detect_block_page(CLOUDFLARE_BODY))
    report('attention' in (detect_block_page(CLOUDFLARE_BODY) or '').lower(),
           'named by the marker that identified it',
           detect_block_page(CLOUDFLARE_BODY))
    just_a_moment = ('<html><head><title>Just a moment...</title></head>'
                     '<body></body></html>')
    report(detect_block_page(just_a_moment) is not None,
           'and so is the newer "Just a moment..." spelling',
           detect_block_page(just_a_moment))
    report(detect_block_page(PRODUCT_BODY) is None,
           'a real product page is not', detect_block_page(PRODUCT_BODY))
    report('captchaRelativeURL' in REDSKY_403_BODY,
           'the Redsky 403 body is what the scraper logs on a refusal')


def check_session_test_button():
    """
    The settings page's Test button: one request, and an honest verdict.

    What it is for. A pasted session either works or it does not, and today the
    only way to find out is to wait for the next scheduled check and read the
    logs - by which time a 403 could be a stale clearance, a wrong User-Agent, a
    changed IP address or the retailer having a bad minute, and those are
    indistinguishable after the fact. The button asks once, now, and says which.

    What has to hold, and why each has a cost if it does not:

      * The rejection memo moves one way only. A success clears it, so a session
        that was set aside and has since been renewed resumes without a restart.
        A failure records nothing: the memo exists to stop the scheduler
        spending a doomed request every cycle, and a person standing at the
        settings page pressing Test is the opposite case - one bad minute must
        not sideline a good paste.
      * A 200 carrying the challenge page is a refusal. Cloudflare serves its
        interstitial with a 200, so a button that trusted the status line would
        call the wall a success and send the user away satisfied.
      * The cooldown refuses without spending a request. A double-click or a
        resubmitted form would otherwise cost two requests against a clearance
        Cloudflare is counting.
    """
    print("The Test button asks once and reports what came back")
    from app.scrapers import gamestop_scraper, target_scraper
    from app.scrapers.gamestop_scraper import test_gamestop_session
    from app.scrapers.target_scraper import test_target_session

    clear_rejected_cookie_headers()
    clear_session_probe_history()
    for name in ('TARGET_COOKIES', 'GAMESTOP_COOKIES', 'GAMESTOP_USER_AGENT'):
        os.environ.pop(name, None)

    # Nothing saved: say so rather than probe the retailer anonymously, which
    # would report a 403 that says nothing about any session.
    with Intercepted(target_scraper, FakeResponse(200)) as caught:
        result = test_target_session()
    report(not result['ok'] and 'nothing to test' in result['message'],
           'with no session saved it says there is nothing to test', result['message'])
    report(caught.calls == [], 'and makes no request at all', len(caught.calls))

    os.environ['TARGET_COOKIES'] = TARGET_HEADER
    os.environ['GAMESTOP_COOKIES'] = GAMESTOP_HEADER

    # A 200 is the answer the user is hoping for, and it clears the memo.
    note_cookie_header_rejected(TARGET_HEADER, 'Target')
    clear_session_probe_history()
    with Intercepted(target_scraper, FakeResponse(200, payload={'data': {}})) as caught:
        result = test_target_session()
    report(result['ok'] and result['level'] == 'success',
           'Target 200 reads as a working session', result['message'])
    report(result['status'] == 200, 'and reports the status it saw', result['status'])
    report(not cookie_header_is_rejected(TARGET_HEADER),
           'a success clears an earlier refusal, so a renewed session resumes')
    report('_px3' in ' '.join(caught.sent_cookies),
           'the clearance token was on the request', caught.sent_cookies)

    # Redsky's own 403. The memo must stay untouched.
    clear_session_probe_history()
    with Intercepted(target_scraper, FakeResponse(403, text=REDSKY_403_BODY)):
        result = test_target_session()
    report(not result['ok'] and result['level'] == 'error',
           'Target 403 reads as a refusal', result['message'])
    report('press-and-hold' in result['message'],
           'naming the challenge the user has to pass themselves', result['message'])
    report(not cookie_header_is_rejected(TARGET_HEADER),
           'and a failed test does NOT sideline the session')

    # A 206 is Redsky disliking the question, not the caller: a session that
    # gets one is a session that was accepted.
    clear_session_probe_history()
    with Intercepted(target_scraper, FakeResponse(206)):
        result = test_target_session()
    report(result['ok'], 'a 206 still counts as accepted', result['message'])

    # GameStop's three answers. The middle one is the trap.
    clear_session_probe_history()
    with Intercepted(gamestop_scraper, FakeResponse(200, text=PRODUCT_BODY)) as caught:
        result = test_gamestop_session()
    report(result['ok'] and result['level'] == 'success',
           'GameStop 200 with a product page reads as working', result['message'])
    report('cf_clearance' in ' '.join(caught.sent_cookies),
           'the clearance was on the request', caught.sent_cookies)

    clear_session_probe_history()
    with Intercepted(gamestop_scraper, FakeResponse(200, text=CLOUDFLARE_BODY)):
        result = test_gamestop_session()
    report(not result['ok'] and result['level'] == 'error',
           'a 200 carrying the interstitial is a refusal, not a success',
           result['message'])
    report('User-Agent' in result['message'],
           'and with no User-Agent saved, that is named as the likely cause',
           result['message'])

    os.environ['GAMESTOP_USER_AGENT'] = GAMESTOP_UA
    clear_session_probe_history()
    with Intercepted(gamestop_scraper, FakeResponse(403, text=CLOUDFLARE_BODY)) as caught:
        result = test_gamestop_session()
    report(not result['ok'] and result['status'] == 403,
           'a 403 reads as a refusal', result['message'])
    report('expired' in result['message'] or 'IP address' in result['message'],
           'blaming expiry or a changed address once a User-Agent is saved',
           result['message'])
    report(caught.sent_headers.get('User-Agent') == GAMESTOP_UA,
           'and the paste was tested with the User-Agent it was issued to',
           caught.sent_headers.get('User-Agent'))
    report(not cookie_header_is_rejected(GAMESTOP_HEADER),
           'a failed GameStop test does not sideline the session either')

    # A retailer that never answers is its own outcome. Telling that apart from
    # a refusal is most of the reason the button exists.
    clear_session_probe_history()
    with Timeout(target_scraper):
        result = test_target_session()
    report(not result['ok'] and result['status'] is None,
           'a retailer that never answers is not reported as a refusal',
           result['message'])
    report(not cookie_header_is_rejected(TARGET_HEADER),
           'and does not sideline the session')

    # The rate limit: one test per store per minute, counted in this process.
    clear_session_probe_history()
    with Intercepted(target_scraper, FakeResponse(200, payload={'data': {}})) as caught:
        test_target_session()
        second = test_target_session()
    report(len(caught.calls) == 1, 'a second press inside the minute makes no request',
           len(caught.calls))
    report(not second['ok'] and 'seconds' in second['message'],
           'and says when it can be tried again', second['message'])
    report(second['level'] == 'warning',
           'as a warning, not an error - nothing is wrong with the session',
           second['level'])

    with Intercepted(gamestop_scraper, FakeResponse(200, text=PRODUCT_BODY)) as caught:
        other = test_gamestop_session()
    report(other['ok'] and len(caught.calls) == 1,
           'the limit is per store, so a Target test does not block GameStop',
           other['message'])

    clear_session_probe_history()
    clear_rejected_cookie_headers()
    for name in ('TARGET_COOKIES', 'GAMESTOP_COOKIES', 'GAMESTOP_USER_AGENT'):
        os.environ.pop(name, None)


def check_session_test_routes():
    """
    The buttons themselves, driven through Flask.

    Two things here that the function-level checks cannot see: that the routes
    exist and flash what the scraper decided, and that they are POSTs. The POST
    matters - auth.py gates every non-safe method behind the dashboard password
    and the same-origin check, so as a GET this would be a request to a retailer
    that any page the user visits could trigger from their browser, and a
    diagnostic is not worth an exemption.
    """
    print("The Test buttons are POST routes that flash the verdict")
    from app import create_app
    from app.scrapers import target_scraper

    clear_session_probe_history()
    clear_rejected_cookie_headers()
    os.environ['TARGET_COOKIES'] = TARGET_HEADER

    app = create_app('testing')
    client = app.test_client()

    with Intercepted(target_scraper, FakeResponse(200, payload={'data': {}})) as caught:
        response = client.post('/test-target-session', follow_redirects=True)
    body = response.get_data(as_text=True)
    report(response.status_code == 200, 'the Target test posts', response.status_code)
    report(len(caught.calls) == 1, 'and makes exactly one request', len(caught.calls))
    report('200' in body, 'the verdict is flashed onto the settings page')
    report(TARGET_HEADER not in body and 'px3token' not in body,
           'and the session itself is never rendered back into the page')

    response = client.get('/test-target-session')
    report(response.status_code == 405,
           'a GET is refused, so the button stays under the same-origin guard',
           response.status_code)

    response = client.get('/settings')
    page = response.get_data(as_text=True)
    report('test-target-session' in page and 'test-gamestop-session' in page,
           'and both buttons are on the settings page')

    clear_session_probe_history()
    clear_rejected_cookie_headers()
    for name in ('TARGET_COOKIES', 'GAMESTOP_COOKIES', 'GAMESTOP_USER_AGENT'):
        os.environ.pop(name, None)


def main():
    check_header_parsing()
    check_jar_scope()
    check_config_reading()
    check_env_round_trip()
    check_target_requests_path()
    check_target_browser_path()
    check_gamestop_requests_path()
    check_gamestop_browser_path()
    check_block_pages_still_classify()
    check_session_test_button()
    check_session_test_routes()

    print()
    if FAILED:
        print(f'{len(FAILED)} FAILED')
        for item in FAILED:
            print('  - ' + item)
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
