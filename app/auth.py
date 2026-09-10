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

from flask import Response, current_app, request

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
        {'WWW-Authenticate': 'Basic realm="Product Tracker", charset="UTF-8"'},
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
