import logging
from collections import deque
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app
from app import db
from app.models.product import Product, PriceHistory
from app.cart_screenshots import save_cart_screenshot
from app.scrapers import (detect_store_type, get_scraper, is_by_design_refusal,
                          store_display_name)
from app.notifications import send_product_alert, notify_cart_success
from app.notifications.telegram import TelegramNotifier
import urllib.parse
import threading
import atexit

# Set up logger
logger = logging.getLogger(__name__)

# Global scheduler reference
scheduler = None

# Lock for check_all_products to prevent concurrent execution
check_lock = threading.Lock()

# --------------------------------------------------------------------------
# Per-store backoff
#
# The retailers with bot protection answer a burst of failed scrapes by
# blocking harder and for longer: Best Buy's Akamai starts serving block pages
# to every request, Target puts up the PX press-and-hold. Retrying that store
# on every check cycle is what turns a minute of rate limiting into hours of
# block pages, so a store that keeps failing is left alone for a while.
#
# Defaults: two consecutive failures buy 15 minutes of quiet, and each further
# failure doubles that up to an hour.
DEFAULT_STORE_BACKOFF_FAILURES = 2
DEFAULT_STORE_BACKOFF_MINUTES = 15
STORE_BACKOFF_MAX_MINUTES = 60

# store_type -> consecutive failed scrapes, and the UTC time it may be tried
# again. Only touched from check_all_products, which check_lock serializes.
_store_failures = {}
_store_retry_at = {}

# --------------------------------------------------------------------------
# Scheduler liveness
#
# A dead scheduler is invisible from outside the process: the dashboard still
# answers 200, the jobs still show as registered, and the only trace is a
# traceback in a log nobody is reading. That is how the live tracker sat dead
# for 83 minutes on 2026-09-12 while looking perfectly healthy, and it is why
# "has a check actually completed recently" has to be a first-class question
# rather than something inferred from an HTTP status.
#
# All times here are naive UTC, matching the rest of the app.
_health_lock = threading.Lock()
_process_started_at = datetime.utcnow()
_last_pass_started = None
_last_pass_completed = None
# True between entering check_all_products and leaving it, however it leaves.
#
# This must NOT be inferred from the timestamps. "started is newer than
# completed" is true both for a pass that is still running and for one that
# raised and never completed - and the outage this whole module exists for was
# the second kind, firing every interval. Inferring it would refresh the start
# time every 19 seconds, keep the tracker permanently "in flight", and exempt
# it from the stall check forever. The flag is cleared in check_all_products'
# outer finally, so a crash clears it just as a clean return does.
_pass_in_flight = False
# None until a scheduler has been configured in this process.
_interval_seconds = None
_scheduler_enabled = False

# How many intervals may elapse with nothing completing before it is a stall.
OVERDUE_INTERVAL_MULTIPLE = 3
# ...but never alarm sooner than this. At a 19-second interval three intervals
# is under a minute, and one slow retailer scrape would trip it.
MIN_OVERDUE_SECONDS = 90
WATCHDOG_POLL_SECONDS = 30

# Durations (seconds) of the last few completed passes, newest last. Used to
# size the hung-pass cap below off what this instance actually does, rather
# than off the check interval, which says nothing about how long a pass takes.
PASS_DURATION_SAMPLES = 5
_pass_durations = deque(maxlen=PASS_DURATION_SAMPLES)

# A pass that is still running is not a stall, but it cannot be exempt forever:
# check_all_products holds check_lock for its whole run, so one Chrome that
# hangs blocks every later pass and no timestamp ever advances again. That is a
# real outage the user must hear about, so an in-flight pass is only exempt up
# to this cap. The floor is deliberately generous - five products, several of
# them driving undetected-chromedriver through CAPTCHAs, is minutes of honest
# work - because the cost of crying wolf on the live channel is that the next
# real alert gets ignored.
HUNG_PASS_FLOOR_SECONDS = 300
HUNG_PASS_DURATION_MULTIPLE = 2


def _iso_z(moment):
    """A naive-UTC datetime as ISO 8601 with an explicit Z, or None."""
    if moment is None:
        return None
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _record_pass_started(now=None):
    global _last_pass_started, _pass_in_flight
    with _health_lock:
        _last_pass_started = now or datetime.utcnow()
        _pass_in_flight = True


def _record_pass_finished():
    """
    The pass has left check_all_products, by any route.

    Called from the outer finally, so it runs after a clean pass, a pass that
    raised, and a pass killed by an exception in the app-context handling.
    Completion is recorded separately and only on the success path: finishing
    and succeeding are different facts, and conflating them is what would let a
    permanently failing tracker report itself healthy.
    """
    global _pass_in_flight
    with _health_lock:
        _pass_in_flight = False


