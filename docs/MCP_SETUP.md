# MCP Setup

The tracker ships an MCP server (`mcp_server.py`) so an MCP client — Claude Code,
Claude Desktop, or a bot bridged to your Telegram/Discord channel — can add and
inspect tracked products. Anything added this way is announced to the alert
channels the app already has configured, so adding a product and hearing about it
in the channel is one round trip.

The server is a thin client over the app's JSON API. It talks HTTP rather than
importing the Flask app, so the same server works against `python run.py` locally
or against the Docker container.

```
Claude / any MCP client
        |
        v
  mcp_server.py   (stdio)
   add_product, list_products, get_product, check_product,
   remove_product, send_channel_message, list_supported_stores
        |  HTTP  (PRODUCT_TRACKER_URL, X-API-Token)
        v
  Flask app  /api/*
        |
        +--> SQLite (Product / PriceHistory)
        +--> Telegram channel + per-product Discord webhook
```

## 1. Install

```bash
pip install -r requirements.txt      # brings in the mcp package
```

## 2. Start the tracker

The MCP server needs the Flask app running — it does not start it.

```bash
python run.py                        # or: docker-compose up -d
```

## 3. Point a client at it

A project-scoped `.mcp.json` is committed at the repo root, so Claude Code picks
the server up when run from this directory. It assumes the venv layout used here:

```json
{
  "mcpServers": {
    "product-tracker": {
      "command": "venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "env": {
        "PRODUCT_TRACKER_URL": "http://localhost:5000",
        "PRODUCT_TRACKER_TIMEOUT": "180"
      }
    }
  }
}
```

On macOS/Linux change the command to `venv/bin/python`. For Claude Desktop, copy
the same block into its config file and use absolute paths for both the
interpreter and `mcp_server.py`.

Verify the wiring by asking the client to call `list_supported_stores` — it
returns the retailer list and the base URL it reached, or a clear "could not
reach the tracker" if the app isn't up.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PRODUCT_TRACKER_URL` | `http://localhost:5000` | Base URL of the Flask app |
| `API_TOKEN` | *(unset)* | Shared secret; must match the app's `API_TOKEN` |
| `PRODUCT_TRACKER_TIMEOUT` | `180` | Seconds to wait on scraping calls |

`API_TOKEN` is optional. Leave it blank and the API is open, which matches the
rest of the app and is fine on localhost. Set it in `.env` — and in the MCP
server's environment — before exposing port 5000 anywhere else; every mutating
endpoint then requires a matching `X-API-Token` header, while reads stay open.

If the app is published through a Cloudflare quick tunnel, keep
`PRODUCT_TRACKER_URL` on `http://localhost:5000` in `.mcp.json`. The MCP server
runs on the same machine as Flask and should not go out through
`trycloudflare.com`. Put the public hostname in `PRODUCT_TRACKER_PUBLIC_URL`
instead. Viewing through the tunnel is open; mutating API calls still use `API_TOKEN`.

Scraping Newegg and Adorama drives a real browser and can take a minute or more.
That is why the scrape timeout defaults to 180s; raise it if `add_product` or
`check_product` times out.

## Tools

| Tool | What it does |
|---|---|
| `add_product` | Start tracking a URL. Scrapes immediately and posts a "Now Tracking" announcement to the channels. Takes `target_price`, `auto_cart`, `discord_webhook_url`, and the `notify_on_*` flags; `scrape=false` / `announce=false` opt out. |
| `list_products` | All tracked products with price and stock state. `available_only` filters to in-stock. |
| `get_product` | One product plus its full price history. |
| `check_product` | Re-scrape now instead of waiting for the scheduler. Records price history and fires price-drop / restock alerts. |
| `remove_product` | Stop tracking and delete the price history. |
| `send_channel_message` | Post free text (optionally with an image) to the Telegram channel. |
| `list_supported_stores` | Retailer list, and a reachability check on the app. |

Every tool returns a `summary` string alongside the structured fields, so a chat
client can relay one readable line without formatting the payload itself.

Store detection is by URL hostname, so `store_type` is normally unnecessary —
pass it only to override. Supported: `amazon`, `walmart`, `newegg`, `bestbuy`,
`microcenter`, `bh`, `adorama`, and `test` (a built-in fake store for testing
without hitting a real retailer).

## Testing without a retailer

The `test` store simulates a product from URL parameters — useful for verifying
the whole path, announcements included, without scraping anything real:

```
https://test-store.example.com/product/anything?scenario=success&price=1999.99&name=RTX+5090
```

`scenario` accepts `success`, `outofstock`, `noprice`, `fail`, and `timeout`.

## HTTP API

The same endpoints are usable directly, which is what you'd point a webhook or a
non-MCP bot at:

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/products` | Add a product. Body: `url` required, plus the options above |
| `GET` | `/api/products` | List all tracked products |
| `GET` | `/api/products/<id>` | One product, with price history |
| `POST` | `/api/products/<id>/check` | Re-scrape now |
| `DELETE` | `/api/products/<id>` | Stop tracking |
| `POST` | `/api/channels/send` | Post a message to Telegram |
| `GET` | `/api/stores` | Supported store keys |

```bash
curl -X POST http://localhost:5000/api/products \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.newegg.com/p/N82E16814137812", "target_price": 1899.99}'
```

Add `-H 'X-API-Token: ...'` when `API_TOKEN` is set.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not reach the tracker" | The Flask app isn't running, or `PRODUCT_TRACKER_URL` points at the wrong port |
| `401 Invalid or missing X-API-Token` | The app has `API_TOKEN` set but the MCP server's environment doesn't match |
| "Timed out after 180s" | A Selenium store was slow; raise `PRODUCT_TRACKER_TIMEOUT` |
| Product added but "Initial scrape did not succeed" | Bot protection blocked the scrape. The product is still tracked and the scheduler retries; for Newegg see [bot_protection_bypass.md](bot_protection_bypass.md) |
| "No channel announcement was sent" | Telegram isn't configured and the product has no Discord webhook — see [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) |
