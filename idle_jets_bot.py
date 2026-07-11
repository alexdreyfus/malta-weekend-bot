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
  AEROAPI_KEY           (OPTIONAL -- flightaware.com/aeroapi; plans + inbound)
  FR24_TOKEN            (OPTIONAL -- fr24api.flightradar24.com; watchlist location)
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
from datetime import datetime, timezone, timedelta

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
_BUDGET = 300  # seconds: hard ceiling for the whole run's network work


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

# Offline ICAO -> airport name (no API calls). apname keeps the code and
# appends the name: "LFPB (Paris Le Bourget)".
try:
    import airportsdata
    _AP_DB = airportsdata.load("ICAO")
except Exception:
    _AP_DB = {}


def apname(code):
    if not code or code == "?":
        return code or "?"
    rec = _AP_DB.get(str(code).upper())
    if not rec:
        return code
    nm = (rec.get("name") or "").replace(" Airport", "").strip()
    if not nm or len(nm) > 26:
        nm = rec.get("city") or nm[:26]
    return f"{code} ({nm})" if nm else code


AIRPORTS = [a.strip().upper() for a in
            os.getenv("AIRPORTS", "LMML,GMMX").split(",") if a.strip()]
AIRPORT_NAMES = {"LMML": "Malta (LMML)", "GMMX": "Marrakech (GMMX)"}
# FR24 airport filter uses IATA; map our ICAO codes across.
AIRPORT_IATA = {"LMML": "MLA", "GMMX": "RAK"}
# Tails to keep an eye on (override with the WATCH env var).
WATCH = [w.strip().upper() for w in os.getenv("WATCH",
         "9H-CITY,T7-BSIC,9H-GOAT,9H-EHC,9H-EHB,9H-OTI,9H-EHA"
         ).split(",") if w.strip()]
OPENSKY_BASE = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
             "opensky-network/protocol/openid-connect/token")
ADSBDB = "https://api.adsbdb.com/v0/aircraft/"
AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
FR24_BASE = "https://fr24api.flightradar24.com/api"
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
USE_OPENSKY = os.getenv("USE_OPENSKY", "1") == "1"  # free secondary source
AEROAPI_KEY = os.getenv("AEROAPI_KEY", "").strip()
FR24_TOKEN = os.getenv("FR24_TOKEN", "").strip()


