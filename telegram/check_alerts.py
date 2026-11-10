#!/usr/bin/env python3
"""Pump Alert Bot — 24/7 Telegram checker (server side).

Reads alerts from alerts.json next to this script, fetches live prices from
DexScreener, evaluates each alert exactly the way the shareable site does,
and sends a Telegram message via the Bot API when one fires.

Site semantics (mirrored from pump-alert-bot-shareable.html):
  * above: price >= target
  * below: price <= target
  * pump:  priceChange[window] >= +target %
  * dump:  priceChange[window] <= -target %
  * an alert fires ONCE, then stays "triggered" until re-armed.

One deliberate difference from the site: the site re-arms only when the user
taps "Re-arm alert". There is no user at 3am, so this checker auto-re-arms
an alert only after a later check finds its condition FALSE again (price back
on the safe side / move back inside the threshold) — the fresh-crossing rule
from the recurring_price_alerts skill. While the condition stays true, the
alert stays triggered and silent; it never spams one message per check.

Telegram buttons (inline keyboards, added 2026-11-08):
  * Every alert message carries one row of three buttons: "📈 View chart"
    (opens that coin's DexScreener pair page), "🌐 Open site" (opens
    https://pump-alert-bot.netlify.app), "🔕 Mute this alert" (callback
    "mute:<alert_id>").
  * Every checker run also polls getUpdates once (same vault token path,
    limit 50, timeout 0) and handles each new update exactly once,
    tracked in hidden_files/last_update_id.json so a restart never
    replays old taps and no backlog is ever sent:
    - "mute:<id>": that alert's status becomes "muted" in alerts.json.
      Muted alerts are skipped by evaluation and never fire until
      re-armed. The bot answers the tap with "Alert muted" and sends a
      confirmation carrying a "🔄 Re-arm" button ("rearm:<id>").
    - "rearm:<id>" (button) or "/rearm" / "/rearm <id>" (message):
      the alert goes back to "active" (trigger fields cleared).
    - "myalerts" (button) or "/myalerts": a message listing every alert
      with coin, type, target, window and status.
    - "price" (button) or "/price": the current AGC price with its 1h
      change, fetched live from DexScreener.
    - "/start" (or any other plain message): a welcome message with
      "➕ Add alert" (opens the site — alert creation lives there),
      "📋 My alerts" and "💰 Check price" buttons. The bot never stays
      silent on a message.
  * Adding alerts by typing: a user can type an alert straight into the
    chat, e.g. "LOOT above $0.001", "AGC below $0.000003", "LOOT pump 25%",
    "AGC dump 15%". The coin is resolved by symbol via DexScreener search
    (highest-liquidity matching pair; LOOT is pinned to its TON pair),
    the alert is added to THAT sender's own config only — never another
    person's — and the bot confirms back with the alert details. A coin
    that can't be found, or a message that can't be parsed, gets a reply
    saying so plus an example. Alert only: never a trade.
  * The test message (--test-send) carries the welcome keyboard.
  * --send-text TEXT sends one message to the configured chat_id and
    exits (used by the daily morning-briefing cron). Exit 0 sent,
    2 no chat_id, 3 send failed.
  * Mute/re-arm writes go through the same atomic alerts.json save as
    the checker. --dry-run never sends anything and never writes state
    (including update state).

Token handling — NO token is ever stored in a file, in argv, or in logs:
  * Default (vault mode): the bot token lives in the Secure Vault as the
    custom connector "custom.telegram-bot" (api.telegram.org, api_key,
    placement url_path_segment:/bot{}/sendMessage). The token is fetched as
    an "hsurr:*" surrogate from authd and swapped for the real token by
    Sentinel only on the approved egress to api.telegram.org. See
    /opt/muse/skills/custom-connector-setup/SKILL.md.
  * Manual/test mode: --token-env NAME reads the token from that
    environment variable (e.g. TELEGRAM_BOT_TOKEN), passed at run time by
    the operator. Never export it into a file or a cron definition.
  * The chat_id is NOT a secret; it lives in alerts.json ("chat_id") and is
    set once Blaze has messaged the bot (see --discover-chat).

Usage:
  python3 check_alerts.py [--config alerts.json] [--dry-run] [--test-send]
                          [--discover-chat] [--token-env NAME]
                          [--log-dir hidden_files]

Exit codes: 0 check ok (even if nothing fired); 1 partial (a coin fetch
failed, others ok); 2 config error (bad alerts.json, or an alert fired but
no chat_id / no usable token); 3 Telegram send failed.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "alerts.json")
DEFAULT_LOG_DIR = os.path.join(HERE, "hidden_files")

DEX_API = "https://api.dexscreener.com/latest/dex"
SITE_URL = "https://pump-alert-bot.netlify.app"
TELEGRAM_HOST = "api.telegram.org"
CREDENTIAL_NAME = "custom.telegram-bot"  # Secure Vault connection, see CRON_DRAFT.md
TELEGRAM_URL_TEMPLATE = "https://api.telegram.org/bot{}/sendMessage"
TELEGRAM_GETUPDATES_TEMPLATE = "https://api.telegram.org/bot{}/getUpdates"
TELEGRAM_ANSWER_CALLBACK_TEMPLATE = "https://api.telegram.org/bot{}/answerCallbackQuery"

CHAIN_NAMES = {
    "bsc": "BNB Chain", "ethereum": "Ethereum", "solana": "Solana",
    "base": "Base", "arbitrum": "Arbitrum", "polygon": "Polygon",
}
WIN_LABEL = {"m5": "5 minutes", "h1": "1 hour", "h6": "6 hours", "h24": "24 hours"}
WIN_SHORT = {"m5": "5m", "h1": "1h", "h6": "6h", "h24": "24h"}


# ---------------------------------------------------------------- logging
def log_line(log_dir, text):
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    with open(os.path.join(log_dir, "check-log.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {text}\n")


def fmt_price(p):
    if p is None:
        return "—"
    if p >= 1000:
        return "$" + f"{p:,.2f}"
    if p >= 1:
        return "$" + f"{p:.2f}"
    s = f"{p:.4g}"
    if "e" in s:  # tiny price: expand, like the site's exact mode
        s = f"{p:.18f}".rstrip("0").rstrip(".")
    return "$" + s


def fmt_pct(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


# ---------------------------------------------------------------- config
def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("alerts"), list):
        raise ValueError(f"{path}: expected an object with an 'alerts' list")
    if not isinstance(cfg.get("coins"), list):
        cfg["coins"] = []
    return cfg


def save_config_atomic(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- DexScreener
def dex_get(path, timeout=20):
    # DexScreener blocks Python-urllib's default User-Agent (403); a
    # browser UA works. Retry once on 403/5xx — the feed flakes.
    headers = {"Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
               "Safari/537.36"}
    last = None
    for _ in range(2):
        req = urllib.request.Request(DEX_API + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(2)
    raise last


def fetch_coin(coin):
    """Refresh one coin from DexScreener. Prefers the stored pairAddress,
    like the site; falls back to the highest-liquidity pair for the token."""
    pair_addr = (coin.get("pairAddress") or "").lower()
    chain = coin.get("chainId", "bsc")
    try:
        data = dex_get(f"/pairs/{chain}/{coin['pairAddress']}")
        pairs = data.get("pairs") or []
        pair = pairs[0] if pairs else None
    except Exception:
        pair = None
    if pair is None:
        data = dex_get(f"/tokens/{coin['address']}")
        pairs = data.get("pairs") or []
        mine = [p for p in pairs
                if p.get("chainId") == chain and p.get("pairAddress", "").lower() == pair_addr]
        if mine:
            pair = mine[0]
        else:
            pool = [p for p in pairs if p.get("priceUsd")]
            pool.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
                      reverse=True)
            pair = pool[0] if pool else None
    if pair is None:
        raise RuntimeError(f"no DexScreener pair found for {coin.get('symbol')}")
    coin["price"] = float(pair["priceUsd"]) if pair.get("priceUsd") else None
    coin["priceChange"] = pair.get("priceChange") or {}
    coin["liquidity"] = (pair.get("liquidity") or {}).get("usd")
    coin["pairAddress"] = pair.get("pairAddress", coin.get("pairAddress"))
    coin["url"] = pair.get("url", coin.get("url", ""))
    coin["lastCheckedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    return coin


# ---------------------------------------------------------------- evaluation
def evaluate_alert(alert, coin):
    """Same rules as the site's evaluateAlert()."""
    price = coin.get("price")
    if price is None:
        return False
    t = alert["type"]
    if t == "above":
        return price >= alert["target"]
    if t == "below":
        return price <= alert["target"]
    ch = (coin.get("priceChange") or {}).get(alert.get("window", "h1"))
    if ch is None:
        return False
    if t == "pump":
        return ch >= alert["target"]
    if t == "dump":
        return ch <= -alert["target"]
    return False


