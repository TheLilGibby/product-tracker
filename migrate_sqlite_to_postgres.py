"""
Copy an existing SQLite tracker database into Postgres, once.

This is a one-way move for the switch to a real database server. It reads
whatever tables and columns the SQLite file actually has rather than what the
current models declare, because the running database is usually a branch or two
ahead of whichever checkout you run this from -- `stock_checks` existed in the
live file long before it existed on `integration`. Anything the SQLite side has
and Postgres does not is reported and skipped, never silently dropped.

    python create_db.py                    # create the Postgres tables first
    python migrate_sqlite_to_postgres.py --dry-run
    python migrate_sqlite_to_postgres.py

The target comes from DATABASE_URI, which must already point at Postgres. The
SQLite file is left untouched, so this can be re-run after a failure.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import normalize_database_uri  # noqa: E402

# Parents before children, so foreign keys resolve during the copy. Tables the
# SQLite file has that are not named here are copied afterwards, in whatever
# order SQLite reports them.
TABLE_ORDER = ('products', 'price_histories', 'stock_checks')

# SQLite writes datetimes as text; these are the shapes it produces.
_DATETIME_FORMATS = (
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%dT%H:%M:%S.%f',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d',
)


def parse_datetime(value):
    """Turn a SQLite datetime string into a real datetime, or give up loudly."""
    if value is None or isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    raise ValueError(f'unrecognised datetime: {value!r}')


def coerce(value, pg_type):
    """
    Convert one SQLite value to something Postgres will accept.

    SQLite has no real boolean or datetime types -- it hands back 0/1 integers
    and strings -- and Postgres, unlike SQLite, actually enforces its column
    types. This is where nearly every naive copy falls over.
    """
    if value is None:
        return None
    if pg_type == 'boolean':
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in ('1', 't', 'true', 'yes', 'on')
    if pg_type.startswith('timestamp') or pg_type == 'date':
        return parse_datetime(value)
    return value


def sqlite_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    names = [r[0] for r in rows]
    ordered = [t for t in TABLE_ORDER if t in names]
    ordered += [t for t in names if t not in ordered]
    return ordered


def sqlite_columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def postgres_columns(pg, table):
    """Map column name -> Postgres data_type for one table, empty if absent."""
    rows = pg.execute(
        text(
            'SELECT column_name, data_type FROM information_schema.columns '
            'WHERE table_schema = current_schema() AND table_name = :t'
        ),
        {'t': table},
    ).fetchall()
    return {name: dtype for name, dtype in rows}


def resolve_sqlite_path(explicit):
    if explicit:
        return explicit
    # Flask-SQLAlchemy 3 keeps relative sqlite files under instance/.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'instance', 'product_tracker.db')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', help='path to the SQLite file to read')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='report what would be copied and change nothing',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='copy even if the target tables already hold rows',
    )
    args = parser.parse_args()

    sqlite_path = resolve_sqlite_path(args.sqlite)
    if not os.path.exists(sqlite_path):
        sys.exit(f'No SQLite database at {sqlite_path}. Pass --sqlite PATH.')

    target = normalize_database_uri(os.environ.get('DATABASE_URI', ''))
    if not target.startswith('postgresql'):
        sys.exit(
            'DATABASE_URI does not point at Postgres.\n'
            f'  got: {target or "(unset)"}\n'
            'Set it to your Postgres URL before running this.'
        )

    print(f'source : {sqlite_path}')
    # Never print the URL itself; it carries the password.
    print(f'target : Postgres ({target.split("@")[-1] if "@" in target else target})')
    print()

    lite = sqlite3.connect(sqlite_path)
    lite.row_factory = sqlite3.Row
    engine = create_engine(target)

    copied, skipped_cols, problems = {}, {}, []

    with engine.begin() as pg:
        tables = sqlite_tables(lite)

        # Refuse to double-load unless asked, so a re-run after a partial
        # failure does not quietly duplicate every product.
        if not args.force and not args.dry_run:
            for table in tables:
                if not postgres_columns(pg, table):
                    continue
                existing = pg.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar()
                if existing:
                    sys.exit(
                        f'Target table "{table}" already has {existing} rows.\n'
                        'Refusing to copy on top of it. Re-run with --force if '
                        'you really mean to add to what is already there.'
                    )

        for table in tables:
            pg_cols = postgres_columns(pg, table)
            if not pg_cols:
                problems.append(
                    f'table "{table}" does not exist in Postgres -- run '
                    'create_db.py from a checkout whose models define it'
                )
                continue

            src_cols = sqlite_columns(lite, table)
            usable = [c for c in src_cols if c in pg_cols]
            missing = [c for c in src_cols if c not in pg_cols]
            if missing:
                skipped_cols[table] = missing

            rows = lite.execute(f'SELECT * FROM "{table}"').fetchall()
            if not rows:
                copied[table] = 0
                continue

            if args.dry_run:
                copied[table] = len(rows)
                continue

            quoted = ', '.join(f'"{c}"' for c in usable)
            placeholders = ', '.join(f':{c}' for c in usable)
            insert = text(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
            )

            payload = []
            for row in rows:
                payload.append({
                    c: coerce(row[c], pg_cols[c]) for c in usable
                })
            pg.execute(insert, payload)
            copied[table] = len(payload)

            # Explicit ids were just inserted, so the sequence behind the
            # primary key is still at 1. Without this the very next product
            # you add fails on a duplicate key.
            if 'id' in usable:
                pg.execute(text(
                    "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
                    "COALESCE((SELECT MAX(id) FROM \"%s\"), 1), true)" % table
                ), {'t': table})

    lite.close()

    print('copied:' if not args.dry_run else 'would copy:')
    for table, n in copied.items():
        print(f'  {table:20s} {n:6d} rows')

    for table, cols in skipped_cols.items():
        print(f'\n  ! "{table}" columns not present in Postgres, skipped: '
              f'{", ".join(cols)}')
    for problem in problems:
        print(f'\n  ! {problem}')

    if args.dry_run:
        print('\nDry run: nothing was written.')
    else:
        print('\nDone. Point DATABASE_URI at Postgres and restart the app.')
        print('Keep the SQLite file until you have confirmed the dashboard '
              'looks right -- this script does not delete it.')

    if problems:
        sys.exit(1)


if __name__ == '__main__':
    main()
