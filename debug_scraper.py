import sys
import inspect
import logging
import abc

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper_debug.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('scraper_debug')

logger.info('Starting BaseScraper diagnostic')

# First, attempt to import BaseScraper directly
logger.info('Attempting to import BaseScraper')
try:
    from app.scrapers.base_scraper import BaseScraper
    logger.info('Successfully imported BaseScraper')
except Exception as e:
    logger.error(f'Error importing BaseScraper: {str(e)}')
    sys.exit(1)

# Log BaseScraper class details
logger.info(f'BaseScraper class: {BaseScraper}')
logger.info(f'BaseScraper MRO: {BaseScraper.__mro__}')
logger.info(f'BaseScraper bases: {BaseScraper.__bases__}')

# Check if BaseScraper is abstract
logger.info(f'Is BaseScraper ABC subclass: {issubclass(BaseScraper, abc.ABC)}')

# Inspect BaseScraper method resolution order for ABC
for cls in BaseScraper.__mro__:
    logger.info(f'Class in MRO: {cls}')
    if hasattr(cls, '__abstractmethods__'):
        logger.info(f'Abstract methods in {cls}: {cls.__abstractmethods__}')

# Check if BaseScraper has __abstractmethods__ attribute
if hasattr(BaseScraper, '__abstractmethods__'):
    logger.info(f'BaseScraper abstract methods: {BaseScraper.__abstractmethods__}')
else:
    logger.info('BaseScraper has no __abstractmethods__ attribute')

# List all methods in BaseScraper
logger.info('BaseScraper methods:')
for name, method in inspect.getmembers(BaseScraper, predicate=inspect.isfunction):
    logger.info(f'Method: {name}, Abstract: {getattr(method, "__isabstractmethod__", False)}')

# Try to instantiate BaseScraper
logger.info('Attempting to instantiate BaseScraper')
try:
    base_scraper = BaseScraper()
    logger.info('Successfully instantiated BaseScraper')
except TypeError as e:
    logger.error(f'TypeError instantiating BaseScraper: {str(e)}')
except Exception as e:
    logger.error(f'Other error instantiating BaseScraper: {str(e)}')

# Import and check BestBuyScraper
logger.info('Checking BestBuyScraper')
try:
    from app.scrapers.bestbuy_scraper import BestBuyScraper
    logger.info('Successfully imported BestBuyScraper')
    logger.info(f'BestBuyScraper class: {BestBuyScraper}')
    logger.info(f'BestBuyScraper MRO: {BestBuyScraper.__mro__}')
    logger.info(f'Is BestBuyScraper inheriting from BaseScraper: {issubclass(BestBuyScraper, BaseScraper)}')
    
    logger.info('Attempting to instantiate BestBuyScraper')
    best_buy_scraper = BestBuyScraper()
    logger.info('Successfully instantiated BestBuyScraper')
except Exception as e:
    logger.error(f'Error with BestBuyScraper: {str(e)}')

logger.info('Diagnostic complete') 