def cond_text(alert):
    if alert["type"] == "above":
        return f"rises above {fmt_price(alert['target'])}"
    if alert["type"] == "below":
        return f"falls below {fmt_price(alert['target'])}"
    if alert["type"] == "pump":
        return f"pumps +{alert['target']}% in {WIN_LABEL[alert.get('window', 'h1')]}"
    return f"dumps -{alert['target']}% in {WIN_LABEL[alert.get('window', 'h1')]}"


def build_message(alert, coin):
    """Mirror the site's firePing() headlines, as Telegram HTML."""
    kind_up = alert["type"] in ("pump", "above")
    ch = (coin.get("priceChange") or {}).get(alert.get("window", "h1"))
    sym = html.escape(coin.get("symbol", "?"))
    if alert["type"] == "pump":
        headline = f"{sym} is pumping — {fmt_pct(ch)} in {WIN_LABEL[alert['window']]}"
    elif alert["type"] == "dump":
        headline = f"{sym} is dumping — {fmt_pct(ch)} in {WIN_LABEL[alert['window']]}"
    elif alert["type"] == "above":
        headline = f"{sym} broke above {fmt_price(alert['target'])}"
    else:
        headline = f"{sym} fell below {fmt_price(alert['target'])}"
    chain = CHAIN_NAMES.get(coin.get("chainId", ""), coin.get("chainId", ""))
    detail = (f"Price is now <b>{fmt_price(coin.get('price'))}</b> on {html.escape(chain)}"
              f" · Your alert: {html.escape(cond_text(alert))}")
    url = coin.get("url") or ""
    text = f"🚨 <b>{headline}</b>\n{detail}"
    if url:
        text += f"\n<a href=\"{html.escape(url, quote=True)}\">View on DexScreener ↗</a>"
    text += "\n\n<i>Pump Alert Bot · alert only, no trade made.</i>"
    return text


# ---------------------------------------------------------------- Telegram
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def telegram_url(template, token_env):
    """Return the send URL with the credential applied — never logged.

    Vault mode (default): surrogate from authd, swapped by Sentinel on
    approved egress to api.telegram.org. Env mode: token from the named
    environment variable, for manual/test runs only.
    """
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise RuntimeError(f"environment variable {token_env} is not set")
        return template.replace("{}", urllib.parse.quote(token, safe=""), 1)
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import dynamic_credential_entry
    # IMPORTANT: the surrogate must go into the URL RAW, never percent-encoded.
    # url_with_surrogate_path_segment() quotes it (hsurr%3A...), and Sentinel
    # then does not recognise it, so the surrogate itself reaches Telegram and
    # every call 404s. With the raw "hsurr:..." in the path, Sentinel swaps in
    # the real token on egress (verified live 2026-11-08: sendMessage 200).
    surrogate = dynamic_credential_entry(CREDENTIAL_NAME)["surrogate"]
    return template.replace("{}", surrogate, 1)


def telegram_post(url, payload, timeout=30):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error_code": e.code,
                    "description": f"HTTP {e.code}"}


