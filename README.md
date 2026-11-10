# Pump Alert Bot

A web app and Telegram checker that watch coin prices and send alerts when a coin pumps, dumps, or crosses a price you set. Works for any coin on any chain DexScreener covers (BSC, Ethereum, Solana, Base, Arbitrum, Polygon, and more).

Live site: https://pump-alert-bot.netlify.app
Telegram bot: [@BlazePumpAlertBot](https://t.me/BlazePumpAlertBot)

## What's in this repo

- `index.html` — the whole web app in one file. Open it in a browser or deploy it to any static host (it runs live on Netlify). Coins, alerts and preferences save in the browser on each device. No login, no server.
- `telegram/` — the 24/7 Telegram checker that sends alerts even when the site is closed.

## The site

Add a coin by name or contract address, then set alerts:

- **Price above / price below** — fires when the price crosses a level you set.
- **Pump / dump % over 1h** — fires on a fast move (for example +25% or −15% in one hour).

A fired alert pings once, then waits to be re-armed. Search results hide coins with fake-looking liquidity and flag borderline ones with a "Liquidity looks unusual" warning. There is also a top gainers section (top 8 by 1h gain).

## The Telegram checker

See [`telegram/README.md`](telegram/README.md). In short: `check_alerts.py` checks prices every run, fires each alert once, and sends it to Telegram. It runs every 5 minutes, with `poll_updates.py` answering bot messages within seconds.

## Plan

Free site to bring people in. Paid Telegram alerts later: $20/month, USD only, pay first, no free trial. Payment via Paystack or Flutterwave (not built yet).
