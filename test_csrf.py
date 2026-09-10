"""
Offline checks for the same-origin guard and the .env write.

No network, no browser, no retailer. Runs the Flask test client against an
in-memory database.

    python test_csrf.py

Exit code 0 when every check passes, 1 otherwise.
"""
import os
import sys
import tempfile

# The .env write resolves its path from the current working directory, so every
# test runs inside a throwaway directory. Done before create_app so nothing can
# reach the real .env by accident.
WORKDIR = tempfile.mkdtemp(prefix='csrf_test_')
os.chdir(WORKDIR)

from app import create_app  # noqa: E402

ENDPOINT = '/update-newegg-cookies'
HOST = 'localhost'

PASSED, FAILED = [], []


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('  ok   ' if condition else '  FAIL ') + name +
          (f'  [{detail}]' if detail and not condition else ''))


def build_client(**config):
    app = create_app('testing')
    app.config.update(config)
    return app.test_client()


def post(client, headers=None, data=None):
    return client.post(ENDPOINT, headers=headers or {},
                       data=data if data is not None else {'newegg_cookies': 'a=1'},
                       base_url=f'http://{HOST}')


client = build_client()

print("\nSec-Fetch-Site decides when the browser sent it")
check("a cross-site POST is refused",
      post(client, {'Sec-Fetch-Site': 'cross-site'}).status_code == 403)
check("a same-origin POST is not",
      post(client, {'Sec-Fetch-Site': 'same-origin'}).status_code != 403)
check("'none' - a typed URL or a bookmark - is allowed",
      post(client, {'Sec-Fetch-Site': 'none'}).status_code != 403)
check("'same-site' is refused too: a sibling host is where a subdomain "
      "takeover puts an attacker",
      post(client, {'Sec-Fetch-Site': 'same-site'}).status_code == 403)
check("the header is read case-insensitively",
      post(client, {'Sec-Fetch-Site': 'Same-Origin'}).status_code != 403)
check("a forged matching Origin cannot rescue a cross-site request",
      post(client, {'Sec-Fetch-Site': 'cross-site',
                    'Origin': f'http://{HOST}'}).status_code == 403)

print("\nOrigin and Referer, for browsers too old to send Sec-Fetch-Site")
check("an Origin from another host is refused",
      post(client, {'Origin': 'http://evil.example'}).status_code == 403)
check("a matching Origin is allowed",
      post(client, {'Origin': f'http://{HOST}'}).status_code != 403)
check("a mismatched port is still another origin",
      post(client, {'Origin': f'http://{HOST}:9999'}).status_code == 403)
check("Origin 'null' - a sandboxed iframe - never matches",
      post(client, {'Origin': 'null'}).status_code == 403)
check("Referer is used when Origin is absent",
      post(client, {'Referer': 'http://evil.example/page'}).status_code == 403)
check("a matching Referer is allowed",
      post(client, {'Referer': f'http://{HOST}/settings'}).status_code != 403)
check("Origin wins over Referer when both are present",
      post(client, {'Origin': 'http://evil.example',
                    'Referer': f'http://{HOST}/settings'}).status_code == 403)

print("\nthe documented allow: no browser-set headers at all")
# A browser always sends Sec-Fetch-Site, so this branch is not reachable from
# the attack being defended against - it identifies a non-browser client, which
# carries no browser-managed credentials to be spent on its behalf.
check("a header-less client (curl, a script) is allowed through",
      post(client).status_code != 403)

print("\nsafe methods are never blocked")
check("a cross-site GET is not refused - it changes nothing",
      client.get('/settings', headers={'Sec-Fetch-Site': 'cross-site'},
                 base_url=f'http://{HOST}').status_code != 403)

print("\nCSRF_TRUSTED_ORIGINS, so a proxy mismatch is not fixed by disabling this")
proxied = build_client(CSRF_TRUSTED_ORIGINS=['https://tracker.example'])
check("a configured origin is accepted",
      post(proxied, {'Origin': 'https://tracker.example'}).status_code != 403)
check("anything else still is not",
      post(proxied, {'Origin': 'https://other.example'}).status_code == 403)
check("a comma-separated string is accepted too",
      post(build_client(CSRF_TRUSTED_ORIGINS='https://a.example, https://b.example'),
           {'Origin': 'https://b.example'}).status_code != 403)

print("\nthe .env write cannot be made to add a second setting")


def env_path():
    return os.path.join(WORKDIR, '.env')


def write_env(text):
    with open(env_path(), 'w') as handle:
        handle.write(text)


def read_env():
    with open(env_path(), 'r') as handle:
        return handle.read()


BASELINE = "SECRET_KEY=original\nCHECK_INTERVAL_MINUTES=5\n"
allowed = {'Sec-Fetch-Site': 'same-origin'}

write_env(BASELINE)
injection = "a=1\nSECRET_KEY=owned"
response = post(client, allowed, {'newegg_cookies': injection})
check("a value containing a newline is refused", response.status_code in (302, 200))
check("...and nothing at all was written",
      read_env() == BASELINE, read_env())
check("...so SECRET_KEY still holds its original value",
      'SECRET_KEY=original' in read_env() and 'owned' not in read_env())

write_env(BASELINE)
post(client, allowed, {'newegg_cookies': 'a=1\r\nAPI_TOKEN=owned'})
check("a carriage return is refused as well", read_env() == BASELINE)

write_env(BASELINE)
post(client, allowed, {'newegg_cookies': 'a=1\x00API_TOKEN=owned'})
check("so is a NUL", read_env() == BASELINE)

write_env(BASELINE)
post(client, allowed, {'newegg_cookies': "a=1'; SECRET_KEY='owned"})
check("a single quote is refused - it would end the quoted value early",
      read_env() == BASELINE)

print("\nand an ordinary cookie header still round-trips")
write_env(BASELINE)
post(client, allowed, {'newegg_cookies': 'sid=abc123; token=xyz #9'})
contents = read_env()
check("the value is written back", 'NEWEGG_COOKIES=' in contents)
check("quoted, so spaces and '#' survive dotenv",
      "NEWEGG_COOKIES='sid=abc123; token=xyz #9'" in contents, contents)
check("the other settings are untouched",
      'SECRET_KEY=original' in contents and 'CHECK_INTERVAL_MINUTES=5' in contents)
check("exactly one line was added",
      len(contents.strip().splitlines()) == 3, contents)

from dotenv import dotenv_values  # noqa: E402

check("and dotenv reads back exactly what was pasted",
      dotenv_values(env_path()).get('NEWEGG_COOKIES') == 'sid=abc123; token=xyz #9',
      str(dotenv_values(env_path()).get('NEWEGG_COOKIES')))

post(client, allowed, {'newegg_cookies': 'sid=second'})
contents = read_env()
check("a second write replaces the line rather than appending",
      contents.count('NEWEGG_COOKIES=') == 1, contents)
check("no temp file is left behind",
      not os.path.exists(env_path() + '.tmp'))

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
for name in FAILED:
    print(f"  FAILED: {name}")
sys.exit(1 if FAILED else 0)
