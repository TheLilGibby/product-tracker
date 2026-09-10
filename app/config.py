import json
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


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