def _record_pass_completed(now=None):
    global _last_pass_completed
    with _health_lock:
        _last_pass_completed = now or datetime.utcnow()
        # Keep how long it took, so the hung-pass cap tracks reality. Guarded
        # against a clock that went backwards and against a completion with no
        # matching start (only reachable if the globals were set by hand).
        if _last_pass_started is not None:
            elapsed = (_last_pass_completed - _last_pass_started).total_seconds()
            if elapsed >= 0:
                _pass_durations.append(elapsed)


def overdue_threshold_seconds(interval_seconds=None):
    """Seconds without a completed pass that count as stalled."""
    interval = _interval_seconds if interval_seconds is None else interval_seconds
    if not interval:
        return MIN_OVERDUE_SECONDS
    return max(int(interval) * OVERDUE_INTERVAL_MULTIPLE, MIN_OVERDUE_SECONDS)


def in_flight_cap_seconds():
    """
    How long a single pass may run before it counts as hung.

    Sized off the slowest of the last few completed passes rather than off the
    check interval: the interval controls how often a pass starts, and says
    nothing about how long one takes. On the live instance that difference is
    the whole point - a 19-second interval with 50-second passes was one slow
    Chrome away from reporting a stall on a tracker that was working.

    Falls back to the floor until a pass has completed, so a first pass that
    hangs is still caught.
    """
    with _health_lock:
        durations = list(_pass_durations)
    return _cap_from(durations)


def _cap_from(durations):
    """The cap for an already-read list of durations. _health_lock is a plain
    Lock, not an RLock, so callers that already hold it must use this."""
    if not durations:
        return HUNG_PASS_FLOOR_SECONDS
    return max(int(max(durations) * HUNG_PASS_DURATION_MULTIPLE),
               HUNG_PASS_FLOOR_SECONDS)


def scheduler_health(now=None):
    """
    Whether this process is actually checking products, as a plain dict.

    The shape is the `scheduler` object of GET /api/health; the route wraps the
    instance label around it.

    `overdue` is the only field worth alerting on. It is False whenever the
    scheduler is switched off, because a UI-only instance is not supposed to be
    checking anything. A process that has never completed a pass gets one
    threshold of grace from boot, so a fresh start is not born stalled.

    There are two distinct failures here and they need different clocks:

      * idle - nothing is running and nothing has completed recently. This is
        the 83-minute outage: every job crashed on fire, so no pass ever
        started. Measured as time since the last completed pass (or boot).
      * hung - a pass started and never finished. check_all_products holds
        check_lock for its whole run, so this also blocks every later pass.
        Measured as time since the current pass started, against a cap sized
        off real pass durations.

    A pass that is merely slow is neither, which is the false positive this
    guards against: the idle clock must not run while a pass is in flight, or
    any pass longer than the threshold reports a stall on a healthy tracker.
    """
    now = now or datetime.utcnow()
    with _health_lock:
        started = _last_pass_started
        completed = _last_pass_completed
        interval = _interval_seconds
        enabled = _scheduler_enabled
        booted = _process_started_at
        cap = _cap_from(list(_pass_durations))
        in_flight = _pass_in_flight

    running_seconds = ((now - started).total_seconds()
                       if in_flight and started is not None else None)
    if running_seconds is None:
        # A flag with no start time cannot be timed, so do not let it suppress
        # the idle clock - unmeasurable is not the same as healthy.
        in_flight = False

    threshold = overdue_threshold_seconds(interval)
    if not enabled:
        overdue = False
    elif in_flight:
        overdue = running_seconds > cap
    elif completed is None:
        overdue = (now - booted).total_seconds() > threshold
    else:
        overdue = (now - completed).total_seconds() > threshold

    return {
        'enabled': enabled,
        'interval_seconds': int(interval or 0),
        'last_pass_started': _iso_z(started),
        'last_pass_completed': _iso_z(completed),
        'overdue': overdue,
        'in_flight': in_flight,
        'current_pass_seconds': None if running_seconds is None else int(running_seconds),
        'in_flight_cap_seconds': cap,
    }


# The watchdog deliberately does NOT run as an APScheduler job: a dead
# scheduler cannot report its own death, which is the whole failure being
# guarded against. It is a plain daemon thread, so it also cannot hold the
# process open at exit.
_watchdog_thread = None
_watchdog_stop = None


