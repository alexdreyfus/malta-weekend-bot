#!/usr/bin/env python3
"""Find weekend getaway destinations from Malta with good weather."""

import os
import sys
import time
from datetime import date, timedelta
from urllib.parse import quote

import requests

DESTINATIONS = [
    ("CDG", "Paris", 49.0097, 2.5479),
    ("LHR", "London", 51.4700, -0.4543),
    ("AMS", "Amsterdam", 52.3105, 4.7683),
    ("BRU", "Brussels", 50.9014, 4.4844),
    ("FRA", "Frankfurt", 50.0379, 8.5622),
    ("MUC", "Munich", 48.3538, 11.7861),
    ("BER", "Berlin", 52.3667, 13.5033),
    ("HAM", "Hamburg", 53.6304, 9.9882),
    ("DUS", "Dusseldorf", 51.2895, 6.7668),
    ("VIE", "Vienna", 48.1103, 16.5697),
    ("ZRH", "Zurich", 47.4647, 8.5492),
    ("GVA", "Geneva", 46.2381, 6.1090),
    ("MAD", "Madrid", 40.4983, -3.5676),
    ("BCN", "Barcelona", 41.2974, 2.0833),
    ("VLC", "Valencia", 39.4893, -0.4816),
    ("AGP", "Malaga", 36.6749, -4.4991),
    ("LIS", "Lisbon", 38.7813, -9.1359),
    ("OPO", "Porto", 41.2481, -8.6814),
    ("FCO", "Rome", 41.8003, 12.2389),
    ("MXP", "Milan", 45.6306, 8.7281),
    ("VCE", "Venice", 45.5053, 12.3519),
    ("BLQ", "Bologna", 44.5354, 11.2887),
    ("PSA", "Pisa", 43.6839, 10.3927),
    ("NAP", "Naples", 40.8860, 14.2908),
    ("CTA", "Catania", 37.4668, 15.0664),
    ("PMO", "Palermo", 38.1759, 13.0910),
    ("BRI", "Bari", 41.1389, 16.7606),
    ("TRN", "Turin", 45.2008, 7.6496),
    ("ATH", "Athens", 37.9364, 23.9445),
    ("IST", "Istanbul", 41.2753, 28.7519),
    ("CPH", "Copenhagen", 55.6180, 12.6508),
    ("DUB", "Dublin", 53.4264, -6.2499),
    ("PRG", "Prague", 50.1008, 14.2600),
    ("BUD", "Budapest", 47.4369, 19.2611),
    ("WAW", "Warsaw", 52.1657, 20.9671),
    ("KRK", "Krakow", 50.0777, 19.7848),
    ("TLV", "Tel Aviv", 32.0117, 34.8867),
    ("DXB", "Dubai", 25.2532, 55.3657),
    ("TUN", "Tunis", 36.8510, 10.2272),
    ("CMN", "Casablanca", 33.3675, -7.5898),
    ("RAK", "Marrakech", 31.6069, -8.0363),
    ("LCA", "Larnaca", 34.8751, 33.6249),
]

MIN_DAY_HIGH = 15.0
MAX_DAY_HIGH = 28.0
MIN_NIGHT_LOW = 8.0
MAX_RAIN_PROB = 40.0
IDEAL_TEMP = 22.0


def upcoming_weekend(today: date) -> tuple[date, date]:
    """Return (Friday, Sunday) of the upcoming weekend. Sat/Sun jumps to next Friday."""
    weekday = today.weekday()  # Mon=0 ... Sun=6
    if weekday <= 4:  # Mon-Fri
        days_to_friday = (4 - weekday) % 7
    else:  # Sat or Sun -> next Friday
        days_to_friday = (4 - weekday) % 7 + 7
    friday = today + timedelta(days=days_to_friday)
    sunday = friday + timedelta(days=2)
    return friday, sunday


def fetch_weather(lat: float, lon: float, start: date, end: date) -> dict | None:
    """Fetch forecast for date range. Retries once on failure. Returns None if both attempts fail."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "auto",
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 1:
                time.sleep(1.5)
    print(f"weather fetch failed for {lat},{lon} after 2 attempts: {last_exc}", file=sys.stderr)
    return None


def evaluate(data: dict) -> dict | None:
    """Compute aggregated metrics and check thresholds."""
    daily = data.get("daily") or {}
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []
    if not highs or not lows or not rains:
        return None
    avg_high = sum(highs) / len(highs)
    avg_low = sum(lows) / len(lows)
    max_rain = max(r for r in rains if r is not None) if any(r is not None for r in rains) else 0
    if not (MIN_DAY_HIGH <= avg_high <= MAX_DAY_HIGH):
        return None
    if avg_low < MIN_NIGHT_LOW:
        return None
    if max_rain > MAX_RAIN_PROB:
        return None
    score = abs(avg_high - IDEAL_TEMP) + max_rain / 20.0
    return {
        "avg_high": avg_high,
        "avg_low": avg_low,
        "max_rain": max_rain,
        "score": score,
    }


def skyscanner_link(dest_iata: str, friday: date, sunday: date) -> str:
    out = friday.strftime("%y%m%d")
    ret = sunday.strftime("%y%m%d")
    return (
        f"https://www.skyscanner.net/transport/flights/mla/{dest_iata.lower()}/"
        f"{out}/{ret}/?adults=1&directflight=true"
    )


def build_message(matches: list[dict], friday: date, sunday: date) -> str:
    header = f"*Malta weekend getaways* — {friday.strftime('%a %d %b')} → {sunday.strftime('%a %d %b')}\n\n"
    if not matches:
        return header + "_No destinations matched the weather criteria this weekend._"
    lines = [header]
    for i, m in enumerate(matches[:10], start=1):
        link = skyscanner_link(m["iata"], friday, sunday)
        lines.append(
            f"{i}. [{m['name']}]({link}) — "
            f"{m['avg_low']:.0f}–{m['avg_high']:.0f}°C, rain {m['max_rain']:.0f}%"
        )
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 1

    friday, sunday = upcoming_weekend(date.today())
    print(f"Checking weekend: {friday} -> {sunday}")

    matches = []
    for iata, name, lat, lon in DESTINATIONS:
        data = fetch_weather(lat, lon, friday, sunday)
        if data is None:
            continue
        metrics = evaluate(data)
        if metrics is None:
            continue
        matches.append({"iata": iata, "name": name, **metrics})

    matches.sort(key=lambda m: m["score"])
    print(f"Matched {len(matches)} destinations")

    text = build_message(matches, friday, sunday)
    send_telegram(token, chat_id, text)
    print("Telegram message sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
