# Telegram checker

Sends Pump Alert Bot alerts to Telegram 24/7, even when the site is closed.

## Files

- `check_alerts.py` — the checker. Fetches live DexScreener prices, evaluates each alert exactly like the site (price above/below, pump/dump % over 1h), fires each alert once, then re-arms it only after a later check finds the condition false again. Also handles bot messages: buttons, `/gainers`, `/myalerts`, `/price`, and typed alerts like `LOOT above $0.001`.
- `poll_updates.py` — always-on poller. Long-polls Telegram's `getUpdates` and runs the checker the moment a message arrives, so the bot replies within seconds instead of waiting for the next 5-minute check.
- `sync_backend.py` — merges alert definitions from the Pump Alert Bot backend into `alerts.json`. Only used with the backend setup; skip it if you manage `alerts.json` by hand.
- `alerts.example.json` — copy this to `alerts.json` and fill in your own `chat_id` and alerts.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get its token.
2. Copy the example config and set it up:
   ```bash
   cp alerts.example.json alerts.json
   ```
   Then message your bot once on Telegram and run this to find your chat ID:
   ```bash
   python3 check_alerts.py --discover-chat --token-env TELEGRAM_BOT_TOKEN
   ```
   Put the chat ID it prints into `alerts.json` (`"chat_id"`), along with your bot's username (`"bot_username"`).
3. Test it without sending anything:
   ```bash
   python3 check_alerts.py --dry-run --token-env TELEGRAM_BOT_TOKEN
   ```
4. Run it every 5 minutes with cron (replace the paths with your own):
   ```
   */5 * * * * cd /path/to/telegram && TELEGRAM_BOT_TOKEN=your-token-here python3 check_alerts.py --token-env TELEGRAM_BOT_TOKEN
   ```
   For instant replies, also run `poll_updates.py` in the background.

## Token safety

Never put the bot token in a file, in a cron definition, or in git. Pass it at run time only, through the `TELEGRAM_BOT_TOKEN` environment variable shown above. The `chat_id` is not a secret — it lives in `alerts.json`.

In the original setup the token lives in a secure vault instead of an environment variable; `--token-env` is the manual mode for running it anywhere else.

## Exit codes

- `0` — check ok (even if nothing fired)
- `1` — a coin price fetch failed
- `2` — config problem (bad `alerts.json`, or an alert fired but no `chat_id` / no usable token)
- `3` — Telegram send failed
