"""Local-first хранилище: SQLite + очередь (outbox) + удалённое хранилище.

Запись: сначала в SQLite и outbox (задача уже не потеряется), затем попытка
сразу отправить очередь в удалённое хранилище. Если оно недоступно — лог,
уведомление админу, повтор фоновой задачей sync().

Чтение списка: из удалённого хранилища (там могли поправить руками),
поверх — ещё не отправленные локальные изменения. При ошибке — из SQLite.

Удалённое хранилище — любой TaskStorage: сейчас Google Sheets, позже Bitrix24.
"""

import asyncio
import logging
import time
from datetime import datetime

from bot.models import Task
from bot.services.notifier import AdminNotifier
from bot.storage.base import StorageError, TaskStorage
from bot.storage.sqlite import OutboxItem, OutboxOp, SQLiteOutbox, SQLiteStorage

log = logging.getLogger(__name__)

_NOTIFY_KEY = "remote-down"


class SyncedStorage(TaskStorage):
    def __init__(
        self,
        local: SQLiteStorage,
        outbox: SQLiteOutbox,
        remote: TaskStorage,
        notifier: AdminNotifier,
        remote_name: str = "Google Sheets",
        min_interval_s: float = 0.0,
        batch_size: int = 50,
    ) -> None:
        self._local = local
        self._outbox = outbox
        self._remote = remote
        self._notifier = notifier
        self._name = remote_name
        # Пауза между запросами к удалённому API (лимиты Sheets / Bitrix24)
        self._min_interval = min_interval_s
        self._batch_size = batch_size
        self._lock = asyncio.Lock()
        self._remote_down = False
        self._last_call = 0.0

    # --- TaskStorage ---

    async def add(self, task: Task) -> Task:
        await self._local.add(task)
        await self._outbox.enqueue(OutboxOp.CREATE, task)
        await self.sync()
        return await self._local.get(task.id) or task

    async def get(self, task_id: str) -> Task | None:
        # SQLite содержит все задачи, созданные ботом
        return await self._local.get(task_id)

    async def mark_done(self, task_id: str, done_at: datetime) -> Task | None:
        task = await self._local.mark_done(task_id, done_at)
        if task is None:
            return None
        await self._outbox.enqueue(OutboxOp.MARK_DONE, task)
        await self.sync()
        return task

    async def list_open(self) -> list[Task]:
        # Сначала пробуем дослать очередь; если не вышло — не ждём второй таймаут
        if await self._outbox.count():
            await self.sync()
            if self._remote_down:
                log.warning("Список задач взят из локальной копии SQLite")
                return await self._local.list_open()
        try:
            await self._throttle()
            remote = await self._remote.list_open()
        except StorageError as exc:
            await self._on_failure(exc)
            log.warning("Список задач взят из локальной копии SQLite")
            return await self._local.list_open()
        await self._on_success(0)

        # Неотправленные изменения новее, чем данные в удалённом хранилище
        pending_ids = await self._outbox.pending_task_ids()
        merged = {t.id: t for t in remote if t.id not in pending_ids}
        for t in await self._local.list_open():
            if t.id in pending_ids:
                merged[t.id] = t
        return sorted(merged.values(), key=lambda t: t.due)

    async def close(self) -> None:
        await self._remote.close()

    # --- синхронизация ---

    @property
    def remote_available(self) -> bool:
        return not self._remote_down

    async def sync(self) -> int:
        """Отправляет очередь по порядку. Останавливается на первой ошибке. Возвращает число отправленных."""
        async with self._lock:
            sent = 0
            while items := await self._outbox.pending(self._batch_size):
                for item in items:
                    try:
                        await self._throttle()
                        await self._push(item)
                    except Exception as exc:  # noqa: BLE001 — запись остаётся в очереди
                        await self._outbox.mark_failed(item.id, str(exc))
                        if not isinstance(exc, StorageError):
                            log.exception("Неожиданная ошибка синхронизации задачи %s", item.task.id)
                        await self._on_failure(exc)
                        return sent
                    await self._outbox.mark_sent(item.id)
                    sent += 1
            await self._on_success(sent)
            return sent

    async def _push(self, item: OutboxItem) -> None:
        if item.op is OutboxOp.CREATE:
            stored = await self._remote.add(item.task)
            if stored.external_id and stored.external_id != item.task.external_id:
                await self._local.set_external_id(item.task.id, stored.external_id)
        elif item.op is OutboxOp.MARK_DONE:
            if await self._remote.mark_done(item.task.id, item.task.done_at or datetime.now()) is None:
                log.warning("%s: задача %s не найдена (удалена вручную?)", self._name, item.task.id)

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._last_call + self._min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _on_failure(self, exc: Exception) -> None:
        log.error("%s недоступен: %s", self._name, exc)
        first = not self._remote_down
        self._remote_down = True
        queued = await self._outbox.count()
        if first:
            self._notifier.reset(_NOTIFY_KEY)
        await self._notifier.notify(
            f"⚠️ {self._name} недоступен: {exc}\n"
            f"Задачи сохраняются локально, в очереди: {queued}. Повторю отправку автоматически.",
            key=_NOTIFY_KEY,
        )

    async def _on_success(self, sent: int) -> None:
        if self._remote_down:
            self._remote_down = False
            log.info("%s снова доступен, отправлено из очереди: %d", self._name, sent)
            await self._notifier.notify(f"✅ {self._name} снова доступен. Отправлено из очереди: {sent}.")
