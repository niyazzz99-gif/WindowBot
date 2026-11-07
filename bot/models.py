"""Доменные модели задач."""

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class WorkType(StrEnum):
    MOUNTING = "Монтаж"
    DISMANTLING = "Демонтаж"
    PAINTING = "Покраска"
    MAINTENANCE = "Обслуживание"


# Уличные работы — для них проверяем погоду
OUTDOOR_TYPES: frozenset[WorkType] = frozenset({WorkType.MOUNTING, WorkType.DISMANTLING})


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"


def _new_id() -> str:
    return uuid4().hex[:8]


class Task(BaseModel):
    """Задача. Все даты — локальное время сервера без tzinfo."""

    id: str = Field(default_factory=_new_id)
    work_type: WorkType
    order_no: str = Field(min_length=1)
    due: datetime
    assignee: str = Field(min_length=1)
    chat_id: int
    status: TaskStatus = TaskStatus.OPEN
    created_at: datetime = Field(default_factory=lambda: datetime.now().replace(microsecond=0))
    done_at: datetime | None = None
    raw_text: str = ""
    # ID записи во внешней системе (строка в Sheets, задача в Bitrix24)
    external_id: str | None = None

    @field_validator("due", "created_at", "done_at")
    @classmethod
    def _to_local_naive(cls, v: datetime | None) -> datetime | None:
        # Приводим aware-даты к локальному времени сервера
        if v is not None and v.tzinfo is not None:
            return v.astimezone().replace(tzinfo=None)
        return v

    @property
    def is_outdoor(self) -> bool:
        return self.work_type in OUTDOOR_TYPES

    def is_overdue(self, now: datetime) -> bool:
        return self.status is TaskStatus.OPEN and self.due < now