def _watchdog_tick(app, was_overdue, now=None):
    """
    One watchdog evaluation. Returns the overdue state to carry forward.

    Posts only on a transition, so a stall produces exactly one "stalled"
    message and one "recovered" message however long it lasts. Split out from
    the loop so it is testable without waiting on a real poll interval.
    """
    health = scheduler_health(now=now)
    overdue = health['overdue']
    if overdue == was_overdue:
        return overdue

    since = health['last_pass_completed'] or ('process start ' + _iso_z(_process_started_at))
    if overdue and health['in_flight']:
        # Distinct from the idle case on purpose: "no completed check since X"
        # would send someone hunting for a dead scheduler when the scheduler is
        # fine and one pass is wedged, most likely on a Chrome that never
        # returned. Naming the stuck pass points at the actual thing to kill.
        logger.error(
            "Tracker stalled: the pass started at %s has been running %ss, past "
            "the %ss cap; check_lock is held, so no later pass can run",
            health['last_pass_started'], health['current_pass_seconds'],
            health['in_flight_cap_seconds'])
        message = ('tracker stalled: a check started at %s is still running after '
                   '%ss (cap %ss) and is blocking every later check'
                   % (health['last_pass_started'], health['current_pass_seconds'],
                      health['in_flight_cap_seconds']))
    elif overdue:
        logger.error(
            "Tracker stalled: no completed product check since %s (interval %ss, "
            "overdue after %ss)", since, health['interval_seconds'],
            overdue_threshold_seconds(health['interval_seconds'] or None))
        message = 'tracker stalled: no completed check since %s' % since
    else:
        logger.info("Tracker recovered: completed a product check at %s",
                    health['last_pass_completed'])
        message = 'tracker recovered: completed a check at %s' % health['last_pass_completed']

    # Under an app context so TELEGRAM_ALERTS_ENABLED and INSTANCE_LABEL are
    # read from config exactly as they are for a scheduled alert. send_message
    # already gates on the former and applies the latter, so an instance that
    # is not the designated sender stays silent here too. There is no user
    # content in the text, so nothing to escape.
    try:
        with app.app_context():
            TelegramNotifier.send_message(message)
    except Exception:
        logger.error("Could not post scheduler watchdog message", exc_info=True)

    return overdue


def _watchdog_loop(app, stop_event, poll_seconds=WATCHDOG_POLL_SECONDS):
    was_overdue = False
    # wait() first, so a freshly started process is never alarmed on instantly.
    while not stop_event.wait(poll_seconds):
        try:
            was_overdue = _watchdog_tick(app, was_overdue)
        except Exception:
            logger.error("Scheduler watchdog tick failed", exc_info=True)


def start_scheduler_watchdog(app, poll_seconds=WATCHDOG_POLL_SECONDS):
    """Start (or restart) the liveness watchdog thread. Returns the thread."""
    global _watchdog_thread, _watchdog_stop
    if _watchdog_stop is not None:
        _watchdog_stop.set()
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        _watchdog_thread.join(timeout=2)
    _watchdog_stop = threading.Event()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        args=(app, _watchdog_stop, poll_seconds),
        name='scheduler-watchdog',
        daemon=True,
    )
    _watchdog_thread.start()
    logger.info("Scheduler watchdog started (polling every %ss)", poll_seconds)
    return _watchdog_thread


def stop_scheduler_watchdog():
    """Stop the watchdog thread if one is running. Used by tests and at exit."""
    global _watchdog_thread, _watchdog_stop
    if _watchdog_stop is not None:
        _watchdog_stop.set()
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        _watchdog_thread.join(timeout=2)
    _watchdog_thread = None
    _watchdog_stop = None


def _backoff_settings():
    """
    (failures before backing off, first backoff in minutes) from app config.

    Falls back to the defaults outside an application context so the helpers
    below stay usable from a script or a test.
    """
    config = _config()
    return (int(config.get('STORE_BACKOFF_FAILURES', DEFAULT_STORE_BACKOFF_FAILURES)),
            float(config.get('STORE_BACKOFF_MINUTES', DEFAULT_STORE_BACKOFF_MINUTES)))


def _config():
    """current_app.config, or an empty mapping outside an app context."""
    try:
        return current_app.config
    except RuntimeError:
        return {}


def store_is_backed_off(store_type, now=None):
    """
    True when this store failed too often recently and should be skipped.

    Logs one INFO the first time a store comes due again, so the log says why
    a store went quiet and when it came back.
    """
    retry_at = _store_retry_at.get(store_type)
    if retry_at is None:
        return False

    now = now or datetime.utcnow()
    if now < retry_at:
        return True

    del _store_retry_at[store_type]
    logger.info(f"Retrying {store_type} after backoff "
                f"({_store_failures.get(store_type, 0)} consecutive failures so far)")
    return False


def record_store_failure(store_type, reason, now=None):
    """
    Count a failed scrape and, once there have been enough, put the store on ice.

    Returns the number of consecutive failures. The WARNING fires only when the
    store actually enters backoff, so it is one line per cycle at worst.
    """
    failures = _store_failures.get(store_type, 0) + 1
    _store_failures[store_type] = failures
    threshold, base_minutes = _backoff_settings()

    if failures < threshold:
        logger.debug(f"{store_type} scrape failed ({reason}); "
                     f"{failures} of {threshold} before backing off")
        return failures

    cap = float(_config().get('STORE_BACKOFF_MAX_MINUTES', STORE_BACKOFF_MAX_MINUTES))
    minutes = min(base_minutes * (2 ** (failures - threshold)), cap)
    _store_retry_at[store_type] = (now or datetime.utcnow()) + timedelta(minutes=minutes)
    logger.warning(f"{store_type} has failed {failures} scrapes in a row ({reason}); "
                   f"skipping its products for {minutes:g} minutes")
    return failures


