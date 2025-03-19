from app import create_app, db
from app.models.product import Product, PriceHistory
import os
import pathlib

app = create_app()

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
    
    # Create all tables
    try:
        db.create_all()
        print("Database tables created successfully!")
    except Exception as e:
        print(f"ERROR creating database tables: {e}")
        # Try to diagnose the issue
        import sqlite3
        try:
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