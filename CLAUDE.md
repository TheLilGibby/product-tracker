# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Flask app that tracks price/availability of products on e-commerce sites (Amazon, Walmart, Target, GameStop, Newegg, Best Buy, Microcenter, B&H, Adorama), records price history, sends Discord/Telegram alerts, and can automatically add products to a retailer's cart. A JSON API and an MCP server (`mcp_server.py`) expose the same add/inspect operations to agents and bots. SQLite + SQLAlchemy, APScheduler for background polling, Selenium/undetected-chromedriver for the sites with bot protection.

## Commands

```bash
# Local dev
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # then edit
python create_db.py                               # create/migrate the SQLite schema
python run.py                                     # dev server on :5000

# Docker (production path; start.sh runs create_db.py + `flask db upgrade`, then gunicorn)
docker-compose up -d --build
docker-compose logs -f
docker-compose down
```

There is **no test framework** (no pytest/unittest). The `test_*.py` and `verify_*.py` files at the repo root are standalone scripts run individually with `python test_newegg.py`, `python test_adorama.py`, etc. Most hit live retailer sites and require Chrome + a working chromedriver; several need an app context (they call `create_app()` themselves). `check_product.py` is a scratch DB query script. There is no linter or formatter configured.

## Architecture

`run.py` → `create_app()` in `app/__init__.py`, which wires config, `db.create_all()`, the single `main_bp` blueprint, error handlers, and the APScheduler jobs. Note `create_app` calls `init_scheduler` twice (once unconditionally, once for non-testing) — a known quirk; the second call tears down and replaces the first scheduler's jobs.

**Scrapers (`app/scrapers/`)** — the core of the codebase. `get_scraper(store_type)` in `app/scrapers/__init__.py` maps a store string to a scraper class; `add_to_cart(store_type, url, quantity)` is the cart entry point and contains per-store special-casing (Newegg, Target, and GameStop must be pre-scraped so `current_product_url` is set; `test` and `amazon` take `url` as an argument, all others read it off the instance).

Two distinct scraper styles coexist:

- **Requests + BeautifulSoup** (`walmart`, `bestbuy`, `bh`, `microcenter`, `amazon` for scraping): `__init__(self)` sets a UA header; `scrape_product(url)` returns `{'name', 'price', 'available', 'image_url'}` or `None`, delegating to `extract_*(soup)` methods full of fallback selectors.
- **undetected-chromedriver** (`newegg`, `adorama`, `target`, `gamestop`, and `amazon.add_to_cart`): keep a persistent Chrome profile under `~/.chrome_profiles/<store>_profile`, build fresh `uc.ChromeOptions` per run via `_get_chrome_options()` (reusing an options object raises), inject stealth JS, simulate human behavior, and solve CAPTCHAs. These also implement `scrape_via_requests()` plus `extract_*_from_html(soup)` as an HTTP fallback path parallel to the `extract_*(soup, driver)` browser path — when changing extraction logic, both paths usually need the update. Target tries Redsky JSON first, then the browser DOM. GameStop is Cloudflare-blocked over plain HTTP, so the browser path reads JSON-LD and the SFCC buy box.

`base_scraper.BaseScraper` is largely vestigial: only `TestScraper` inherits from it, and its `__init__(self, url)` signature does not match the `__init__(self)` used by every real scraper. Don't assume it constrains anything.

**Store detection is by URL hostname.** `detect_store_type(url)` in `app/scrapers/__init__.py` is the single source of truth (backed by `STORE_DOMAINS`, with `supported_stores()` for the key list); `app/tasks.py`, `app/routes/api.py`, and `app/routes/main.py` (`update_product`, `add_product_to_cart`) all use it. Adding a store means updating `STORE_DOMAINS`, `STORE_DISPLAY_NAMES`, `get_scraper`'s map, and the `<select name="scraper_type">` in `app/templates/products/add.html`.

