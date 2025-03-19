# PowerShell script to set up directories for Docker volumes

# Create data directory if it doesn't exist
if (!(Test-Path -Path .\data)) {
    Write-Host "Creating data directory..."
    New-Item -ItemType Directory -Path .\data
} else {
    Write-Host "Data directory already exists."
}

# Create logs directory if it doesn't exist
if (!(Test-Path -Path .\logs)) {
    Write-Host "Creating logs directory..."
    New-Item -ItemType Directory -Path .\logs
} else {
    Write-Host "Logs directory already exists."
}

Write-Host "Directory setup complete."
Write-Host "You can now run 'docker-compose up -d' to start the application." 