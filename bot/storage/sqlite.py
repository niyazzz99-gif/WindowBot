"""SQLite: локальное хранилище задач и очередь синхронизации (outbox).

Используется stdlib sqlite3 в отдельном потоке (asyncio.to_thread),
на каждую операцию — своё короткое соединение: просто и потокобезопасно.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from bot.models import Task, TaskStatus
from bot.storage.base import StorageError, TaskStorage

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    work_type   TEXT NOT NULL,
    order_no    TEXT NOT NULL,
    due         TEXT NOT NULL,
    assignee    TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    done_at     TEXT,
    raw_text    TEXT NOT NULL DEFAULT '',
    external_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due);

CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    op         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL
);
"""

_TASK_COLUMNS = tuple(Task.model_fields)


class _Database:
    """Общая обвязка: схема, соединения, перевод ошибок в StorageError."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def job() -> T:
            with closing(self._connect()) as conn, conn:  # commit / rollback
                return fn(conn)

        try:
            return await asyncio.to_thread(job)
        except sqlite3.Error as exc:
            raise StorageError(f"SQLite: {exc}") from exc


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task.model_validate(dict(row))


class SQLiteStorage(_Database, TaskStorage):
    async def add(self, task: Task) -> Task:
        data = task.model_dump(mode="json")
        placeholders = ", ".join(f":{c}" for c in _TASK_COLUMNS)
        sql = f"INSERT INTO tasks ({', '.join(_TASK_COLUMNS)}) VALUES ({placeholders})"
        await self._run(lambda c: c.execute(sql, data))
        return task

    async def get(self, task_id: str) -> Task | None:
        row = await self._run(
            lambda c: c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
        return _row_to_task(row) if row else None

    async def list_open(self) -> list[Task]:
        rows = await self._run(
            lambda c: c.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY due", (TaskStatus.OPEN.value,)
            ).fetchall()
        )
        return [_row_to_task(r) for r in rows]

    async def mark_done(self, task_id: str, done_at: datetime) -> Task | None:
        # done_at не перезаписываем, если задача уже закрыта
        await self._run(
            lambda c: c.execute(
                "UPDATE tasks SET status = ?, done_at = ? WHERE id = ? AND status = ?",
                (TaskStatus.DONE.value, done_at.isoformat(), task_id, TaskStatus.OPEN.value),
            )
        )
        return await self.get(task_id)

    async def set_external_id(self, task_id: str, external_id: str) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE tasks SET external_id = ? WHERE id = ?", (external_id, task_id)
            )
        )


class OutboxOp(StrEnum):
    CREATE = "create"
    MARK_DONE = "mark_done"


@dataclass(frozen=True)
class OutboxItem:
    id: int
    op: OutboxOp
    task: Task  # снимок задачи на момент постановки в очередь
    attempts: int
    last_error: str | None


class SQLiteOutbox(_Database):
    """Очередь операций, которые ещё не дошли до удалённого хранилища.

    Порядок строго по id: create всегда уходит раньше mark_done той же задачи.
    """

    async def enqueue(self, op: OutboxOp, task: Task) -> int:
        def insert(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO outbox (task_id, op, payload, created_at) VALUES (?, ?, ?, ?)",
                (task.id, op.value, task.model_dump_json(), datetime.now().isoformat()),
            )
            return int(cur.lastrowid)

        return await self._run(insert)

    async def pending(self, limit: int = 50) -> list[OutboxItem]:
        rows = await self._run(
            lambda c: c.execute("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        )
        return [
            OutboxItem(
                id=r["id"],
                op=OutboxOp(r["op"]),
                task=Task.model_validate_json(r["payload"]),
                attempts=r["attempts"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    async def mark_sent(self, item_id: int) -> None:
        await self._run(lambda c: c.execute("DELETE FROM outbox WHERE id = ?", (item_id,)))

    async def mark_failed(self, item_id: int, error: str) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:500], item_id),
            )
        )

    async def count(self) -> int:
        return await self._run(lambda c: c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    async def pending_task_ids(self) -> set[str]:
        rows = await self._run(lambda c: c.execute("SELECT DISTINCT task_id FROM outbox").fetchall())
        return {r[0] for r in rows}
