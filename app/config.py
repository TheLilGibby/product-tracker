import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

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
    
    # Default timezone setting (uses UTC by default)
    DEFAULT_TIMEZONE = os.environ.get('DEFAULT_TIMEZONE', 'UTC')
    
    # Time display format preference (24-hour/military or 12-hour/AM-PM)
    # Options: '24h' or '12h'
    TIME_FORMAT = os.environ.get('TIME_FORMAT', '24h')
    
    # Telegram alerts: one global channel for every product (leave blank to disable)
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
    

    # Minimum minutes between auto-cart attempts for the same product
    AUTO_CART_COOLDOWN_MINUTES = int(os.environ.get('AUTO_CART_COOLDOWN_MINUTES', 30))

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