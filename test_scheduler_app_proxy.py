"""
Offline regression test: the scheduler's jobs must survive being registered
from a request.

Run it directly, like the other test_* scripts at this root:

    python test_scheduler_app_proxy.py

It touches no retailer, no Chrome and no network, and runs against a throwaway
SQLite file with the bot token blanked.

The bug it pins down, observed live on 2026-09-12: the settings page's
/update-check-interval calls init_scheduler(current_app), and every job is a
closure over that argument. current_app is a LocalProxy bound to the request
that is handling the POST. When the job later fires on a scheduler thread there
is no request and no application context, the proxy cannot resolve, and

    with app.app_context():

raises "RuntimeError: Working outside of application context" -- every interval,
forever, for both the product check and auto-cart. Nothing is checked and
nothing is carted, and the only symptom is a traceback in the log. Changing the
interval from the UI therefore *disabled* the tracker.

create_app() passes the real Flask object, so a freshly started process is fine
and the breakage only appears after someone touches the interval.

What it asserts:
  * A job registered via init_scheduler(current_app) runs cleanly when invoked
    with no request and no app context, and really reaches check_all_products.
  * The same for the auto-cart job.
  * init_scheduler stored the unwrapped Flask object, not the proxy.
  * The old shape genuinely fails that way -- otherwise the two checks above
    would pass without the fix and prove nothing.

Exit code 0 means every check passed.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(label, condition, detail=''):
    status = 'ok  ' if condition else 'FAIL'
    print('  [%s] %s%s' % (status, label, (' -- ' + detail) if detail else ''))
    if not condition:
        failures.append(label)


def main():
    sys.path.insert(0, HERE)
    os.chdir(tempfile.mkdtemp(prefix='sched_proxy_'))

    # Belt and braces: this process must not be able to post anywhere even if
    # something below decides to notify.
    for key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'DISCORD_WEBHOOK_URL'):
        os.environ[key] = ''
    os.environ['DATABASE_URI'] = 'sqlite:///' + os.path.join(os.getcwd(), 'proxy_test.db')
    # No scheduler from create_app; this test starts the one it wants to look at.
    os.environ['SCHEDULER_ENABLED'] = '0'
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '0'

    from flask import current_app
    from werkzeug.local import LocalProxy

    import app.tasks as tasks
    from app import create_app

    flask_app = create_app('default')

    from app.notifications.telegram import TelegramNotifier
    assert not TelegramNotifier().is_configured(), 'refusing to run with a live bot token'

    # Stand in for the two jobs so nothing scrapes. Both are looked up through
    # module globals at call time, so replacing the attribute is enough.
    ran = []
    tasks.check_all_products = lambda: ran.append('check')
    tasks.check_auto_cart_opportunities = lambda: ran.append('autocart')

    # init_scheduler returns None unless the flag is on; flip it on the config
    # object rather than the environment so create_app above stayed quiet.
    flask_app.config['SCHEDULER_ENABLED'] = True
    flask_app.config['CHECK_INTERVAL_MINUTES'] = 0
    flask_app.config['CHECK_INTERVAL_SECONDS'] = 3600
    flask_app.config['SNAPSHOT_INTERVAL_MINUTES'] = 0

    print('\nregistering the jobs the way /update-check-interval does')
    with flask_app.test_request_context('/update-check-interval', method='POST'):
        assert isinstance(current_app, LocalProxy), 'current_app should be a proxy'
        scheduler = tasks.init_scheduler(current_app)
        jobs = {job.id: job.func for job in scheduler.get_jobs()}

    # Stop it immediately: this test invokes the job functions by hand, it does
    # not wait for a trigger. Drop app.scheduler afterwards so init_scheduler's
    # atexit handler, which does not guard against an already-stopped
    # scheduler, does not print a SchedulerNotRunningError over the results.
    scheduler.shutdown(wait=False)
    if hasattr(flask_app, 'scheduler'):
        del flask_app.scheduler

    check('both jobs were registered',
          set(jobs) >= {'check_products', 'check_auto_cart'},
          'got %s' % sorted(jobs))

    # The point of the whole test: we are now outside the request that
    # registered them, exactly like a scheduler thread.
    for job_id, marker in (('check_products', 'check'),
                           ('check_auto_cart', 'autocart')):
        ran[:] = []
        error = None
        try:
            jobs[job_id]()
        except Exception as exc:          # noqa: BLE001 - the failure is the point
            error = exc
        check('%s runs outside a request context' % job_id,
              error is None,
              '' if error is None else '%s: %s' % (type(error).__name__, error))
        check('%s reached its work function' % job_id,
              ran == [marker],
              'ran=%s' % ran)

    # Look straight at what the closure captured, so this still fails loudly if
    # someone re-wraps the app later in a way the two calls above happen to
    # tolerate.
    captured = [cell.cell_contents for cell in (jobs['check_products'].__closure__ or ())]
    check('the check job closed over the real Flask app, not a proxy',
          any(obj is flask_app for obj in captured),
          'captured=%s' % [type(obj).__name__ for obj in captured])

    # Negative control. Without this, the checks above would also pass on the
    # unfixed code if the proxy happened to stay bound, and the test would be
    # asserting nothing.
    print('\nnegative control: the pre-fix closure shape')
    with flask_app.test_request_context('/update-check-interval', method='POST'):
        broken = (lambda proxy: lambda: tasks.check_all_products_with_context(proxy))(current_app)
    raised = None
    try:
        broken()
    except RuntimeError as exc:
        raised = exc
    check('closing over current_app really does break',
          raised is not None and 'application context' in str(raised),
          'raised=%r' % (raised,))

    print()
    if failures:
        print('FAILED: %s' % ', '.join(failures))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
