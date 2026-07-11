#!/usr/bin/env python3
"""
idle_jets_bot.py
----------------
Daily Telegram alert of private/business jets sitting IDLE at Malta
International (LMML). A jet that arrived N days ago and hasn't departed is a
charter candidate with no positioning (ferry) cost — the metal is already here.

Drops into the existing `malta-weekend-bot` repo and reuses its Telegram
secrets. Only new secrets required: OpenSky OAuth credentials.

ENV / SECRETS
=============
  TELEGRAM_BOT_TOKEN   (already in the repo)
  TELEGRAM_CHAT_ID     (already in the repo)
  OPENSKY_CLIENT_ID    (new — from opensky-network.org Account page)
  OPENSKY_CLIENT_SECRET(new)
Optional tuning:
  LOOKBACK_DAYS   default 6      (keep <= 7 per OpenSky call)
  MIN_DWELL_DAYS  default 1.0
  JETS_ONLY       default "1"    (set "0" to include everything parked)
  SEND_EMPTY      default "1"    (set "0" to stay silent when nothing found)
"""

import os
import time
import urllib.parse
from datetime import datetime, timezone

import requests

AIRPORT = "LMML"
OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
ADSBDB = "https://api.adsbdb.com/v0/aircraft/"  # free icao24 -> reg/type lookup

# Business-jet ICAO type designators used to keep airliners/GA turboprops out.
BIZJET_TYPES = {
    "C25A","C25B","C25C","C25M","C500","C501","C510","C525","C550","C560",
    "C56X","C650","C680","C68A","C700","C750","CL30","CL35","CL60","GL5T",
    "GL7T","GLEX","GLF4","GLF5","GLF6","GA5C","GA6C","GA7C","G150","G280",
    "LJ35","LJ45","LJ60","LJ70","LJ75","FA10","FA20","FA50","FA7X","FA8X",
    "F900","F2TH","FA5X","E50P","E55P","E545","E550","E135","E35L","H25B",
    "HA4T","HDJT","PC24","PRM1","BE40","CL600","CL604",
}

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "6"))
MIN_DWELL_DAYS = float(os.getenv("MIN_DWELL_DAYS", "1.0"))
JETS_ONLY = os.getenv("JETS_ONLY", "1") == "1"
SEND_EMPTY = os.getenv("SEND_EMPTY", "1") == "1"


def get_token():
    cid = os.environ["OPENSKY_CLIENT_ID"]
    secret = os.environ["OPENSKY_CLIENT_SECRET"]
    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "client_credentials",
        "client_id": cid, "client_secret": secret})
    r.raise_for_status()
    return r.json()["access_token"]


def fetch(endpoint, begin, end, token):
    r = requests.get(
        f"{OPENSKY_BASE}/flights/{endpoint}",
        params={"airport": AIRPORT, "begin": int(begin), "end": int(end)},
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def enrich(icao24):
    """Best-effort icao24 -> (registration, typecode). Degrades gracefully."""
    try:
        r = requests.get(ADSBDB + icao24, timeout=15)
        if r.status_code != 200:
            return "", ""
        ac = r.json().get("response", {}).get("aircraft", {})
        return ac.get("registration", "") or "", ac.get("icao_type", "") or ""
    except Exception:
        return "", ""


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        timeout=30,
        data={"chat_id": chat_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"},
    ).raise_for_status()


def main():
    now = int(time.time())
    begin = now - LOOKBACK_DAYS * 86400
    token = get_token()

    arrivals = fetch("arrival", begin, now, token)
    departures = fetch("departure", begin, now, token)

    last_dep = {}
    for d in departures:
        k = d["icao24"]
        last_dep[k] = max(last_dep.get(k, 0), d.get("firstSeen") or 0)

    last_arr = {}
    for a in arrivals:
        k = a["icao24"]
        if (a.get("lastSeen") or 0) > (last_arr.get(k, {}).get("lastSeen", 0)):
            last_arr[k] = a

    parked = []
    for icao24, a in last_arr.items():
        arr_t = a.get("lastSeen") or 0
        if last_dep.get(icao24, 0) > arr_t:          # departed after arriving
            continue
        dwell = (now - arr_t) / 86400
        if dwell < MIN_DWELL_DAYS:
            continue
        reg, typ = enrich(icao24)
        if JETS_ONLY and typ and typ not in BIZJET_TYPES:
            continue
        # If we couldn't ID the type and JETS_ONLY is on, keep it but flag "?"
        parked.append({
            "reg": reg or icao24.upper(),
            "type": typ or "?",
            "from": a.get("estDepartureAirport") or "?",
            "dwell": dwell,
            "arr": datetime.fromtimestamp(arr_t, tz=timezone.utc),
        })

    parked.sort(key=lambda x: x["dwell"], reverse=True)

    if not parked:
        if SEND_EMPTY:
            send_telegram("🛩️ <b>Malta idle-jet radar</b>\nNothing parked "
                          f"over {MIN_DWELL_DAYS:g}d right now.")
        return

    lines = [f"🛩️ <b>Idle jets at Malta (LMML)</b> — {len(parked)} parked "
             f">{MIN_DWELL_DAYS:g}d\n"]
    for p in parked:
        lines.append(
            f"• <b>{p['reg']}</b>  {p['type']} · {p['dwell']:.1f}d · "
            f"from {p['from']} · in {p['arr']:%b %d}")
    lines.append("\nAlready on-island = zero ferry cost. "
                 "Cross-check the tail's operator before you call.")
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