def get_token():
    # Auth host is flaky from cloud IPs, so be patient: many tries, long connect.
    r = _req("POST", TOKEN_URL, tries=6, timeout=(25, 30), data={
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


def scheduled_arrivals(airport, hours=48, max_pages=2):
    """FlightAware: charter aircraft expected to LAND at `airport` within
    `hours`. Requires AEROAPI_KEY. FlightAware holds filed plans ~2 days out,
    so this mostly surfaces flights filed within a day or so."""
    if not AEROAPI_KEY or _time_left() <= 0:
        return []
    try:
        r = _req("GET",
            f"{AEROAPI_BASE}/airports/{airport}/flights/scheduled_arrivals",
            params={"max_pages": max_pages},
            headers={"x-apikey": AEROAPI_KEY}, tries=2, timeout=(10, 20))
        if r.status_code != 200:
            return []
        data = r.json()
        flights = (data.get("scheduled_arrivals") or data.get("arrivals")
                   or data.get("flights") or [])
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) + timedelta(hours=hours)
    out = []
    for f in flights:
        reg = f.get("registration") or ""
        typ = f.get("aircraft_type") or ""
        if REQUIRE_TAIL and not reg:
            continue
        if JETS_ONLY and typ and typ not in CHARTER_TYPES:
            continue
        eta_iso = f.get("estimated_on") or f.get("scheduled_on")
        if not eta_iso:
            continue
        try:
            eta = datetime.fromisoformat(eta_iso.replace("Z", "+00:00"))
        except Exception:
            continue
        if eta > cutoff:
            continue
        origin = f.get("origin") or {}
        out.append({
            "reg": reg,
            "type": typ or "?",
            "from": origin.get("code_icao") or origin.get("code") or "?",
            "eta": eta_iso,
        })
    out.sort(key=lambda x: x["eta"])
    return out


def _fa_watch(reg):
    """FlightAware -> (location|None, plan|None). location None if FA has no data
    (common for blocked 9H tails); plan is the next filed leg if any."""
    if not AEROAPI_KEY or _time_left() <= 0:
        return (None, None)
    ident = re.sub(r"[^A-Za-z0-9]", "", reg)
    try:
        r = _req("GET", f"{AEROAPI_BASE}/flights/{ident}",
                 params={"ident_type": "registration", "max_pages": 1},
                 headers={"x-apikey": AEROAPI_KEY}, tries=1, timeout=(6, 12))
        if r.status_code != 200:
            return (None, None)
        flights = r.json().get("flights", [])
    except Exception:
        return (None, None)
    if not flights:
        return (None, None)

    def code(d):
        d = d or {}
        return d.get("code_icao") or d.get("code") or "?"

    now = datetime.now(timezone.utc)
    airborne = last_arr = next_plan = None
    for f in flights:
        dep = f.get("actual_off") or f.get("actual_out")
        arr = f.get("actual_on") or f.get("actual_in")
        if dep and not arr:
            airborne = f
        if arr and (last_arr is None or arr > last_arr[0]):
            last_arr = (arr, f)
        if not (f.get("actual_out") or f.get("actual_off")):
            d = f.get("estimated_out") or f.get("scheduled_out")
            if d:
                try:
                    when = datetime.fromisoformat(d.replace("Z", "+00:00"))
                except Exception:
                    when = None
                if when and when >= now - timedelta(hours=1):
                    if next_plan is None or d < next_plan[0]:
                        next_plan = (d, f)

    loc = None
    if airborne is not None:
        eta = (airborne.get("estimated_in") or airborne.get("estimated_on")
               or airborne.get("scheduled_on"))
        loc = (f"airborne {apname(code(airborne.get('origin')))}"
               f"\u2192{apname(code(airborne.get('destination')))}")
        if eta:
            loc += f", ETA {fmt_dep(eta)}"
    elif last_arr is not None:
        loc = (f"on ground {apname(code(last_arr[1].get('destination')))} "
               f"since {fmt_dep(last_arr[0])}")

    plan = None
    if next_plan is not None:
        plan = f"{code(next_plan[1].get('destination'))} {fmt_dep(next_plan[0])}"
    return (loc, plan)


def _fr24_get(path, params):
    return _req("GET", FR24_BASE + path, params=params,
                headers={"Accept": "application/json",
                         "Authorization": f"Bearer {FR24_TOKEN}",
                         "Accept-Version": "v1"},
                tries=2, timeout=(8, 15))


def _fr24_location(reg):
    """Flightradar24 -> location string or None. Live feed for airborne tails,
    else most recent completed flight for parked ones."""
    if not FR24_TOKEN or _time_left() <= 0:
        return None
    # 1) Airborne right now?
    try:
        r = _fr24_get("/live/flight-positions/full", {"registrations": reg})
        if r.status_code == 200:
            data = r.json().get("data", []) or []
            if data:
                f = data[0]
                orig = f.get("orig_icao") or f.get("orig_iata") or "?"
                dest = f.get("dest_icao") or f.get("dest_iata") or "?"
                loc = f"airborne {apname(orig)}\u2192{apname(dest)}"
                if f.get("eta"):
                    loc += f", ETA {fmt_dep(f['eta'])}"
                return loc
    except Exception:
        pass
    # 2) Parked -> most recent completed flight -> on ground at its destination.
    try:
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        r = _fr24_get("/flight-summary/light",
                      {"registrations": reg,
                       "flight_datetime_from": frm,
                       "flight_datetime_to": to})
        if r.status_code == 200:
            data = r.json().get("data", []) or []

            def landed(f):
                return f.get("datetime_landed") or f.get("last_seen") or ""

            data = [f for f in data if landed(f)]
            if data:
                f = max(data, key=landed)
                dest = f.get("dest_icao") or f.get("dest_iata") or "?"
                return f"on ground {apname(dest)} since {fmt_dep(landed(f))}"
    except Exception:
        pass
    return None


def watch_status(reg):
    """Combine sources: FR24 for location (better coverage of blocked 9H
    tails), FlightAware for the next filed plan. Returns (loc, plan)."""
    if not (FR24_TOKEN or AEROAPI_KEY):
        return None
    fr_loc = _fr24_location(reg) if FR24_TOKEN else None
    fa_loc, fa_plan = _fa_watch(reg)
    loc = fr_loc or fa_loc or "no recent activity"
    return (loc, fa_plan)


def render_watch(statuses):
    lines = ["\U0001F465 <b>Friends fleet</b>"]
    for reg, st in statuses:
        if st is None:
            lines.append(f"\u2022 <b>{reg}</b>  \u2014 lookup unavailable")
            continue
        loc, plan = st
        lines.append(f"\u2022 <b>{reg}</b>  {loc}")
        lines.append(f"   \u21B3 next: {plan}" if plan
                     else "   \u21B3 nothing planned")
    return lines


def send_telegram(text):
    _req("POST",
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"},
    ).raise_for_status()


def scan_airport_opensky(airport, begin, now, token):
    """OpenSky fallback: idle charter aircraft parked at `airport`."""
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


def _fr24_summary(airport, direction, frm, to, limit=100):
    """FR24 flight-summary/light for one direction. Returns list or None."""
    if _time_left() <= 0:
        return None
    code = AIRPORT_IATA.get(airport, airport)
    try:
        r = _fr24_get("/flight-summary/light", {
            "airports": f"{direction}:{code}",
            "flight_datetime_from": frm,
            "flight_datetime_to": to,
            "limit": limit,
        })
        if r.status_code != 200:
            print(f"[warn] FR24 {direction} {airport}: {r.status_code} "
                  f"{r.text[:180]}", file=sys.stderr)
            return None
        return r.json().get("data", []) or []
    except Exception as e:
        print(f"[warn] FR24 {direction} {airport}: {e}", file=sys.stderr)
        return None


def _fr24_candidates(airport, begin_dt, now_dt):
    """FR24 arrivals-minus-departures -> dict reg -> {type, from, arr_dt}.
    Cheap first pass; None if FR24 is unavailable this run."""
    if not FR24_TOKEN or _time_left() <= 0:
        return None
    frm = begin_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    arrivals = _fr24_summary(airport, "inbound", frm, to)
    if arrivals is None:
        return None
    departures = _fr24_summary(airport, "outbound", frm, to) or []
    last_dep = {}
    for f in departures:
        reg = (f.get("reg") or "").upper()
        t = f.get("datetime_takeoff") or f.get("first_seen")
        if reg and t and (reg not in last_dep or t > last_dep[reg]):
            last_dep[reg] = t
    cand = {}
    for f in arrivals:
        reg = (f.get("reg") or "").upper()
        t = f.get("datetime_landed") or f.get("last_seen")
        if not reg or not t:
            continue
        if reg in cand and t <= cand[reg]["_t"]:
            continue
        if reg in last_dep and last_dep[reg] > t:      # departed after arriving
            cand.pop(reg, None)
            continue
        try:
            arr_dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:
            continue
        cand[reg] = {"type": (f.get("type") or "").upper(),
                     "from": f.get("orig_icao") or f.get("orig_iata") or "?",
                     "arr_dt": arr_dt, "_t": t}
    return cand


def _opensky_candidates(airport, begin, now):
    """Free secondary source -> dict reg -> {...}. None on failure (fast-fail
    token so a flaky OpenSky never stalls the run)."""
    try:
        r = _req("POST", TOKEN_URL, tries=1, timeout=(8, 12), data={
            "grant_type": "client_credentials",
            "client_id": os.environ["OPENSKY_CLIENT_ID"],
            "client_secret": os.environ["OPENSKY_CLIENT_SECRET"]})
        r.raise_for_status()
        token = r.json()["access_token"]
    except Exception:
        return None
    try:
        parked = scan_airport_opensky(airport, begin, now, token)
    except Exception:
        return None
    out = {}
    for p in parked:
        out[p["reg"].upper()] = {"type": p["type"], "from": p["from"],
                                 "arr_dt": p["arr"]}
    return out


def _fr24_airborne(regs):
    """Subset of regs currently airborne (FR24 live feed). Bulk, chunked."""
    regs = [r for r in regs if r]
    if not regs or not FR24_TOKEN or _time_left() <= 0:
        return set()
    out = set()
    for i in range(0, len(regs), 15):
        try:
            r = _fr24_get("/live/flight-positions/light",
                          {"registrations": ",".join(regs[i:i + 15])})
            if r.status_code == 200:
                for f in (r.json().get("data", []) or []):
                    if f.get("reg"):
                        out.add(f["reg"].upper())
        except Exception:
            pass
    return out


def _fr24_last_landing(reg):
    """Most recent completed flight -> (dest_icao, landed_iso) or None."""
    if not FR24_TOKEN or _time_left() <= 0:
        return None
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = _fr24_get("/flight-summary/light",
                      {"registrations": reg,
                       "flight_datetime_from": frm, "flight_datetime_to": to})
        if r.status_code != 200:
            return None
        data = r.json().get("data", []) or []
    except Exception:
        return None
    best = None
    for f in data:
        t = f.get("datetime_landed") or f.get("last_seen")
        if not t:
            continue
        if best is None or t > best[1]:
            best = (f.get("dest_icao") or f.get("dest_iata") or "?", t)
    return best


def scan_parked(airport, begin, now, begin_dt, now_dt):
    """Merged, accuracy-verified parked list for `airport`. FR24 primary +
    OpenSky secondary for discovery; live + last-landing checks for truth.
    Returns list, or None if every source was unavailable this run."""
    fr = _fr24_candidates(airport, begin_dt, now_dt)
    osc = _opensky_candidates(airport, begin, now) if USE_OPENSKY else {}
    fr_ok, os_ok = fr is not None, osc is not None
    fr, osc = fr or {}, osc or {}
    if not fr_ok and not os_ok:
        return None

    cand = {}
    for reg in set(fr) | set(osc):
        base = dict(fr.get(reg) or osc.get(reg) or {})
        srcs = ([s for s, d in (("FR24", fr), ("OS", osc)) if reg in d])
        base["src"] = "+".join(srcs)
        cand[reg] = base

    airborne = _fr24_airborne(list(cand)) if FR24_TOKEN else set()
    icao, iata = airport, AIRPORT_IATA.get(airport, "")
    result = []
    for reg, m in cand.items():
        if reg in airborne:                       # flying now -> it left
            continue
        arr_dt = m.get("arr_dt")
        if FR24_TOKEN:                            # confirm it is still here
            last = _fr24_last_landing(reg)
            if last is not None:
                dest, landed_iso = last
                if dest not in (icao, iata):     # last landed elsewhere -> left
                    continue
                try:
                    arr_dt = datetime.fromisoformat(
                        landed_iso.replace("Z", "+00:00"))
                except Exception:
                    pass
        if arr_dt is None:
            continue
        dwell = (now_dt - arr_dt).total_seconds() / DAY
        if dwell < MIN_DWELL_DAYS:
            continue
        typ = m.get("type") or "?"
        if JETS_ONLY and typ != "?" and typ not in CHARTER_TYPES:
            continue
        plan = next_departure(reg) if AEROAPI_KEY else None
        result.append({"reg": reg, "type": typ, "from": m.get("from", "?"),
                       "dwell": dwell, "arr": arr_dt, "plan": plan,
                       "src": m.get("src", "")})
    result.sort(key=lambda x: x["dwell"], reverse=True)
    return result


def render_section(airport, parked, inbound):
    name = AIRPORT_NAMES.get(airport, airport)
    lines = [f"\U0001F6E9\uFE0F <b>{name}</b>"]

    # --- Parked / idle now (parked is None => scan failed this run) ---
    if parked is None:
        lines.append(f"<i>Idle &gt;{MIN_DWELL_DAYS:g}d:</i> scan unavailable "
                     "this run")
    elif not parked:
        lines.append(f"<i>Idle &gt;{MIN_DWELL_DAYS:g}d:</i> none")
    else:
        lines.append(f"<i>Idle &gt;{MIN_DWELL_DAYS:g}d:</i> {len(parked)}")
        for p in parked:
            base = (f"\u2022 <b>{p['reg']}</b>  {p['type']} \u00B7 "
                    f"{p['dwell']:.1f}d \u00B7 from {apname(p['from'])} \u00B7 "
                    f"in {p['arr']:%b %d}")
            if p.get("src"):
                base += f" \u00B7 <i>{p['src']}</i>"
            lines.append(base)
            if AEROAPI_KEY:
                if p["plan"]:
                    dest, dep_iso = p["plan"]
                    lines.append(f"   \u21B3 next: {dest} \u00B7 {fmt_dep(dep_iso)}")
                else:
                    lines.append("   \u21B3 no plan filed \u2014 open target")

    # --- Inbound in next 48h (FlightAware) ---
    if AEROAPI_KEY:
        if inbound:
            lines.append(f"<i>Landing &lt;48h:</i> {len(inbound)}")
            for q in inbound:
                lines.append(
                    f"\u2b07\uFE0F <b>{q['reg']}</b>  {q['type']} \u00B7 "
                    f"from {apname(q['from'])} \u00B7 ETA {fmt_dep(q['eta'])}")
        else:
            lines.append("<i>Landing &lt;48h:</i> none filed")
    return lines


def main():
    now = int(time.time())
    begin = now - LOOKBACK_DAYS * DAY
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    begin_dt = datetime.fromtimestamp(begin, tz=timezone.utc)

    # Forward arrivals forecast (FlightAware).
    inbound = {ap: scheduled_arrivals(ap) for ap in AIRPORTS}

    # Idle / presence scan: FR24 primary + OpenSky secondary, each candidate
    # verified on the ground here now. idle[ap] == None -> scan unavailable.
    idle = {ap: scan_parked(ap, begin, now, begin_dt, now_dt)
            for ap in AIRPORTS}

    watch = [(reg, watch_status(reg)) for reg in WATCH]

    have = (any(idle[ap] for ap in AIRPORTS)
            or any(inbound.values()) or bool(WATCH))
    if not have and not SEND_EMPTY:
        return

    blocks = ["\n".join(render_section(ap, idle[ap], inbound[ap]))
              for ap in AIRPORTS]
    if WATCH:
        blocks.append("\n".join(render_watch(watch)))
    msg = "\n\n".join(blocks)
    msg += ("\n\nIdle = zero ferry cost. Inbound = about to be on-site. "
            "Check the operator before you call.")
    send_telegram(msg)


if __name__ == "__main__":
    main()
