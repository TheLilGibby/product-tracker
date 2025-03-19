#!/bin/bash
set -e

# Create directories if they don't exist and ensure proper permissions
mkdir -p /app/data
mkdir -p /app/logs
chmod 777 /app/data /app/logs

echo "Checking data directory permissions:"
ls -la /app/data

# Initialize the database if it doesn't exist
if [ ! -f /app/data/product_tracker.db ]; then
    echo "Initializing database..."
    python create_db.py
    if [ $? -ne 0 ]; then
        echo "Error initializing database. Checking permissions:"
        ls -la /app/data
        echo "Creating empty file to test permissions:"
        touch /app/data/test.txt
        echo "Database initialization failed, but continuing..."
    else
        echo "Database initialized successfully."
    fi
else
    echo "Database already exists."
fi

# Run migrations if needed
echo "Running migrations..."
flask db upgrade || echo "No migrations to run or migrations table doesn't exist yet."

# Start the application with Gunicorn - configure for better performance with Chrome automation
echo "Starting Product Tracker application..."
exec gunicorn \
    --bind 0.0.0.0:5000 \
    --timeout 300 \
    --workers 2 \
    --threads 4 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    "run:app" 