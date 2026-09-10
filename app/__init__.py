import os
import logging
from flask import Flask
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

# Windows consoles default to cp1252; product names carry characters like ™ and –
# which would raise UnicodeEncodeError inside the handler ("--- Logging error ---")
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass

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
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    # Refuse state-changing requests that came from another site. Installed on
    # the app rather than on main_bp so anything registered later - api_bp, which
    # is not on this branch yet - is covered by default instead of by remembering.
    from app.auth import register_request_guards
    register_request_guards(app)
    
    # Setup error handlers
    from app.errors import register_error_handlers
    register_error_handlers(app)
    
    # Create all database tables
    with app.app_context():
        db.create_all()
    
    # Initialize and start the scheduler if not in testing mode. This must run
    # exactly once: every init_scheduler() call starts a BackgroundScheduler and
    # a second one made check_auto_cart_opportunities fire twice per minute.
    if not app.config.get('TESTING', False):
        global scheduler
        from app.tasks import init_scheduler
        scheduler = init_scheduler(app)
        
        # Ensure the scheduler shuts down when the app exits
        atexit.register(lambda: scheduler.shutdown(wait=False))
        
        # Get interval details for logging
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 15)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 0)
        
        # Log with appropriate format based on whether seconds are included
        if seconds > 0:
            app.logger.info(f"Product checker scheduled to run every {minutes} minutes and {seconds} seconds")
        else:
            app.logger.info(f"Product checker scheduled to run every {minutes} minutes")
    
    logger.info("App created successfully")
    return app 