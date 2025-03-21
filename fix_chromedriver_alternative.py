"""
Alternative solution for chromedriver installation
This script installs chromedriver-py which includes the chromedriver binary
and sets up the appropriate symlinks for undetected_chromedriver to use it
"""

import os
import sys
import subprocess
import logging
import shutil

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def install_chromedriver_py():
    """Install chromedriver-py which includes the binary"""
    try:
        logger.info("Installing chromedriver-py package...")
        
        # Install the chromedriver-py package
        subprocess.run([
            sys.executable, "-m", "pip", "install", "chromedriver-py==114.0.5735.90"
        ], check=True)
        
        logger.info("chromedriver-py installed successfully")
        return True
    except Exception as e:
        logger.error(f"Error installing chromedriver-py: {e}")
        return False

def create_symlinks():
    """Create necessary symlinks for undetected_chromedriver to find chromedriver"""
    try:
        # Find the chromedriver binary from chromedriver-py
        import chromedriver_py
        chromedriver_binary = chromedriver_py.binary_path
        
        logger.info(f"Found chromedriver binary at: {chromedriver_binary}")
        
        # Create directories for undetected_chromedriver
        home_dir = os.path.expanduser("~")
        uc_dir = os.path.join(home_dir, ".local", "share", "undetected_chromedriver")
        os.makedirs(uc_dir, exist_ok=True)
        
        # Create nested directories
        undetected_dir = os.path.join(uc_dir, "undetected")
        os.makedirs(undetected_dir, exist_ok=True)
        
        chrome_linux_dir = os.path.join(undetected_dir, "chromedriver-linux64")
        os.makedirs(chrome_linux_dir, exist_ok=True)
        
        # Copy the binary to the directory
        target_path = os.path.join(chrome_linux_dir, "chromedriver")
        shutil.copy2(chromedriver_binary, target_path)
        os.chmod(target_path, 0o755)  # Make executable
        
        # Create symlink
        symlink_path = os.path.join(undetected_dir, "undetected_chromedriver")
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(target_path, symlink_path)
        
        logger.info(f"Created symlink at {symlink_path} -> {target_path}")
        return True
    except Exception as e:
        logger.error(f"Error creating symlinks: {e}")
        return False

def fix_chromedriver_directly():
    """Install chromium-browser and manually download chromedriver"""
    try:
        # Install chromium-browser
        logger.info("Installing chromium-browser...")
        subprocess.run(["apt-get", "update"], check=True)
        subprocess.run(["apt-get", "install", "-y", "chromium-browser"], check=True)
        
        # Get chromium version
        result = subprocess.run(
            ["chromium-browser", "--version"], 
            capture_output=True, 
            text=True
        )
        
        version = result.stdout.strip().split()[-1].split('.')[0]
        logger.info(f"Chromium version: {version}")
        
        # Download matching chromedriver
        logger.info(f"Downloading chromedriver for version {version}...")
        download_url = f"https://chromedriver.storage.googleapis.com/LATEST_RELEASE_{version}"
        
        # Get latest release
        result = subprocess.run(
            ["wget", "-qO-", download_url], 
            capture_output=True, 
            text=True
        )
        
        if result.returncode != 0:
            logger.error(f"Failed to get latest chromedriver release: {result.stderr}")
            return False
        
        latest_version = result.stdout.strip()
        logger.info(f"Latest chromedriver version: {latest_version}")
        
        # Download chromedriver
        chromedriver_url = f"https://chromedriver.storage.googleapis.com/{latest_version}/chromedriver_linux64.zip"
        
        # Create temp directory
        temp_dir = "/tmp/chromedriver"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Download and extract
        subprocess.run(["wget", "-q", chromedriver_url, "-O", f"{temp_dir}/chromedriver.zip"], check=True)
        subprocess.run(["unzip", "-q", "-o", f"{temp_dir}/chromedriver.zip", "-d", temp_dir], check=True)
        
        # Create directories for undetected_chromedriver
        home_dir = os.path.expanduser("~")
        uc_dir = os.path.join(home_dir, ".local", "share", "undetected_chromedriver")
        os.makedirs(uc_dir, exist_ok=True)
        
        undetected_dir = os.path.join(uc_dir, "undetected")
        os.makedirs(undetected_dir, exist_ok=True)
        
        chrome_linux_dir = os.path.join(undetected_dir, "chromedriver-linux64")
        os.makedirs(chrome_linux_dir, exist_ok=True)
        
        # Copy the binary and make it executable
        target_path = os.path.join(chrome_linux_dir, "chromedriver")
        shutil.copy2(f"{temp_dir}/chromedriver", target_path)
        os.chmod(target_path, 0o755)
        
        # Create symlink
        symlink_path = os.path.join(undetected_dir, "undetected_chromedriver")
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(target_path, symlink_path)
        
        logger.info("Chromedriver installed successfully!")
        return True
    except Exception as e:
        logger.error(f"Error in fix_chromedriver_directly: {e}")
        return False

if __name__ == "__main__":
    logger.info("Starting chromedriver fix...")
    
    # First try the chromedriver-py approach
    if install_chromedriver_py() and create_symlinks():
        logger.info("Successfully fixed chromedriver with chromedriver-py!")
        sys.exit(0)
    
    # If that doesn't work, try the direct approach
    logger.info("First approach failed, trying direct installation...")
    if fix_chromedriver_directly():
        logger.info("Successfully fixed chromedriver with direct installation!")
        sys.exit(0)
    
    logger.error("Failed to fix chromedriver issue")
    sys.exit(1) 