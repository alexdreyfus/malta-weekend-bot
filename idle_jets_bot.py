#!/usr/bin/env python3
"""
idle_jets_bot.py
----------------
Telegram alert of private/business jets sitting IDLE at Malta International
(LMML) -- arrived N days ago, not departed since. Charter candidates with no
positioning (ferry) cost because the metal is already on-island.

Runs 4x/day. Reuses the malta-weekend-bot Telegram secrets.

WHAT EACH RUN DOES
==================
1. OpenSky (free): arrivals minus departures over the lookback -> still parked,
   with dwell time. OpenSky's flight tables are batched nightly, so this list
   only really changes once a day -- that's a data limit, not a bug.
2. adsbdb (free): icao24 -> registration + type. Rows with no resolvable tail
   number are dropped (REQUIRE_TAIL), since there's nothing actionable there.
3. FlightAware AeroAPI (paid, OPTIONAL): if AEROAPI_KEY is set, each parked
   tail is checked for an upcoming filed departure (destination + time). This
   is the layer that updates through the day, which is why we run 4x. With no
   key set, this step is skipped entirely and costs nothing.

ENV / SECRETS
=============
  TELEGRAM_BOT_TOKEN    (already in the repo)
  TELEGRAM_CHAT_ID      (already in the repo)
  OPENSKY_CLIENT_ID     (opensky-network.org Account page)
  OPENSKY_CLIENT_SECRET
  AEROAPI_KEY           (OPTIONAL -- flightaware.com/aeroapi; enables flight plans)
Optional tuning:
  LOOKBACK_DAYS   default 6
  MIN_DWELL_DAYS  default 1.0
  JETS_ONLY       default "1"   (set "0" to include non-jets)
  REQUIRE_TAIL    default "1"   (set "0" to keep tail-less rows)
  SEND_EMPTY      default "1"   (set "0" to stay silent when nothing found)
"""

import os
import re
import sys
import time
from datetime import datetime, timezone

import socket
import time as _t

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

# --- Force IPv4 -----------------------------------------------------------
# GitHub runners intermittently hang on IPv6 connects to some hosts (notably
# auth.opensky-network.org), surfacing as a connect timeout. Filter DNS to A
# records so every connection goes over IPv4.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    v4 = [ai for ai in res if ai[0] == socket.AF_INET]
    return v4 or res  # fall back to original if no A record


socket.getaddrinfo = _ipv4_only

# --- Session (no adapter-level retries; we retry explicitly below) ---------
_SESSION = requests.Session()

_START = _t.time()
_BUDGET = 240  # seconds: hard ceiling for the whole run's network work


def _time_left():
    return _BUDGET - (_t.time() - _START)


def _req(method, url, tries=3, timeout=(10, 20), **kw):
    """Bounded retry: `tries` attempts, short timeouts, and never retry past
    the global time budget so one dead host can't hang the run."""
    kw["timeout"] = timeout
    last = None
    for attempt in range(tries):
        if _time_left() <= 0:
            raise TimeoutError("run time budget exhausted")
        try:
            return _SESSION.request(method, url, **kw)
        except requests.exceptions.RequestException as e:
            last = e
            _t.sleep(min(2 * (attempt + 1), max(0, _time_left())))
    raise last

AIRPORTS = [a.strip().upper() for a in
            os.getenv("AIRPORTS", "LMML,GMMX").split(",") if a.strip()]
AIRPORT_NAMES = {"LMML": "Malta (LMML)", "GMMX": "Marrakech (GMMX)"}
OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
ADSBDB = "https://api.adsbdb.com/v0/aircraft/"
AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
DAY = 86400

CHARTER_TYPES = {
    "C25A","C25B","C25C","C25M","C500","C501","C510","C525","C550","C560",
    "C56X","C650","C680","C68A","C700","C750","CL30","CL35","CL60","GL5T",
    "GL7T","GLEX","GLF4","GLF5","GLF6","GA5C","GA6C","GA7C","G150","G280",
    "LJ35","LJ45","LJ60","LJ70","LJ75","FA10","FA20","FA50","FA7X","FA8X",
    "F900","F2TH","FA5X","E50P","E55P","E545","E550","E135","E35L","H25B",
    "HA4T","HDJT","PC24","PRM1","BE40","CL600","CL604",
    # Turboprops commonly chartered:
    "PC12","TBM7","TBM8","TBM9","TBM10","PC6T","P180","B350","BE20",
    "BE9L","BE9T","C208","C208B","C425","C441","DHC6","AC90","AC95",
    "SW4","E110","PAY1","PAY2","PAY3","PAY4","MU2",
}

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "6"))
MIN_DWELL_DAYS = float(os.getenv("MIN_DWELL_DAYS", "1.0"))
JETS_ONLY = os.getenv("JETS_ONLY", "1") == "1"
REQUIRE_TAIL = os.getenv("REQUIRE_TAIL", "1") == "1"
SEND_EMPTY = os.getenv("SEND_EMPTY", "1") == "1"
AEROAPI_KEY = os.getenv("AEROAPI_KEY", "").strip()


def get_token():
    r = _req("POST", TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["OPENSKY_CLIENT_ID"],
        "client_secret": os.environ["OPENSKY_CLIENT_SECRET"]})
    r.raise_for_status()
    return r.json()["access_token"]


