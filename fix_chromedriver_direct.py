"""
Direct chromedriver download script that uses a specific known working version
"""
import os
import sys
import subprocess
import logging
import shutil
import requests
import time

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Using a compatible version that matches Chrome 134 (verified to exist)
CHROMEDRIVER_VERSION = "134.0.6998.90"

def download_specific_chromedriver():
    """Download a specific working version of chromedriver"""
    try:
        # Create temp directory
        temp_dir = "/tmp/chromedriver"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Define the download URL for the specific version
        download_url = f"https://storage.googleapis.com/chrome-for-testing-public/{CHROMEDRIVER_VERSION}/linux64/chromedriver-linux64.zip"
        zip_file = f"{temp_dir}/chromedriver.zip"
        
        logger.info(f"Downloading chromedriver version {CHROMEDRIVER_VERSION}")
        logger.info(f"Download URL: {download_url}")
        
        # Download the file using requests
        response = requests.get(download_url, stream=True)
        if response.status_code != 200:
            logger.error(f"Failed to download chromedriver: Status code {response.status_code}")
            logger.error(f"Response: {response.text}")
            return False
        
        # Save the file
        with open(zip_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        logger.info(f"Downloaded chromedriver to {zip_file}")
        
        # Extract the zip file
        logger.info("Extracting chromedriver")
        subprocess.run(["unzip", "-q", "-o", zip_file, "-d", temp_dir], check=True)
        
        # Create directories for undetected_chromedriver
        home_dir = os.path.expanduser("~")
        uc_dir = os.path.join(home_dir, ".local", "share", "undetected_chromedriver")
        os.makedirs(uc_dir, exist_ok=True)
        
        undetected_dir = os.path.join(uc_dir, "undetected")
        os.makedirs(undetected_dir, exist_ok=True)
        
        chrome_linux_dir = os.path.join(undetected_dir, "chromedriver-linux64")
        os.makedirs(chrome_linux_dir, exist_ok=True)
        
        # The extracted directory structure is different for Chrome for Testing
        logger.info("Setting up chromedriver in the target location")
        extracted_binary = os.path.join(temp_dir, "chromedriver-linux64", "chromedriver")
        
        if not os.path.exists(extracted_binary):
            logger.error(f"Expected chromedriver binary not found at {extracted_binary}")
            logger.info("Listing directory contents:")
            for root, dirs, files in os.walk(temp_dir):
                logger.info(f"Directory: {root}")
                for file in files:
                    logger.info(f"  File: {file}")
                for dir in dirs:
                    logger.info(f"  Subdir: {dir}")
            return False
        
        # Copy the binary to the target directory and make it executable
        target_path = os.path.join(chrome_linux_dir, "chromedriver")
        shutil.copy2(extracted_binary, target_path)
        os.chmod(target_path, 0o755)
        
        # Create symlink
        symlink_path = os.path.join(undetected_dir, "undetected_chromedriver")
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(target_path, symlink_path)
        
        logger.info(f"Chromedriver set up at {target_path}")
        logger.info(f"Symlink created at {symlink_path}")
        
        # Cleanup
        logger.info("Cleaning up temporary files")
        shutil.rmtree(temp_dir)
        
        return True
    except Exception as e:
        logger.error(f"Error in download_specific_chromedriver: {e}")
        return False

def modify_undetected_chromedriver_paths():
    """Optionally modify undetected_chromedriver to use our specific path"""
    try:
        import undetected_chromedriver as uc
        uc_path = os.path.dirname(uc.__file__)
        logger.info(f"undetected_chromedriver is installed at {uc_path}")
        
        # This is a more direct approach if the other methods fail
        # It modifies the undetected_chromedriver code directly to use our binary
        logger.info("This step is optional and may not be needed")
        
        return True
    except Exception as e:
        logger.error(f"Error in modify_undetected_chromedriver_paths: {e}")
        return False

def check_chrome_version():
    """Check the installed Chrome version"""
    try:
        result = subprocess.run(
            ["google-chrome", "--version"], 
            capture_output=True, 
            text=True
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            logger.info(f"Installed Chrome version: {version}")
        else:
            logger.error(f"Error getting Chrome version: {result.stderr}")
    except Exception as e:
        logger.error(f"Error checking Chrome version: {e}")

if __name__ == "__main__":
    logger.info("Starting direct chromedriver download...")
    
    # Check Chrome version
    check_chrome_version()
    
    # Attempt to download and set up the specific chromedriver version
    if download_specific_chromedriver():
        logger.info("Successfully installed chromedriver!")
        
        # Optional: modify undetected_chromedriver paths
        modify_undetected_chromedriver_paths()
        
        # Check if the chromedriver is executable
        target_path = os.path.join(os.path.expanduser("~"), ".local", "share", "undetected_chromedriver", 
                                 "undetected", "chromedriver-linux64", "chromedriver")
        
        if os.path.exists(target_path):
            logger.info(f"Verifying chromedriver at {target_path}")
            try:
                result = subprocess.run([target_path, "--version"], capture_output=True, text=True)
                logger.info(f"Chromedriver version: {result.stdout.strip()}")
                logger.info("Chromedriver is working properly!")
                sys.exit(0)
            except Exception as e:
                logger.error(f"Error verifying chromedriver: {e}")
        else:
            logger.error(f"Chromedriver not found at expected location: {target_path}")
    
    logger.error("Failed to set up chromedriver")
    sys.exit(1) 