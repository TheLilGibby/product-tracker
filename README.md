# Product Tracker

A web application that tracks product prices and availability from various e-commerce sites including Amazon, Walmart, Newegg, Best Buy, Microcenter, and B&H Photo.

## Features

- Track product prices from multiple retailers
- Get notified when prices drop or products become available
- Set price drop targets
- View price history
- Configure check intervals
- Timezone support

## Dockerized Setup

The application is containerized using Docker for easy deployment and management.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

### Environment Variables

You can customize the application by setting the following environment variables:

- `SECRET_KEY`: Secret key for the Flask application (default: `your-secret-key-here`)
- `DEBUG`: Set to `1` to enable debug mode (default: `0`)
- `TWOCAPTCHA_API_KEY`: API key for 2captcha service (required for solving CAPTCHAs on Newegg)
- `CHECK_INTERVAL_MINUTES`: How often to check products in minutes (default: `15`)
- `CHECK_INTERVAL_SECONDS`: Additional seconds for check interval (default: `0`)
- `DEFAULT_TIMEZONE`: Default timezone for displaying times (default: `UTC`)
- `TELEGRAM_BOT_TOKEN`: Telegram bot token from @BotFather (optional, enables Telegram alerts)
- `TELEGRAM_CHAT_ID`: Chat/channel/group id the bot posts alerts to (required with `TELEGRAM_BOT_TOKEN`)

The easiest way to set these is a `.env` file next to `docker-compose.yml` (copy `.env.example`);
Docker Compose reads it automatically. Never commit `.env`.

### Building and Running

1. Clone the repository:
   ```
   git clone <repository-url>
   cd product-tracker
   ```

2. Build and start the Docker container:
   ```
   docker-compose up -d
   ```

3. Access the application:
   ```
   http://localhost:5000
   ```

### Stopping the Container

```
docker-compose down
```

### Viewing Logs

```
docker-compose logs -f
```

### Persistence

The application data is stored in Docker volumes to ensure persistence across container restarts:

- `app_data`: Contains the SQLite database
- `app_logs`: Contains the application logs

### Updating

To update the application:

1. Pull the latest changes:
   ```
   git pull
   ```

2. Rebuild and restart the container:
   ```
   docker-compose up -d --build
   ```

## Development

### Running Locally Without Docker

1. Create a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up environment variables or create a `.env` file.

4. Initialize the database:
   ```
   python create_db.py
   ```

5. Run the application:
   ```
   python run.py
   ```

## License

[MIT License](LICENSE) 