def alert_keyboard(alert, coin):
    """One row under a fired alert: View chart / Open site / Mute."""
    row = []
    url = coin.get("url") or ""
    if url:
        row.append({"text": "📈 View chart", "url": url})
    row.append({"text": "🌐 Open site", "url": SITE_URL})
    row.append({"text": "🔕 Mute this alert",
                "callback_data": f"mute:{alert['id']}"})
    return {"inline_keyboard": [row]}


def welcome_keyboard():
    """Menu shown with the test message, /start, and unknown messages."""
    return {"inline_keyboard": [
        [{"text": "➕ Add alert", "url": SITE_URL}],
        [{"text": "📋 My alerts", "callback_data": "myalerts"},
         {"text": "💰 Check price", "callback_data": "price"}],
        [{"text": "🚀 Top gainers", "callback_data": "gainers"}],
    ]}


# ---------------------------------------------------------------- top gainers
AGC_ADDRESS_LOWER = "0x63dea5acdbe1f27c968907d08610a5f0a0284ba6"
GAINERS_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
GAINERS_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"


def _web_get_json(url, timeout=20):
    """GET any DexScreener JSON URL with a browser UA (same as dex_get)."""
    headers = {"Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
               "Safari/537.36"}
    last = None
    for _ in range(2):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _fake_looking_liquidity(coin):
    """Same hide rule as the site: hide a coin whose liquidity looks fake."""
    liq = coin.get("liquidity")
    if liq is None:
        return False
    try:
        liq = float(liq)
    except (TypeError, ValueError):
        return False
    vol = coin.get("volumeH24")
    mcap = coin.get("mcap")
    if liq > 1_000_000 and vol is not None and vol < 1000:
        return True
    if mcap is not None and mcap > 0 and liq > 10 * mcap:
        return True
    created = coin.get("pairCreatedAt")
    txns = coin.get("txnsH24")
    if liq > 100_000 and created is not None and txns is not None:
        age_ms = int(time.time() * 1000) - int(created)
        if 0 <= age_ms < 60 * 60 * 1000 and txns < 10:
            return True
    return False


def fetch_top_gainers(limit=8):
    """Top gainers by 1h % gain, mirroring the site's Top gainers section:
    boosted + latest-profile tokens, each coin's pairs fetched individually
    (a coin's price only ever comes from its own pair), fake-liquidity
    coins hidden, sorted by 1h gain. AGC is excluded."""
    candidates = []
    seen = set()
    for url in (GAINERS_BOOSTS_URL, GAINERS_PROFILES_URL):
        try:
            items = _web_get_json(url)
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("tokenAddress"):
                continue
            addr = str(item["tokenAddress"]).lower()
            if addr == AGC_ADDRESS_LOWER:
                continue
            key = (item.get("chainId") or "") + ":" + addr
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"chainId": item.get("chainId") or "",
                               "tokenAddress": item["tokenAddress"]})
    coins = []
    for cand in candidates[:40]:
        addr = cand["tokenAddress"].lower()
        try:
            data = dex_get(f"/tokens/{cand['tokenAddress']}")
            pairs = data.get("pairs") or []
        except Exception:
            continue
        own = [p for p in pairs
               if p.get("baseToken", {}).get("address", "").lower() == addr
               and (not cand["chainId"] or not p.get("chainId")
                    or p["chainId"] == cand["chainId"])
               and p.get("priceUsd")]
        if not own:
            continue
        own.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
                 reverse=True)
        pair = own[0]
        pc = pair.get("priceChange") or {}
        h1 = pc.get("h1")
        if h1 is None:
            continue
        txns = None
        if pair.get("txns", {}).get("h24"):
            txns = ((pair["txns"]["h24"].get("buys") or 0)
                    + (pair["txns"]["h24"].get("sells") or 0))
        coin = {
            "address": pair.get("baseToken", {}).get("address"),
            "chainId": pair.get("chainId"),
            "liquidity": (pair.get("liquidity") or {}).get("usd"),
            "mcap": pair.get("marketCap"),
            "pairCreatedAt": pair.get("pairCreatedAt"),
            "price": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
            "priceChange": pc,
            "symbol": pair.get("baseToken", {}).get("symbol") or "?",
            "txnsH24": txns,
            "url": pair.get("url") or "",
            "volumeH24": (pair.get("volume") or {}).get("h24"),
        }
        if _fake_looking_liquidity(coin):
            continue
        coins.append(coin)
    coins.sort(key=lambda c: (-(c["priceChange"].get("h1") or -float("inf")),
                             -(c.get("volumeH24") or 0)))
    return coins[:limit]


def _gainers_text(coins):
    if not coins:
        return ("🚀 <b>Top gainers</b>\nNo gainers found right now. "
                "Try again in a few minutes.")
    lines = ["🚀 <b>Top gainers — biggest 1h pumps</b>"]
    for i, c in enumerate(coins, 1):
        sym = html.escape(c.get("symbol", "?"))
        h1 = (c.get("priceChange") or {}).get("h1")
        vol = c.get("volumeH24")
        vol_txt = f", Vol ${vol:,.0f}" if vol else ""
        chain = CHAIN_NAMES.get(c.get("chainId", ""), c.get("chainId", ""))
        line = (f"{i}. <b>{sym}</b> — {fmt_price(c.get('price'))} "
                f"({fmt_pct(h1)} in 1h{vol_txt}) · {html.escape(chain)}")
        if c.get("url"):
            line = (f"{i}. <a href=\"{html.escape(c['url'], quote=True)}\">"
                    f"<b>{sym}</b></a> — {fmt_price(c.get('price'))} "
                    f"({fmt_pct(h1)} in 1h{vol_txt}) · {html.escape(chain)}")
        lines.append(line)
    lines.append("\n<i>Fresh pumps move fast — check liquidity before "
                 "trusting a coin. Tap “Add alert” on the site to watch one.</i>")
    return "\n".join(lines)


