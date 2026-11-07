"""Google Sheets как удалённое хранилище задач.

Одна строка — одна задача, ключ — колонка «ID» (локальный id задачи).
gspread синхронный, поэтому все вызовы идут через asyncio.to_thread.
Любая ошибка сети/API/авторизации превращается в StorageError.
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import gspread
import requests
from google.auth.exceptions import GoogleAuthError
from google.oauth2.service_account import Credentials

from bot.models import Task, TaskStatus
from bot.storage.base import StorageError, TaskStorage

log = logging.getLogger(__name__)
T = TypeVar("T")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
WORKSHEET_TITLE = "Задачи"
HEADER = [
    "ID", "Тип работ", "Заказ", "Срок", "Ответственный",
    "Статус", "Создана", "Выполнена", "Chat ID", "Исходный текст",
]
_COL_STATUS = HEADER.index("Статус") + 1
_COL_DONE_AT = HEADER.index("Выполнена") + 1

_STATUS_TO_SHEET = {TaskStatus.OPEN: "Открыта", TaskStatus.DONE: "Выполнена"}
# Статус могут править руками в таблице — принимаем разные варианты
_STATUS_FROM_SHEET = {
    "открыта": TaskStatus.OPEN, "open": TaskStatus.OPEN, "": TaskStatus.OPEN,
    "выполнена": TaskStatus.DONE, "выполнено": TaskStatus.DONE, "done": TaskStatus.DONE,
}
_DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y")

_ERRORS = (gspread.exceptions.GSpreadException, requests.RequestException, GoogleAuthError, OSError)


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _parse_dt(value: str) -> datetime | None:
    value = value.strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def task_to_row(task: Task) -> list[str]:
    return [
        task.id, task.work_type.value, task.order_no, _fmt_dt(task.due), task.assignee,
        _STATUS_TO_SHEET[task.status], _fmt_dt(task.created_at), _fmt_dt(task.done_at),
        str(task.chat_id), task.raw_text,
    ]


def row_to_task(row: list[str]) -> Task:
    """Строка таблицы → Task. ValueError, если строка битая."""
    row = (row + [""] * len(HEADER))[: len(HEADER)]
    tid, wtype, order, due, assignee, status, created, done_at, chat_id, raw = (c.strip() for c in row)
    if (due_dt := _parse_dt(due)) is None:
        raise ValueError(f"не разобрать срок {due!r}")
    if (st := _STATUS_FROM_SHEET.get(status.lower())) is None:
        raise ValueError(f"неизвестный статус {status!r}")
    return Task(
        id=tid, work_type=wtype, order_no=order, due=due_dt, assignee=assignee,
        status=st, created_at=_parse_dt(created) or due_dt, done_at=_parse_dt(done_at),
        chat_id=int(chat_id), raw_text=raw,
    )


def load_credentials(value: str) -> Credentials:
    """GOOGLE_CREDENTIALS_JSON: путь к файлу ключа или сам JSON."""
    if value.lstrip().startswith("{"):
        return Credentials.from_service_account_info(json.loads(value), scopes=SCOPES)
    return Credentials.from_service_account_file(Path(value), scopes=SCOPES)


class GoogleSheetsStorage(TaskStorage):
    def __init__(self, open_worksheet: Callable[[], Any]) -> None:
        # open_worksheet — фабрика листа; в тестах подменяется фейком
        self._open_worksheet = open_worksheet
        self._ws: Any = None
        self._lock = threading.Lock()  # gspread-клиент не потокобезопасен

    @classmethod
    def from_settings(cls, credentials: str, spreadsheet_id: str, timeout: float = 20) -> "GoogleSheetsStorage":
        def open_worksheet() -> Any:
            client = gspread.authorize(load_credentials(credentials))
            client.set_timeout(timeout)
            spreadsheet = client.open_by_key(spreadsheet_id)
            try:
                ws = spreadsheet.worksheet(WORKSHEET_TITLE)
            except gspread.WorksheetNotFound:
                ws = spreadsheet.add_worksheet(WORKSHEET_TITLE, rows=1000, cols=len(HEADER))
                log.info("Создан лист «%s»", WORKSHEET_TITLE)
            if ws.row_values(1) != HEADER:
                ws.update([HEADER], "A1")
            return ws

        return cls(open_worksheet)

    async def _run(self, fn: Callable[[Any], T]) -> T:
        def job() -> T:
            with self._lock:
                if self._ws is None:
                    self._ws = self._open_worksheet()
                return fn(self._ws)

        try:
            return await asyncio.to_thread(job)
        except _ERRORS as exc:
            self._ws = None  # при следующем вызове переподключимся
            raise StorageError(f"Google Sheets: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _find_row(ws: Any, task_id: str) -> int | None:
        cell = ws.find(task_id, in_column=1)
        return cell.row if cell else None

    async def add(self, task: Task) -> Task:
        def append(ws: Any) -> None:
            # Идемпотентность: повтор из очереди не создаёт дубль
            if self._find_row(ws, task.id) is None:
                ws.append_row(task_to_row(task), value_input_option="RAW")

        await self._run(append)
        return task

    async def get(self, task_id: str) -> Task | None:
        def fetch(ws: Any) -> list[str] | None:
            row = self._find_row(ws, task_id)
            return ws.row_values(row) if row else None

        row = await self._run(fetch)
        return row_to_task(row) if row else None

    async def list_open(self) -> list[Task]:
        rows = await self._run(lambda ws: ws.get_all_values())
        tasks = []
        for n, row in enumerate(rows[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            try:
                task = row_to_task(row)
            except ValueError as exc:
                log.warning("Sheets: пропущена строка %d: %s", n, exc)
                continue
            if task.status is TaskStatus.OPEN:
                tasks.append(task)
        return sorted(tasks, key=lambda t: t.due)

    async def mark_done(self, task_id: str, done_at: datetime) -> Task | None:
        def update(ws: Any) -> list[str] | None:
            row = self._find_row(ws, task_id)
            if row is None:
                return None
            ws.batch_update([
                {"range": gspread.utils.rowcol_to_a1(row, _COL_STATUS),
                 "values": [[_STATUS_TO_SHEET[TaskStatus.DONE]]]},
                {"range": gspread.utils.rowcol_to_a1(row, _COL_DONE_AT),
                 "values": [[_fmt_dt(done_at)]]},
            ])
            return ws.row_values(row)

        row = await self._run(update)
        return row_to_task(row) if row else None
