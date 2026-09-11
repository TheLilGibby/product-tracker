# Product Tracker

A web application that tracks product prices and availability from various e-commerce sites including Amazon, Walmart, Target, GameStop, Newegg, Best Buy, Microcenter, B&H Photo, and Adorama.

## Features

- Track product prices from multiple retailers
- Get notified when prices drop or products become available
- Set price drop targets
- View price history
- Configure check intervals
- Timezone support

## Notifications

- **Discord** — set a webhook URL per product on the add/edit product form.
- **Telegram** — set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` once and every product's alerts
  (price drop, back in stock, auto-cart) are posted to that channel. Open **Telegram** in the nav bar to
  check status and send a test alert, or run `python test_telegram.py`. Other tools can push messages via
  `POST /api/telegram/send` with `{"message": "..."}`. Full walkthrough: [docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md).

## MCP / API access

Products can be added programmatically as well as through the web form:

- **MCP** — `mcp_server.py` exposes `add_product`, `list_products`, `get_product`,
  `check_product`, `remove_product`, `send_channel_message`, and
  `list_supported_stores` over stdio, so Claude Code/Desktop or a bot bridged to your
  Telegram channel can drive the tracker. A project-scoped `.mcp.json` is included.
- **HTTP** — `POST /api/products` with `{"url": "...", "target_price": 1899.99}` does the
  same thing for webhooks and non-MCP bots.

Either path scrapes the product immediately and posts a "Now Tracking" announcement to
the configured channels. Set `API_TOKEN` to require an `X-API-Token` header on mutating
endpoints. Full walkthrough: [docs/MCP_SETUP.md](docs/MCP_SETUP.md).

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
- `CHECK_INTERVAL_MINUTES`: How often to check products in minutes (default: `0`)
- `CHECK_INTERVAL_SECONDS`: Additional seconds for check interval (default: `10`)
- `DEFAULT_TIMEZONE`: Default timezone for displaying times (default: `UTC`)
- `TELEGRAM_BOT_TOKEN`: Bot token from @BotFather (optional; enables Telegram alerts)
- `TELEGRAM_CHAT_ID`: Telegram channel to post alerts to, e.g. `-1001234567890` or `@channelname` (optional)
- `API_TOKEN`: Shared secret required by the JSON API / MCP server (optional; open when blank)
- `AUTH_USER` / `AUTH_PASSWORD`: Optional; required on the public URL for add-to-cart, delete, and cookie paste (viewing and preference saves are open)
- `PRODUCT_TRACKER_PUBLIC_URL`: The `https://….trycloudflare.com` hostname, shown on Settings

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

## Public access (Cloudflare)

A Cloudflare quick tunnel publishes the local app at a random `trycloudflare.com`
URL so Telegram “Open in tracker” links work on your phone. Viewing the dashboard
and product pages through the tunnel does not require a login. Set `API_TOKEN`
before exposing the JSON API.

1. Start the app (`python run.py`).
2. In another terminal:

   ```
   %USERPROFILE%\.local\bin\cloudflared.exe tunnel --url http://127.0.0.1:5000
   ```

3. Copy the printed `https://….trycloudflare.com` URL into `PRODUCT_TRACKER_PUBLIC_URL`
   in `.env`. The hostname changes every time `cloudflared` restarts.

Keep `PRODUCT_TRACKER_URL=http://localhost:5000` for the MCP server — it should
talk to Flask on loopback, not bounce through the tunnel.

## License

[MIT License](LICENSE) 