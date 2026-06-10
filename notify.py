"""Shared Telegram notification helper.

Single source of truth for sending Telegram messages across all pipelines.
Credentials default to the environment (`TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID`); callers override the chat target (e.g. the
weather-briefing group via `TELEGRAM_CHAT_GROUP_ID`) or the parse mode as
needed.

Missing credentials or a send error are a graceful no-op that returns
``False`` instead of raising — a single unset secret or a transient Telegram
hiccup can't fail an otherwise-healthy Action run whose real work (compute +
commit) already succeeded by the time it notifies. Transient failures
(connection errors, timeouts, 429, 5xx) are retried twice with a short
backoff before giving up; permanent 4xx errors (bad chat id, malformed
message) are not retried. Zero third-party deps beyond ``requests``.

Also provides :func:`sanitize_error` — secret-safe rendering of exceptions.
WU- en Telegram-URLs dragen hun credential als query-param (`apiKey=...`,
`/bot<token>/`); een requests-exceptie bevat de volledige URL, dus rauwe
``str(e)`` in een log of Telegram-bericht zou het geheim lekken. GitHub
Actions maskeert secrets in de eigen logs, maar niet in een doorgestuurd
Telegram-bericht — daarom altijd via deze helper printen/doorsturen.
"""

import os
import re
import time

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

RETRY_DELAYS = (2, 4)  # seconds between attempts on transient failures

_SECRET_PATTERNS = (
    # query-param credentials (WU apiKey + stationId — beide secrets, zie
    # Project 7 — en generieke token/key params)
    (re.compile(r"(apiKey=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(stationId=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"((?:api_key|token|key|access_token)=)[^&\s\"']+", re.IGNORECASE), r"\1***"),
    # Telegram bot token in the URL path: /bot<id>:<secret>/
    (re.compile(r"(/bot)[0-9]+:[A-Za-z0-9_-]+"), r"\1***"),
    # Gist-ID in API-URLs: GIST_ID/TADO_GIST_ID zijn secrets en een requests-
    # exceptie op een Gist-call bevat de volledige URL.
    (re.compile(r"(gists/)[0-9a-f]{16,}", re.IGNORECASE), r"\1***"),
)


def sanitize_error(e: BaseException) -> str:
    """Render an exception as ``Type: message`` with credentials scrubbed."""
    msg = str(e)
    for pattern, repl in _SECRET_PATTERNS:
        msg = pattern.sub(repl, msg)
    return f"{type(e).__name__}: {msg}"


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
    for attempt in range(len(RETRY_DELAYS) + 1):
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
            if r.status_code == 200:
                print("[telegram] ✓ verzonden")
                return True
            # 429/5xx zijn transient → retry; overige 4xx (bad request,
            # verkeerd chat_id) worden niet beter van een retry.
            transient = r.status_code == 429 or r.status_code >= 500
            print(f"[telegram] HTTP {r.status_code}" + (" (retry)" if transient and attempt < len(RETRY_DELAYS) else ""))
            if not transient:
                return False
        except requests.RequestException as e:
            print(f"[telegram] fout: {sanitize_error(e)}")
        except Exception as e:
            print(f"[telegram] fout: {sanitize_error(e)}")
            return False
        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
    return False
