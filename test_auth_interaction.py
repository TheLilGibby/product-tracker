"""
The seam between the two before_request guards in app/auth.py.

The Basic-auth gate (DASHBOARD_PASSWORD) and the same-origin guard were written
separately and merged into one module. Each has its own tests, and neither
covers the combination: test_csrf.py runs with DASHBOARD_PASSWORD unset, which
is the one configuration in which neither guard has anything to do.

The case worth having a test for is the third one below - a cross-site POST
carrying valid Basic credentials. That is the precise attack the pairing exists
to stop: the user authenticates to the tunnel, the browser caches the
credentials and attaches them to a cross-site POST by itself, so the password
gate passes and the origin guard is the only thing left standing.

No network, no browser, in-memory database, throwaway working directory.

    python test_auth_interaction.py

Exit code 0 when every check passes (or when the file is skipped), 1 otherwise.
"""
import base64
import os
import sys
import tempfile

# The .env write resolves its path from the current working directory, so this
# runs inside a throwaway one. Done before create_app so nothing can reach the
# real .env by accident.
os.chdir(tempfile.mkdtemp(prefix='auth_interaction_'))

from app import auth as auth_module  # noqa: E402

# The Basic-auth gate arrives on its own branch. Where it is absent there is no
# seam to test, so skip rather than fail - this file should be green on every
# branch and real wherever both guards are present.
if not hasattr(auth_module, 'register_auth'):
    print("skipped: app.auth has no register_auth, so the password gate is not "
          "on this branch and there is no interaction to test")
    sys.exit(0)

from app import create_app  # noqa: E402

PASSED, FAILED = [], []
HOST = 'localhost'
PASSWORD = 'drop-day'
ENDPOINT = '/update-newegg-cookies'


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('  ok   ' if condition else '  FAIL ') + name +
          (f'  [{detail}]' if detail and not condition else ''))


def client(password=PASSWORD):
    app = create_app('testing')
    app.config['DASHBOARD_PASSWORD'] = password
    return app.test_client()


def creds(password=PASSWORD):
    token = base64.b64encode(f'user:{password}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def post(test_client, headers):
    return test_client.post(ENDPOINT, headers=headers,
                            data={'newegg_cookies': 'a=1'},
                            base_url=f'http://{HOST}')


c = client()

print("\nboth guards active (DASHBOARD_PASSWORD set)")
status = post(c, {'Sec-Fetch-Site': 'same-origin'}).status_code
check("an unauthenticated same-origin POST is refused by the password gate",
      status == 401, str(status))

status = post(c, dict(creds(), **{'Sec-Fetch-Site': 'same-origin'})).status_code
check("an authenticated same-origin POST still works - the app is not bricked",
      status == 302, str(status))

# The attack the pairing exists for. The credentials ride along automatically,
# so the password gate passes and the origin guard is all that is left.
status = post(c, dict(creds(), **{'Sec-Fetch-Site': 'cross-site'})).status_code
check("a cross-site POST WITH valid credentials is still refused",
      status == 403, str(status))

status = post(c, {'Sec-Fetch-Site': 'cross-site'}).status_code
check("a cross-site POST without credentials is refused too",
      status in (401, 403), str(status))

status = post(c, dict(creds(), **{'Sec-Fetch-Site': 'same-site'})).status_code
check("same-site with credentials is still refused",
      status == 403, str(status))

status = post(c, dict(creds(), **{'Origin': 'http://evil.example'})).status_code
check("a mismatched Origin with credentials is refused",
      status == 403, str(status))

print("\nthe password gate's exemptions do not become origin holes")
status = c.post('/healthz', headers={'Sec-Fetch-Site': 'cross-site'},
                base_url=f'http://{HOST}').status_code
check("/healthz is exempt from the password gate but not from the origin guard",
      status not in (200, 302), str(status))

status = c.get('/healthz', base_url=f'http://{HOST}').status_code
check("...while an unauthenticated GET /healthz still answers, as intended",
      status == 200, str(status))

print("\nordering")
# The gates are registered password-first, so an unauthenticated cross-site
# POST answers 401 rather than 403. Either order is safe, but this one tells an
# unauthenticated prober only that a password is set, rather than that its
# origin was judged and found wanting. Pinned so a later merge cannot quietly
# swap the order back.
status = post(c, {'Sec-Fetch-Site': 'cross-site'}).status_code
check("an unauthenticated cross-site POST answers 401, not 403",
      status == 401, str(status))

print("\nwith the password blank, the origin guard still stands alone")
c = client(password='')
status = post(c, {'Sec-Fetch-Site': 'cross-site'}).status_code
check("a cross-site POST is refused even with no dashboard password",
      status == 403, str(status))
status = post(c, {'Sec-Fetch-Site': 'same-origin'}).status_code
check("and a same-origin POST works", status == 302, str(status))

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
for name in FAILED:
    print(f"  FAILED: {name}")
sys.exit(1 if FAILED else 0)
