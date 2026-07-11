#!/usr/bin/env python3
"""
idle_jets_bot.py
----------------
Daily Telegram alert of private/business jets sitting IDLE at Malta
International (LMML). A jet that arrived N days ago and hasn't departed is a
charter candidate with no positioning (ferry) cost -- the metal is already here.

Reuses the malta-weekend-bot's existing Telegram secrets. Only new secrets:
OpenSky OAuth credentials.

NOTE ON DATA FRESHNESS
======================
OpenSky's arrival/departure tables are built by a nightly batch, so they run
roughly a day behind and each query is capped at a ~2-day window (we slice the
lookback into daily chunks below to respect that). For spotting jets parked
more than a day this lag is fine; the one edge effect is that a jet which left
*this morning* may still show as parked until the batch catches up.

ENV / SECRETS
=============
  TELEGRAM_BOT_TOKEN    (already in the repo)
  TELEGRAM_CHAT_ID      (already in the repo)
  OPENSKY_CLIENT_ID     (new -- opensky-network.org Account page)
  OPENSKY_CLIENT_SECRET (new)
Optional tuning:
  LOOKBACK_DAYS   default 6
  MIN_DWELL_DAYS  default 1.0
  JETS_ONLY       default "1"    (set "0" to include everything parked)
  SEND_EMPTY      default "1"    (set "0" to stay silent when nothing found)
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests

AIRPORT = "LMML"
OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
ADSBDB = "https://api.adsbdb.com/v0/aircraft/"  # free icao24 -> reg/type lookup
DAY = 86400

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
    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["OPENSKY_CLIENT_ID"],
        "client_secret": os.environ["OPENSKY_CLIENT_SECRET"]})
    r.raise_for_status()
    return r.json()["access_token"]


def day_chunks(begin, end):
    """Yield [lo, hi] slices <= 1 day, aligned to UTC midnight, so each call
    stays within OpenSky's 2-day / single-day-boundary interval limit."""
    t = begin - (begin % DAY)  # floor to UTC midnight
    while t < end:
        yield max(t, begin), min(t + DAY, end)
        t += DAY


def fetch(endpoint, begin, end, token):
    """endpoint in {'arrival','departure'}; queries in daily slices."""
    headers = {"Authorization": f"Bearer {token}"}
    out = []
    for lo, hi in day_chunks(begin, end):
        try:
            r = requests.get(
                f"{OPENSKY_BASE}/flights/{endpoint}",
                params={"airport": AIRPORT, "begin": int(lo), "end": int(hi)},
                headers=headers, timeout=60)
            if r.status_code == 404:      # no flights in this slice
                continue
            r.raise_for_status()
            out.extend(r.json())
        except Exception as e:            # one bad slice shouldn't kill the run
            print(f"[warn] {endpoint} {int(lo)}-{int(hi)}: {e}", file=sys.stderr)
    return out


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
    begin = now - LOOKBACK_DAYS * DAY
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
        dwell = (now - arr_t) / DAY
        if dwell < MIN_DWELL_DAYS:
            continue
        reg, typ = enrich(icao24)
        if JETS_ONLY and typ and typ not in BIZJET_TYPES:
            continue
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
            send_telegram("\U0001F6E9\uFE0F <b>Malta idle-jet radar</b>\nNothing parked "
                          f"over {MIN_DWELL_DAYS:g}d right now.")
        return

    lines = [f"\U0001F6E9\uFE0F <b>Idle jets at Malta (LMML)</b> \u2014 {len(parked)} parked "
             f">{MIN_DWELL_DAYS:g}d\n"]
    for p in parked:
        lines.append(
            f"\u2022 <b>{p['reg']}</b>  {p['type']} \u00B7 {p['dwell']:.1f}d \u00B7 "
            f"from {p['from']} \u00B7 in {p['arr']:%b %d}")
    lines.append("\nAlready on-island = zero ferry cost. "
                 "Cross-check the tail's operator before you call.")
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