def record_store_success(store_type):
    """Clear a store's failure history after a scrape that actually worked."""
    if _store_failures.pop(store_type, None):
        logger.info(f"{store_type} is answering again; backoff cleared")
    _store_retry_at.pop(store_type, None)


def reset_store_backoff():
    """Forget every store's backoff state. For tests and manual recovery."""
    _store_failures.clear()
    _store_retry_at.clear()


# --------------------------------------------------------------------------
# Per-store check intervals
#
# The scheduler runs on one global interval, but not every store can take it.
# A store listed in STORE_CHECK_INTERVALS is only scraped once its own interval
# has passed; everything else keeps the global cadence.
def store_check_interval(store_type):
    """
    Minutes between checks for this store, or None when it has no setting of
    its own and just follows the scheduler's global interval.
    """
    intervals = _config().get('STORE_CHECK_INTERVALS') or {}
    try:
        return float(intervals[store_type])
    except (KeyError, TypeError, ValueError):
        return None


def store_check_is_due(store_type, last_checked, now=None):
    """
    False only when a store has an interval of its own and was checked inside it.

    A store with no setting is left to the scheduler's cadence rather than
    gated here. Gating it on the global interval would read the same on a
    scheduled run and break the "Update All Products" button, which calls
    check_all_products directly and must still refresh what the user asked for.
    """
    interval = store_check_interval(store_type)
    if interval is None or interval <= 0 or last_checked is None:
        return True

    # The grace keeps a store whose interval matches the scheduler's own from
    # slipping a whole cycle when a run starts a moment early.
    grace = min(60.0, interval * 60.0 * 0.1)
    elapsed = ((now or datetime.utcnow()) - last_checked).total_seconds()
    return elapsed >= (interval * 60.0) - grace