def rearm_keyboard(alert):
    """Buttons on the muted confirmation: re-arm that alert, or list all."""
    return {"inline_keyboard": [
        [{"text": "🔄 Re-arm this alert",
          "callback_data": f"rearm:{alert['id']}"}],
        [{"text": "📋 My alerts", "callback_data": "myalerts"}],
    ]}


WELCOME_TEXT = (
    "👋 <b>Pump Alert Bot</b>\n"
    "I watch your alerts 24/7 and message you here when one fires — "
    "even when the site is closed.\n\n"
    "Add or change alerts on the site, or just type one here, e.g.\n"
    "“LOOT above $0.001”, “AGC below $0.000003”, “LOOT pump 25%”.\n\n"
    "Use the buttons below to see your alerts or check the price."
)

# Sent to someone who messages the bot but has no alerts set up yet
# (their chat is not the configured chat_id for this profile). Each
# person's alerts live in their own separate file, so nobody's alerts
# ever land in anyone else's list.
ONBOARD_TEXT = (
    "👋 <b>Pump Alert Bot</b>\n"
    "This bot sends price alerts for the Pump Alert Bot site.\n\n"
    "To get your own alerts here:\n"
    "1. Open https://pump-alert-bot.netlify.app and set your alerts.\n"
    "2. Tap “Export alerts for Telegram” on the site.\n"
    "3. Send the exported alerts to Blaze and he will switch them on "
    "for your Telegram.\n\n"
    "Your alerts will always stay yours — nobody else sees them."
)


def send_telegram(token_env, chat_id, text, reply_markup=None):
    url = telegram_url(TELEGRAM_URL_TEMPLATE, token_env)
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for attempt in (1, 2):
        res = telegram_post(url, payload)
        if res.get("ok"):
            return res["result"]
        params = res.get("parameters") or {}
        if res.get("error_code") == 429 and params.get("retry_after") and attempt == 1:
            time.sleep(min(int(params["retry_after"]), 60) + 1)
            continue
        raise RuntimeError(
            f"Telegram sendMessage failed: {res.get('description') or res}")
    raise RuntimeError("Telegram sendMessage failed after retry")


def answer_callback(token_env, callback_query_id, text):
    """Acknowledge a button tap so Telegram stops showing the spinner.
    Failure here is non-fatal: the follow-up message is what matters."""
    try:
        url = telegram_url(TELEGRAM_ANSWER_CALLBACK_TEMPLATE, token_env)
        telegram_post(url, {"callback_query_id": callback_query_id,
                            "text": text})
    except Exception:
        pass


def discover_chats(token_env, log_dir):
    """List chats that have messaged the bot (getUpdates) so Blaze's chat_id
    can be recorded in alerts.json. Prints chat id + username/first name."""
    url = telegram_url(TELEGRAM_GETUPDATES_TEMPLATE, token_env)
    res = telegram_post(url, {"limit": 50, "timeout": 0})
    if not res.get("ok"):
        raise RuntimeError(f"getUpdates failed: {res.get('description') or res}")
    seen = {}
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = {
                "id": chat["id"], "type": chat.get("type"),
                "username": chat.get("username"),
                "first_name": chat.get("first_name"),
                "title": chat.get("title"),
            }
    for c in seen.values():
        print(f"chat_id={c['id']} type={c['type']} username={c['username']} "
              f"name={c['first_name'] or c['title']}")
    log_line(log_dir, f"discover-chat: {len(seen)} chat(s) seen via getUpdates")
    return seen


# ---------------------------------------------------------------- bot buttons / callbacks
def _update_state_path(log_dir):
    return os.path.join(log_dir, "last_update_id.json")


def _load_last_update_id(log_dir):
    try:
        with open(_update_state_path(log_dir), encoding="utf-8") as f:
            return int(json.load(f).get("last_update_id", 0))
    except Exception:
        return 0


