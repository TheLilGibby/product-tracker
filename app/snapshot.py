"""
Dashboard snapshot: render the tracker's own web UI with headless Chrome and
return it as PNG bytes, optionally posting it to the Telegram alert channel.

This runs inside the same process/container as the app, so the default URL is
the app itself on the loopback interface. Nothing here raises: failures are
logged and reported as None / {'success': False} per the app conventions.
"""
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger('app.snapshot')

DEFAULT_DASHBOARD_URL = 'http://127.0.0.1:5000/'
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 900
PAGE_LOAD_TIMEOUT = 30
# Give fonts / charts a moment after the DOM is ready before grabbing pixels
SETTLE_SECONDS = 1.5


def _config_value(key, default=''):
    """Read a value from Flask config when an app context exists, else the environment."""
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            value = current_app.config.get(key)
            if value not in (None, ''):
                return value
    except RuntimeError:
        pass
    return os.environ.get(key, default)


def get_public_url():
    """
    Externally reachable base URL of the app (PUBLIC_URL), or '' when it is not
    configured or the dashboard has no password. An unauthenticated admin UI is
    never advertised in a chat message, however the tunnel is set up.
    """
    url = str(_config_value('PUBLIC_URL', '') or '').strip().rstrip('/')
    if not url:
        return ''
    if not str(_config_value('DASHBOARD_PASSWORD', '') or '').strip():
        logger.warning(
            "PUBLIC_URL is set but DASHBOARD_PASSWORD is blank; omitting the link "
            "rather than pointing at an unprotected dashboard"
        )
        return ''
    return url


def get_dashboard_url():
    """URL the screenshot is taken of: SNAPSHOT_URL override, else the app itself."""
    return str(_config_value('SNAPSHOT_URL', '') or '').strip() or DEFAULT_DASHBOARD_URL


def resolve_dashboard_path(path=None):
    """
    Turn an app-relative path ('/product/3') into a full URL on the dashboard host.
    Only same-app paths are accepted; anything else falls back to the dashboard root.
    """
    base = get_dashboard_url().rstrip('/')
    if path and isinstance(path, str) and path.startswith('/') and not path.startswith('//'):
        return base + path
    return base + '/'


def _authorize(driver):
    """
    Give the headless browser the dashboard password, when one is set, so the
    screenshot is of the dashboard rather than of a 401 page. Sent as a header
    over the loopback interface; Chrome strips credentials from a URL.
    """
    password = str(_config_value('DASHBOARD_PASSWORD', '') or '').strip()
    if not password:
        return
    import base64

    token = base64.b64encode(f"snapshot:{password}".encode('utf-8')).decode('ascii')
    try:
        driver.execute_cdp_cmd('Network.enable', {})
        driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {'headers': {'Authorization': f'Basic {token}'}})
    except Exception as e:
        logger.warning(f"Could not set the dashboard credentials on the snapshot browser: {str(e)}")


def capture_dashboard(url=None, width=WINDOW_WIDTH, height=WINDOW_HEIGHT):
    """
    Screenshot the dashboard with the headless Chrome that is already installed
    for the scrapers (same undetected-chromedriver launcher and Chrome-version
    detection they use, so the driver matches the browser in the image).

    Args:
        url: Page to capture (default: the app's own dashboard)
        width / height: Browser window size in pixels

    Returns:
        PNG bytes, or None on any failure (logged, never raised)
    """
    url = url or get_dashboard_url()
    driver = None
    started = time.time()
    try:
        import undetected_chromedriver as uc
        from app.scrapers.common import detect_chrome_major

        # A fresh options object per launch: the launcher refuses to reuse one
        options = uc.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--hide-scrollbars')
        options.add_argument(f'--window-size={width},{height}')

        driver = uc.Chrome(options=options, version_main=detect_chrome_major())
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        driver.set_window_size(width, height)
        _authorize(driver)
        driver.get(url)
        time.sleep(SETTLE_SECONDS)
        png = driver.get_screenshot_as_png()
        logger.info(f"Captured dashboard snapshot of {url} ({len(png)} bytes) in {time.time() - started:.1f}s")
        return png
    except Exception as e:
        logger.error(f"Dashboard snapshot of {url} failed: {str(e)}", exc_info=True)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as e:
                logger.debug(f"Error closing snapshot browser: {str(e)}")


def build_caption(when=None):
    """Caption for the Telegram photo: local timestamp plus the public link when configured."""
    import html
    import pytz

    from app.config import DEFAULT_TIMEZONE

    tz_name = str(_config_value('DEFAULT_TIMEZONE', DEFAULT_TIMEZONE)
                  or DEFAULT_TIMEZONE)
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.utc
    if when is None:
        when = datetime.now(tz)
    elif when.tzinfo is None:
        when = pytz.utc.localize(when).astimezone(tz)

    caption = f"📊 <b>TRACKER_ dashboard</b> — {when.strftime('%Y-%m-%d %H:%M')} {when.tzname()}"
    public_url = get_public_url()
    if public_url:
        caption += f'\n<a href="{html.escape(public_url, quote=True)}">Open the live dashboard</a>'
    return caption


def send_dashboard_snapshot(url=None, caption=None):
    """
    Capture the dashboard and post it to the configured Telegram channel.

    Args:
        url: Page to capture (default: the app's own dashboard)
        caption: HTML caption override (default: timestamp + PUBLIC_URL link)

    Returns:
        Dict {'success': bool, 'message': str}
    """
    from app.notifications.telegram import TelegramNotifier

    if not TelegramNotifier.is_configured():
        return {'success': False, 'message': 'Telegram is not configured'}

    png = capture_dashboard(url)
    if not png:
        return {'success': False, 'message': 'Could not capture the dashboard screenshot; see app.log'}

    if TelegramNotifier.send_photo_file(png, caption or build_caption()):
        return {'success': True, 'message': 'Dashboard snapshot sent to Telegram'}
    return {'success': False, 'message': 'Telegram rejected the snapshot; see app.log'}
