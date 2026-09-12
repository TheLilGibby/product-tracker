import os
import logging
from flask import Flask, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from app.config import config
import atexit
import json
import sys
from logging.handlers import RotatingFileHandler

# Create extensions first (without initializing)
db = SQLAlchemy()
migrate = Migrate()

# The app-wide BackgroundScheduler, set by create_app. Stays None when the app
# is testing or when SCHEDULER_ENABLED is off.
scheduler = None

# Windows consoles default to cp1252; product names carry characters like ™ and –
# which would raise UnicodeEncodeError inside the handler ("--- Logging error ---")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass
# Columns that may be missing from databases created before they were added
# to the Product model. db.create_all() never ALTERs existing tables.
_PRODUCT_SCHEMA_ADDITIONS = (
    ('discord_webhook_url', 'VARCHAR(500)'),
    ('notify_on_price_drop', 'BOOLEAN DEFAULT 1'),
    ('notify_on_availability', 'BOOLEAN DEFAULT 1'),
    ('notify_on_cart', 'BOOLEAN DEFAULT 1'),
    ('auto_cart_enabled', 'BOOLEAN DEFAULT 0'),
    ('auto_cart_quantity', 'INTEGER DEFAULT 1'),
    ('last_cart_attempt', 'DATETIME'),
    ('last_cart_status', 'VARCHAR(100)'),
    ('last_cart_screenshot', 'VARCHAR(120)'),
    ('tracking_enabled', 'BOOLEAN NOT NULL DEFAULT 1'),
)


def _ensure_schema():
    """Add any Product columns that exist on the model but not in SQLite."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if 'products' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('products')}
    missing = [(name, ddl) for name, ddl in _PRODUCT_SCHEMA_ADDITIONS if name not in existing]
    if not missing:
        return

    with db.engine.begin() as conn:
        for name, ddl in missing:
            logger.info(f"Adding missing column products.{name}")
            conn.execute(text(f"ALTER TABLE products ADD COLUMN {name} {ddl}"))


# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('app')

# urllib3 logs every request URL at DEBUG, which for the Telegram Bot API means
# the bot token ends up in app.log. Keep HTTP client logging at INFO.
logging.getLogger('urllib3').setLevel(logging.INFO)

# Selenium's wire log writes the body of every WebDriver response at DEBUG, and
# the response to `driver.page_source` is the whole page. With the root logger at
# DEBUG that is one log line per scrape carrying a megabyte or more of HTML:
# app.log on the integration host reached 470 MB, of which 196 MB was 173 such
# lines - 98% of the file, the longest single line being 1.76 MB. It also leaves
# a copy of every retailer page on disk in a file too large to open. The rest
# below are startup and poll chatter that buries the app's own DEBUG output.
NOISY_LIBRARY_LOGGERS = ('selenium', 'undetected_chromedriver', 'uc', 'apscheduler')


class _LibraryDebugFilter(logging.Filter):
    """
    Drop DEBUG records from the libraries above, wherever they are emitted.

    A filter rather than setLevel: undetected_chromedriver sets its own logger
    to the root's effective level when it is imported, which is DEBUG here and
    silently undoes a setLevel made before the import. Filters on the handlers
    are not something a library can overwrite.
    """

    def filter(self, record):
        if record.levelno > logging.DEBUG:
            return True
        return not record.name.startswith(NOISY_LIBRARY_LOGGERS)


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_LibraryDebugFilter())

def create_app(config_name='default'):
    """
    Create and configure the Flask application.
    
    Args:
        config_name: Configuration to use (default, development, testing, production)
        
    Returns:
        Flask application instance
    """
    logger.info(f"Creating app with config: {config_name}")
    
    # Create the Flask instance
    app = Flask(__name__)
    
    # Load configuration
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)

    # Cloudflare (and any other reverse proxy) sends X-Forwarded-For / Proto / Host.
    # Without this, url_for(_external=True) generates http://127.0.0.1 links.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    
    # Initialize extensions with the app
    db.init_app(app)
    migrate.init_app(app, db)
    
    # Set up logging
    if not app.debug:
        # Configure file handler
        file_handler = RotatingFileHandler('app.log', maxBytes=10240, backupCount=10, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        
        # Configure stderr handler
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        stderr_handler.setLevel(logging.INFO)
        app.logger.addHandler(stderr_handler)
        
        app.logger.setLevel(logging.INFO)
        app.logger.info('Product Tracker startup')
    
    # Add custom Jinja2 filters
    app.jinja_env.filters['tojson'] = json.dumps
    app.jinja_env.filters['fromjson'] = lambda x: json.loads(x)
    
    # Optional password gate, needed before the dashboard is exposed publicly
    from app.auth import register_auth
    register_auth(app)

    # Register blueprints
    from app.routes import main_bp, api_bp
    app.register_blueprint(main_bp)
    from app.routes.groups import groups_bp
    app.register_blueprint(groups_bp)
    app.register_blueprint(api_bp)

    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon',
        )

    # Refuse state-changing requests that came from another site. Installed on
    # the app rather than on a blueprint so everything registered above - main,
    # groups and the JSON API - is covered by default instead of by remembering.
    from app.auth import register_request_guards
    register_request_guards(app)
    
    # Setup error handlers
    from app.errors import register_error_handlers
    register_error_handlers(app)
    
    # Create all database tables, then backfill columns that create_all
    # will not add to an existing SQLite file.
    with app.app_context():
        db.create_all()
        _ensure_schema()
    
    # Initialize and start the scheduler unless this app is testing or has
    # SCHEDULER_ENABLED=0. When it does run it must run
    # exactly once: every init_scheduler() call starts a BackgroundScheduler and
    # a second one made check_auto_cart_opportunities fire twice per minute.
    global scheduler
    if app.config.get('TESTING', False):
        scheduler = None
    elif not app.config.get('SCHEDULER_ENABLED', True):
        # Everything that is not the designated instance runs with
        # SCHEDULER_ENABLED=0 TELEGRAM_ALERTS_ENABLED=0: the database and the UI
        # stay live, but nothing here polls retailers or auto-carts.
        scheduler = None
        app.logger.info(
            "SCHEDULER_ENABLED=0: background scheduler is off - no check, "
            "auto-cart or snapshot job will run in this instance")
    else:
        from app.tasks import init_scheduler
        scheduler = init_scheduler(app)
        
        # Ensure the scheduler shuts down when the app exits
        atexit.register(
            lambda: scheduler.shutdown(wait=False)
            if scheduler is not None and scheduler.running else None)
        
        # Get interval details for logging
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 0)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 10)
        
        # Log with appropriate format based on whether seconds are included
        if seconds > 0:
            app.logger.info(f"Product checker scheduled to run every {minutes} minutes and {seconds} seconds")
        else:
            app.logger.info(f"Product checker scheduled to run every {minutes} minutes")
    
    logger.info("App created successfully")
    return app 