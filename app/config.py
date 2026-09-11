import json
import os
from dotenv import load_dotenv

# Always load the project-root .env, regardless of the process cwd
# (the Flask reloader and Docker both change it).
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
load_dotenv(_ENV_PATH, override=False)


def parse_store_intervals(raw):
    """
    Read a per-store check interval map from an environment value.

    Accepts either "gamestop=60,bestbuy=15" or a JSON object
    {"gamestop": 60, "bestbuy": 15}, both in minutes. Entries that are not a
    store name and a positive number are dropped rather than taking the app
    down, since this arrives from the environment.
    """
    raw = (raw or '').strip()
    if not raw:
        return {}

    if raw.startswith('{'):
        try:
            pairs = json.loads(raw).items()
        except (ValueError, AttributeError):
            print(f"Ignoring unreadable STORE_CHECK_INTERVALS: {raw!r}")
            return {}
    else:
        pairs = (entry.split('=', 1) for entry in raw.split(',') if entry.strip())

    intervals = {}
    for pair in pairs:
        try:
            store, minutes = pair
            minutes = float(minutes)
        except (TypeError, ValueError):
            print(f"Ignoring unreadable STORE_CHECK_INTERVALS entry: {pair!r}")
            continue
        store = str(store).strip().lower()
        if store and minutes > 0:
            intervals[store] = minutes
        else:
            print(f"Ignoring STORE_CHECK_INTERVALS entry {store!r}={minutes!r}")
    return intervals


