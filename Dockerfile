FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=run.py

WORKDIR /app

# Install Chrome and required dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install additional packages needed for Chrome automation
RUN pip install --no-cache-dir undetected-chromedriver

# Create a non-root user to run the app
RUN useradd -m appuser

# Create necessary directories with proper permissions
RUN mkdir -p /app/data /app/logs \
    && chmod 777 /app/data /app/logs \
    && chown -R appuser:appuser /app

# Copy the application code
COPY --chown=appuser:appuser . .

# Make the start script executable
RUN chmod +x /app/start.sh

# Set up volume mount points
VOLUME ["/app/data", "/app/logs"]

# Expose the port the app runs on
EXPOSE 5000

# Switch to non-root user
USER appuser

# Command to run the application with the start script
CMD ["/app/start.sh"] 