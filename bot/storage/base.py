"""Интерфейс хранилища задач.

Хендлеры и планировщик работают только с TaskStorage, поэтому
Google Sheets / SQLite / Bitrix24 подменяются без правки их кода.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from bot.models import Task


class StorageError(Exception):
    """Хранилище временно недоступно или вернуло ошибку."""


class TaskStorage(ABC):
    @abstractmethod
    async def add(self, task: Task) -> Task:
        """Сохраняет новую задачу и возвращает её (с external_id, если есть)."""

    @abstractmethod
    async def get(self, task_id: str) -> Task | None:
        """Задача по локальному id или None."""

    @abstractmethod
    async def list_open(self) -> list[Task]:
        """Все незакрытые задачи, отсортированные по сроку."""

    @abstractmethod
    async def mark_done(self, task_id: str, done_at: datetime) -> Task | None:
        """Помечает задачу выполненной. None, если задачи нет. Повторный вызов безопасен."""

    async def close(self) -> None:
        """Освобождение ресурсов (по умолчанию ничего)."""