def day_chunks(begin, end):
    """<=1-day UTC-midnight-aligned slices (OpenSky flight interval cap)."""
    t = begin - (begin % DAY)
    while t < end:
        yield max(t, begin), min(t + DAY, end)
        t += DAY


def fetch(airport, endpoint, begin, end, token):
    headers = {"Authorization": f"Bearer {token}"}
    out = []
    for lo, hi in day_chunks(begin, end):
        try:
            r = _req("GET",
                f"{OPENSKY_BASE}/flights/{endpoint}",
                params={"airport": airport, "begin": int(lo), "end": int(hi)},
                headers=headers)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            out.extend(r.json())
        except Exception as e:
            print(f"[warn] {airport} {endpoint} {int(lo)}-{int(hi)}: {e}",
                  file=sys.stderr)
    return out


def enrich(icao24):
    """icao24 -> (registration, typecode). Empty strings if unresolved."""
    if _time_left() <= 0:
        return "", ""
    try:
        r = _req("GET", ADSBDB + icao24, tries=1, timeout=(5, 8))
        if r.status_code != 200:
            return "", ""
        ac = r.json().get("response", {}).get("aircraft", {})
        return ac.get("registration", "") or "", ac.get("icao_type", "") or ""
    except Exception:
        return "", ""


def fmt_dep(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%b %d %H:%MZ")
    except Exception:
        return iso


def next_departure(reg):
    """FlightAware: next filed/scheduled departure for a tail, or None.
    Returns (destination_icao, iso_time). Requires AEROAPI_KEY."""
    if _time_left() <= 0:
        return None
    ident = re.sub(r"[^A-Za-z0-9]", "", reg)  # FlightAware idents drop hyphens
    try:
        r = _req("GET",
            f"{AEROAPI_BASE}/flights/{ident}",
            params={"ident_type": "registration", "max_pages": 1},
            headers={"x-apikey": AEROAPI_KEY}, tries=1, timeout=(5, 10))
        if r.status_code != 200:
            return None
        upcoming = []
        for f in r.json().get("flights", []):
            if f.get("actual_out"):                    # already left the gate
                continue
            dep = f.get("estimated_out") or f.get("scheduled_out")
            if dep:
                upcoming.append((dep, f))
        if not upcoming:
            return None
        upcoming.sort(key=lambda x: x[0])
        dep_iso, f = upcoming[0]
        dest = (f.get("destination") or {})
        return (dest.get("code_icao") or dest.get("code") or "?", dep_iso)
    except Exception:
        return None


def send_telegram(text):
    _req("POST",
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"},
    ).raise_for_status()


def scan_airport(airport, begin, now, token):
    """Return the sorted list of idle charter aircraft parked at `airport`."""
    arrivals = fetch(airport, "arrival", begin, now, token)
    departures = fetch(airport, "departure", begin, now, token)

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
        if last_dep.get(icao24, 0) > arr_t:
            continue
        dwell = (now - arr_t) / DAY
        if dwell < MIN_DWELL_DAYS:
            continue
        reg, typ = enrich(icao24)
        if REQUIRE_TAIL and not reg:                  # no tail -> not actionable
            continue
        if JETS_ONLY and typ and typ not in CHARTER_TYPES:
            continue
        plan = next_departure(reg) if (AEROAPI_KEY and reg) else None
        parked.append({
            "reg": reg or icao24.upper(),
            "type": typ or "?",
            "from": a.get("estDepartureAirport") or "?",
            "dwell": dwell,
            "arr": datetime.fromtimestamp(arr_t, tz=timezone.utc),
            "plan": plan,
        })

    parked.sort(key=lambda x: x["dwell"], reverse=True)
    return parked


def render_section(airport, parked):
    name = AIRPORT_NAMES.get(airport, airport)
    lines = [f"\U0001F6E9\uFE0F <b>{name}</b> \u2014 "
             f"{len(parked)} parked >{MIN_DWELL_DAYS:g}d"]
    if not parked:
        lines.append("   nothing idle right now")
        return lines
    for p in parked:
        lines.append(
            f"\u2022 <b>{p['reg']}</b>  {p['type']} \u00B7 {p['dwell']:.1f}d "
            f"\u00B7 from {p['from']} \u00B7 in {p['arr']:%b %d}")
        if AEROAPI_KEY:
            if p["plan"]:
                dest, dep_iso = p["plan"]
                lines.append(f"   \u21B3 next: {dest} \u00B7 {fmt_dep(dep_iso)}")
            else:
                lines.append("   \u21B3 no plan filed \u2014 open target")
    return lines


def main():
    now = int(time.time())
    begin = now - LOOKBACK_DAYS * DAY
    token = get_token()

    results = {ap: scan_airport(ap, begin, now, token) for ap in AIRPORTS}
    total = sum(len(v) for v in results.values())

    if total == 0 and not SEND_EMPTY:
        return

    blocks = []
    for ap in AIRPORTS:
        blocks.append("\n".join(render_section(ap, results[ap])))
    msg = "\n\n".join(blocks)
    msg += ("\n\nAlready on-site = zero ferry cost. "
            "Check the operator before you call.")
    send_telegram(msg)


if __name__ == "__main__":
    main()
