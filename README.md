# Product Tracker

A web application that tracks product prices and availability from various e-commerce sites including Amazon, Walmart, Newegg, Best Buy, Microcenter, and B&H Photo.

## Features

- Track product prices from multiple retailers
- Get notified when prices drop or products become available
- Set price drop targets
- View price history
- Configure check intervals
- Timezone support

## Polling & backoff

The scheduler checks every tracked product on one global interval
(`CHECK_INTERVAL_MINUTES` / `CHECK_INTERVAL_SECONDS`), but retailers with bot
protection cannot all take that cadence, and hammering one that is already
blocking only makes the block last longer. Two settings shape that:

- `STORE_CHECK_INTERVALS`: how often each store may be checked, in minutes,
  written as `gamestop=60,bestbuy=15` or as the equivalent JSON object. A store
  that is not listed is checked on every cycle, as before. Use it for stores
  that are expensive or fragile to scrape - GameStop needs a visible Chrome
  window, Best Buy starts serving block pages at roughly five loads a minute.
- `STORE_BACKOFF_FAILURES` (default `2`), `STORE_BACKOFF_MINUTES` (default
  `15`) and `STORE_BACKOFF_MAX_MINUTES` (default `60`): after that many
  consecutive failed scrapes, a store's products are skipped for that many
  minutes, doubling with each further failure up to the ceiling. The first
  successful scrape clears it.

A product skipped for either reason keeps its stored price, availability and
last-checked time, so the dashboard shows when the figures were last known
good rather than blanking them. The dashboard's "Store status" strip names any
store that is currently failing or backing off, and how long until it is tried
again.

Auto-carting is separate and is not throttled by either setting; it has its own
`AUTO_CART_COOLDOWN_MINUTES` (default `30`) between attempts on the same
product.

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
- `DASHBOARD_PASSWORD`: HTTP Basic password for every page (any username). Blank means no login, which is fine on localhost but **required before exposing the app publicly**
- `PUBLIC_URL`: Externally reachable base URL used for links in Telegram messages (ignored while `DASHBOARD_PASSWORD` is blank)
- `SNAPSHOT_INTERVAL_MINUTES`: Post a dashboard screenshot to Telegram every N minutes (`0` = off)

The easiest way to set these is a `.env` file next to `docker-compose.yml` (copy `.env.example`);
Docker Compose reads it automatically. Never commit `.env`.

See [Polling & backoff](#polling--backoff) for the per-store polling settings.

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
- `chrome_profiles`: Chrome profiles for the browser scrapers, so retailer logins survive restarts

### Dashboard snapshots in Telegram

With Telegram configured, the **Telegram** page has a *Send Dashboard Snapshot* button that renders the
dashboard with headless Chrome inside the container and posts the PNG to your channel, so you can check
on tracked products from your phone. `POST /api/telegram/snapshot` does the same (optional JSON body
`{"path": "/product/3"}`), and `SNAPSHOT_INTERVAL_MINUTES=30` in `.env` sends one automatically.

To make the caption link open the live dashboard from your phone, expose the app with the optional
Cloudflare quick-tunnel sidecar (no account needed). **Set `DASHBOARD_PASSWORD` in `.env` first.**
The dashboard has no accounts: anyone who can reach it can add or delete tracked products, trigger
cart attempts and rewrite `.env`. With the password set, every page asks for HTTP Basic credentials
(any username, that password); while it is blank the app refuses to put the public link in Telegram
messages.

```
docker compose --profile tunnel up -d
docker compose --profile tunnel logs tunnel | grep trycloudflare
```

Copy the printed `https://<random>.trycloudflare.com` URL into `.env` as `PUBLIC_URL=...` and run
`docker compose up -d` again. The quick-tunnel URL changes every time the tunnel container restarts;
for a stable hostname create a named tunnel in your Cloudflare account and use `tunnel run --token ...`
as the sidecar command. ngrok (`ngrok http 5000`) works the same way if you prefer it.

Quick check from the shell: `docker compose exec app-tracker python test_snapshot.py --send`.

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