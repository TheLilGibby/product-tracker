"""
Put the Zelda 40th listings that are already tracked into their product groups.

    python seed_groups.py              # show what would change, write nothing
    python seed_groups.py --apply      # write it
    python seed_groups.py --db x.db    # use another SQLite file (a copy, say)

Without --db it uses the database the tracker itself uses: DATABASE_URI from
.env, else instance/product_tracker.db.

A listing is matched by its store and the retailer's own item id (Target TCIN,
Best Buy SKU code, Walmart item id, Amazon ASIN, GameStop and Nintendo product
numbers), not by row id, because row ids differ from one database to the next.
The script only ever adds a listing to a group:
- a listing that is already in a group stays there, even a different group,
  because someone put it there;
- a listing it does not recognise is left alone;
- nothing is deleted, and no listing's checks or auto-cart settings change.
Running it again changes nothing.

It runs the app with the testing config, so no scheduler starts: no store is
checked and nothing is carted while it runs.
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlparse

from app import create_app, db
from app.config import config
from app.groups import assign_group, find_or_create_group, store_label
from app.models.product import Product

REPO = os.path.dirname(os.path.abspath(__file__))

# (group name, ((store host, item id as it appears in the URL path), ...))
# GameStop serves one product under two ids, a SKU (20037854) and a shorter
# product number (451607), so both are listed.
GROUPS = (
    ('Zelda 40th console', (
        ('target.com', '/A-1013322047'),
        ('bestbuy.com', '/J7GSL57HTY'),
        ('walmart.com', '/21002656445'),
        ('amazon.com', '/B0HJ6F8L6V'),
        ('gamestop.com', '/20037854.html'),
        ('gamestop.com', '/451607.html'),
        ('nintendo.com', '-121642'),
    )),
    ('Zelda 40th Pro Controller', (
        ('target.com', '/A-1013213521'),
        ('bestbuy.com', '/J7GSL57W27'),
        ('walmart.com', '/20954470204'),
        ('gamestop.com', '/20037855.html'),
        ('gamestop.com', '/451609.html'),
        # My Nintendo Store sells it on its own and, as a store exclusive,
        # with a display stand. Both are the same controller.
        ('nintendo.com', '-127074'),
        ('nintendo.com', '-127076'),
    )),
    ('Zelda 40th carrying case', (
        ('target.com', '/A-1013213522'),
        ('bestbuy.com', '/J7GSL57WCW'),
        ('gamestop.com', '/451628.html'),
        ('nintendo.com', '-127073'),
    )),
)

REQUIRED_TABLES = ('products', 'product_groups', 'product_group_members')

# What seeding does to one listing. action is 'add', 'already' (in this group),
# 'kept' (in another group, left there) or 'unmatched'.
Row = namedtuple('Row', 'product group action current')


def group_for(url):
    """The group a listing URL belongs in, or None when it is not one of these listings."""
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower()
    for name, listings in GROUPS:
        for store_host, item_id in listings:
            # The id must end there, so -121642 does not match -1216420
            if ((host == store_host or host.endswith('.' + store_host))
                    and re.search(re.escape(item_id) + r'(?=$|[/.])', parsed.path, re.IGNORECASE)):
                return name
    return None


def seed(apply=False):
    """
    What seeding does to each tracked listing, as Rows in id order. With
    apply=True it is also written. Needs an app context.
    """
    rows = []
    groups = {}
    for product in Product.query.order_by(Product.id).all():
        name = group_for(product.url)
        membership = product.group_membership
        current = membership.group.name if membership else None
        if name is None:
            rows.append(Row(product, None, 'unmatched', current))
        elif current is None:
            rows.append(Row(product, name, 'add', None))
            if apply:
                if name not in groups:
                    # Reuses a group of that name, whatever its case
                    groups[name] = find_or_create_group(name)
                assign_group(product, groups[name])
        elif current.lower() == name.lower():
            rows.append(Row(product, name, 'already', current))
        else:
            rows.append(Row(product, name, 'kept', current))
    if apply:
        db.session.commit()
    return rows


def describe(row, applied):
    if row.action == 'add':
        return f'{"added" if applied else "add"} to "{row.group}"'
    if row.action == 'already':
        return f'already in "{row.current}"'
    if row.action == 'kept':
        return f'left in "{row.current}", where it was put by hand'
    if row.current:
        return f'not a Zelda 40th listing, stays in "{row.current}"'
    return 'not a Zelda 40th listing, left alone'


def database_path(override):
    """Absolute path of the SQLite file to seed."""
    if override:
        return os.path.abspath(override)
    uri = config['default'].SQLALCHEMY_DATABASE_URI
    if not uri.startswith('sqlite:///'):
        sys.exit(f"The tracker's database is not a SQLite file ({uri.split(':', 1)[0]}); pass --db.")
    path = uri[len('sqlite:///'):]
    # Flask-SQLAlchemy resolves a relative SQLite path against the instance folder
    return path if os.path.isabs(path) else os.path.join(REPO, 'instance', path)


def missing_tables(path):
    """The tables this script needs that the file does not have."""
    con = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
    try:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    return [table for table in REQUIRED_TABLES if table not in names]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='write the groups; without it nothing is written')
    parser.add_argument('--db', help="SQLite file to use instead of the tracker's own")
    args = parser.parse_args()

    path = database_path(args.db)
    if not os.path.isfile(path):
        print(f"No database at {path}")
        return 1
    # create_app() would create missing tables. Leave that to the tracker itself
    # rather than change a schema from here.
    missing = missing_tables(path)
    if missing:
        print(f"{path} has no {', '.join(missing)} table yet. Start the tracker from a checkout "
              "that has product groups once, so it creates them, then run this again.")
        return 1

    # Same database, testing config: create_app() starts no scheduler
    config['testing'].SQLALCHEMY_DATABASE_URI = 'sqlite:///' + path.replace('\\', '/')
    app = create_app('testing')
    print(f"Database: {path}")
    with app.app_context():
        rows = seed(apply=args.apply)
        for row in rows:
            print(f"  #{row.product.id:<4} {store_label(row.product.url):<18} {describe(row, args.apply)}")
    adds = sum(1 for row in rows if row.action == 'add')
    if not adds:
        print("Nothing to do: every listing it knows is already in a group.")
    elif args.apply:
        print(f"Added {adds} listing(s) to their groups.")
    else:
        print(f"Dry run, nothing written. Run again with --apply to add {adds} listing(s).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
