#!/usr/bin/env python
"""
Offline checks for writing values back into .env.

The settings page stores pasted Newegg and Amazon session cookies in .env so
they survive a restart, which makes the app a writer of its own config file.
Two routes were doing that by hand and had drifted: one wrote the value
single-quoted and refused a quote inside it, the other wrote it bare and
refused nothing; one renamed a temp file over the original, the other truncated
the real file and wrote into it. Both are app.env_file.set_env_value now, and
these checks are what that convention is.

The round-trip cases are the point of the file. They do not assert on the text
written - they assert that python-dotenv, the thing that actually reads .env at
startup, gets back exactly the value that went in. A cookie header is full of
the characters that make that non-obvious: spaces, ';', '=', and a '#' that
would begin a comment if the value were not quoted.

Run: python test_env_file.py
No network, no Chrome, no database. Writes only inside a temp directory.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import dotenv_values  # noqa: E402

from app.env_file import set_env_value, validate_env_value  # noqa: E402

# A real Amazon paste: spaces after the separators, base64 '=' padding inside a
# value, and a '#' that starts a comment in an unquoted .env line.
COOKIE_HEADER = 'session-id=141-0000; at-main=Atza|abc==; ubid-main=133-1#2'

EXISTING_ENV = (
    "SECRET_KEY=keep-me\n"
    "NEWEGG_COOKIES='old-newegg-value'\n"
    "CHECK_INTERVAL_SECONDS=30\n"
)


def _write_env(directory, contents):
    path = os.path.join(directory, '.env')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(contents)
    return path


def run_validation_checks():
    """
    What may not be stored, and why. Both refusals protect the same thing:
    that one assignment in .env stays one assignment.
    """
    cases = [
        ('an ordinary cookie header is kept', COOKIE_HEADER, COOKIE_HEADER),
        ('surrounding whitespace is trimmed', '  a=1  ', 'a=1'),
        ('empty is allowed, and is how a value is cleared', '', ''),
        ('None is empty', None, ''),
        ('a newline is refused, not stripped', 'a=1\nSECRET_KEY=hijacked', ValueError),
        ('a carriage return is refused', 'a=1\rSECRET_KEY=hijacked', ValueError),
        ('a control character is refused', 'a=1\x00', ValueError),
        ("a single quote is refused: it would close the stored value", "a=it's", ValueError),
    ]

    failures = 0
    for label, value, expected in cases:
        try:
            got = validate_env_value(value)
            ok = got == expected
        except ValueError:
            ok = expected is ValueError
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label}")

    # The message is shown to whoever pasted the value, so it should name the
    # retailer rather than the character class.
    try:
        validate_env_value('a=1\nb=2', 'Newegg cookies')
        ok = False
    except ValueError as exc:
        ok = str(exc).startswith('Newegg cookies')
    failures += 0 if ok else 1
    print(f"  [{'ok' if ok else 'FAIL'}] the refusal names what was being saved")
    return failures


def run_write_checks():
    """set_env_value against a real file in a temp directory."""
    failures = 0
    directory = tempfile.mkdtemp(prefix='env-file-checks-')
    try:
        # Replacing an assignment that is already there.
        path = _write_env(directory, EXISTING_ENV)
        set_env_value('NEWEGG_COOKIES', COOKIE_HEADER, path=path)
        values = dotenv_values(path)
        ok = (values['NEWEGG_COOKIES'] == COOKIE_HEADER
              and values['SECRET_KEY'] == 'keep-me'
              and values['CHECK_INTERVAL_SECONDS'] == '30'
              and len(values) == 3)
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] an existing line is replaced, the rest of .env untouched")

        # Appending one that is not.
        set_env_value('AMAZON_COOKIES', COOKIE_HEADER, path=path)
        values = dotenv_values(path)
        ok = (values['AMAZON_COOKIES'] == COOKIE_HEADER
              and values['NEWEGG_COOKIES'] == COOKIE_HEADER
              and values['SECRET_KEY'] == 'keep-me')
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] a missing line is appended, and both cookies round-trip")

        # A '#' in an unquoted value would begin a comment; a quoted one is the
        # whole reason the convention is what it is.
        ok = values['AMAZON_COOKIES'].endswith('#2')
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] a '#' inside the value survives instead of starting a comment")

        # An .env whose last line has no newline: the naive append joins the
        # two into one unreadable line and loses both settings.
        path = _write_env(directory, 'SECRET_KEY=keep-me')
        set_env_value('AMAZON_COOKIES', 'a=1', path=path)
        values = dotenv_values(path)
        ok = values.get('SECRET_KEY') == 'keep-me' and values.get('AMAZON_COOKIES') == 'a=1'
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] a final line with no newline is not run together with the new one")

        # No .env yet - the first save on a fresh checkout.
        path = os.path.join(directory, 'fresh.env')
        set_env_value('AMAZON_COOKIES', COOKIE_HEADER, path=path)
        ok = dotenv_values(path).get('AMAZON_COOKIES') == COOKIE_HEADER
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] a .env that does not exist yet is created")

        # Clearing, which is how the settings page removes a saved session.
        set_env_value('AMAZON_COOKIES', '', path=path)
        ok = dotenv_values(path).get('AMAZON_COOKIES') == ''
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] clearing leaves an empty value, not a deleted line")

        # A refused value must not have touched the file, and must not leave
        # the temp file behind for the next writer to trip over.
        path = _write_env(directory, EXISTING_ENV)
        try:
            set_env_value('AMAZON_COOKIES', 'a=1\nSECRET_KEY=hijacked', path=path)
            refused = False
        except ValueError:
            refused = True
        with open(path, encoding='utf-8') as handle:
            unchanged = handle.read() == EXISTING_ENV
        ok = refused and unchanged and not os.path.exists(path + '.tmp')
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] a refused value writes nothing and leaves no .tmp behind")

        # The injection this all exists to stop, stated as a check: if the
        # newline ever got through, dotenv would read a second setting.
        values = dotenv_values(path)
        ok = 'SECRET_KEY' in values and values['SECRET_KEY'] == 'keep-me'
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] SECRET_KEY is still the real one, not the pasted one")
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return failures


def main():
    print("What may be stored in .env:")
    failures = run_validation_checks()

    print("\nWriting it, read back through python-dotenv:")
    failures += run_write_checks()

    print("")
    print(f"{'PASS' if failures == 0 else 'FAIL'} ({failures} failure(s))")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
