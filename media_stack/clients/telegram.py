"""Telegram Bot API client — used by normalize-driver.sh + future
healthcheck alerting (AUDIT A9) to push notifications.

The bot token is intentionally kept OUT of `.env`; it lives in
`$HOME/.claude/channels/telegram/.env` (managed by the Claude
Telegram plugin) so a leak in this repo can't compromise the token.
"""

from __future__ import annotations

import urllib.parse
import urllib.request


def send(bot_token: str, chat_id: str, text: str, *, timeout: int = 30) -> bool:
    """Send `text` to `chat_id` via the Bot API.  Returns True on HTTP
    200, False otherwise.  The Bot API requires the token in the URL
    path (`/bot<token>/sendMessage`); it can't be moved to the body
    (codex round-5 #6 corrected an earlier docstring claim).  The
    practical leak protection here is that the URL is constructed in
    Python and passed to urllib — it never appears in process argv,
    and callers must not log the resulting request URL or response.
    `chat_id` + `text` go in the form-encoded body to avoid `?text=`
    query-string logging.

    Caller is responsible for loading the token from its source-of-
    truth file; this function does not read .env.
    """
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
