"""
Captcha solver module.
Provides an interface to various CAPTCHA solving services.
Currently supports 2captcha.com with a uniform interface.
"""

import os
import logging
from twocaptcha.solver import TwoCaptcha as OriginalTwoCaptcha
from twocaptcha.api import ApiClient, NetworkException, ApiException

# Set up logging
logger = logging.getLogger('app.captcha.solver')

class TwoCaptcha:
    """
    Wrapper for the 2captcha-python package, providing a uniform interface.
    """
    
    def __init__(self, api_key=None):
        """
        Initialize the 2captcha solver with an API key.
        
        Args:
            api_key (str, optional): The 2captcha API key. If None, will attempt to 
                                     retrieve from environment variables.
        """
        if api_key is None:
            api_key = os.environ.get('TWOCAPTCHA_API_KEY')
            if not api_key:
                logger.warning("No 2captcha API key found in environment variables")
                
        self.solver = OriginalTwoCaptcha(api_key)
        logger.debug("2captcha solver initialized")
    
    def recaptcha(self, sitekey, url, version='v2', action=None, enterprise=0):
        """
        Solve a reCAPTCHA challenge.
        
        Args:
            sitekey (str): The site key for the reCAPTCHA
            url (str): The URL where the reCAPTCHA is located
            version (str): The reCAPTCHA version ('v2' or 'v3')
            action (str, optional): The action for reCAPTCHA v3
            enterprise (int): Whether this is an enterprise reCAPTCHA (0 or 1)
            
        Returns:
            dict: The solution response with 'code' key containing the solution
        """
        try:
            logger.debug(f"Solving reCAPTCHA {version} with site key {sitekey} at {url}")
            
            kwargs = {
                "sitekey": sitekey,
                "url": url
            }
            
            if version == 'v3':
                if action:
                    kwargs["action"] = action
                kwargs["version"] = 'v3'
            
            if enterprise:
                kwargs["enterprise"] = 1
                
            result = self.solver.recaptcha(**kwargs)
            logger.debug("reCAPTCHA solution received")
            return result
            
        except (NetworkException, ApiException) as e:
            logger.error(f"Error solving reCAPTCHA: {str(e)}")
            return None
    
    def funcaptcha(self, sitekey, url, data=None):
        """
        Solve a FunCaptcha (Arkose Labs) challenge.
        
        Args:
            sitekey (str): The public key for the FunCaptcha
            url (str): The URL where the FunCaptcha is located
            data (dict, optional): Additional parameters for FunCaptcha
            
        Returns:
            dict: The solution response with 'code' key containing the solution
        """
        try:
            logger.debug(f"Solving FunCaptcha with site key {sitekey} at {url}")
            
            kwargs = {
                "sitekey": sitekey,
                "url": url
            }
            
            if data:
                for key, value in data.items():
                    kwargs[key] = value
                
            result = self.solver.funcaptcha(**kwargs)
            logger.debug("FunCaptcha solution received")
            return result
            
        except (NetworkException, ApiException) as e:
            logger.error(f"Error solving FunCaptcha: {str(e)}")
            return None
    
    def hcaptcha(self, sitekey, url):
        """
        Solve an hCaptcha challenge.
        
        Args:
            sitekey (str): The site key for the hCaptcha
            url (str): The URL where the hCaptcha is located
            
        Returns:
            dict: The solution response with 'code' key containing the solution
        """
        try:
            logger.debug(f"Solving hCaptcha with site key {sitekey} at {url}")
            
            result = self.solver.hcaptcha(
                sitekey=sitekey,
                url=url
            )
            logger.debug("hCaptcha solution received")
            return result
            
        except (NetworkException, ApiException) as e:
            logger.error(f"Error solving hCaptcha: {str(e)}")
            return None
    
    def normal(self, image_url=None, file_path=None, base64=None):
        """
        Solve a standard image CAPTCHA.
        
        Args:
            image_url (str, optional): URL to the CAPTCHA image
            file_path (str, optional): Path to a local CAPTCHA image file
            base64 (str, optional): Base64-encoded CAPTCHA image
            
        Returns:
            dict: The solution response with 'code' key containing the solution
        """
        try:
            logger.debug("Solving image CAPTCHA")
            
            kwargs = {}
            if image_url:
                kwargs["imageUrl"] = image_url
            elif file_path:
                kwargs["file"] = file_path
            elif base64:
                kwargs["base64"] = base64
            else:
                logger.error("No image source provided for image CAPTCHA")
                return None
                
            result = self.solver.normal(**kwargs)
            logger.debug("Image CAPTCHA solution received")
            return result
            
        except (NetworkException, ApiException) as e:
            logger.error(f"Error solving image CAPTCHA: {str(e)}")
            return None

def get_captcha_solver(api_key=None):
    """
    Factory function to get a captcha solver instance.
    
    Args:
        api_key (str, optional): API key for the CAPTCHA service
        
    Returns:
        TwoCaptcha: An instance of the TwoCaptcha solver
    """
    return TwoCaptcha(api_key) 