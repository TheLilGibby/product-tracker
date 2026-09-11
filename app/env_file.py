"""
Writing single values back into the .env file.

The app rewrites its own config at runtime: the settings page stores pasted
Newegg and Amazon session cookies as ``NEWEGG_COOKIES`` / ``AMAZON_COOKIES`` so
they survive a restart. Two routes doing that by hand had drifted apart - one
quoted the value and rejected a quote inside it, the other did neither, and only
one replaced the file atomically - so the rules live here instead, once.

Three things this has to get right, in order of how badly they end:

1. **A newline ends the assignment.** ``NAME=<value>`` with a line break in the
   value is two lines to python-dotenv, and the second one is whatever the
   person pasting chose - ``SECRET_KEY=``, ``DEBUG=1``. Refused, not stripped:
   quietly dropping part of a credential yields one that fails later for a
   reason nobody will connect to this.
2. **A quote ends the quoting.** The value is written single-quoted, so a
   single quote inside it closes the string early and the remainder is parsed
   as more settings. Also refused.
3. **A partial write destroys every other setting in the file**, not just this
   one. The new contents go to a temporary file alongside and are renamed over
   the original, which is atomic on both POSIX and Windows.

Single quotes rather than double: python-dotenv takes single-quoted contents
literally, so a cookie header's spaces and any ``#`` in it survive the round
trip instead of being read as a comment.
"""

import logging
import os
import re

logger = logging.getLogger('app.env_file')

# Printable ASCII and nothing else. A Cookie header is already limited to this,
# so the restriction costs nothing and it is far easier to reason about than an
# enumeration of the characters that would break dotenv.
_SAFE_VALUE_RE = re.compile(r'^[\x20-\x7e]*$')

ENV_FILENAME = '.env'


def env_file_path():
    """The .env the app reads, which is the one in the working directory."""
    return os.path.join(os.getcwd(), ENV_FILENAME)


def validate_env_value(value, label='This value'):
    """
    Return the cleaned value, or raise ValueError naming what is wrong with it.

    The message is shown to the person who pasted it, so it says what to do
    rather than which character class failed.
    """
    value = (value or '').strip()
    if not _SAFE_VALUE_RE.match(value):
        raise ValueError(
            f'{label} contains characters that cannot be stored in .env '
            '(a line break or a control character). Copy the value again as a '
            'single line.'
        )
    if "'" in value:
        raise ValueError(
            f"{label} contains a single quote, which would end the stored "
            "value early. Copy the value again without it."
        )
    return value


def set_env_value(name, value, label=None, path=None):
    """
    Store ``name=value`` in .env, replacing any existing assignment.

    Returns the cleaned value. The caller is expected to set os.environ itself:
    this writes the file, and what the running process should believe is the
    caller's decision, not this function's.
    """
    value = validate_env_value(value, label or name)
    path = path or env_file_path()

    lines = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as handle:
            lines = handle.readlines()

    new_line = f"{name}='{value}'\n"
    for index, line in enumerate(lines):
        if line.startswith(name + '='):
            lines[index] = new_line
            break
    else:
        # An .env whose last line has no newline would otherwise be joined to
        # the new assignment, producing one unreadable line and losing both.
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(new_line)

    temp_path = path + '.tmp'
    with open(temp_path, 'w', encoding='utf-8') as handle:
        handle.writelines(lines)
    os.replace(temp_path, path)
    logger.info(f"Updated {name} in {path}")
    return value