def _minutes_until(moment, now):
    """Whole minutes from now until moment, rounded up."""
    return int(-(-(moment - now).total_seconds() // 60))


def get_store_backoff_state(now=None):
    """
    One row per store the scheduler is currently having trouble with.

    A store that is answering normally holds no state at all, so an empty list
    means every store is fine. Each row is:

        {'store_type', 'label', 'backed_off', 'failures',
         'retry_at' (naive UTC, or None), 'minutes_remaining'}

    Read without check_lock - the scheduler thread may be writing while a
    request reads. The dict copies below are single C-level operations, and a
    row that is a few seconds stale only affects what the page says.
    """
    now = now or datetime.utcnow()
    failures = dict(_store_failures)
    retry_times = dict(_store_retry_at)

    rows = []
    for store_type in sorted(set(failures) | set(retry_times)):
        retry_at = retry_times.get(store_type)
        backed_off = retry_at is not None and now < retry_at
        rows.append({
            'store_type': store_type,
            'label': store_display_name(store_type),
            'backed_off': backed_off,
            'failures': failures.get(store_type, 0),
            'retry_at': retry_at if backed_off else None,
            # Rounded up, so a store due in 40 seconds reads "1 min", not "0 min".
            'minutes_remaining': (_minutes_until(retry_at, now) if backed_off else 0),
        })
    return rows
_BAD_SCRAPE_NAMES = {
    'unknown product',
    'robot or human?',
    'robot or human',
    'access denied',
    'sorry, you have been blocked',
    'attention required',
    'attention required!',
}


def usable_scraped_name(name):
    """Return a real product name, or None if the scrape hit a wall/placeholder."""
    if not name:
        return None
    cleaned = str(name).strip()
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower in _BAD_SCRAPE_NAMES:
        return None
    if any(token in lower for token in ('captcha', 'verify you', 'are you a robot', 'you have been blocked')):
        return None
    return cleaned


def scrape_answered(product_data):
    """
    True when a scrape came back with something that is actually a product page.

    The browser-based scrapers return None when they recognise a wall, but the
    requests-based ones hand back a dict carrying the wall's own title and no
    price - which is what _BAD_SCRAPE_NAMES exists for. Reading a page needs to
    have produced at least one of the two things a listing has: a name that is
    not a placeholder, or a price.
    """
    if not product_data:
        return False
    return (usable_scraped_name(product_data.get('name')) is not None
            or product_data.get('price') is not None)


def blocked_scrape_message(store_type, product_data):
    """
    What to tell the person who asked for a check that did not come back.

    "Failed to retrieve product information" reads like the listing is gone
    when what actually happened is that the retailer served a bot wall, which
    is a different thing to do something about.
    """
    label = store_display_name(store_type)
    if product_data:
        return (f"{label} served a block page instead of the listing. "
                "The stored price and availability were left as they were.")
    return (f"{label} did not return the listing - it is blocking, or the page "
            "has no buy box. The stored price and availability were left as "
            "they were.")


def note_scrape_result(store_type, product_data, error=None):
    """
    Record one scrape against the store's health, and say whether it answered.

    Every path that scrapes - the scheduler, the JSON API and the dashboard's
    Update button - goes through here, so the store status on the dashboard
    means the same thing whichever of them ran last. Skipping it is what let a
    store that was serving nothing but block pages keep reporting that it was
    answering: only the scheduler recorded health at all, and it recorded a
    success as soon as a dict came back, before the name in it was looked at.

    Args:
        store_type: the key from detect_store_type
        product_data: what the scraper returned, or None
        error: a short reason when the scrape raised instead of returning

    Returns:
        True when the store answered and product_data is worth writing.
    """
    if error is not None:
        record_store_failure(store_type, error)
        return False
    if not product_data:
        record_store_failure(store_type, 'scrape returned no data')
        return False
    if not scrape_answered(product_data):
        record_store_failure(store_type, 'scrape returned a block page')
        return False
    record_store_success(store_type)
    return True


def check_all_products():
    """
    Check all products in the database for updates.
    Updates price and availability information.
    """
    # Use a lock to prevent concurrent execution
    if not check_lock.acquire(blocking=False):
        logger.warning("Skipping check_all_products, already running")
        return
        
    try:
        # Only after the lock is held: a skipped run is not a pass.
        _record_pass_started()
        logger.info(f"Starting scheduled check of all products at {datetime.utcnow()}")
        
        from flask import current_app
        
        # Get app context
        if not current_app:
            # If no current app context, create one
            from app import create_app
            app = create_app()
            ctx = app.app_context()
            ctx.push()
            logger.info("Created new app context for scheduled task")
        else:
            app = current_app
            ctx = None
            logger.info("Using existing app context for scheduled task")
        
        try:
            # Products switched off from the dashboard are skipped, not deleted
            products = Product.query.filter(Product.tracking_enabled == True).all()
            logger.info(f"Found {len(products)} products to check")
            
            for product in products:
                try:
                    # Store detection lives in app/scrapers; see STORE_DOMAINS there.
                    store_type = detect_store_type(product.url)
                    
                    if not store_type:
                        logger.error(f"Could not determine store type for URL: {product.url}")
                        continue
                    
                    # Leave a blocked or rate-limited store alone. The product
                    # keeps its stored data and its last_checked, so the page
                    # still shows when the figures were last known good.
                    if store_is_backed_off(store_type):
                        logger.debug(f"Skipping product {product.id}: {store_type} is backing off")
                        continue

                    # Stores that cannot take the global cadence wait for their
                    # own interval, again keeping their stored data untouched.
                    if not store_check_is_due(store_type, product.last_checked):
                        logger.debug(f"Skipping product {product.id}: {store_type} was checked "
                                     f"less than {store_check_interval(store_type):g} minutes ago")
                        continue

                    # Get appropriate scraper
                    try:
                        scraper = get_scraper(store_type)
                    except ValueError as e:
                        logger.error(f"Error getting scraper for {store_type}: {str(e)}")
                        continue
                    except Exception as e:
                        logger.error(f"Unexpected error getting scraper for {store_type}: {str(e)}")
                        continue
                    
                    # Scrape product data
                    try:
                        if store_type == 'test':
                            # TestScraper uses get_product_info instead of scrape_product
                            product_data = scraper.get_product_info(url=product.url)
                        else:
                            product_data = scraper.scrape_product(product.url)
                    except Exception as e:
                        logger.error(f"Error scraping product {product.id}: {str(e)}")
                        note_scrape_result(store_type, None,
                                           error=f"scrape raised {type(e).__name__}")
                        continue

                    # A wall leaves the stored price and availability alone, the
                    # same as a scraper that returned None: writing available=False
                    # from a block page is how a blocked store came to look like an
                    # out-of-stock one.
                    if not note_scrape_result(store_type, product_data):
                        logger.error(f"Failed to retrieve data for product {product.id}")
                        continue

                    # Update product with new data
                    old_price = product.current_price
                    old_availability = product.available
                    
                    # Only accept a real name; scrapers return "Unknown Product" or a
                    # bot-wall title when extraction fails, and a wall has no image.
                    scraped_name = usable_scraped_name(product_data.get('name'))
                    if scraped_name:
                        product.name = scraped_name
                        product.image_url = product_data.get('image_url') or product.image_url
                    product.current_price = product_data.get('price') or product.current_price
                    product.available = product_data.get('available', False)
                    product.last_checked = datetime.utcnow()
                    product.record_availability(product.last_checked)

                    # Record price history if price changed
                    if product.current_price is not None and product.current_price != old_price:
                        history = PriceHistory(
                            product_id=product.id,
                            price=product.current_price,
                            timestamp=datetime.utcnow()
                        )
                        db.session.add(history)
                        logger.info(f"Price changed for product {product.id}: {old_price} -> {product.current_price}")
                    
                    # Send notifications to every configured channel (Discord webhook, Telegram)
                    # Price drop notification
                    if (product.notify_on_price_drop and 
                        product.current_price is not None and 
                        old_price is not None and 
                        product.current_price < old_price):
                        send_product_alert(product, old_price=old_price, is_availability_alert=False)
                        logger.info(f"Price drop notification for product {product.id}: {product.current_price} -> {old_price}")
                    
                    # Availability notification
                    if (product.notify_on_availability and 
                        product.available and 
                        not old_availability):
                        send_product_alert(product, old_price=old_price, is_availability_alert=True)
                        logger.info(f"Availability notification for product {product.id}: {product.current_price} -> {old_price}")
                    
                    # Commit changes
                    db.session.commit()
                    logger.info(f"Updated product {product.id}: {product.name}")
                    
                except Exception as e:
                    logger.error(f"Error updating product {product.id}: {str(e)}", exc_info=True)
                    db.session.rollback()

            # Reached only when the whole product loop got through. A pass
            # that raised out of the query or the loop has not completed,
            # and must not refresh the liveness timestamp -- that would
            # make a permanently failing tracker look healthy.
            _record_pass_completed()
        except Exception as e:
            logger.error(f"Error in check_all_products: {str(e)}", exc_info=True)
            db.session.rollback()
        finally:
            # Release app context if we created one
            if ctx:
                ctx.pop()
                logger.info("Released app context for scheduled task")
    finally:
        # Always release the lock, even if an exception occurred
        check_lock.release()
        # Paired with _record_pass_started above. Here rather than on the
        # success path on purpose: this pass is no longer holding check_lock
        # however it ended, so it can no longer be what is blocking later ones.
        _record_pass_finished()
        logger.info("Finished scheduled check of all products")

def init_scheduler(app):
    """
    Initialize the APScheduler.
    
    Args:
        app: Flask application instance

    Returns:
        The started BackgroundScheduler, or None when SCHEDULER_ENABLED is off.
    """
    # The settings page calls this with `current_app`, which is a LocalProxy.
    # Every job registered below is a closure over `app`, and those fire on a
    # scheduler thread with no request and no application context -- where the
    # proxy is unbound and raises "Working outside of application context"
    # instead of running the check. Resolve it to the real Flask object once,
    # here, so the closures capture something that stays valid. If it is ever
    # unresolvable this raises at the call site rather than every interval.
    if hasattr(app, '_get_current_object'):
        app = app._get_current_object()

    # Guarded here rather than only at the create_app call site because the
    # settings page calls init_scheduler() again to apply a new check interval,
    # and that would otherwise start the scheduler this instance is meant not to
    # have. Returning None leaves app.scheduler unset for the caller to see.
    global _scheduler_enabled, _interval_seconds

    if not app.config.get('SCHEDULER_ENABLED', True):
        logger.info("SCHEDULER_ENABLED is off - not starting a scheduler")
        # Say so in the health contract too, so a UI-only instance reports
        # enabled=false rather than looking like a stalled tracker.
        _scheduler_enabled = False
        _interval_seconds = None
        return None

    logger.info("Initializing scheduler")
    
    with app.app_context():
        # Get interval from app config (minutes and seconds)
        minutes = app.config.get('CHECK_INTERVAL_MINUTES', 0)
        seconds = app.config.get('CHECK_INTERVAL_SECONDS', 10)
        
        # Convert to total seconds
        interval_seconds = (minutes * 60) + seconds
        
        # Ensure minimum interval of 10 seconds
        if interval_seconds < 10:
            logger.warning("Check interval too low, setting to 10 seconds minimum")
            interval_seconds = 10
            
        logger.info(f"Scheduler will run every {interval_seconds} seconds")
        _scheduler_enabled = True
        _interval_seconds = interval_seconds
        
        if hasattr(app, 'scheduler'):
            logger.info("Removing existing scheduler jobs")
            app.scheduler.remove_all_jobs()
            app.scheduler.shutdown()
            
        # Create a scheduler
        app.scheduler = BackgroundScheduler()
        
        # Add job to check products on the interval
        app.scheduler.add_job(
            func=lambda: check_all_products_with_context(app),
            trigger='interval',
            seconds=interval_seconds,
            id='check_products',
            name='Check all products',
            replace_existing=True
        )
        
        # Add job to check for auto-cart opportunities (runs every minute)
        app.scheduler.add_job(
            func=lambda: check_auto_cart_opportunities_with_context(app),
            trigger='interval',
            seconds=60,
            id='check_auto_cart',
            name='Check auto cart opportunities',
            replace_existing=True
        )
        
        # Optional third job: post a dashboard screenshot to Telegram on an interval (0 = off)
        snapshot_minutes = app.config.get('SNAPSHOT_INTERVAL_MINUTES', 0) or 0
        if snapshot_minutes > 0:
            app.scheduler.add_job(
                func=lambda: send_dashboard_snapshot_with_context(app),
                trigger='interval',
                minutes=snapshot_minutes,
                id='telegram_snapshot',
                name='Send dashboard snapshot to Telegram',
                replace_existing=True
            )
            logger.info(f"Dashboard snapshot to Telegram scheduled every {snapshot_minutes} minutes")
        
        # Start the scheduler
        app.scheduler.start()
        logger.info("Scheduler started")

        # Outside APScheduler on purpose - see start_scheduler_watchdog.
        start_scheduler_watchdog(app)
        
        # Register a function to shut down the scheduler when the app exits.
        # Guarded on .running as well as existence: a test or script that shuts
        # its own scheduler down leaves the attribute in place, and calling
        # shutdown() twice raises SchedulerNotRunningError from inside atexit,
        # which prints a traceback over the output of a run that passed.
        def _shutdown_scheduler():
            scheduler = getattr(app, 'scheduler', None)
            if scheduler is not None and scheduler.running:
                scheduler.shutdown()

        atexit.register(_shutdown_scheduler)

        return app.scheduler

def check_all_products_with_context(app):
    """
    Run check_all_products in the application context.
    
    Args:
        app: Flask application instance
    """
    with app.app_context():
        try:
            check_all_products()
        except Exception as e:
            logger.error(f"Error in product check: {str(e)}", exc_info=True)

def check_auto_cart_opportunities_with_context(app):
    """
    Run check_auto_cart_opportunities in the application context.
    
    Args:
        app: Flask application instance
    """
    with app.app_context():
        try:
            check_auto_cart_opportunities()
        except Exception as e:
            logger.error(f"Error in auto cart opportunity check: {str(e)}", exc_info=True)

def send_dashboard_snapshot_with_context(app):
    """
    Run the Telegram dashboard snapshot in the application context.
    
    Args:
        app: Flask application instance
    """
    with app.app_context():
        try:
            from app.snapshot import send_dashboard_snapshot
            result = send_dashboard_snapshot()
            if not result.get('success'):
                logger.warning(f"Scheduled dashboard snapshot failed: {result.get('message')}")
        except Exception as e:
            logger.error(f"Error in scheduled dashboard snapshot: {str(e)}", exc_info=True)

def check_auto_cart_opportunities():
    """
    Check for products that meet auto-cart criteria and add them to cart automatically.
    """
    logger.info("Checking for auto-cart opportunities")
    
    # Query for products with auto_cart_enabled that are:
    # 1. Available and notify_on_availability is True, OR
    # 2. Below target price and notify_on_price_drop is True
    eligible_products = Product.query.filter(
        Product.tracking_enabled == True,
        Product.auto_cart_enabled == True,
        db.or_(
            db.and_(
                Product.available == True,
                Product.notify_on_availability == True
            ),
            db.and_(
                Product.current_price != None,
                Product.target_price != None,
                Product.current_price <= Product.target_price,
                Product.notify_on_price_drop == True
            )
        )
    ).all()

    # Don't re-add the same item every cycle: skip products already carted
    # successfully, and rate-limit retries after a failed attempt.
    cooldown = timedelta(minutes=current_app.config.get('AUTO_CART_COOLDOWN_MINUTES', 30))
    now = datetime.utcnow()
    products_to_cart = []
    for product in eligible_products:
        status = (product.last_cart_status or '').lower()
        if status.startswith('success'):
            logger.debug(f"Skipping product {product.id}: already in cart ({product.last_cart_status})")
            continue
        if product.last_cart_attempt and now - product.last_cart_attempt < cooldown:
            logger.debug(f"Skipping product {product.id}: last cart attempt {product.last_cart_attempt} is within the {cooldown} cooldown")
            continue
        products_to_cart.append(product)
    eligible_products = products_to_cart

    logger.info(f"Found {len(eligible_products)} products eligible for auto-cart")

    from app.scrapers import add_to_cart
    
    # Track if we had any successful cart additions
    had_successful_cart = False
    
    for product in eligible_products:
        try:
            # Store detection lives in app/scrapers; see STORE_DOMAINS there.
            store_type = detect_store_type(product.url)
            
            if not store_type:
                logger.warning(f"Could not determine store type for {product.url}")
                continue
                
            # Try to add to cart
            logger.info(f"Attempting to add product {product.id} ({product.name}) to cart")
            
            # Get quantity from product settings
            quantity = product.auto_cart_quantity or 1
            
            try:
                previous_status = product.last_cart_status
                result = add_to_cart(store_type, product.url, quantity)
                message = result.get('message', 'Unknown status')

                # Update product with cart attempt results. This is what arms
                # the cooldown, and it runs for a refusal exactly as it does for
                # a success - a scraper that declines to click has still had its
                # turn, and retrying it a minute later would only decline again.
                product.last_cart_attempt = datetime.utcnow()
                product.last_cart_status = message
                # Assigned even when there is no image, so the picture on the
                # product page is never one attempt out of step with the status
                # beside it. The HTTP-only refusals never open a browser and so
                # never have one.
                product.last_cart_screenshot = save_cart_screenshot(
                    product.id, result.get('screenshot'))
                db.session.commit()

                if result.get('success'):
                    logger.info(f"Successfully added product {product.id} to cart")
                    had_successful_cart = True

                    # Send notification about auto-cart success
                    notify_cart_success(product, cart_url=result.get('cart_url'))
                elif is_by_design_refusal(message):
                    # Not a failure: the scraper looked at the page and correctly
                    # declined. Said once at INFO, and dropped to DEBUG while the
                    # answer stays the same, so a product that will refuse for
                    # weeks does not file a warning every cooldown for weeks.
                    if message == previous_status:
                        logger.debug(f"Product {product.id} still not cartable: {message}")
                    else:
                        logger.info(f"Leaving product {product.id} alone: {message}")
                else:
                    logger.warning(f"Failed to add product {product.id} to cart: {message}")
            except Exception as e:
                logger.error(f"Error adding product {product.id} to cart: {str(e)}", exc_info=True)
                product.last_cart_attempt = datetime.utcnow()
                product.last_cart_status = f"Error: {str(e)}"
                product.last_cart_screenshot = None
                db.session.commit()
        except Exception as e:
            logger.error(f"Error processing auto-cart for product {product.id}: {str(e)}", exc_info=True)
    
    # This used to call update_cart_count(), which writes the badge count into the
    # Flask session. A session needs a request context, and the scheduler thread has
    # none, so every successful auto-cart logged "Working outside of request context"
    # and the write went nowhere. The badge is recomputed from the database by
    # GET /api/cart-count, which every page polls every 30s, so nothing is needed here.
    if had_successful_cart:
        logger.info("Auto-cart succeeded for at least one product; the nav cart badge "
                    "refreshes on the next /api/cart-count poll")

def refresh_product(product):
    """
    Scrape a single product, persist any changes, and fire channel alerts.

    Shared by the JSON API (and therefore the MCP server) so a one-off check
    behaves exactly like a scheduled one.

    Args:
        product: A Product instance

    Returns:
        dict: {'success', 'message', 'price_changed', 'became_available',
               'old_price', 'new_price'}
    """
    store_type = detect_store_type(product.url)
    if not store_type:
        return {
            'success': False,
            'message': f"Could not determine store type for URL: {product.url}",
            'price_changed': False,
            'became_available': False,
            'old_price': product.current_price,
            'new_price': product.current_price,
        }

    try:
        scraper = get_scraper(store_type)
        if store_type == 'test':
            # TestScraper uses get_product_info instead of scrape_product
            product_data = scraper.get_product_info(url=product.url)
        else:
            product_data = scraper.scrape_product(product.url)
    except Exception as e:
        logger.error(f"Error scraping product {product.id}: {str(e)}", exc_info=True)
        note_scrape_result(store_type, None, error=f"scrape raised {type(e).__name__}")
        return {
            'success': False,
            'message': f"Error scraping product: {str(e)}",
            'price_changed': False,
            'became_available': False,
            'old_price': product.current_price,
            'new_price': product.current_price,
        }

    # A one-off check counts towards the store's health exactly like a scheduled
    # one, so the dashboard does not go on saying a store is answering while
    # every manual check of it comes back blocked.
    if not note_scrape_result(store_type, product_data):
        if not product.image_url and detect_store_type(product.url) == 'bestbuy':
            from app.scrapers.bestbuy_scraper import BestBuyScraper
            fallback = BestBuyScraper.image_url_from_url(product.url)
            if fallback:
                product.image_url = fallback
                db.session.commit()
        return {
            'success': False,
            'message': blocked_scrape_message(store_type, product_data),
            'price_changed': False,
            'became_available': False,
            'old_price': product.current_price,
            'new_price': product.current_price,
        }

    old_price = product.current_price
    old_availability = product.available

    scraped_name = usable_scraped_name(product_data.get('name'))
    if scraped_name:
        product.name = scraped_name
        product.image_url = product_data.get('image_url') or product.image_url
    product.current_price = product_data.get('price') or product.current_price
    product.available = product_data.get('available', False)
    product.last_checked = datetime.utcnow()
    product.record_availability(product.last_checked)

    price_changed =product.current_price is not None and product.current_price != old_price
    if price_changed:
        db.session.add(PriceHistory(
            product_id=product.id,
            price=product.current_price,
            timestamp=datetime.utcnow()
        ))

    # Send notifications to every configured channel (Discord webhook, Telegram)
    if (product.notify_on_price_drop and
            product.current_price is not None and
            old_price is not None and
            product.current_price < old_price):
        send_product_alert(product, old_price=old_price, is_availability_alert=False)

    became_available = bool(product.available and not old_availability)
    if product.notify_on_availability and became_available:
        send_product_alert(product, old_price=old_price, is_availability_alert=True)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error saving product {product.id}: {str(e)}", exc_info=True)
        return {
            'success': False,
            'message': f"Error saving product: {str(e)}",
            'price_changed': False,
            'became_available': False,
            'old_price': old_price,
            'new_price': product.current_price,
        }

    return {
        'success': True,
        'message': 'Product refreshed',
        'price_changed': price_changed,
        'became_available': became_available,
        'old_price': old_price,
        'new_price': product.current_price,
    }
