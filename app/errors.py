"""
Error handlers for the Flask application.
"""

import logging
from flask import render_template, jsonify, request

# Set up logger
logger = logging.getLogger(__name__)

def register_error_handlers(app):
    """
    Register error handlers for common HTTP errors.
    
    Args:
        app: The Flask application instance
    """
    @app.errorhandler(404)
    def not_found_error(error):
        """Handle 404 Not Found errors."""
        logger.warning(f"404 error: {error}")
        return render_template('errors/404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        """Handle 500 Internal Server errors."""
        logger.error(f"500 error: {error}")
        return render_template('errors/500.html'), 500
    
    @app.errorhandler(403)
    def forbidden_error(error):
        """Handle 403 Forbidden errors."""
        logger.warning(f"403 error: {error}")
        return render_template('errors/403.html'), 403
    
    # API error handlers
    @app.errorhandler(400)
    def bad_request_error(error):
        """Handle 400 Bad Request errors."""
        logger.warning(f"400 error: {error}")
        if request.path.startswith('/api/'):
            return jsonify(error="Bad Request", message=str(error)), 400
        return render_template('errors/400.html'), 400
    
    logger.debug("Error handlers registered") 