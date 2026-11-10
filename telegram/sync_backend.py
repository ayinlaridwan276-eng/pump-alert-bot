#!/usr/bin/env python3
"""Sync Blaze's Pump Alert Bot backend profile into the Telegram checker's alerts.json.

Reads:  hidden_files/backend-state.json  (output of artifact action getstate, code 26MXAAAP)
Writes: alerts.json (merged, atomically)

Rules:
- The backend is the source of truth for alert DEFINITIONS (coin, type, threshold).
  alerts.json keeps the RUNTIME state (active/triggered/muted, triggeredAt, triggerPrice).
- Backend alert id N maps to checker alert id "al_N".
- Backend types map: price_above->above, price_below->below,
  pump_pct_1h->pump, dump_pct_1h->dump. Window is always h1.
- New backend alerts are added as active. Managed alerts (al_<digits>) that no
  longer exist in the backend are removed. Alerts not matching that pattern are
  left untouched.
- Backend coins are ensured in alerts.json with their own pairAddress; coins
  with no alerts left are dropped.
- chat_id comes from the backend profile's telegramChatId.
- Never touches any token. Never executes a trade.
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "hidden_files", "backend-state.json")
ALERTS_PATH = os.path.join(BASE, "alerts.json")

TYPE_MAP = {
    "price_above": "above",
    "price_below": "below",
    "pump_pct_1h": "pump",
    "dump_pct_1h": "dump",
}
MANAGED = re.compile(r"^al_(\d+)$")


def main():
    with open(STATE_PATH) as f:
        state = json.load(f)
    # getstate returns {"data": {...}} when saved via invoke_action, or the bare state.
    if isinstance(state, dict) and "data" in state and isinstance(state["data"], dict):
        state = state["data"]

    profile = state.get("profile", {})
    coins = state.get("coins", [])
    alerts = state.get("alerts", [])

    with open(ALERTS_PATH) as f:
        cfg = json.load(f)

    old_alerts = {a["id"]: a for a in cfg.get("alerts", [])}
    # Match backend alerts to existing checker alerts by definition
    # (coin, type, target) so ids and runtime state survive a re-sync.
    old_by_def = {}
    for a in cfg.get("alerts", []):
        old_by_def[(a.get("coinKey", "").lower(), a.get("type"), a.get("target"))] = a
    coin_by_id = {c["id"]: c for c in coins}

    new_alerts = []
    for ba in alerts:
        coin = coin_by_id.get(ba.get("coinId"))
        if not coin:
            continue
        ctype = TYPE_MAP.get(ba.get("type"))
        if not ctype:
            continue
        coin_key = "%s:%s" % (coin["chainId"], coin["tokenAddress"].lower())
        old = old_by_def.get((coin_key, ctype, ba["threshold"]))
        if old:
            # Keep the checker's id and runtime state; refresh definition fields.
            old["coinKey"] = coin_key
            old["type"] = ctype
            old["target"] = ba["threshold"]
            old["window"] = "h1"
            new_alerts.append(old)
        else:
            new_alerts.append({
                "coinKey": coin_key,
                "createdAt": ba.get("createdAt"),
                "createdPrice": coin.get("priceUsd"),
                "example": False,
                "id": "al_%s" % ba["id"],
                "status": "active",
                "target": ba["threshold"],
                "triggerChange": None,
                "triggerPrice": None,
                "triggeredAt": None,
                "type": ctype,
                "window": "h1",
            })

    # Keep any unmanaged alerts that were in the file before.
    for aid, old in old_alerts.items():
        if not MANAGED.match(aid):
            new_alerts.append(old)

    # Preserve the old file's alert order for existing alerts; new ones go last.
    order = {a["id"]: i for i, a in enumerate(cfg.get("alerts", []))}
    new_alerts.sort(key=lambda a: order.get(a["id"], 10_000))

    # Coins: keep only coins that still have alerts, plus their fresh details.
    # Coin keys are matched case-insensitively (old files used mixed case).
    kept_keys = {a["coinKey"].lower() for a in new_alerts}
    old_coins = {c["key"].lower(): c for c in cfg.get("coins", [])}
    new_coins = []
    for coin in coins:
        key = "%s:%s" % (coin["chainId"], coin["tokenAddress"].lower())
        if key not in kept_keys:
            continue
        old = old_coins.get(key, {})
        merged = dict(old)
        merged.update({
            "address": coin["tokenAddress"],
            "chainId": coin["chainId"],
            "key": key,
            "liquidity": coin.get("liquidityUsd"),
            "name": coin.get("name"),
            "pairAddress": coin.get("pairAddress"),
            "price": coin.get("priceUsd"),
            "symbol": coin.get("symbol"),
            "url": coin.get("dexUrl") or old.get("url"),
        })
        if coin.get("volume24h") is not None:
            merged["volume24h"] = coin["volume24h"]
        new_coins.append(merged)
    # Any unmanaged coin entries stay as they were.
    for key, old in old_coins.items():
        if key not in {c["key"].lower() for c in new_coins} and key not in kept_keys:
            new_coins.append(old)

    cfg["alerts"] = new_alerts
    cfg["coins"] = new_coins
    chat_id = profile.get("telegramChatId")
    if chat_id:
        cfg["chat_id"] = int(chat_id)

    tmp = ALERTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, ALERTS_PATH)
    print("synced: %d alerts, %d coins, chat_id=%s" % (
        len(new_alerts), len(new_coins), cfg.get("chat_id")))


if __name__ == "__main__":
    sys.exit(main())
