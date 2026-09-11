# Routes package initialization file
from app.routes.main import main_bp
from app.routes.api import api_bp

# Export the blueprints
__all__ = ['main_bp', 'api_bp'] 