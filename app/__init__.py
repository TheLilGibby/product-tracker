import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from app.config import config
import atexit

# Create extensions first (without initializing)
db = SQLAlchemy()
migrate = Migrate()

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
        config_name: The configuration profile to use ('development', 'production', 'testing', or 'default')
        
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
    
    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)
    
    # Setup error handlers
    from app.errors import register_error_handlers
    register_error_handlers(app)
    
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
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 15)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 0)
        
        # Log with appropriate format based on whether seconds are included
        if seconds > 0:
            app.logger.info(f"Product checker scheduled to run every {minutes} minutes and {seconds} seconds")
        else:
            app.logger.info(f"Product checker scheduled to run every {minutes} minutes")
    
    logger.info("App created successfully")
    return app 