#!/bin/bash
set -e

# Create directories if they don't exist
mkdir -p /app/data
mkdir -p /app/logs

# Try to set permissions but don't fail if it doesn't work (for Windows compatibility)
echo "Setting directory permissions (this may fail on Windows mounts):"
chmod 777 /app/data /app/logs || echo "Could not change permissions - this is expected on Windows and is not a problem"

echo "Checking data directory existence and contents:"
ls -la /app/data || echo "Could not list directory contents"

# Always run create_db.py to ensure schema is up to date
echo "Updating database schema..."
python create_db.py
if [ $? -ne 0 ]; then
    echo "Error updating database schema. Checking permissions:"
    ls -la /app/data || echo "Could not list directory contents"
    echo "Creating empty file to test permissions:"
    touch /app/data/test.txt || echo "Could not create test file - check volume mount permissions"
    echo "Database schema update failed, but continuing..."
else
    echo "Database schema updated successfully."
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