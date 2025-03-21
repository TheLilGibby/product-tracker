"""
Script to check for available ChromeDriver versions for Chrome 134
"""
import requests
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def check_possible_versions():
    """Check for possible ChromeDriver versions for Chrome 134"""
    # Base URL for Chrome for Testing downloads
    base_url = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
    
    try:
        # Fetch the list of available versions
        logger.info(f"Fetching available versions from {base_url}")
        response = requests.get(base_url)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch versions: Status code {response.status_code}")
            return
            
        data = response.json()
        
        # Filter for versions that match 134
        logger.info("Filtering for Chrome 134 versions...")
        chrome_134_versions = []
        
        for version in data.get('versions', []):
            if version.get('version', '').startswith('134.'):
                downloads = version.get('downloads', {})
                if 'chromedriver' in downloads:
                    for platform in downloads['chromedriver']:
                        if platform['platform'] == 'linux64':
                            chrome_134_versions.append({
                                'version': version['version'],
                                'url': platform['url']
                            })
        
        # Sort versions by newest first
        chrome_134_versions.sort(key=lambda x: x['version'], reverse=True)
        
        # Print available versions
        if chrome_134_versions:
            logger.info(f"Found {len(chrome_134_versions)} versions for Chrome 134:")
            for i, version in enumerate(chrome_134_versions):
                logger.info(f"{i+1}. Version: {version['version']}")
                logger.info(f"   URL: {version['url']}")
        else:
            logger.error("No versions found for Chrome 134")
            
        return chrome_134_versions
    
    except Exception as e:
        logger.error(f"Error fetching ChromeDriver versions: {e}")
        return []

if __name__ == "__main__":
    logger.info("Checking for available ChromeDriver versions for Chrome 134...")
    versions = check_possible_versions()
    
    if versions:
        logger.info(f"Found {len(versions)} suitable ChromeDriver versions.")
        # Print the newest one as recommendation
        logger.info(f"Recommended version: {versions[0]['version']}")
        logger.info(f"Download URL: {versions[0]['url']}")
    else:
        logger.error("No suitable ChromeDriver versions found.") 