def _save_last_update_id(log_dir, update_id):
    os.makedirs(log_dir, exist_ok=True)
    tmp = _update_state_path(log_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_update_id": update_id}, f)
    os.replace(tmp, _update_state_path(log_dir))


def _alert_desc(alert, coins):
    coin = coins.get(alert.get("coinKey"), {})
    sym = html.escape(coin.get("symbol", "?"))
    return f"{sym} — {html.escape(cond_text(alert))} [{alert.get('id')}]"


def _myalerts_text(cfg, coins):
    alerts = cfg.get("alerts") or []
    if not alerts:
        return ("📋 <b>Your alerts</b>\nNo alerts yet. Tap “Add alert” "
                "to create one on the site.")
    lines = ["📋 <b>Your alerts</b>"]
    for a in alerts:
        lines.append(f"• {_alert_desc(a, coins)} — "
                     f"<b>{html.escape(a.get('status', 'active'))}</b>")
    lines.append("\nMute or re-arm from the buttons under an alert, "
                 "or send /rearm to re-arm every muted alert.")
    return "\n".join(lines)


def _price_text(coins):
    # AGC is the watched coin; fall back to the first coin with a price.
    coin = None
    for c in coins.values():
        if c.get("symbol") == "AGC" and c.get("price") is not None:
            coin = c
            break
    if coin is None:
        for c in coins.values():
            if c.get("price") is not None:
                coin = c
                break
    if coin is None:
        return "💰 Price check failed — no live price right now. Try again in a minute."
    sym = html.escape(coin.get("symbol", "?"))
    ch1 = (coin.get("priceChange") or {}).get("h1")
    return (f"💰 <b>{sym}</b> is now <b>{fmt_price(coin.get('price'))}</b> "
            f"({fmt_pct(ch1)} in 1h)")


def _set_alert_status(cfg, config_path, alert_id, status):
    """Set one alert's status; clears trigger fields on re-arm. Returns
    the alert dict, or None when no alert has that id."""
    for a in cfg.get("alerts") or []:
        if a.get("id") == alert_id:
            a["status"] = status
            if status == "active":
                a["triggeredAt"] = None
                a["triggerPrice"] = None
                a["triggerChange"] = None
            save_config_atomic(config_path, cfg)
            return a
    return None


# ---------------------------------------------------------------- add alerts by typing
# "LOOT above $0.001" | "AGC below $0.000003" | "LOOT pump 25%" | "AGC dump 15%"
# An optional leading "add" is accepted. Prices take an optional "$";
# pump/dump values are percents over 1h (an optional "%" is accepted).
ADD_RE = re.compile(
    r"^\s*(?:add\s+)?([A-Za-z0-9]{2,12})\s+(above|below|pump|dump)\s+"
    r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%?\s*$", re.IGNORECASE)

# Symbols pinned to the exact coin Blaze watches — the symbol alone is
# ambiguous (several tokens share "AGC"), so these never resolve to a
# same-symbol token on another chain.
PINNED_COINS = {
    # symbol -> (chainId, token address, preferred pair address or None)
    "AGC": ("bsc", "0x63dea5aCdbE1F27c968907D08610A5f0A0284bA6",
            "0xE56041CdDBB46DB01c67B7A62684F428a04Db8C6"),
    "LOOT": ("ton", "EQCM4DEzDVFLq7kaSodQX-8-u_aF9M9DDe1EePWsuMozqGkt",
             "EQAJ_seDWXCvKh7zObjzByO4quyDzAcaZjRgsmTnyMumXR1H"),
}

# LOOT is Blaze's private coin (never on the public site). It trades on
# several chains; the TON one is the one he watches.
LOOT_TON_ADDRESS = "EQCM4DEzDVFLq7kaSodQX-8-u_aF9M9DDe1EePWsuMozqGkt"
LOOT_TON_PAIR = "EQAJ_seDWXCvKh7zObjzByO4quyDzAcaZjRgsmTnyMumXR1H"

ADD_HELP_TEXT = (
    "➕ <b>Add an alert by typing</b>\n"
    "Send a message like:\n"
    "• <code>LOOT above $0.001</code>\n"
    "• <code>AGC below $0.000003</code>\n"
    "• <code>LOOT pump 25%</code>\n"
    "• <code>AGC dump 15%</code>\n\n"
    "I find the coin on DexScreener, add the alert to your own list, "
    "and message you here when it fires."
)


def parse_add_command(text):
    """Parse an add-alert message. Returns {symbol, type, target} or None."""
    m = ADD_RE.match(text or "")
    if not m:
        return None
    try:
        value = float(m.group(3).replace(",", ""))
    except ValueError:
        return None
    if value <= 0:
        return None
    return {"symbol": m.group(1).upper(),
            "type": m.group(2).lower(), "target": value}


def _looks_like_add(text):
    """True when the message looks like an add attempt but did not parse
    (bad number, missing value, ...), so we can answer with the format."""
    words = [(w or "").strip(",").lower() for w in (text or "").split()]
    if words and words[0] == "add":
        words = words[1:]
    return len(words) >= 3 and words[1] in ("above", "below", "pump", "dump")


def _coin_from_pair(pair):
    bt = pair.get("baseToken") or {}
    return {
        "key": "%s:%s" % (pair.get("chainId"),
                          (bt.get("address") or "").lower()),
        "chainId": pair.get("chainId"),
        "pairAddress": pair.get("pairAddress"),
        "address": bt.get("address"),
        "symbol": bt.get("symbol") or "?",
        "name": bt.get("name") or bt.get("symbol") or "Unknown token",
        "url": pair.get("url") or "",
        "price": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
        "priceChange": pair.get("priceChange") or {},
        "liquidity": (pair.get("liquidity") or {}).get("usd"),
    }


def resolve_coin_by_symbol(symbol):
    """Find a coin by symbol on DexScreener. Returns a checker coin dict
    or None. Pinned symbols (AGC, LOOT) always resolve to Blaze's exact
    coin; anything else takes the highest-liquidity pair with that symbol."""
    data = dex_get("/search?q=%s" % urllib.parse.quote(symbol))
    pairs = data.get("pairs") or []
    sym = symbol.lower()
    mine = [p for p in pairs
            if (p.get("baseToken") or {}).get("symbol", "").lower() == sym
            and p.get("priceUsd")]
    if not mine:
        return None
    pin = PINNED_COINS.get(symbol.upper())
    if pin:
        chain, addr, pair_addr = pin
        pinned = [p for p in mine
                  if p.get("chainId") == chain
                  and (p.get("baseToken") or {}).get("address") == addr]
        if pinned:
            if pair_addr:
                main = [p for p in pinned
                        if p.get("pairAddress") == pair_addr]
                if main:
                    return _coin_from_pair(main[0])
            pinned.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
                        reverse=True)
            return _coin_from_pair(pinned[0])
        return None
    mine.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
              reverse=True)
    return _coin_from_pair(mine[0])


def add_alert_from_chat(cfg, config_path, coin, atype, target):
    """Add coin (if new) and alert to THIS config only, then save.
    Returns the new alert dict. Never touches any other config."""
    coins = {c["key"]: c for c in cfg.get("coins", [])}
    existing = coins.get(coin["key"])
    if existing:
        for k, v in coin.items():
            if v is not None:
                existing[k] = v
        coin = existing
    else:
        cfg.setdefault("coins", []).append(coin)
    nums = []
    for a in cfg.get("alerts", []):
        m = re.match(r"al_(\d+)$", a.get("id", ""))
        if m:
            nums.append(int(m.group(1)))
    alert = {
        "coinKey": coin["key"],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "createdPrice": coin.get("price"),
        "example": False,
        "id": "al_%d" % (max(nums) + 1 if nums else 1),
        "status": "active",
        "target": target,
        "triggerChange": None,
        "triggerPrice": None,
        "triggeredAt": None,
        "type": atype,
        "window": "h1",
    }
    cfg.setdefault("alerts", []).append(alert)
    save_config_atomic(config_path, cfg)
    return alert


