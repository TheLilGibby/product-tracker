from app import create_app, db
from app.models.product import Product, PriceHistory
import os
import pathlib
import sqlite3

app = create_app()

def get_columns(table_name, conn):
    """Get all columns for a given table"""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]

with app.app_context():
    # Make sure the data directory exists
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    
    # Print debugging info
    print(f"Database URI: {db_uri}")
    
    if db_uri.startswith('sqlite:///'):
        # Extract the path part after sqlite:///
        db_path = db_uri.replace('sqlite:///', '')
        print(f"Database path: {db_path}")
        
        # Check if the path is absolute, if not make it relative to instance path
        if not os.path.isabs(db_path):
            if db_path.startswith('data/'):
                # Ensure the data directory exists
                data_dir = os.path.join(os.getcwd(), 'data')
                print(f"Creating data directory: {data_dir}")
                os.makedirs(data_dir, exist_ok=True)
                # Verify permissions
                print(f"Data directory permissions: {oct(os.stat(data_dir).st_mode)}")
                
                # Try creating a test file to verify write permissions
                test_file = os.path.join(data_dir, 'test.txt')
                try:
                    with open(test_file, 'w') as f:
                        f.write('test')
                    print(f"Successfully created test file: {test_file}")
                    os.remove(test_file)
                except Exception as e:
                    print(f"ERROR: Could not write to data directory: {e}")
    
    # Check if database already exists
    full_path = ""
    if db_uri.startswith('sqlite:///'):
        db_path = db_uri.replace('sqlite:///', '')
        if not os.path.isabs(db_path):
            full_path = os.path.join(os.getcwd(), db_path)
        else:
            full_path = db_path
        
        db_exists = os.path.exists(full_path)
        print(f"Database file exists: {db_exists}")
        
        # If database exists, check if we need to add new columns
        if db_exists:
            print("Checking for schema updates...")
            try:
                conn = sqlite3.connect(full_path)
                
                # Check if products table has the new auto cart columns
                columns = get_columns('products', conn)
                
                # Add new auto cart columns if they don't exist
                if 'auto_cart_enabled' not in columns:
                    print("Adding auto_cart_enabled column...")
                    conn.execute("ALTER TABLE products ADD COLUMN auto_cart_enabled BOOLEAN DEFAULT 0")
                
                if 'auto_cart_quantity' not in columns:
                    print("Adding auto_cart_quantity column...")
                    conn.execute("ALTER TABLE products ADD COLUMN auto_cart_quantity INTEGER DEFAULT 1")
                
                if 'last_cart_attempt' not in columns:
                    print("Adding last_cart_attempt column...")
                    conn.execute("ALTER TABLE products ADD COLUMN last_cart_attempt DATETIME")
                
                if 'last_cart_status' not in columns:
                    print("Adding last_cart_status column...")
                    conn.execute("ALTER TABLE products ADD COLUMN last_cart_status VARCHAR(100)")
                
                conn.commit()
                conn.close()
                print("Schema updates completed!")
            except Exception as e:
                print(f"Error updating schema: {e}")
    
    if not db_uri.startswith('sqlite'):
        import time

        from sqlalchemy import text as sa_text
        from sqlalchemy.exc import OperationalError

        # Wait for the server to accept connections. In Docker, start.sh runs
        # this immediately and the db container is no longer a depends_on
        # healthcheck gate (it is an opt-in profile now), so on the very first
        # `--profile postgres up` this races Postgres's initdb by a good 10-20
        # seconds. Outside Docker it covers a server that is still starting.
        DB_WAIT_SECONDS = int(os.environ.get('DB_WAIT_SECONDS', '60'))
        deadline = time.monotonic() + DB_WAIT_SECONDS
        attempt = 0
        while True:
            attempt += 1
            try:
                with db.engine.connect() as conn:
                    conn.execute(sa_text('SELECT 1'))
                break
            except OperationalError as e:
                if time.monotonic() >= deadline:
                    print(
                        f"Database still unreachable after {DB_WAIT_SECONDS}s "
                        f"({attempt} attempts). Giving up."
                    )
                    raise
                if attempt == 1:
                    # Print the reason once; the retries are the interesting
                    # part after that, not the same message repeated.
                    print(f"Database not ready yet ({e.__class__.__name__}); "
                          f"retrying for up to {DB_WAIT_SECONDS}s...")
                time.sleep(2)
        if attempt > 1:
            print(f"Database reachable after {attempt} attempts.")

        # db.create_all() adds missing *tables* but never missing *columns*, and
        # this project has no migrations/ directory -- create_db.py is the
        # migration mechanism (see CLAUDE.md). The SQLite path above keeps a
        # hand-written ALTER list; on Postgres we can ask the models instead, so
        # a new column needs no second edit here.
        #
        # Caveat: this replays the model's full column DDL, so a new column
        # declared nullable=False with no server_default will fail on a table
        # that already has rows -- Postgres cannot fill the existing ones. Give
        # such a column a server_default, or add it by hand and backfill.
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.schema import CreateColumn

        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())

        with db.engine.begin() as conn:
            for table in db.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue  # create_all() below will build it whole
                have = {c['name'] for c in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in have:
                        continue
                    ddl = CreateColumn(column).compile(db.engine)
                    print(f"Adding {table.name}.{column.name} column...")
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS {ddl}'
                    )
        print("Schema updates completed!")

    # Create all tables
    try:
        db.create_all()
        print("Database tables created successfully!")
    except Exception as e:
        print(f"ERROR creating database tables: {e}")
        if not db_uri.startswith('sqlite'):
            # The SQLite diagnostics below would only mislead here: on Postgres
            # this is almost always the server being down or the credentials
            # being wrong, not a directory permission.
            raise
        # Try to diagnose the issue
        import sqlite3
        try:
            if not full_path:
                full_path = os.path.join(os.getcwd(), 'data/product_tracker.db')
            print(f"Attempting direct SQLite connection to: {full_path}")
            conn = sqlite3.connect(full_path)
            print("Direct SQLite connection successful!")
            conn.close()
        except Exception as e2:
            print(f"Direct SQLite connection failed: {e2}")
            
            # Check all parent dirs
            curr_path = pathlib.Path(full_path)
            while curr_path != curr_path.parent:
                curr_path = curr_path.parent
                print(f"Checking parent dir: {curr_path}")
                if os.path.exists(curr_path):
                    print(f"  Exists: {os.path.exists(curr_path)}")
                    print(f"  Permissions: {oct(os.stat(curr_path).st_mode)}")
                else:
                    print("  Does not exist!") 