from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    """Telegram'a mesaj gönderir; ayarlı değilse yalnız günlüğe yazar."""

    def __init__(self, enabled: bool):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") if enabled else None
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID") if enabled else None

    def send(self, text: str) -> None:
        log.info("BİLDİRİM: %s", text)
        if not (self.token and self.chat_id):
            return
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps({"chat_id": self.chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # bildirim hatası botu durdurmasın
            log.warning("Telegram mesajı gönderilemedi: %s", e)
