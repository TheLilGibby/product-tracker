# Running on PostgreSQL

The default SQLite file is convenient and fragile. It lives at
`instance/product_tracker.db`, which is gitignored, is **not shared between git
worktrees**, and is deleted by `git clean -fd` or by re-cloning. Every agent
working in its own worktree gets its own empty database.

Pointing `DATABASE_URI` at Postgres moves the data out of the working tree
entirely, so it survives app restarts, reinstalls, branch switches and fresh
clones. Nothing else about the app changes — the scrapers, scheduler and
templates all go through SQLAlchemy already.

`DATABASE_URI` is the only switch. Leave it on `sqlite:///...` and everything
behaves exactly as before.

---

## Option A — Postgres already installed on this machine

Check first; you may not need to install anything:

```bash
# Windows
sc query postgresql-x64-17
netstat -ano | findstr :5432
```

### 1. Create the role and database

Run this yourself — it asks for the `postgres` superuser password, and you
should choose the tracker password:

```bash
# Windows (adjust the version in the path if needed)
"/c/Program Files/PostgreSQL/17/bin/psql" -U postgres -c "CREATE ROLE tracker LOGIN PASSWORD 'choose-a-password';"
"/c/Program Files/PostgreSQL/17/bin/psql" -U postgres -c "CREATE DATABASE product_tracker OWNER tracker;"

# macOS / Linux
psql -U postgres -c "CREATE ROLE tracker LOGIN PASSWORD 'choose-a-password';"
psql -U postgres -c "CREATE DATABASE product_tracker OWNER tracker;"
```

### 2. Install the driver

```bash
pip install -r requirements.txt      # pulls psycopg[binary]
```

`psycopg[binary]` ships prebuilt wheels, so there is no libpq build step on
Windows. It is only imported when `DATABASE_URI` points at Postgres.

### 3. Point the app at it

In `.env` (never commit this file):

```
DATABASE_URI=postgresql+psycopg://tracker:choose-a-password@127.0.0.1:5432/product_tracker
```

`postgres://` and `postgresql://` are both accepted and rewritten to
`postgresql+psycopg://` automatically, so a URL copied from a hosted provider
works as-is.

### 4. Create the schema

```bash
python create_db.py
```

### 5. Bring the existing data across

```bash
python migrate_sqlite_to_postgres.py --dry-run    # look first
python migrate_sqlite_to_postgres.py
```

By default it reads `instance/product_tracker.db`; pass `--sqlite PATH` for a
file somewhere else — e.g. when running from a worktree whose own `instance/`
is empty:

```bash
python migrate_sqlite_to_postgres.py --sqlite ../product-tracker/instance/product_tracker.db
```

It refuses to run against a non-Postgres target, refuses to copy on top of
tables that already hold rows (override with `--force`), never prints the URL
(it contains the password), and never deletes the SQLite file. Keep that file
until the dashboard looks right.

### 6. Start the app

```bash
python run.py
```

---

## Option B — Docker

`docker-compose.yml` now brings up a `postgres:17-alpine` service alongside the
app, with the data in a named `pg_data` volume. Set the password in `.env`:

```
POSTGRES_USER=tracker
POSTGRES_PASSWORD=choose-a-password
POSTGRES_DB=product_tracker
```

`POSTGRES_PASSWORD` has no default. Compose refuses to start without it rather
than standing up a database anyone could guess into.

```bash
docker-compose up -d --build
```

The app waits on the database's `pg_isready` healthcheck before starting,
because `start.sh` runs `create_db.py` immediately and would otherwise race
Postgres's first-boot initdb.

The database port is deliberately not published. To reach it with `psql` from
the host, uncomment the `ports:` block in the `db:` service — it maps to
**5433**, so it will not collide with a Postgres already running on the host.

`docker-compose down` keeps the volume. Only `down -v` (or an explicit
`docker volume rm`) destroys the data.

---

## Notes

- **Schema changes.** There is still no `migrations/` directory; `create_db.py`
  is the migration mechanism. On SQLite it applies a hand-written `ALTER TABLE`
  list. On Postgres it instead diffs the models against the live schema and adds
  any missing columns, so a new column on a model needs no second edit there.
  `db.create_all()` adds missing *tables* but never missing *columns* — that is
  the gap both paths exist to close.
- **Pooling.** Postgres connections get `pool_pre_ping` and a 280-second
  `pool_recycle`, which sits under the common five-minute idle timeout on
  proxies and poolers. This matters here because the scheduler keeps the process
  alive for days between restarts. Tune with `DB_POOL_SIZE` and
  `DB_MAX_OVERFLOW`. SQLite gets no pool options at all.
- **Backups.**
  ```bash
  pg_dump -U tracker -d product_tracker -Fc -f tracker-$(date +%F).dump
  ```
- **Going back to SQLite** is just setting `DATABASE_URI` back. The old file is
  still there; it will be stale by however long you ran on Postgres.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Can't load plugin: sqlalchemy.dialects:postgres` | A bare `postgres://` URL reached SQLAlchemy 2.x. The app rewrites it, so this means something else read the variable directly. |
| `ModuleNotFoundError: No module named 'psycopg'` | `pip install -r requirements.txt` in the venv you are actually running. |
| `password authentication failed for user "tracker"` | Role/password mismatch, or `pg_hba.conf` is set to `ident` for host connections. |
| `duplicate key value violates unique constraint "products_pkey"` after migrating | The id sequence was not advanced. The migration script does this itself; it only shows up if rows were loaded some other way. |
| `connection refused` on 5432 | The server is not running, or in Docker the app is using `localhost` instead of the `db` hostname. |
