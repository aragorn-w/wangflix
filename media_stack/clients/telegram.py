"""Telegram Bot API client. The token lives outside the repo, in
`$HOME/.claude/channels/telegram/.env`; the caller loads it."""

from __future__ import annotations

import urllib.parse
import urllib.request


def send(bot_token: str, chat_id: str, text: str, *, timeout: int = 30) -> bool:
    """True on HTTP 200. The Bot API puts the token in the URL path, so callers must not log the
    request URL or response."""
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
