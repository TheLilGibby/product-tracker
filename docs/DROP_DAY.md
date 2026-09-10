# Drop day runbook — Zelda 40th Anniversary Switch 2

For **29 October 2026**, the release date of the Nintendo Switch 2 The Legend of
Zelda 40th Anniversary Edition console ($519.99) and the matching Pro Controller
($99.99).

Read this page once before the day. On the day, work the
[morning-of checklist](#morning-of-checklist) from the top.

## The contract: cart only, never checkout

**Nothing in this application ever completes a purchase.** Auto-cart puts an item
in a retailer's cart and stops. Reviewing the cart, entering payment and placing
the order are yours to do by hand.

That is what the code does, not just an intention. Every scraper that can cart at
all ends on a cart page and returns from there: `target.com/cart`,
`walmart.com/cart`, `bestbuy.com/cart`, `amazon.com/gp/cart/view.html`. No code
path anywhere in `app/scrapers/` clicks a checkout or place-order button, and no
checkout or payment method exists in the package.

Two details are worth knowing because they look like the opposite:

- Amazon's cart step waits for the checkout button to *appear* as proof the add
  worked (`app/scrapers/amazon_scraper.py:149-151`). It never clicks it.
- When an Amazon listing offers only "Buy Now" and no add-to-cart button, the
  scraper deliberately refuses to click it and reports failure instead
  (`app/scrapers/amazon_scraper.py:210-222`).

So the best realistic outcome is: the tracker sees stock, puts the item in a cart,
and Telegram tells you. You still finish the order yourself, quickly. Plan the
evening around being at the keyboard.

## What is tracked, and where carting actually works

The console is sold by six retailers. Newegg, B&H, Micro Center, Adorama, Costco
and Sam's Club **do not list it at all** — each was checked directly. They carry
ordinary Switch 2 hardware and Zelda games, which is why loose searching suggests
otherwise. Do not spend drop-day attention on them.

| Retailer | Scrapes | Can auto-cart | How availability is decided |
|---|---|---|---|
| Walmart | yes | yes | `availabilityStatus` in stock, pre-order or limited, and `orderLimit` is not zero |
| Target | yes | yes | Redsky fulfillment status, or the buy button's text and disabled state |
| Best Buy | yes | yes | The page's own call-to-action button state |
| Amazon | yes | yes, with a caveat below | Buy box, then unavailable text, then a live add-to-cart button |
| GameStop | yes | **no** | `data-available` on the availability element |
| Nintendo Store | yes | **no** | `isSalableQty` is exactly true |

**GameStop and Nintendo Store cannot auto-cart.** Neither scraper implements a cart
method at all, and asking the dispatcher to cart at those stores raises an error
rather than doing something surprising. They are stock monitors only. If the drop
lands at GameStop first, you are buying it by hand.

**Amazon's cart attempt runs in a throwaway browser.** Unlike the others, Amazon's
cart path launches a fresh Chrome with no persistent profile and no login
(`app/scrapers/amazon_scraper.py:95-103`). It is not your signed-in session, so a
"success" there does not put the item in the cart you see when you open Amazon
yourself. Treat an Amazon cart success as a stock signal, not as a reserved item.

The Nintendo Store's Pro Controller is a store exclusive bundled with a display
stand, so it is a different SKU rather than a duplicate row.

## One-time setup, before the day

Retailer logins live in per-store Chrome profiles under
`~/.chrome_profiles/<store>_profile`, which is a named Docker volume mounted at
`/home/appuser/.chrome_profiles`. They survive restarts and rebuilds. They do not
survive `docker compose down -v` or deleting that volume, so do not do either
between setup and the drop.

Only one Chrome may use a profile at a time. A cross-process lock enforces this,
waiting up to sixty seconds before giving up with a "profile busy" error
(`app/scrapers/common.py:163-211`). If a setup script reports that, something else
is already driving that profile: let the scheduled check finish, then retry.

### Headless behaviour per store

This matters because some setup steps need a window you can actually click.

| Store | Env var | Default | Notes |
|---|---|---|---|
| Target | `TARGET_HEADLESS` | headless on | Set to `0` to solve a press-and-hold challenge or sign in |
| Walmart | `WALMART_HEADLESS` | headless on | Cart path only; scraping uses no browser |
| Best Buy | `BESTBUY_HEADLESS` | headless on | Scheduled scrapes never pop a window; a user-initiated cart attempt may |
| GameStop | `GAMESTOP_HEADLESS` | **headless off** | Deliberate, see below |

**GameStop is the awkward one.** Cloudflare serves headless Chrome a challenge page
every time, so a headless run returns nothing on every poll. The scraper therefore
runs a *visible* Chrome window by default, which means a scheduled GameStop check
opens a real browser window on your desktop. Give GameStop a long per-store
interval so this happens rarely. It also cannot work in the container, which has no
display.

Target's browser fallback and Best Buy's cart retry can also want a visible window.
Plan to run those setup steps on the Windows host, not inside Docker.

### The container cannot show you a browser window

This is the one setup problem to solve before the day rather than during it.

The image installs Chrome but no display server, and nothing sets `DISPLAY`. Best
Buy's code checks for a display and quietly skips its headed fallback when there
is none, so headed steps inside the container do not crash — they just never show
you anything you can click.

Meanwhile the container's profiles live in the `chrome_profiles` Docker volume,
while a login script run on Windows writes to your Windows `~/.chrome_profiles`.
**These are different directories.** Signing in on the host does not sign the
container in.

So for the retailers whose carting needs a logged-in session, pick one:

- **Run the app on the Windows host for drop day**, where the login scripts and the
  scheduler share one profile directory. Simplest, and the only path that works for
  GameStop at all.
- **Keep the container and move the session in by hand** — sign in on the host, then
  copy the resulting profile or `cookies.json` into the `chrome_profiles` volume.
  Do this well before the day and verify it with a headless cart run, because there
  is no first-party command for it.

There is also **no script that resets a stuck profile**. If a Target press-and-hold
challenge wedges the profile, the fixes are to re-run the login flow and solve it by
hand, import a fresh cookie export over it, or delete
`~/.chrome_profiles/target_profile` yourself.

### One-time setup commands

Run these from the repository root on the **Windows host**. The `--login` and
`--import-cookies` forms all open a visible window on purpose.

```bash
# Target: sign in, or clear a press-and-hold challenge
python test_target_cart.py --login
python test_target_cart.py --import-cookies path\to\cookies.txt
python test_target_cart.py                    # headless verification run

# Walmart: same shape
python test_walmart_cart.py --login
python test_walmart_cart.py --import-cookies path\to\cookies.txt
python test_walmart_cart.py                   # headless verification run

# Best Buy
python test_bestbuy_browser.py --login        # one-time sign-in, visible window
python test_bestbuy_browser.py                # defaults to the two Zelda 40th URLs
python test_bestbuy_browser.py --offline      # parser checks only, no Chrome, no network
python test_bestbuy_browser.py --headed       # force a visible window
python test_bestbuy_browser.py --cart         # also cart the first buyable product

# GameStop: no arguments, and always opens a visible window
python test_gamestop.py

# Nintendo: no browser, no profile, no setup
python test_nintendo.py
```

There is no cookie *export* flag anywhere in the repository. Export from a real
browser and feed the file to `--import-cookies`.

These read `sys.argv` directly rather than parsing arguments properly, so a typo in
a flag name is silently ignored rather than reported. Check the script's own output
says it entered login mode before assuming it did.

Two checks need no network at all and are worth running any time you have changed
scheduling or store detection:

```bash
python test_store_backoff.py     # in-memory DB, fake scrapers, no retailer contacted
python test_store_detection.py   # pure string handling
python test_amazon.py            # HTML fixtures only; add --live to hit amazon.com
```

Anything headless runs equally well inside the container with
`docker compose exec app-tracker python <script>.py`.

## Settings that matter

All of these live in `.env` beside `docker-compose.yml`. The default is what you get
when the key is absent.

### Polling cadence

| Key | Default | Effect |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `15` | Global loop over every tracked product |
| `CHECK_INTERVAL_SECONDS` | `0` | Added to the above |
| `STORE_CHECK_INTERVALS` | empty | Per-store floor, in minutes |

The two interval keys are summed. **Anything under ten seconds is forced up to ten**
with a warning (`app/tasks.py:372-374`), so you cannot poll harder by setting zero.

`STORE_CHECK_INTERVALS` is written as `gamestop=60,bestbuy=15`, or as the equivalent
JSON object. **It ships empty**, so without it every store is hit on every cycle.
Two stores need it: Best Buy starts serving block pages at roughly five page loads a
minute, and GameStop opens a visible window every time it runs. Entries the parser
cannot read are dropped with a warning rather than crashing, so read the log after
editing instead of assuming your change took.

### Backoff when a retailer starts blocking

| Key | Default | Effect |
|---|---|---|
| `STORE_BACKOFF_FAILURES` | `2` | Consecutive failures before that store is rested |
| `STORE_BACKOFF_MINUTES` | `15` | Length of the first rest |
| `STORE_BACKOFF_MAX_MINUTES` | `60` | Ceiling on the doubling |

Two failures in a row rest that store for fifteen minutes. Each further failure
doubles the wait up to an hour, and the first success clears it completely.

**Do not shorten these on the day.** Polling harder at a retailer that is already
blocking you extends the block. A rested store keeps its last known price and
availability rather than blanking, so the dashboard still shows the last good
reading.

### Auto-cart

| Key | Default | Effect |
|---|---|---|
| `AUTO_CART_COOLDOWN_MINUTES` | `30` | Minimum gap between attempts on one product |

Auto-cart runs on its own sixty-second loop, hard-coded and independent of the
polling interval. Neither the per-store interval nor the backoff throttles it.

### Alerts and access

| Key | Default | Effect |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | empty | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | empty | Channel, group or user that receives alerts |
| `DASHBOARD_PASSWORD` | empty | HTTP Basic password for every page |
| `PUBLIC_URL` | empty | Base URL used for links in Telegram messages |
| `SNAPSHOT_INTERVAL_MINUTES` | `0` | Periodic dashboard screenshot to Telegram |
| `DEFAULT_TIMEZONE` | `UTC` | Timezone for displayed times |

If you expose the dashboard through the tunnel to watch it from your phone, **set
`DASHBOARD_PASSWORD` first**. The dashboard has no accounts: anyone who reaches it
can add or delete products, trigger cart attempts and rewrite `.env`. While that
password is blank the app refuses to put the public link in Telegram at all. The
health check path and static files stay reachable without credentials so the
container can still report healthy.

## Reading the store status strip

The dashboard's status strip is the only signal that displayed figures may be
stale. It has three states.

- **"every store is answering"** — no store has a recent failure. This is one
  combined badge, not one per store. Healthy stores never get their own row.
- **"*store* still checking, N recent failure(s)"** — that store has failed at
  least once but has not hit the backoff threshold, or its rest just expired and it
  has not yet succeeded.
- **"*store* backing off, retry in N min (F failure(s))"** — that store is resting
  and is not being checked at all until the retry time.

A row disappears only when that store's scrape actually succeeds. The specific
failure reason is written to the log, not to the strip, so a store that stays in
the second state is worth a log check rather than a wait.

## Arming auto-cart on a row

Auto-cart is off on every product by default. Turn it on per product from that
product's page.

An armed product fires a cart attempt when either
(`app/tasks.py:477-491`):

1. **It is in stock** and "notify on availability" is on for that row, or
2. **its price is at or below your target** and "notify on price drop" is on.

The second branch requires a target price to actually be set. **A row with no
target price can never trigger on price**, because the query explicitly excludes
null targets. For a launch-day console at a fixed $519.99, that is what you want:
leave the target empty and let availability be the trigger. Setting a target below
retail would quietly disable the price branch while looking like protection you do
not have.

### After an attempt

The attempt time and outcome are recorded on the row. Two rules then apply:

- Within the cooldown, that product is skipped.
- **Once the recorded status begins with "success", that product is never retried
  by the auto-cart job again**, cooldown or not (`app/tasks.py:499-502`).

That second rule surprises people. "Success" means the item reached a cart, not
that you own it. If the cart later empties or a hold expires, the app will not try
again by itself. Check the cart yourself rather than trusting an hour-old status.

**A failed cart attempt sends no alert.** Only successes notify
(`app/tasks.py:539-546`); failures are written to the log only. Silence does not
mean nothing was tried.

## Known problems going in

**Amazon reports this console as available when it cannot be ordered.** The listing
page is up but pre-orders have not opened, and the availability check treats a live
add-to-cart button as sufficient. Expect a false alert from that row. Do not arm
auto-cart on it until the check is fixed.

**Best Buy returns nothing over plain HTTP.** The connection hangs until timeout
rather than returning a block page, so those rows sit at placeholder names with no
price until the browser-based scraper is in the running image.

**There is no Telegram inbox.** Alerts go out; nothing reads replies. Commands sent
back to the bot are not processed.

**Adding a product from the dashboard does not announce itself.** Only price drops,
restocks and successful cart attempts notify. Notes elsewhere describe a
"now tracking" alert; it is sent only by the JSON API route, which the dashboard
form never calls, and it does not exist on `integration` at all.

## Morning-of checklist

1. **Confirm the app is healthy.** `docker compose ps` should show it healthy, not
   restarting. On the host, confirm the scheduler logged its first cycle.
2. **Check the store status strip** on the dashboard. Anything backing off before
   the drop wants fixing now, not at launch.
3. **Confirm the alert path end to end.** Run `python test_telegram.py "drop day
   check"` and confirm it lands on the phone you will actually be holding. Before
   trusting the result, check that Windows user-scope `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` are not overriding `.env` and pointing at an older bot; that
   has happened before.
4. **Prove each cart-capable retailer still has a session.** Run the headless
   verification form of each setup script, which carts and stops:

   ```bash
   python test_target_cart.py
   python test_walmart_cart.py
   python test_bestbuy_browser.py --cart
   ```

   A profile that quietly lost its login fails at cart time, which is the worst
   possible moment to discover it. Empty the carts afterwards so a real hit is
   obvious.
5. **Confirm GameStop and Nintendo are being watched, not carted.** `python
   test_gamestop.py` opens a visible window by design; `python test_nintendo.py`
   needs nothing. Neither can cart, so plan to buy by hand there.
6. **Verify each armed row.** Check which products have auto-cart on, and confirm
   none of them is the Amazon row while its availability check is still wrong.
7. **Leave the intervals alone.** Whatever cadence you settled on during testing is
   the one that has not been blocked yet.

None of this is automated yet. A readiness check is planned on the
`feature/readiness-check` branch, but that branch contains no such script today, so
work the list by hand and do not go looking for a command that runs it for you.
