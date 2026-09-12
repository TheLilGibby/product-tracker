"""
Offline checks for scheduler liveness: scheduler_health(), GET /api/health and
the watchdog.

Run it directly, like the other test_* scripts at this root:

    python test_scheduler_health.py

It touches no retailer, no Chrome and no network. Telegram is exercised only
through a stubbed TelegramNotifier.send_message, and the real one is asserted
unconfigured before anything else happens, so a stub that failed to take cannot
quietly post to the live channel.

Why this exists: on 2026-09-12 the live tracker sat dead for 83 minutes while
the dashboard answered 200 on every page. The scheduler's jobs were crashing on
every fire, and nothing anywhere said so. HTTP 200 proves Flask is serving; it
says nothing about whether a product check has completed this hour. These
checks pin down the thing that does say so.

What it asserts:
  * The /api/health response matches the agreed contract exactly - same keys,
    same types, ISO-8601 Z timestamps - because a dashboard indicator is being
    built against it in parallel.
  * overdue is False on a fresh process, False once a pass completes, and True
    once the last completed pass ages past 3x the interval.
  * The 90-second floor holds, so a 19-second interval does not alarm on one
    slow scrape.
  * A pass that is merely slow is NOT overdue, however far past the idle
    threshold it runs. The live instance runs a 19-second interval with
    ~55-second passes, so without this any slow Chrome posts a false stall to
    the channel - and a false alert is how the next real one gets ignored.
  * ...but a pass past the hung-pass cap IS overdue, because check_all_products
    holds check_lock for its whole run, so one wedged pass blocks all later
    ones. The cap is sized off real pass durations, not the interval.
  * "In flight" comes from a flag cleared in the outer finally, never inferred
    from "started is newer than completed" - a pass that RAISED leaves exactly
    that state, and inferring it would have made the original outage (jobs
    crashing on every fire) permanently invisible.
  * A pass that raises does NOT refresh last_pass_completed - a permanently
    failing tracker must not look healthy.
  * A skipped run (lock already held) is not recorded as a pass.
  * SCHEDULER_ENABLED=0 reports enabled=false and is never overdue: a UI-only
    instance is not supposed to be checking anything.
  * The watchdog posts exactly once per state change, not once per poll, and
    posts a recovery message when it clears.
  * The watchdog is NOT an APScheduler job - a dead scheduler cannot report
    its own death.
  * TELEGRAM_ALERTS_ENABLED=0 silences the watchdog.

Exit code 0 means every check passed.
"""

import io
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(label, condition, detail=''):
    detail = '' if detail == '' or detail is None else str(detail)
    print('  [%s] %s%s' % ('ok  ' if condition else 'FAIL', label,
                           (' -- ' + detail) if detail else ''))
    if not condition:
        failures.append(label)


