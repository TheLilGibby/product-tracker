"""
Offline checks for SCHEDULER_ENABLED.

Run it directly, like the other test_* scripts at this root:

    python test_scheduler_toggle.py

Unlike most of them it touches no retailer and no network at all. Each case runs
in its own subprocess - the flag is read from the environment when app.config is
imported, so one process can only ever see one value - against a throwaway SQLite
file, with the bot token blanked and TelegramNotifier.is_configured() asserted
false before anything else happens.

What it pins down:
  * SCHEDULER_ENABLED=0 (and false/OFF/empty) creates no BackgroundScheduler at
    all: no check job, no auto-cart job, no snapshot job.
  * ...because of the flag, not because of TESTING, which would also force an
    in-memory database and so cannot be used to run a live read-only UI.
  * The settings page's interval change still applies the new interval but does
    not start a scheduler - /update-check-interval calls init_scheduler() again,
    so guarding only create_app would miss it.
  * The database and the UI stay live: /, /settings and /update-all-products all
    still work, and the manual update really runs a check.
  * Unset or 1 schedules check_products and check_auto_cart exactly as before.

Exit code 0 means every check passed.
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CASE_MARKER = 'SCHEDULER_TOGGLE_CASE'


# --------------------------------------------------------------------------
# The child: one create_app() under one value of the flag. Prints KEY=value
# lines that the parent asserts on.
# --------------------------------------------------------------------------
def run_case():
    import logging

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logging.getLogger().addHandler(Capture())
    logging.getLogger().setLevel(logging.INFO)

    sys.path.insert(0, HERE)
    os.chdir(tempfile.mkdtemp(prefix='sched_case_'))

    import app as app_pkg
    from app import create_app, db
    from app.notifications.telegram import TelegramNotifier

    application = create_app('default')

    # Refuse to go any further if this process could post to the real channel.
    if TelegramNotifier.is_configured():
        print('ABORT=telegram_configured')
        return 2

    print('CONFIG_FLAG=%r' % application.config.get('SCHEDULER_ENABLED'))
    print('TESTING=%r' % application.config.get('TESTING'))
    print('PKG_SCHEDULER=%r' % (app_pkg.scheduler,))

    sched = getattr(application, 'scheduler', None)
    print('APP_SCHEDULER=%s' % type(sched).__name__)
    print('JOBS=%s' % (sorted(j.id for j in sched.get_jobs()) if sched else []))
    print('OFF_LOG=%d' % sum('SCHEDULER_ENABLED=0' in m for m in records))

    # The settings page calls init_scheduler() again to apply a new interval.
    with application.app_context():
        db.create_all()
    client = application.test_client()
    resp = client.post('/update-check-interval',
                       data={'check_interval_minutes': '2',
                             'check_interval_seconds': '0'})
    print('SETTINGS_STATUS=%d' % resp.status_code)
    sched = getattr(application, 'scheduler', None)
    print('AFTER_SETTINGS=%s' % type(sched).__name__)
    print('AFTER_JOBS=%s' % (sorted(j.id for j in sched.get_jobs()) if sched else []))
    print('INTERVAL_APPLIED=%r' % application.config.get('CHECK_INTERVAL_MINUTES'))

    # The database and the UI stay live, and the manual update path still runs.
    print('INDEX=%d' % client.get('/').status_code)
    print('SETTINGS_PAGE=%d' % client.get('/settings').status_code)
    print('UPDATE_ALL=%d' % client.get('/update-all-products').status_code)
    print('CHECKED_ALL=%d' % sum('Finished scheduled check of all products' in m
                                 for m in records))

    if sched is not None and getattr(sched, 'running', False):
        sched.shutdown(wait=False)
    print('DONE=1')
    return 0


# --------------------------------------------------------------------------
# The parent: spawn one child per value of the flag and check what it printed.
# --------------------------------------------------------------------------
PASSED, FAILED = [], []


def check(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name +
          (('  [%s]' % detail) if detail and not ok else ''))


def spawn(label, flag):
    env = dict(os.environ)
    env[CASE_MARKER] = '1'
    # Never the real database, never the real bot.
    env['DATABASE_URI'] = 'sqlite:///' + os.path.join(
        tempfile.mkdtemp(prefix='sched_db_'), 'case.db').replace(os.sep, '/')
    env['TELEGRAM_BOT_TOKEN'] = ''
    env['TELEGRAM_CHAT_ID'] = ''
    env['TELEGRAM_ALERTS_ENABLED'] = '0'
    env['SNAPSHOT_INTERVAL_MINUTES'] = '0'
    env.pop('SCHEDULER_ENABLED', None)
    if flag is not None:
        env['SCHEDULER_ENABLED'] = flag

    proc = subprocess.run([sys.executable, os.path.abspath(__file__)],
                          env=env, capture_output=True, text=True)
    out = {}
    for line in proc.stdout.splitlines():
        if '=' in line and line.split('=', 1)[0].isupper():
            key, value = line.split('=', 1)
            out[key] = value

    print('\n--- %s (SCHEDULER_ENABLED=%r) exit %d' % (label, flag, proc.returncode))
    if proc.returncode != 0 or 'DONE' not in out:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
    return out


def main():
    for label, flag in (('off: "0"', '0'), ('off: "false"', 'false'),
                        ('off: "OFF"', 'OFF'), ('off: empty', '')):
        r = spawn(label, flag)
        check('%s - the case ran to the end' % label, r.get('DONE') == '1')
        check('%s - the config flag is False' % label,
              r.get('CONFIG_FLAG') == 'False', r.get('CONFIG_FLAG'))
        check('%s - it is not just testing mode doing it' % label,
              r.get('TESTING') == 'False', r.get('TESTING'))
        check('%s - app.scheduler was never created' % label,
              r.get('APP_SCHEDULER') == 'NoneType', r.get('APP_SCHEDULER'))
        check('%s - the package-level scheduler is None' % label,
              r.get('PKG_SCHEDULER') == 'None', r.get('PKG_SCHEDULER'))
        check('%s - no jobs: no check, no auto-cart, no snapshot' % label,
              r.get('JOBS') == '[]', r.get('JOBS'))
        check('%s - exactly one INFO line says the scheduler is off' % label,
              r.get('OFF_LOG') == '1', r.get('OFF_LOG'))
        check('%s - the settings interval change is accepted' % label,
              r.get('SETTINGS_STATUS') == '302', r.get('SETTINGS_STATUS'))
        check('%s - ...and still starts no scheduler' % label,
              r.get('AFTER_SETTINGS') == 'NoneType', r.get('AFTER_SETTINGS'))
        check('%s - ...and still has no jobs' % label,
              r.get('AFTER_JOBS') == '[]', r.get('AFTER_JOBS'))
        check('%s - ...while the new interval is applied' % label,
              r.get('INTERVAL_APPLIED') == '2', r.get('INTERVAL_APPLIED'))
        check('%s - the dashboard still renders' % label,
              r.get('INDEX') == '200', r.get('INDEX'))
        check('%s - the settings page still renders' % label,
              r.get('SETTINGS_PAGE') == '200', r.get('SETTINGS_PAGE'))
        check('%s - /update-all-products still runs a check' % label,
              r.get('UPDATE_ALL') == '302' and r.get('CHECKED_ALL') == '1',
              '%s / %s' % (r.get('UPDATE_ALL'), r.get('CHECKED_ALL')))

    both_jobs = "['check_auto_cart', 'check_products']"
    for label, flag in (('default: unset', None), ('on: "1"', '1')):
        r = spawn(label, flag)
        check('%s - the case ran to the end' % label, r.get('DONE') == '1')
        check('%s - the config flag is True' % label,
              r.get('CONFIG_FLAG') == 'True', r.get('CONFIG_FLAG'))
        check('%s - a real BackgroundScheduler started' % label,
              r.get('APP_SCHEDULER') == 'BackgroundScheduler', r.get('APP_SCHEDULER'))
        check('%s - create_app got it back, not None' % label,
              r.get('PKG_SCHEDULER', '').startswith('<'), r.get('PKG_SCHEDULER'))
        check('%s - both jobs are scheduled' % label,
              r.get('JOBS') == both_jobs, r.get('JOBS'))
        check('%s - the settings change keeps the jobs' % label,
              r.get('AFTER_JOBS') == both_jobs, r.get('AFTER_JOBS'))
        check('%s - no off-log line was emitted' % label,
              r.get('OFF_LOG') == '0', r.get('OFF_LOG'))

    print('\n%d passed, %d failed' % (len(PASSED), len(FAILED)))
    for name in FAILED:
        print('  FAILED: ' + name)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(run_case() if os.environ.get(CASE_MARKER) else main())
