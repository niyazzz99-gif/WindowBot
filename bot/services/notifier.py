"""Уведомления администратору с защитой от спама."""

import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)


class Sender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> object: ...


class AdminNotifier:
    def __init__(self, sender: Sender | None, admin_chat_id: int | None, cooldown_s: float = 1800) -> None:
        self._sender = sender
        self._chat_id = admin_chat_id
        self._cooldown = cooldown_s
        self._last_sent: dict[str, float] = {}

    async def notify(self, text: str, key: str | None = None) -> None:
        """Шлёт сообщение админу. С key — не чаще раза в cooldown. Никогда не бросает исключений."""
        if key is not None:
            now = time.monotonic()
            last = self._last_sent.get(key)
            if last is not None and now - last < self._cooldown:
                return
            self._last_sent[key] = now
        if self._sender is None or self._chat_id is None:
            log.warning("ADMIN_CHAT_ID не задан, уведомление только в лог: %s", text)
            return
        try:
            await self._sender.send_message(self._chat_id, text)
        except Exception:  # noqa: BLE001 — уведомление не должно ронять бота
            log.exception("Не удалось отправить уведомление администратору")

    def reset(self, key: str) -> None:
        self._last_sent.pop(key, None)
