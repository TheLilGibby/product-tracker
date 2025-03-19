"""
Test script for the Newegg scraper.
"""

import os
import sys
import logging
from app import create_app
from app.scrapers.newegg_scraper import NeweggScraper

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('test_newegg')

def test_captcha_module():
    """Test the captcha module with the Newegg scraper."""
    # Create app context
    app = create_app('development')
    
    with app.app_context():
        logger.info("Initializing Newegg scraper...")
        scraper = NeweggScraper()
        
        # Check if the TwoCaptcha solver was initialized
        if scraper.solver:
            logger.info("TwoCaptcha solver initialized successfully!")
            logger.info(f"API_KEY is set: {bool(scraper.solver.solver.API_KEY)}")
            logger.info("Test successful!")
        else:
            logger.error("TwoCaptcha solver not initialized")

if __name__ == "__main__":
    print("Testing Newegg scraper captcha integration...")
    test_captcha_module()
    print("Test completed.") 