def handle_updates(token_env, cfg, config_path, coins, log_dir):
    """Poll getUpdates once and act on new button taps / messages.

    Uses the same vault token path as sends. Each update_id is handled
    once (tracked in hidden_files/last_update_id.json); older updates
    are never replayed and no backlog is sent. Only the configured
    chat_id is answered. Errors are logged and never crash the run —
    the price check already done is what decides the exit code."""
    chat_id = cfg.get("chat_id")
    if not chat_id:
        return
    last = _load_last_update_id(log_dir)
    try:
        url = telegram_url(TELEGRAM_GETUPDATES_TEMPLATE, token_env)
        res = telegram_post(url, {"limit": 50, "timeout": 0,
                                  "offset": last + 1})
    except Exception as e:
        log_line(log_dir, f"updates poll FAILED: {e}")
        return
    if not res.get("ok"):
        log_line(log_dir,
                 f"updates poll FAILED: {res.get('description') or res}")
        return
    updates = res.get("result") or []
    if not updates:
        return
    max_id = last
    for upd in updates:
        uid = upd.get("update_id", 0)
        max_id = max(max_id, uid)
        try:
            _handle_one_update(token_env, cfg, config_path, coins,
                               chat_id, upd, log_dir)
        except Exception as e:
            log_line(log_dir, f"update {uid} FAILED: {e}")
    _save_last_update_id(log_dir, max_id)
    log_line(log_dir, f"updates handled: {len(updates)} new")