def main():
    sys.path.insert(0, HERE)
    os.chdir(tempfile.mkdtemp(prefix='sched_health_'))

    for key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'DISCORD_WEBHOOK_URL'):
        os.environ[key] = ''
    os.environ['DATABASE_URI'] = 'sqlite:///' + os.path.join(os.getcwd(), 'health.db')
    os.environ['SCHEDULER_ENABLED'] = '0'
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '0'
    os.environ['INSTANCE_LABEL'] = 'testinstance'

    import app.tasks as tasks
    from app import create_app
    from app.notifications.telegram import TelegramNotifier

    flask_app = create_app('default')
    assert not TelegramNotifier.is_configured(), 'refusing to run with a live bot token'

    # Every Telegram send in this process goes into this list instead of the
    # network. Patched on the class the watchdog actually calls.
    posted = []
    TelegramNotifier.send_message = staticmethod(lambda text, **kw: posted.append(text) or True)

    def reset(interval=None, enabled=True, completed=None, started=None, booted=None,
              durations=(), in_flight=False):
        tasks._interval_seconds = interval
        tasks._scheduler_enabled = enabled
        tasks._last_pass_completed = completed
        tasks._last_pass_started = started
        tasks._pass_in_flight = in_flight
        tasks._pass_durations.clear()
        tasks._pass_durations.extend(durations)
        if booted is not None:
            tasks._process_started_at = booted
        posted[:] = []

    now = datetime(2026, 9, 12, 3, 0, 0)

    # ------------------------------------------------------------------
    print('\nthe /api/health contract')
    # ------------------------------------------------------------------
    reset(interval=19, completed=now - timedelta(seconds=5),
          started=now - timedelta(seconds=6), booted=now - timedelta(hours=1))
    client = flask_app.test_client()
    response = client.get('/api/health')
    check('GET /api/health is 200', response.status_code == 200,
          'got %s' % response.status_code)
    check('it is JSON and does not redirect',
          response.mimetype == 'application/json', response.mimetype)
    body = json.loads(response.data)
    check('top-level keys are exactly instance_label + scheduler',
          set(body) == {'instance_label', 'scheduler'}, sorted(body))
    check('instance_label is the configured label',
          body.get('instance_label') == 'testinstance', repr(body.get('instance_label')))
    sched = body.get('scheduler', {})
    # The first five are the contract the dashboard indicator was built
    # against and must not change shape; the last three were added with the
    # hung-pass cap. Adding keys is safe for a consumer reading by name, which
    # is why this is an equality check - a *removed* key must fail loudly.
    check('scheduler keys match the contract',
          set(sched) == {'enabled', 'interval_seconds', 'last_pass_started',
                         'last_pass_completed', 'overdue',
                         'in_flight', 'current_pass_seconds', 'in_flight_cap_seconds'},
          sorted(sched))
    check('in_flight is a real boolean',
          isinstance(sched.get('in_flight'), bool), repr(sched.get('in_flight')))
    check('in_flight_cap_seconds is an int',
          isinstance(sched.get('in_flight_cap_seconds'), int),
          repr(sched.get('in_flight_cap_seconds')))
    check('current_pass_seconds is null when no pass is running',
          sched.get('current_pass_seconds') is None,
          repr(sched.get('current_pass_seconds')))
    check('enabled and overdue are real booleans',
          isinstance(sched.get('enabled'), bool) and isinstance(sched.get('overdue'), bool),
          '%r / %r' % (sched.get('enabled'), sched.get('overdue')))
    check('interval_seconds is an int',
          isinstance(sched.get('interval_seconds'), int) and sched['interval_seconds'] == 19,
          repr(sched.get('interval_seconds')))
    check('timestamps are ISO-8601 with an explicit Z',
          sched.get('last_pass_completed', '').endswith('Z')
          and len(sched['last_pass_completed']) == 20,
          repr(sched.get('last_pass_completed')))
    check('a null timestamp really is JSON null, not the string "None"',
          json.loads(client.get('/api/health').data)['scheduler']['last_pass_started'] is not None)
    reset(interval=19, completed=None, started=None, booted=now)
    check('never-run reports last_pass_completed: null',
          json.loads(client.get('/api/health').data)['scheduler']['last_pass_completed'] is None)

    # ------------------------------------------------------------------
    print('\noverdue')
    # ------------------------------------------------------------------
    reset(interval=19, booted=now)
    check('a fresh process is not overdue',
          tasks.scheduler_health(now=now)['overdue'] is False)
    check('...and is still not overdue inside the grace window',
          tasks.scheduler_health(now=now + timedelta(seconds=89))['overdue'] is False)
    check('a process that has never completed a pass goes overdue after the floor',
          tasks.scheduler_health(now=now + timedelta(seconds=91))['overdue'] is True)

    reset(interval=19, completed=now, booted=now - timedelta(hours=1))
    check('a just-completed pass is not overdue',
          tasks.scheduler_health(now=now)['overdue'] is False)
    check('the 90s floor beats 3x19s=57s',
          tasks.scheduler_health(now=now + timedelta(seconds=80))['overdue'] is False,
          'would be overdue on 3x interval alone')
    check('overdue once past the floor',
          tasks.scheduler_health(now=now + timedelta(seconds=91))['overdue'] is True)

    reset(interval=900, completed=now, booted=now - timedelta(hours=1))
    check('a 15-minute interval uses 3x, not the floor',
          tasks.overdue_threshold_seconds(900) == 2700, tasks.overdue_threshold_seconds(900))
    check('...not overdue at 40 minutes',
          tasks.scheduler_health(now=now + timedelta(minutes=40))['overdue'] is False)
    check('...overdue at 50 minutes',
          tasks.scheduler_health(now=now + timedelta(minutes=50))['overdue'] is True)

    # ------------------------------------------------------------------
    print('\na slow pass is not a stall (the false positive this prevents)')
    # ------------------------------------------------------------------
    # The live shape on 2026-09-12: 19-second interval, ~55-second passes. The
    # idle threshold is the 90s floor, so without the in-flight exemption any
    # pass slower than 90s posts "tracker stalled" while the tracker works.
    reset(interval=19, started=now, completed=now - timedelta(seconds=1),
          booted=now - timedelta(hours=1), durations=(55.0, 12.0, 12.0),
          in_flight=True)
    check('a pass running 80s is not overdue',
          tasks.scheduler_health(now=now + timedelta(seconds=80))['overdue'] is False)
    check('...nor at 200s, still under the cap',
          tasks.scheduler_health(now=now + timedelta(seconds=200))['overdue'] is False,
          'the idle clock must not run while a pass is in flight')
    health = tasks.scheduler_health(now=now + timedelta(seconds=80))
    check('...and it reports itself as in flight',
          health['in_flight'] is True and health['current_pass_seconds'] == 80,
          '%r / %r' % (health['in_flight'], health['current_pass_seconds']))

    # NEGATIVE CONTROL: the exemption above must not swallow a genuinely hung
    # pass. check_all_products holds check_lock for its whole run, so a Chrome
    # that never returns blocks every later pass - the one failure the cap
    # exists to catch. If this check ever stops failing-over-to-true, the
    # in-flight exemption has become a blindfold.
    check('a pass past the cap IS overdue',
          tasks.scheduler_health(now=now + timedelta(seconds=301))['overdue'] is True,
          'floor is %ss' % tasks.HUNG_PASS_FLOOR_SECONDS)

    reset(interval=19, started=now, completed=now - timedelta(seconds=1),
          booted=now - timedelta(hours=1), durations=(400.0,), in_flight=True)
    check('the cap tracks real pass durations, not the interval',
          tasks.in_flight_cap_seconds() == 800, tasks.in_flight_cap_seconds())
    check('...so a slow instance is not overdue at 700s',
          tasks.scheduler_health(now=now + timedelta(seconds=700))['overdue'] is False)
    check('...and is overdue at 801s',
          tasks.scheduler_health(now=now + timedelta(seconds=801))['overdue'] is True)

    reset(interval=19, started=now, completed=None, booted=now, in_flight=True)
    check('a first-ever pass that hangs still trips, on the floor',
          tasks.in_flight_cap_seconds() == tasks.HUNG_PASS_FLOOR_SECONDS
          and tasks.scheduler_health(now=now + timedelta(seconds=301))['overdue'] is True)

    # REGRESSION GUARD. Inferring "in flight" from "started is newer than
    # completed" looks equivalent and is not: a pass that raises leaves exactly
    # that state and never clears it. With jobs crashing on every fire - the
    # 83-minute outage, precisely - the start time refreshes every interval, so
    # an inferred flag would read as a young in-flight pass forever and the
    # watchdog would never fire. The flag must come from the finally block.
    reset(interval=19, started=now, completed=None,
          booted=now - timedelta(hours=1), in_flight=False)
    check('a pass that raised is NOT treated as in flight',
          tasks.scheduler_health(now=now + timedelta(seconds=5))['in_flight'] is False)
    check('...so repeated crashing passes still go overdue on the idle clock',
          tasks.scheduler_health(now=now + timedelta(seconds=95))['overdue'] is True,
          'this is the outage the watchdog exists for')

    # The outage itself: jobs crashing on fire means no pass ever starts, so
    # nothing is in flight and the idle clock is the one that must catch it.
    reset(interval=19, started=now - timedelta(seconds=5),
          completed=now - timedelta(seconds=4), booted=now - timedelta(hours=1))
    check('a finished pass leaves nothing in flight',
          tasks.scheduler_health(now=now)['in_flight'] is False)
    check('...and the idle clock still catches a dead scheduler',
          tasks.scheduler_health(now=now + timedelta(seconds=95))['overdue'] is True)

    # ------------------------------------------------------------------
    print('\npass durations feed the cap')
    # ------------------------------------------------------------------
    reset(interval=19, booted=now)
    tasks._record_pass_started(now=now)
    tasks._record_pass_completed(now=now + timedelta(seconds=30))
    check('a completed pass records its duration',
          list(tasks._pass_durations) == [30.0], list(tasks._pass_durations))
    check('...and the cap follows it, still floored',
          tasks.in_flight_cap_seconds() == tasks.HUNG_PASS_FLOOR_SECONDS)
    for extra in (10, 20, 30, 40, 50, 60):
        tasks._record_pass_started(now=now)
        tasks._record_pass_completed(now=now + timedelta(seconds=extra))
    check('only the last %d durations are kept' % tasks.PASS_DURATION_SAMPLES,
          len(tasks._pass_durations) == tasks.PASS_DURATION_SAMPLES,
          list(tasks._pass_durations))
    check('the cap uses the slowest of those, not the newest',
          tasks.in_flight_cap_seconds() == max(
              int(max(tasks._pass_durations) * tasks.HUNG_PASS_DURATION_MULTIPLE),
              tasks.HUNG_PASS_FLOOR_SECONDS))

    reset(interval=None, enabled=False, booted=now - timedelta(days=1))
    health = tasks.scheduler_health(now=now)
    check('SCHEDULER_ENABLED=0 reports enabled=false',
          health['enabled'] is False, repr(health['enabled']))
    check('...and is never overdue, however old the process',
          health['overdue'] is False,
          'a UI-only instance is not supposed to be checking')
    check('...and reports interval_seconds 0 rather than null',
          health['interval_seconds'] == 0, repr(health['interval_seconds']))

    # ------------------------------------------------------------------
    print('\nwhat counts as a completed pass')
    # ------------------------------------------------------------------
    reset(interval=19, booted=now)
    # check_all_products logs the failure below with exc_info, and a passing
    # test that prints a traceback invites someone to read it as a failure.
    # The traceback IS the expected behaviour here, so quiet that one logger.
    import logging
    task_logger = logging.getLogger('app.tasks')
    previous_level = task_logger.level
    task_logger.setLevel(logging.CRITICAL)
    with flask_app.app_context():
        # A pass whose product query blows up must not look like success.
        import app.models.product as product_model
        real_query = product_model.Product.query

        class Boom:
            def filter(self, *a, **kw):
                raise RuntimeError('database is on fire')

        product_model.Product.query = Boom()
        try:
            tasks.check_all_products()
        finally:
            product_model.Product.query = real_query
            task_logger.setLevel(previous_level)
    check('a pass that raised did NOT set last_pass_completed',
          tasks._last_pass_completed is None,
          'a permanently failing tracker would look healthy')
    check('...but it did record that a pass started',
          tasks._last_pass_started is not None)
    check('...so it is overdue once the grace window closes',
          tasks.scheduler_health(now=now + timedelta(seconds=91))['overdue'] is True)

    reset(interval=19, booted=now)
    with flask_app.app_context():
        tasks.check_all_products()          # empty database, but a real pass
    check('an empty but clean pass DOES set last_pass_completed',
          tasks._last_pass_completed is not None)
    check('...and clears overdue',
          tasks.scheduler_health()['overdue'] is False)

    reset(interval=19, booted=now)
    tasks.check_lock.acquire()
    try:
        tasks.check_all_products()          # must bail out immediately
    finally:
        tasks.check_lock.release()
    check('a skipped run (lock held) is not recorded as a pass',
          tasks._last_pass_started is None and tasks._last_pass_completed is None,
          'started=%s completed=%s' % (tasks._last_pass_started, tasks._last_pass_completed))

    # ------------------------------------------------------------------
    print('\nthe watchdog')
    # ------------------------------------------------------------------
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '1'
    os.environ['TELEGRAM_BOT_TOKEN'] = 'stub'
    os.environ['TELEGRAM_CHAT_ID'] = 'stub'
    flask_app.config['TELEGRAM_ALERTS_ENABLED'] = '1'

    reset(interval=19, completed=now, booted=now - timedelta(hours=1))
    stalled_at = now + timedelta(seconds=200)

    state = tasks._watchdog_tick(flask_app, False, now=now)
    check('no post while healthy', posted == [] and state is False, repr(posted))

    state = tasks._watchdog_tick(flask_app, state, now=stalled_at)
    check('one post on the healthy -> stalled transition',
          len(posted) == 1 and state is True, repr(posted))
    check('the stall message names the last completed pass',
          posted and 'stalled' in posted[0] and '2026-09-12T03:00:00Z' in posted[0],
          repr(posted[0] if posted else None))

    tasks._watchdog_tick(flask_app, state, now=stalled_at + timedelta(seconds=30))
    tasks._watchdog_tick(flask_app, state, now=stalled_at + timedelta(seconds=60))
    check('no further posts while it stays stalled', len(posted) == 1,
          '%d posts' % len(posted))

    # Recovery: a pass completes.
    tasks._last_pass_completed = stalled_at + timedelta(seconds=70)
    state = tasks._watchdog_tick(flask_app, True, now=stalled_at + timedelta(seconds=75))
    check('one post on the stalled -> recovered transition',
          len(posted) == 2 and state is False, '%d posts' % len(posted))
    check('the recovery message says recovered',
          'recovered' in posted[-1], repr(posted[-1]))

    # A hung pass is a different failure and must read as one. "No completed
    # check since X" would send someone hunting a dead scheduler while the
    # scheduler is fine and one Chrome is wedged holding check_lock.
    reset(interval=19, started=now, completed=now - timedelta(seconds=1),
          booted=now - timedelta(hours=1), in_flight=True)
    tasks._watchdog_tick(flask_app, False, now=now + timedelta(seconds=400))
    check('a hung pass posts a message naming the stuck pass, not a dead scheduler',
          len(posted) == 1 and 'still running' in posted[0] and 'blocking' in posted[0],
          repr(posted[0] if posted else None))

    # Silencing.
    flask_app.config['TELEGRAM_ALERTS_ENABLED'] = '0'
    os.environ['TELEGRAM_ALERTS_ENABLED'] = '0'
    reset(interval=19, completed=now, booted=now - timedelta(hours=1))
    # The stub bypasses the real gate, so assert the gate itself is shut: that
    # is what send_message checks before it posts.
    from app.notifications.telegram import telegram_alerts_enabled
    with flask_app.app_context():
        check('TELEGRAM_ALERTS_ENABLED=0 shuts the gate the watchdog posts through',
              telegram_alerts_enabled() is False)

    # ------------------------------------------------------------------
    print('\nthe watchdog is not an APScheduler job')
    # ------------------------------------------------------------------
    flask_app.config['SCHEDULER_ENABLED'] = True
    flask_app.config['CHECK_INTERVAL_MINUTES'] = 0
    flask_app.config['CHECK_INTERVAL_SECONDS'] = 3600
    flask_app.config['SNAPSHOT_INTERVAL_MINUTES'] = 0
    scheduler = tasks.init_scheduler(flask_app)
    job_ids = sorted(job.id for job in scheduler.get_jobs())
    thread_names = [t.name for t in threading.enumerate()]
    scheduler.shutdown(wait=False)
    if hasattr(flask_app, 'scheduler'):
        del flask_app.scheduler
    tasks.stop_scheduler_watchdog()

    check('no watchdog job was registered with APScheduler',
          not any('watchdog' in job_id for job_id in job_ids), job_ids)
    check('the watchdog runs as its own thread',
          'scheduler-watchdog' in thread_names,
          [n for n in thread_names if 'watch' in n or 'sched' in n])
    check('init_scheduler published the interval to the health contract',
          tasks._interval_seconds == 3600 and tasks._scheduler_enabled is True,
          '%s / %s' % (tasks._interval_seconds, tasks._scheduler_enabled))

    flask_app.config['SCHEDULER_ENABLED'] = False
    check('init_scheduler with the flag off returns None',
          tasks.init_scheduler(flask_app) is None)
    check('...and reports enabled=false in the contract',
          tasks.scheduler_health()['enabled'] is False)

    print()
    if failures:
        print('FAILED (%d): %s' % (len(failures), ', '.join(failures)))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
