"""Shared Telegram notification helper.

Single source of truth for sending Telegram messages across all pipelines.
Credentials default to the environment (`TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID`); callers override the chat target (e.g. the
weather-briefing group via `TELEGRAM_CHAT_GROUP_ID`) or the parse mode as
needed.

Missing credentials or a send error are a graceful no-op that returns
``False`` instead of raising — a single unset secret or a transient Telegram
hiccup can't fail an otherwise-healthy Action run whose real work (compute +
commit) already succeeded by the time it notifies. Zero third-party deps
beyond ``requests``.
"""

import os

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(
    text: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str = "HTML",
    disable_preview: bool = True,
    timeout: int = 20,
) -> bool:
    """Send a Telegram message.

    Returns ``True`` on success, ``False`` on missing credentials or any send
    error. Never raises.
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] geen creds, overslaan")
        return False
    try:
        r = requests.post(
            TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        print("[telegram] ✓ verzonden")
        return True
    except Exception as e:
        print(f"[telegram] fout: {e}")
        return False