def _handle_one_update(token_env, cfg, config_path, coins, chat_id, upd, log_dir):
    query = upd.get("callback_query")
    msg = upd.get("message")
    if query:
        data = (query.get("data") or "").strip()
        qchat = (query.get("message") or {}).get("chat") or {}
        if qchat.get("id") != chat_id:
            return
        qid = query.get("id")
        if data.startswith("mute:"):
            alert_id = data[5:]
            alert = _set_alert_status(cfg, config_path, alert_id, "muted")
            answer_callback(token_env, qid, "Alert muted")
            if alert:
                text = (f"🔕 <b>Alert muted</b>\n{_alert_desc(alert, coins)}\n"
                        "It will not fire again until you re-arm it.")
                send_telegram(token_env, chat_id, text,
                              reply_markup=rearm_keyboard(alert))
                log_line(log_dir, f"callback mute {alert_id} -> muted")
            else:
                send_telegram(token_env, chat_id,
                              f"⚠️ No alert with id {html.escape(alert_id)}.")
            return
        if data.startswith("rearm:"):
            alert_id = data[6:]
            alert = _set_alert_status(cfg, config_path, alert_id, "active")
            answer_callback(token_env, qid, "Alert re-armed")
            if alert:
                send_telegram(token_env, chat_id,
                              f"✅ <b>Alert re-armed</b>\n"
                              f"{_alert_desc(alert, coins)}\n"
                              "It will fire again on the next fresh move.")
                log_line(log_dir, f"callback rearm {alert_id} -> active")
            else:
                send_telegram(token_env, chat_id,
                              f"⚠️ No alert with id {html.escape(alert_id)}.")
            return
        if data == "myalerts":
            answer_callback(token_env, qid, "")
            send_telegram(token_env, chat_id, _myalerts_text(cfg, coins),
                          reply_markup=welcome_keyboard())
            log_line(log_dir, "callback myalerts")
            return
        if data == "price":
            answer_callback(token_env, qid, "")
            send_telegram(token_env, chat_id, _price_text(coins),
                          reply_markup=welcome_keyboard())
            log_line(log_dir, "callback price")
            return
        if data == "gainers":
            answer_callback(token_env, qid, "")
            try:
                gainers = fetch_top_gainers(8)
                text = _gainers_text(gainers)
            except Exception as e:
                text = ("🚀 <b>Top gainers</b>\nCould not load gainers "
                        "right now. Try again in a minute.")
                log_line(log_dir, f"callback gainers FAILED: {e}")
            send_telegram(token_env, chat_id, text,
                          reply_markup=welcome_keyboard())
            log_line(log_dir, "callback gainers")
            return
        answer_callback(token_env, qid, "")
        return
    if not msg:
        return
    mchat = msg.get("chat") or {}
    if mchat.get("id") != chat_id:
        # Someone new messaging the bot (a friend, not the owner of this
        # profile): never stay silent — tell them how to get their own
        # alerts. Their chat is never added to this profile's config.
        try:
            send_telegram(token_env, mchat["id"], ONBOARD_TEXT,
                          reply_markup=welcome_keyboard())
            log_line(log_dir,
                     f"message from unknown chat {mchat['id']} -> onboard")
        except Exception as e:
            log_line(log_dir, f"onboard reply FAILED: {e}")
        return
    text = (msg.get("text") or "").strip()
    low = text.lower()
    if low.startswith("/start") or low == "start":
        send_telegram(token_env, chat_id, WELCOME_TEXT,
                      reply_markup=welcome_keyboard())
        log_line(log_dir, "message /start -> welcome")
        return
    if low.startswith("/rearm"):
        parts = low.split()
        if len(parts) > 1:
            alert = _set_alert_status(cfg, config_path, parts[1], "active")
            if alert:
                send_telegram(token_env, chat_id,
                              f"✅ <b>Alert re-armed</b>\n"
                              f"{_alert_desc(alert, coins)}")
                log_line(log_dir, f"message /rearm {parts[1]} -> active")
            else:
                send_telegram(token_env, chat_id,
                              f"⚠️ No alert with id {html.escape(parts[1])}.")
        else:
            muted = [a for a in cfg.get("alerts") or []
                     if a.get("status") == "muted"]
            for a in muted:
                _set_alert_status(cfg, config_path, a["id"], "active")
            send_telegram(token_env, chat_id,
                          f"✅ Re-armed {len(muted)} muted alert(s).")
            log_line(log_dir, f"message /rearm -> {len(muted)} re-armed")
        return
    if low in ("/myalerts", "myalerts"):
        send_telegram(token_env, chat_id, _myalerts_text(cfg, coins),
                      reply_markup=welcome_keyboard())
        log_line(log_dir, "message /myalerts")
        return
    if low in ("/price", "price"):
        send_telegram(token_env, chat_id, _price_text(coins),
                      reply_markup=welcome_keyboard())
        log_line(log_dir, "message /price")
        return
    if low in ("/gainers", "gainers"):
        try:
            gainers = fetch_top_gainers(8)
            text = _gainers_text(gainers)
        except Exception as e:
            text = ("🚀 <b>Top gainers</b>\nCould not load gainers "
                    "right now. Try again in a minute.")
            log_line(log_dir, f"message /gainers FAILED: {e}")
        send_telegram(token_env, chat_id, text,
                      reply_markup=welcome_keyboard())
        log_line(log_dir, "message /gainers")
        return
    # Add an alert by typing: "LOOT above $0.001", "AGC pump 25%", ...
    # Only ever added to THIS sender's own config (cfg/config_path).
    parsed = parse_add_command(text)
    if parsed or _looks_like_add(text):
        if not parsed:
            send_telegram(token_env, chat_id, ADD_HELP_TEXT,
                          reply_markup=welcome_keyboard())
            log_line(log_dir, "message add-parse-failed -> help")
            return
        try:
            coin = resolve_coin_by_symbol(parsed["symbol"])
        except Exception as e:
            coin = None
            log_line(log_dir,
                     f"resolve {parsed['symbol']} FAILED: {e}")
        if coin is None:
            send_telegram(
                token_env, chat_id,
                f"⚠️ I couldn't find a coin called "
                f"<b>{html.escape(parsed['symbol'])}</b> on DexScreener.\n"
                f"Check the spelling, or send its contract address on the "
                f"site instead.\n\nTry e.g. <code>LOOT above $0.001</code>.",
                reply_markup=welcome_keyboard())
            log_line(log_dir,
                     f"message add {parsed['symbol']} -> coin not found")
            return
        # Sanity check: a price target wildly far from the live price is
        # almost always a mistyped number (e.g. "LOOT below 10" when LOOT
        # trades at $0.0009 — "below $10" would fire at once and is useless).
        # Do NOT add it; ask what was meant instead.
        _price = coin.get("price")
        if (parsed["type"] in ("above", "below") and _price
                and _price > 0):
            _absurd = (parsed["type"] == "below"
                       and parsed["target"] > _price * 10) or \
                      (parsed["type"] == "above"
                       and parsed["target"] < _price / 10)
            if _absurd:
                send_telegram(
                    token_env, chat_id,
                    f"⚠️ That price looks off: "
                    f"<b>{html.escape(coin.get('symbol', ''))}</b> is "
                    f"<b>{fmt_price(_price)}</b> right now, but you typed "
                    f"<b>{parsed['type']} {fmt_price(parsed['target'])}</b>.\n\n"
                    f"Did you mean a price close to "
                    f"{fmt_price(_price)}, like "
                    f"<code>{coin.get('symbol', '')} "
                    f"{parsed['type']} "
                    f"${_price:.4g}</code>? Or a percent move, like "
                    f"<code>{coin.get('symbol', '')} "
                    f"{'dump' if parsed['type'] == 'below' else 'pump'} "
                    f"10%</code>? Type it again and I'll add it.",
                    reply_markup=welcome_keyboard())
                log_line(log_dir,
                         f"message add {parsed['symbol']} "
                         f"{parsed['type']} {parsed['target']} "
                         f"-> absurd vs price {_price}, asked to confirm")
                return
        alert = add_alert_from_chat(cfg, config_path, coin,
                                    parsed["type"], parsed["target"])
        coins[coin["key"]] = coin
        chain = CHAIN_NAMES.get(coin.get("chainId", ""),
                                coin.get("chainId", ""))
        send_telegram(
            token_env, chat_id,
            f"✅ <b>Alert added</b>\n"
            f"{_alert_desc(alert, coins)} [{alert['id']}]\n"
            f"Price now: <b>{fmt_price(coin.get('price'))}</b> "
            f"on {html.escape(chain)} · I watch it 24/7 and will message "
            f"you here when it fires.",
            reply_markup=welcome_keyboard())
        log_line(log_dir,
                 f"message add {alert['id']} {coin.get('symbol')} "
                 f"{alert['type']} {alert['target']} -> added")
        return
    # Any other plain message: never stay silent — send the welcome menu.
    send_telegram(token_env, chat_id, WELCOME_TEXT,
                  reply_markup=welcome_keyboard())
    log_line(log_dir, f"message {text[:40]!r} -> welcome")


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Pump Alert Bot Telegram checker")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate and print what would be sent; send nothing, write no state")
    ap.add_argument("--test-send", action="store_true",
                    help="send one test message to the configured chat_id and exit")
    ap.add_argument("--send-text", default=None, metavar="TEXT",
                    help="send TEXT as a Telegram message to the configured "
                    "chat_id and exit (used by the morning-briefing cron)")
    ap.add_argument("--gainers", default=None, metavar="N", type=int,
                    help="print the top-N gainers text to stdout and exit "
                    "(used by the morning-briefing cron)")
    ap.add_argument("--discover-chat", action="store_true",
                    help="list chats that messaged the bot (getUpdates) and exit")
    ap.add_argument("--token-env", default=None, metavar="NAME",
                    help="read the bot token from env var NAME instead of the Secure Vault")
    args = ap.parse_args(argv)

    if args.gainers is not None:
        try:
            print(_gainers_text(fetch_top_gainers(args.gainers)))
            return 0
        except Exception as e:
            print(f"gainers failed: {e}", file=sys.stderr)
            return 1

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    coins = {c["key"]: c for c in cfg["coins"]}
    alerts = cfg["alerts"]
    chat_id = cfg.get("chat_id")

    if args.discover_chat:
        try:
            discover_chats(args.token_env, args.log_dir)
            return 0
        except Exception as e:
            print(f"discover-chat failed: {e}", file=sys.stderr)
            log_line(args.log_dir, f"discover-chat FAILED: {e}")
            return 3

    if args.test_send:
        if not chat_id:
            print("config error: alerts.json has no chat_id yet — Blaze must "
                  "message the bot first, then run --discover-chat.",
                  file=sys.stderr)
            return 2
        try:
            send_telegram(args.token_env, chat_id,
                          "🔔 <b>Pump Alert Bot test</b>\nTelegram notifications are "
                          "connected. You will get an alert here when a coin hits "
                          "one of your alerts — even when the site is closed.",
                          reply_markup=welcome_keyboard())
            print(f"test message sent to chat_id={chat_id}")
            log_line(args.log_dir, f"test-send OK chat_id={chat_id}")
            return 0
        except Exception as e:
            print(f"test-send failed: {e}", file=sys.stderr)
            log_line(args.log_dir, f"test-send FAILED: {e}")
            return 3

    if args.send_text is not None:
        if not chat_id:
            print("config error: no chat_id in config — nothing to send to.",
                  file=sys.stderr)
            return 2
        try:
            send_telegram(args.token_env, chat_id, args.send_text)
            print(f"message sent to chat_id={chat_id}")
            log_line(args.log_dir, "send-text OK chat_id=%s" % chat_id)
            return 0
        except Exception as e:
            print(f"send-text failed: {e}", file=sys.stderr)
            log_line(args.log_dir, f"send-text FAILED: {e}")
            return 3

    # --- price fetch, one DexScreener call per watched coin
    fetch_errors = []
    for coin in coins.values():
        try:
            fetch_coin(coin)
        except Exception as e:
            fetch_errors.append(f"{coin.get('symbol')}: {e}")
            log_line(args.log_dir, f"fetch FAILED {coin.get('symbol')}: {e}")
    if fetch_errors and not coins:
        return 1

    fired, rearmed, skipped = [], [], []
    state_changed = False
    for alert in alerts:
        coin = coins.get(alert["coinKey"])
        if coin is None or coin.get("price") is None:
            skipped.append((alert, "no price"))
            continue
        active = evaluate_alert(alert, coin)
        status = alert.get("status", "active")
        if status == "muted":
            # Muted by a Telegram button tap: never fires until re-armed.
            skipped.append((alert, "status=muted"))
            continue
        if status == "active" and active:
            alert["status"] = "triggered"
            alert["triggeredAt"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime())
            alert["triggerPrice"] = coin["price"]
            alert["triggerChange"] = (coin.get("priceChange") or {}).get(
                alert.get("window", "h1"))
            fired.append((alert, coin))
            state_changed = True
        elif status == "triggered" and not active:
            # Fresh-crossing re-arm: condition cleared, alert live again.
            alert["status"] = "active"
            alert["triggeredAt"] = None
            alert["triggerPrice"] = None
            alert["triggerChange"] = None
            rearmed.append((alert, coin))
            state_changed = True
        else:
            skipped.append((alert, f"status={status} active={active}"))

    summary = {"fired": [a["id"] for a, _ in fired],
               "rearmed": [a["id"] for a, _ in rearmed],
               "fetch_errors": fetch_errors}
    for alert, coin in fired:
        msg = build_message(alert, coin)
        if args.dry_run:
            print(f"[DRY-RUN] would send to chat_id={chat_id}:\n{msg}\n")
            log_line(args.log_dir,
                     f"dry-run FIRE {alert['id']} {alert['type']} "
                     f"{coin.get('symbol')} @ {fmt_price(coin.get('price'))}")
            continue
        if not chat_id:
            print("config error: alert fired but alerts.json has no chat_id — "
                  "Blaze must message the bot first.", file=sys.stderr)
            log_line(args.log_dir,
                     f"FIRE {alert['id']} NOT SENT: no chat_id configured")
            if state_changed and not args.dry_run:
                save_config_atomic(args.config, cfg)
            return 2
        try:
            send_telegram(args.token_env, chat_id, msg,
                          reply_markup=alert_keyboard(alert, coin))
            log_line(args.log_dir,
                     f"FIRE {alert['id']} {alert['type']} "
                     f"{coin.get('symbol')} @ {fmt_price(coin.get('price'))} "
                     f"-> Telegram chat_id={chat_id} OK")
            print(f"sent: {alert['id']} {coin.get('symbol')} "
                  f"@ {fmt_price(coin.get('price'))}")
        except Exception as e:
            print(f"telegram send failed for {alert['id']}: {e}", file=sys.stderr)
            log_line(args.log_dir, f"FIRE {alert['id']} SEND FAILED: {e}")
            if state_changed:
                save_config_atomic(args.config, cfg)
            return 3
        time.sleep(1)  # Telegram flood control: ~1 msg/sec per chat

    for alert, coin in rearmed:
        log_line(args.log_dir,
                 f"RE-ARM {alert['id']} {alert['type']} {coin.get('symbol')} "
                 f"(condition cleared @ {fmt_price(coin.get('price'))})")

    for coin in coins.values():
        log_line(args.log_dir,
                 f"check {coin.get('symbol')} {fmt_price(coin.get('price'))} "
                 f"h1={fmt_pct((coin.get('priceChange') or {}).get('h1'))} "
                 f"fired={summary['fired']} rearmed={summary['rearmed']} "
                 f"errors={len(fetch_errors)}")

    if state_changed and not args.dry_run:
        save_config_atomic(args.config, cfg)
    if not args.dry_run:
        # Answer button taps / messages from this run's window. Skipped
        # entirely in --dry-run: no sends, no update-state writes.
        handle_updates(args.token_env, cfg, args.config, coins, args.log_dir)
    with open(os.path.join(args.log_dir, "last-check.json"), "w",
              encoding="utf-8") as f:
        json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                   "prices": {c.get("symbol"): c.get("price")
                              for c in coins.values()},
                   **summary}, f, indent=2)
    return 1 if fetch_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
