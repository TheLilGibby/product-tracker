"""
Request gate used when the app is published through a Cloudflare tunnel.

Viewing (GET/HEAD/OPTIONS) is always open so Telegram "Open in tracker" links
and the public dashboard work without a login.

Preference saves (timezone, check interval, product notification / auto-cart
toggles) are also open: those forms are on pages you can already view.
Mutating requests that spend money or rewrite secrets (add-to-cart, delete,
cookie paste) still require HTTP Basic Auth when AUTH_PASSWORD is set.

Machine clients can skip the prompt by sending a matching X-API-Token instead.
"""

import hmac
import logging
import os
import re
from urllib.parse import urlparse

from flask import Response, current_app, request

from app.public_urls import view_token

logger = logging.getLogger(__name__)

_OPEN_POST_PATHS = frozenset({
    '/update-check-interval',
    '/update-timezone',
    '/toggle-time-format',
    '/telegram/test',
})

_OPEN_POST_RE = re.compile(
    r'^/product/\d+/(update-notifications|update-auto-cart)$'
)


def _eq(left, right):
    return hmac.compare_digest(str(left), str(right))


def _from_cloudflare():
    """True when the client hit the public hostname, not localhost.

    ProxyFix rewrites request.host from X-Forwarded-Host, so a tunnel request
    looks like trycloudflare.com even though Flask is bound to 127.0.0.1.
    Do not key off CF-Ray / CF-Connecting-IP: those also appear on WARP and
    would lock localhost POSTs by mistake.
    """
    host = (request.host or '').split(':')[0].lower()
    if host.endswith('trycloudflare.com'):
        return True
    public = (current_app.config.get('PRODUCT_TRACKER_PUBLIC_URL') or '').strip()
    if public:
        public_host = (urlparse(public).hostname or '').lower()
        if public_host and host == public_host:
            return True
    return False


def _open_mutation():
    path = request.path.rstrip('/') or '/'
    if path in _OPEN_POST_PATHS:
        return True
    return bool(_OPEN_POST_RE.match(path))


def _signed_product_view(secret):
    """Telegram 'Open in tracker' links carry ?v= so the in-app browser can GET the page."""
    if request.method != 'GET':
        return False
    parts = (request.path or '').strip('/').split('/')
    if len(parts) != 2 or parts[0] != 'product' or not parts[1].isdigit():
        return False
    provided = request.args.get('v') or ''
    expected = view_token(parts[1], secret)
    if not expected or len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def register_auth(app):
    password = (app.config.get('AUTH_PASSWORD') or os.environ.get('AUTH_PASSWORD') or '').strip()
    if password:
        logger.info('Cloudflare Basic Auth enabled for public-URL requests')
    else:
        logger.warning('Cloudflare Basic Auth disabled — AUTH_PASSWORD is empty')

    @app.before_request
    def _cloudflare_basic_auth():
        password = (app.config.get('AUTH_PASSWORD') or os.environ.get('AUTH_PASSWORD') or '').strip()
        if not password:
            return None
        if not _from_cloudflare():
            return None
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return None
        if _open_mutation():
            return None

        token = (app.config.get('API_TOKEN') or os.environ.get('API_TOKEN') or '').strip()
        provided = request.headers.get('X-API-Token') or ''
        if token and _eq(provided, token):
            return None

        secret = app.config.get('SECRET_KEY') or os.environ.get('SECRET_KEY') or ''
        if _signed_product_view(secret):
            return None

        auth = request.authorization
        user = app.config.get('AUTH_USER') or os.environ.get('AUTH_USER') or 'admin'
        if (
            auth
            and auth.username is not None
            and auth.password is not None
            and _eq(auth.username, user)
            and _eq(auth.password, password)
        ):
            return None

        return Response(
            'Authentication required\n',
            401,
            {'WWW-Authenticate': 'Basic realm="Tracker_"'},
        )
