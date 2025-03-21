"""
Script to install chromedriver for undetected_chromedriver
This will download and install the correct chromedriver version for the installed Chrome browser
"""

import os
import subprocess
import sys
import shutil
import undetected_chromedriver as uc
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def install_chromedriver():
    """Install chromedriver for undetected_chromedriver"""
    try:
        logger.info("Checking Chrome version...")
        
        # Get Chrome version
        result = subprocess.run(
            ["google-chrome", "--version"], 
            capture_output=True, 
            text=True
        )
        
        if result.returncode != 0:
            logger.error(f"Error checking Chrome version: {result.stderr}")
            return False
        
        chrome_version = result.stdout.strip().split()[-1]
        logger.info(f"Chrome version: {chrome_version}")
        
        # Create directories if they don't exist
        home_dir = os.path.expanduser("~")
        driver_dir = os.path.join(home_dir, ".local", "share", "undetected_chromedriver")
        os.makedirs(driver_dir, exist_ok=True)
        
        # Install chromedriver with undetected_chromedriver
        logger.info("Installing chromedriver with undetected_chromedriver...")
        uc.install(executable_path=os.path.join(driver_dir, "chromedriver"))
        
        logger.info("Chromedriver installed successfully!")
        return True
    except Exception as e:
        logger.error(f"Error installing chromedriver: {e}")
        return False

if __name__ == "__main__":
    if install_chromedriver():
        logger.info("Chromedriver installation completed successfully")
        sys.exit(0)
    else:
        logger.error("Chromedriver installation failed")
        sys.exit(1) 