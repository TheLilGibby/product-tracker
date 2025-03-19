#!/bin/bash
# Bash script to set up directories for Docker volumes

# Create data directory if it doesn't exist
if [ ! -d "./data" ]; then
    echo "Creating data directory..."
    mkdir -p ./data
else
    echo "Data directory already exists."
fi

# Create logs directory if it doesn't exist
if [ ! -d "./logs" ]; then
    echo "Creating logs directory..."
    mkdir -p ./logs
else
    echo "Logs directory already exists."
fi

echo "Directory setup complete."
echo "You can now run 'docker-compose up -d' to start the application." 