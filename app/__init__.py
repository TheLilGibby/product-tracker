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
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('app')

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
        file_handler = RotatingFileHandler('app.log', maxBytes=10240, backupCount=10)
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
    
    # Register blueprints
    from app.routes import main_bp, api_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)

    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'favicon.ico',
            mimetype='image/vnd.microsoft.icon',
        )

    from app.auth import register_auth
    register_auth(app)
    
    # Setup error handlers
    from app.errors import register_error_handlers
    register_error_handlers(app)
    
    # Create all database tables, then backfill columns that create_all
    # will not add to an existing SQLite file.
    with app.app_context():
        db.create_all()
        _ensure_schema()
    
    # Initialize scheduler for periodic tasks
    from app.tasks import init_scheduler
    init_scheduler(app)
    
    # Initialize and start the scheduler if not in testing mode
    if not app.config.get('TESTING', False):
        global scheduler
        from app.tasks import init_scheduler
        scheduler = init_scheduler(app)
        
        # Ensure the scheduler shuts down when the app exits
        atexit.register(lambda: scheduler.shutdown(wait=False))
        
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