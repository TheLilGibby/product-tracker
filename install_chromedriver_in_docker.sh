#!/bin/bash
# Script to install Chrome and chromedriver in Docker container

set -e  # Exit on any error

echo "=== Installing Chrome and chromedriver dependencies ==="

# Update package lists
apt-get update

# Install dependencies
apt-get install -y wget unzip curl gnupg ca-certificates

# Install Chrome
echo "=== Installing Google Chrome ==="
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt-get update
apt-get install -y google-chrome-stable

# Get Chrome version
CHROME_VERSION=$(google-chrome --version | awk '{print $3}' | cut -d. -f1)
echo "=== Detected Chrome version: $CHROME_VERSION ==="

# Download matching chromedriver
echo "=== Downloading matching chromedriver ==="
LATEST_DRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_$CHROME_VERSION")
echo "Latest chromedriver version: $LATEST_DRIVER_VERSION"

# Download and extract to temp directory
mkdir -p /tmp/chromedriver
cd /tmp/chromedriver
wget -q "https://chromedriver.storage.googleapis.com/$LATEST_DRIVER_VERSION/chromedriver_linux64.zip"
unzip -q chromedriver_linux64.zip
chmod +x chromedriver

# Create directories for undetected_chromedriver
mkdir -p ~/.local/share/undetected_chromedriver/undetected/chromedriver-linux64

# Copy chromedriver to the target directory
cp chromedriver ~/.local/share/undetected_chromedriver/undetected/chromedriver-linux64/

# Create symlink
cd ~/.local/share/undetected_chromedriver/undetected
ln -sf chromedriver-linux64/chromedriver undetected_chromedriver

echo "=== Installation complete! ==="
echo "ChromeDriver installed at: ~/.local/share/undetected_chromedriver/undetected/chromedriver-linux64/chromedriver"
echo "Symlink created at: ~/.local/share/undetected_chromedriver/undetected/undetected_chromedriver"

# Verify chromedriver works
echo "=== Testing chromedriver ==="
~/.local/share/undetected_chromedriver/undetected/chromedriver-linux64/chromedriver --version

# Clean up
echo "=== Cleaning up ==="
rm -rf /tmp/chromedriver
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "=== All done! ===" 