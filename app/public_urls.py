"""
Public (Cloudflare-tunnel) URLs for alerts and signed product-view links.

Scheduler jobs have no request, so url_for(_external=True) would produce
http://127.0.0.1 — unusable from Telegram. PRODUCT_TRACKER_PUBLIC_URL is the
source of truth whenever the tunnel is up.
"""

import hashlib
import hmac
import os


def public_base_url():
    """Return the Cloudflare (or other public) origin, or '' if unset."""
    try:
        from flask import current_app, has_app_context, has_request_context, request
        if has_app_context():
            configured = (current_app.config.get('PRODUCT_TRACKER_PUBLIC_URL') or '').rstrip('/')
            if configured:
                return configured
        if has_request_context() and request.host and not _is_local_host(request.host):
            return request.url_root.rstrip('/')
    except RuntimeError:
        pass
    return (os.environ.get('PRODUCT_TRACKER_PUBLIC_URL') or '').rstrip('/')


def _is_local_host(host):
    hostname = (host or '').split(':')[0].lower()
    return hostname in ('127.0.0.1', 'localhost', '0.0.0.0', '::1')


def _secret(explicit=None):
    if explicit:
        return explicit
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            return current_app.config.get('SECRET_KEY') or ''
    except RuntimeError:
        pass
    return os.environ.get('SECRET_KEY') or ''


def view_token(product_id, secret=None):
    """Short HMAC so a Telegram link can open /product/<id> without Basic Auth."""
    key = _secret(secret)
    if not key:
        return ''
    raw = key.encode('utf-8') if isinstance(key, str) else key
    digest = hmac.new(raw, f'view:{int(product_id)}'.encode('utf-8'), hashlib.sha256).hexdigest()
    return digest[:24]


def tracker_product_url(product_id):
    """Absolute URL to the tracker's product page, signed for the public tunnel."""
    base = public_base_url()
    if not base or product_id is None:
        return None
    url = f"{base}/product/{int(product_id)}"
    token = view_token(product_id)
    if token:
        url = f"{url}?v={token}"
    return url


def tracker_home_url():
    return public_base_url() or None