**Scheduling (`app/tasks.py`)** — two `BackgroundScheduler` jobs on `app.scheduler`: `check_all_products` every `CHECK_INTERVAL_MINUTES`/`_SECONDS` (min 10s, guarded by a module-level `threading.Lock` so runs can't overlap) and `check_auto_cart_opportunities` every 60s. Auto-cart fires for products with `auto_cart_enabled` that are either in stock (with `notify_on_availability`) or at/below `target_price` (with `notify_on_price_drop`). Changing the interval from the settings page mutates `current_app.config` and calls `init_scheduler` again — it is process-local and not persisted.

**Scrape-and-persist (`refresh_product` in `app/tasks.py`)** — scrapes one product, writes price history, fires channel alerts, and commits. Used by the JSON API so a one-off check behaves exactly like a scheduled one. `check_all_products` still has its own near-identical inline loop; they should be unified, but the loop was left alone to avoid changing scheduler behavior.

**Models (`app/models/product.py`)** — just `Product` and `PriceHistory` (cascade-deleted). `PriceHistory` rows are only written when the scraped price differs from the stored one.

**Schema migrations** — Flask-Migrate is installed but there is no `migrations/` directory. `create_db.py` is the real migration mechanism: it opens the SQLite file directly and `ALTER TABLE`s in any missing columns before calling `db.create_all()`. When adding a column to `Product`, add a matching guarded `ALTER TABLE` there, or existing databases won't pick it up.

**CAPTCHA (`app/captcha/solver.py`)** — thin wrapper over `2captcha-python` exposing `recaptcha`, `hcaptcha`, etc. Newegg's scraper implements per-type handlers (reCAPTCHA v2/v3, hCaptcha, Arkose, image) selected by `detect_captcha_type()`.

## JSON API and MCP server

`app/routes/api.py` (blueprint `api_bp`, prefix `/api`) is the machine-facing surface: `POST /api/products`, `GET|DELETE /api/products/<id>`, `POST /api/products/<id>/check`, `POST /api/channels/send`, `GET /api/stores`. It never redirects or flashes. Note that `GET /api/products` still lives in `main_bp` — the two blueprints share the path with disjoint methods, which Werkzeug dispatches fine, but it means the list and create handlers are in different files.

Mutating endpoints are wrapped in `require_token`, which enforces `X-API-Token` only when `API_TOKEN` is configured; unset means open, matching the rest of the app.

`mcp_server.py` at the repo root is a stdio MCP server (SDK 2.x `MCPServer`, with a `FastMCP` fallback import for SDK 1.x) that is a thin HTTP client over those endpoints — it does not import the Flask app, so it works against a local run or the container via `PRODUCT_TRACKER_URL`. Every tool returns a `summary` string next to the structured fields so a chat client can relay one line. `.mcp.json` wires it up for Claude Code. See `docs/MCP_SETUP.md`.

Adding a product through the API announces it to the channels via `send_product_alert(product, is_new_tracking=True)` — the `is_new_tracking` flavor exists in both notifiers alongside price-drop/availability/auto-cart.

## Configuration

`app/config.py` reads a `.env` via python-dotenv into `Development`/`Testing`/`Production` classes; `default` maps to **Development** (debug on), so `FLASK_ENV=production` matters. If `DATABASE_URI` is a relative `sqlite:///data/...` path it is rewritten to the absolute `/app/data/...` Docker path. Key vars: `SECRET_KEY`, `DATABASE_URI`, `DEBUG`, `TWOCAPTCHA_API_KEY`, `CHECK_INTERVAL_MINUTES`/`_SECONDS`, `DEFAULT_TIMEZONE`, `TIME_FORMAT` (`24h`/`12h`), `NEWEGG_COOKIES`, optional `PROXY_API_URL`.

Timezone and time-format preferences live in the Flask `session` (per-browser), falling back to app config; templates format times through the `format_datetime` context processor rather than calling `strftime` directly.

`POST /update-newegg-cookies` rewrites the `.env` file on disk at runtime — the app mutates its own config file, which is worth knowing before assuming `.env` is static.

## Newegg HTTP fallback — documented but NOT wired in

`http_fallback_docs.md`, `implementation_summary.md`, and `integration_instructions.md` describe a `NeweggScraperWithFallback` that degrades from WebDriver to plain HTTP when Chrome fails in Docker. The implementation files (`direct_http_newegg_cart.py`, `newegg_scraper_with_http_fallback*.py`, `newegg_fallback_config.py`, `modified_scrapers_init.py`, `final_integration*.py`) sit **at the repo root, not under `app/scrapers/`**, and `app/scrapers/__init__.py` still maps `'newegg'` to the plain `NeweggScraper`. Treat those docs as a proposal, not a description of the running system. The same is true of the various `fix_chromedriver*.py` and `check_available_chromedrivers.py` root scripts — one-off Docker/chromedriver repair tools, not part of the app.

## Conventions

- Every module gets a dotted logger (`logging.getLogger('app.scrapers.newegg')`); scraper extraction paths log heavily at DEBUG and swallow exceptions, returning `None`/`False` rather than raising, so a single bad selector degrades one field instead of failing the run.
- Cart results are dicts: `{'success', 'message', 'cart_url', 'screenshot'}` where `screenshot` is base64 (HTTP-only paths return `None` for it).
- Templates are server-rendered Jinja2 under `app/templates/`; `/wiki` and `/auto-cart-testing` render Markdown from `app/static/markdown/` through the `markdown` package into `wiki.html`.
