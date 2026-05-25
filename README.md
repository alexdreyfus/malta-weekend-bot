# malta-weekend-bot

Daily Telegram digest of weekend getaways from Malta (MLA) with good weather. Checks ~40 direct-flight destinations against the Open-Meteo forecast for the upcoming Fri–Sun and sends the top 10 matches as tappable Skyscanner links.

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow prompts.
2. Save the **bot token** it gives you.
3. Send your new bot any message, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the `chat.id` value — that's your **chat ID**.

### 2. Add the secrets to GitHub

In the repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Add both:

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the numeric chat ID |

### 3. Test it

Go to the **Actions** tab → select **Daily weekend weather check** in the sidebar → **Run workflow** → **Run workflow**. The job should finish in well under a minute and you should see a message in Telegram.

## Schedule

Runs daily at **05:00 UTC** (07:00 Malta summer time / 06:00 winter time). Change the `cron:` line in [.github/workflows/daily.yml](.github/workflows/daily.yml) to adjust.

## Tuning weather thresholds

The filters live at the top of [weekend_finder.py](weekend_finder.py):

```python
MIN_DAY_HIGH = 15.0   # reject if avg daytime high below this
MAX_DAY_HIGH = 28.0   # reject if avg daytime high above this
MIN_NIGHT_LOW = 8.0   # reject if avg night min below this
MAX_RAIN_PROB = 40.0  # reject if any day's rain probability above this (%)
IDEAL_TEMP = 22.0     # ranking target — closer to this scores better
```

The `DESTINATIONS` list right below is also where you'd add/remove cities — each entry is `(IATA, name, lat, lon)`.