class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key-please-change')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Parse the database URI to ensure it's properly pointing to the data directory
    db_uri = os.environ.get('DATABASE_URI', 'sqlite:///product_tracker.db')
    
    # If this is Docker and it's a relative path, ensure it points to /app/data
    if db_uri.startswith('sqlite:///'):
        db_path = db_uri.replace('sqlite:///', '')
        if not os.path.isabs(db_path) and db_path.startswith('data/'):
            # We're in Docker, so use absolute path to data directory
            db_uri = f"sqlite:////app/{db_path}"
            print(f"Using SQLite database path: {db_uri}")
            
    SQLALCHEMY_DATABASE_URI = db_uri
    
    DEBUG = os.environ.get('DEBUG', '0') == '1'
    
    # Product checking settings
    CHECK_INTERVAL_MINUTES = int(os.environ.get('CHECK_INTERVAL_MINUTES', 15))
    CHECK_INTERVAL_SECONDS = int(os.environ.get('CHECK_INTERVAL_SECONDS', 0))

    # Per-store backoff. After STORE_BACKOFF_FAILURES consecutive failed scrapes
    # a store's products are skipped for STORE_BACKOFF_MINUTES, doubling with
    # each further failure up to an hour. Retrying a retailer that is already
    # blocking us on every cycle only extends the block.
    STORE_BACKOFF_FAILURES = int(os.environ.get('STORE_BACKOFF_FAILURES', 2))
    STORE_BACKOFF_MINUTES = int(os.environ.get('STORE_BACKOFF_MINUTES', 15))
    STORE_BACKOFF_MAX_MINUTES = int(os.environ.get('STORE_BACKOFF_MAX_MINUTES', 60))

    # How often each store may be checked, in minutes, as
    # STORE_CHECK_INTERVALS="gamestop=60,bestbuy=15" or the equivalent JSON
    # object. A store that is not listed is checked on the global interval.
    # Some retailers cannot take that cadence: GameStop is Cloudflare-blocked
    # over plain HTTP and needs a visible Chrome window, and Best Buy starts
    # serving Akamai block pages at roughly five loads a minute.
    STORE_CHECK_INTERVALS = parse_store_intervals(os.environ.get('STORE_CHECK_INTERVALS', ''))
    
    # Default timezone setting (uses UTC by default)
    DEFAULT_TIMEZONE = os.environ.get('DEFAULT_TIMEZONE', 'UTC')
    
    # Time display format preference (24-hour/military or 12-hour/AM-PM)
    # Options: '24h' or '12h'
    TIME_FORMAT = os.environ.get('TIME_FORMAT', '24h')
    
    # Shared secret for the JSON API / MCP server. Leave blank to leave the API open.
    API_TOKEN = os.environ.get('API_TOKEN', '')

    # HTTP Basic Auth for add-to-cart / delete / cookie paste on the public URL.
    # Viewing and preference saves stay open. Leave AUTH_PASSWORD blank to disable.
    AUTH_USER = os.environ.get('AUTH_USER', 'admin')
    AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', '')

    # Public URL of the Cloudflare tunnel, for display and external links.
    # The MCP server should keep using PRODUCT_TRACKER_URL (localhost).
    PRODUCT_TRACKER_PUBLIC_URL = os.environ.get('PRODUCT_TRACKER_PUBLIC_URL', '').rstrip('/')
    
    # Telegram alerts: one global channel for every product (leave blank to disable)
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
    # Exactly ONE running tracker may post to the channel. Every other process
    # that loads this app - a second dev server, a preview, a check script -
    # reads the same .env and would post the same alerts again from its own
    # database, which is how the channel ended up carrying contradictory stock
    # messages from three instances at once. Set TELEGRAM_ALERTS_ENABLED=0 in
    # the environment of anything that is not the designated instance.
    TELEGRAM_ALERTS_ENABLED = os.environ.get('TELEGRAM_ALERTS_ENABLED', '1').strip().lower() not in (
        '0', 'false', 'no', 'off', '')
    # Short name of this instance, prefixed to every Telegram post so a stray
    # sender can be told apart from the real one (e.g. INSTANCE_LABEL=live).
    INSTANCE_LABEL = os.environ.get('INSTANCE_LABEL', '').strip()[:32]
    # The same rule for the background scheduler, and for a sharper reason: a
    # second instance against the same database is a duplicate auto-carter, not
    # just a duplicate scraper - check_auto_cart_opportunities runs every 60s and
    # fires real cart attempts at retailers. With SCHEDULER_ENABLED=0 no
    # BackgroundScheduler is created at all (no check job, no auto-cart job, no
    # snapshot job) while the database and the UI stay fully live; the manual
    # Update Now buttons still work, because they scrape inline rather than
    # through the scheduler. The pair every non-designated instance sets is
    # SCHEDULER_ENABLED=0 TELEGRAM_ALERTS_ENABLED=0.
    SCHEDULER_ENABLED = os.environ.get('SCHEDULER_ENABLED', '1').strip().lower() not in (
        '0', 'false', 'no', 'off', '')
    

    # Minimum minutes between auto-cart attempts for the same product
    AUTO_CART_COOLDOWN_MINUTES = int(os.environ.get('AUTO_CART_COOLDOWN_MINUTES', 30))

    # HTTP Basic password for the whole dashboard (any username). Blank = no gate.
    # REQUIRED before exposing the app publicly: the UI can add/delete products,
    # trigger cart attempts and rewrite .env, and has no other access control.
    DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '').strip()

    # Public base URL of this app (e.g. a Cloudflare tunnel) used for links in Telegram; blank = none
    PUBLIC_URL = os.environ.get('PUBLIC_URL', '').strip().rstrip('/')
    
    # Post a dashboard screenshot to Telegram every N minutes (0 = off)
    try:
        SNAPSHOT_INTERVAL_MINUTES = int(os.environ.get('SNAPSHOT_INTERVAL_MINUTES', 0) or 0)
    except ValueError:
        SNAPSHOT_INTERVAL_MINUTES = 0
    
    @staticmethod
    def init_app(app):
        """Initialize app with this configuration."""
        pass

class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True

class TestingConfig(Config):
    """Testing configuration."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    # Check scripts and previews use this config, and they inherit the real bot
    # token from .env. They must never post: blank the channel outright.
    TELEGRAM_BOT_TOKEN = ''
    TELEGRAM_CHAT_ID = ''
    TELEGRAM_ALERTS_ENABLED = False

class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False

# Configuration dictionary
config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
} 