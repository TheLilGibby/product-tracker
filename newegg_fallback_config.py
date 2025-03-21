"""
Configuration settings for the Newegg HTTP fallback mechanism.
This file allows controlling the fallback behavior without code changes.
"""

import os

# Whether to enable HTTP fallback for Newegg scraping
ENABLE_HTTP_FALLBACK = True

# Whether to always use HTTP fallback (skipping WebDriver attempt)
# This can be useful in environments where Chrome consistently fails
ALWAYS_USE_HTTP_FALLBACK = os.environ.get('ALWAYS_USE_HTTP_FALLBACK', 'false').lower() == 'true'

# Timeout in seconds for WebDriver operations before falling back to HTTP
WEBDRIVER_TIMEOUT = int(os.environ.get('WEBDRIVER_TIMEOUT', '30'))

# User agent to use for HTTP requests
HTTP_USER_AGENT = os.environ.get(
    'HTTP_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)

# Whether to log detailed information about fallback operations
DEBUG_HTTP_FALLBACK = os.environ.get('DEBUG_HTTP_FALLBACK', 'true').lower() == 'true'

# Maximum number of retries for HTTP requests
HTTP_MAX_RETRIES = int(os.environ.get('HTTP_MAX_RETRIES', '3'))

# Delay between retries (in seconds)
HTTP_RETRY_DELAY = int(os.environ.get('HTTP_RETRY_DELAY', '2'))

# Path to save HTML responses for debugging (set to None to disable)
HTTP_DEBUG_PATH = os.environ.get('HTTP_DEBUG_PATH', None)

# Function to update config at runtime
def update_config(config_dict):
    """
    Update configuration parameters at runtime
    
    Args:
        config_dict: Dictionary of configuration parameters to update
    """
    global ENABLE_HTTP_FALLBACK, ALWAYS_USE_HTTP_FALLBACK, WEBDRIVER_TIMEOUT
    global HTTP_USER_AGENT, DEBUG_HTTP_FALLBACK, HTTP_MAX_RETRIES, HTTP_RETRY_DELAY
    global HTTP_DEBUG_PATH
    
    if 'ENABLE_HTTP_FALLBACK' in config_dict:
        ENABLE_HTTP_FALLBACK = config_dict['ENABLE_HTTP_FALLBACK']
    
    if 'ALWAYS_USE_HTTP_FALLBACK' in config_dict:
        ALWAYS_USE_HTTP_FALLBACK = config_dict['ALWAYS_USE_HTTP_FALLBACK']
    
    if 'WEBDRIVER_TIMEOUT' in config_dict:
        WEBDRIVER_TIMEOUT = config_dict['WEBDRIVER_TIMEOUT']
    
    if 'HTTP_USER_AGENT' in config_dict:
        HTTP_USER_AGENT = config_dict['HTTP_USER_AGENT']
    
    if 'DEBUG_HTTP_FALLBACK' in config_dict:
        DEBUG_HTTP_FALLBACK = config_dict['DEBUG_HTTP_FALLBACK']
    
    if 'HTTP_MAX_RETRIES' in config_dict:
        HTTP_MAX_RETRIES = config_dict['HTTP_MAX_RETRIES']
    
    if 'HTTP_RETRY_DELAY' in config_dict:
        HTTP_RETRY_DELAY = config_dict['HTTP_RETRY_DELAY']
    
    if 'HTTP_DEBUG_PATH' in config_dict:
        HTTP_DEBUG_PATH = config_dict['HTTP_DEBUG_PATH'] 