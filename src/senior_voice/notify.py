"""Telegram notifications to the family (alerts and post-call reports)."""

import httpx

TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message; keep a margin


def chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into parts of at most `limit` characters, preferring line breaks."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text] if text else parts


class Telegram:
    def __init__(self, http: httpx.AsyncClient, token: str, chat_id: str):
        self.http, self.token, self.chat_id = http, token, chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        for part in chunks(text):
            r = await self.http.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": part},
            )
            if r.is_error:  # not raise_for_status(): its message contains the URL, i.e. the bot token
                raise RuntimeError(f"Telegram HTTP {r.status_code}")
        return True
