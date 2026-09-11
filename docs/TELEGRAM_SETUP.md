# Telegram Alerts — Setup Checklist

Product Tracker can send every price-drop, back-in-stock and auto-cart alert to one Telegram channel.
The code is done; it just needs two values from you. This page lists exactly what to do.

## What I need from you

| # | Item | Where it goes | Example |
|---|------|---------------|---------|
| 1 | **Bot token** | `.env` → `TELEGRAM_BOT_TOKEN` | `123456789:AAH4xk...` |
| 2 | **Channel chat ID** | `.env` → `TELEGRAM_CHAT_ID` | `-1001234567890` or `@my_channel` |

Do **not** paste the token into chat or commit it — put it in `.env` only (that file is git-ignored).

## Step 1 — Create the bot (2 minutes)

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot`.
3. Give it a display name (e.g. `Product Tracker Alerts`) and a username ending in `bot` (e.g. `gorag_tracker_bot`).
4. BotFather replies with a token that looks like `123456789:AAH4xk...` — that is `TELEGRAM_BOT_TOKEN`.

## Step 2 — Create the channel and add the bot

1. In Telegram: **New Channel** → name it (e.g. `Deal Alerts`) → choose **Private** (or Public if you want a link).
2. Open the channel → **Administrators** → **Add Administrator** → search your bot's username → add it.
   Make sure **Post Messages** is enabled for it.

## Step 3 — Get the channel's chat ID

**If the channel is public:** the chat ID is just its username with an `@`, e.g. `@my_deal_alerts`. Done.

**If the channel is private** (most likely), pick one:

- **Easiest:** post anything in the channel, then *forward* that post to **@userinfobot** (or **@getidsbot**).
  It replies with the channel's ID — a negative number starting with `-100`, e.g. `-1001234567890`.
- **Alternative (no third-party bot):** post something in the channel, then open this URL in your browser
  (replace `<TOKEN>` with your bot token):

  ```
  https://api.telegram.org/bot<TOKEN>/getUpdates
  ```

  Look for `"chat":{"id":-100...` in the JSON — that number is the chat ID.
  (If it's empty, post once more in the channel and refresh.)

## Step 4 — Put both values in `.env`

Open `product-tracker/.env` and add:

```
TELEGRAM_BOT_TOKEN=123456789:AAH4xk...
TELEGRAM_CHAT_ID=-1001234567890
```

Restart the app (`python run.py`, or `docker-compose restart`).

## Step 5 — Verify

Either:

- Run `python test_telegram.py` from the project folder — you should see `Sent!` and a sample price-drop alert in the channel, **or**
- Open the app → **Telegram** in the nav bar → **Send Test Alert**.

Then ping me with "Telegram is set up" and I'll run the end-to-end check from my side.

## How alerts behave once configured

- Every product's alerts go to the channel, in addition to any per-product Discord webhook.
- Each product's existing **Notify on price drop / Notify on availability** checkboxes still control what fires.
- If Telegram is not configured, nothing changes — the app behaves exactly as before.
- Alerts include an **Open in tracker** link. When `PRODUCT_TRACKER_PUBLIC_URL` is set
  (the Cloudflare tunnel), that link opens the product page on the public URL so you
  can tap it from Telegram on your phone.
- Other tools can push a message into the channel via `POST /api/telegram/send` with `{"message": "..."}`.

## One sender only

Every process that loads this app reads the same `.env`, so a second dev
server, a preview on another port, or a check script that calls
`create_app()` will post the same alerts again from its own database. Three
instances running at once is what made the channel unreadable.

- Pick **one** instance as the sender. Give it `INSTANCE_LABEL=live` (any short
  name) so its posts are prefixed `[live]`.
- Start everything else with `TELEGRAM_ALERTS_ENABLED=0` in its environment.
  Nothing else changes: the instance still scrapes and records history, it
  just never calls the Bot API.
- `create_app('testing')` blanks the token and chat id and disables alerts, so
  check scripts on the testing config cannot post.
- A Windows user-scope `TELEGRAM_BOT_TOKEN` overrides `.env` and is inherited
  by every child process; delete it rather than working around it.

A post without a `[label]` prefix after this is set up is a stray instance:
find it with `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'tracker' }`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Sent!` never appears / 401 in `app.log` | Token is wrong — copy it again from BotFather. |
| `403 Forbidden: bot is not a member` | The bot isn't an admin of the channel — redo Step 2. |
| `400 chat not found` | Chat ID is wrong. Private channel IDs start with `-100`; don't drop the minus sign. |
| Alert goes to the wrong chat / wrong bot | A `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` set as a **Windows/system environment variable** overrides `.env` (python-dotenv never overwrites existing variables). Check with `echo $env:TELEGRAM_CHAT_ID` in PowerShell and remove stale ones. |
| `chat not found` for a group you're in | Group IDs are negative — `-5465533532`, not `5465533532`. Check `.env`. |
| Message arrives but without the product image | Normal — some retailer image links block hotlinking; the app falls back to text automatically. |
