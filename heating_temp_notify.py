"""
Daily heating temperature suggestion via Telegram.

Picks a random temperature between 16.0 and 19.5 °C (0.1 increments)
and sends a Telegram reminder to set the living room thermostat.
"""

import os
import random
import requests

TEMP_MIN = 16.0
TEMP_MAX = 19.5
TEMP_STEP = 0.1


def random_temp():
    steps = round((TEMP_MAX - TEMP_MIN) / TEMP_STEP)
    return TEMP_MIN + random.randint(0, steps) * TEMP_STEP


def send_telegram(message):
    token   = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }, timeout=20)
    r.raise_for_status()


def main():
    temp = random_temp()
    message = f"🌡️ Zet de woonkamer vanavond op *{temp:.1f}°C*"

    print(message)

    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return

    send_telegram(message)
    print("Verzonden naar Telegram.")


if __name__ == "__main__":
    main()
