"""
Optional HTTP Basic gate for the whole dashboard.

The UI has no accounts: anyone who can reach it can add or delete tracked
products, trigger cart attempts and rewrite the Newegg cookies in .env. That is
fine on localhost, but not once the app is published through a tunnel, so
DASHBOARD_PASSWORD must be set before exposing it. Blank password = no gate,
which keeps local development unchanged.

Any username is accepted; only the password is checked, in constant time.
"""
import hmac
import logging

from flask import Response, abort, current_app, request
from urllib.parse import urlparse

logger = logging.getLogger('app.auth')

# Paths that stay reachable without credentials: static assets and the container
# healthcheck, which has no way to send an Authorization header.
EXEMPT_PREFIXES = ('/static/',)
EXEMPT_PATHS = ('/healthz',)


def dashboard_password():
    """The configured dashboard password, or '' when the gate is disabled."""
    return str(current_app.config.get('DASHBOARD_PASSWORD', '') or '').strip()


def is_public_exposure_safe():
    """
    True when it is safe to advertise a public URL: the dashboard is
    password-protected. Used to suppress links to an open admin UI.
    """
    return bool(dashboard_password())


def _unauthorized():
    return Response(
        'Authentication required.',
        401,
        {'WWW-Authenticate': 'Basic realm="TRACKER_", charset="UTF-8"'},
    )


def register_auth(app):
    """
    Install the before_request gate. Does nothing at request time while
    DASHBOARD_PASSWORD is blank, so the setting can be changed by restarting.

    Args:
        app: Flask application instance
    """
    @app.before_request
    def require_dashboard_password():
        password = dashboard_password()
        if not password:
            return None

        path = request.path or '/'
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return None

        auth = request.authorization
        if auth and auth.password and hmac.compare_digest(str(auth.password), password):
            return None

        logger.info(f"Rejected unauthenticated request to {path} from {request.remote_addr}")
        return _unauthorized()

    @app.route('/healthz')
    def healthz():
        """Unauthenticated liveness probe for the container healthcheck."""
        return 'ok', 200

    if str(app.config.get('DASHBOARD_PASSWORD', '') or '').strip():
        app.logger.info('Dashboard password protection is enabled')
    else:
        app.logger.info('Dashboard password protection is OFF (DASHBOARD_PASSWORD is blank)')


# ---------------------------------------------------------------------------
# CSRF: same-origin gate for state-changing requests
# Same-origin enforcement for state-changing requests.
#
# Why this exists: the app has no CSRF tokens and no login of its own. Every
# mutating route is a plain form POST, so any page the user's browser happens to
# visit can submit to it. Today that is survivable only because the tracker
# answers on localhost and holds no credentials a browser would attach on its
# own. Once it is reachable through the tunnel behind Basic auth, the browser
# attaches those credentials to a cross-site POST automatically, and a visited
# page can delete tracked products, drive cart attempts, rewrite the .env-backed
# cookie settings and send Telegram messages as the user. That the attacker
# cannot read the response does not matter for any of those - they are all
# writes.
#
# The check, in order:
#
# 1. Sec-Fetch-Site, when present, decides. Browsers have sent it on every
#    request since Chrome 76 and Firefox 90, and page script cannot forge it: it
#    is a forbidden header name, so fetch() and XHR are not allowed to set it.
#    "same-origin" and "none" (a typed URL, a bookmark, a redirect the user
#    followed) are allowed.
#
#    "same-site" is refused deliberately, even though it sounds harmless. A
#    sibling host on the same registrable domain is exactly the position an
#    attacker occupies after a subdomain takeover, and shared tunnel providers
#    hand out sibling hostnames on one parent domain to unrelated people.
#
# 2. Otherwise fall back to Origin, then Referer, and require the host to match
#    the host being served. This covers browsers too old for Sec-Fetch-Site,
#    which still send Origin on a cross-origin POST.
#
# 3. When none of those headers is present, allow the request. This is the
#    documented decision, not an oversight. Browsers always send Sec-Fetch-Site,
#    so the absence of all three identifies a non-browser client: curl, a script,
#    anything driving the JSON endpoints. A real CSRF attempt cannot reach this
#    branch, and a client that does reach it holds no browser-managed
#    credentials to be confused into spending.
#
# Deliberately not Flask-WTF: no new dependency, no per-template token wiring,
# and nothing to forget on a new form. The trade is that this defends the origin
# boundary rather than proving intent, which is the right shape for an app whose
# whole surface is same-origin forms.
# ---------------------------------------------------------------------------

logger = logging.getLogger('app.auth')

# Methods that must not change state. TRACE is here because it never should.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})

# Sec-Fetch-Site values that mean "this did not come from another site".
ALLOWED_FETCH_SITES = frozenset({'same-origin', 'none'})


def _host_of(url):
    """The host:port of a URL, lowercased, or '' if it has none to give"""
    if not url:
        return ''
    try:
        # 'null' is what a sandboxed iframe sends as its Origin. urlparse gives
        # it an empty netloc, so it can never match a real host - which is the
        # behaviour we want, and worth not "fixing" later.
        return (urlparse(url).netloc or '').lower()
    except ValueError:
        return ''


def _trusted_origin_hosts():
    """
    Hosts allowed in addition to the one being served.

    A reverse proxy that rewrites Host while leaving Origin as the public name
    would otherwise 403 every legitimate POST, and the obvious "fix" for that
    is to turn this check off entirely. Config: CSRF_TRUSTED_ORIGINS, a list or
    a comma-separated string of origins or bare hosts.
    """
    from flask import current_app

    configured = current_app.config.get('CSRF_TRUSTED_ORIGINS') or ()
    if isinstance(configured, str):
        configured = [part.strip() for part in configured.split(',')]
    hosts = set()
    for entry in configured:
        if not entry:
            continue
        hosts.add(_host_of(entry) or entry.strip().lower())
    return hosts


def _permitted(host):
    return bool(host) and (host == (request.host or '').lower()
                           or host in _trusted_origin_hosts())


def _refuse(reason):
    logger.warning(f"Refused cross-site {request.method} {request.path} ({reason})")
    abort(403, description='Cross-site request refused')


def same_origin_required():
    """
    before_request hook: refuse state-changing requests from another site.

    Returns None to let the request proceed; aborts with 403 otherwise.
    """
    if request.method in SAFE_METHODS:
        return None

    fetch_site = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
    if fetch_site:
        if fetch_site in ALLOWED_FETCH_SITES:
            return None
        return _refuse(f'Sec-Fetch-Site: {fetch_site}')

    for header in ('Origin', 'Referer'):
        value = request.headers.get(header)
        if value:
            if _permitted(_host_of(value)):
                return None
            return _refuse(f'{header}: {value}')

    # No browser-set origin headers at all, so not a browser. See module docstring.
    return None


def register_request_guards(app):
    """Install the same-origin guard on every registered route"""
    app.before_request(same_origin_required)
    logger.debug('Same-origin guard installed')
