"""
Captcha handling module.
Provides functionality to solve various CAPTCHA types using external services.
"""

from app.captcha.solver import TwoCaptcha, get_captcha_solver

__all__ = ['TwoCaptcha', 'get_captcha_solver'] 