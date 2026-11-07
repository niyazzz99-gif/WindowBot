import asyncio
from datetime import datetime

import gspread
import pytest
import requests

from bot.config import Settings
from bot.models import Task, TaskStatus, WorkType
from bot.storage import StorageError
from bot.storage.google_sheets import HEADER, GoogleSheetsStorage, row_to_task, task_to_row


class FakeCell:
    def __init__(self, row: int) -> None:
        self.row = row


class FakeWorksheet:
    """Минимальная имитация gspread.Worksheet для используемых методов."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = [list(HEADER)]
        self.fail: Exception | None = None

    def _check(self) -> None:
        if self.fail:
            raise self.fail

    def find(self, query, in_column=None):
        self._check()
        for n, row in enumerate(self.rows, 1):
            if row and row[(in_column or 1) - 1] == query:
                return FakeCell(n)
        return None

    def append_row(self, values, value_input_option=None):
        self._check()
        self.rows.append(list(values))

    def row_values(self, row):
        self._check()
        return list(self.rows[row - 1])

    def get_all_values(self):
        self._check()
        return [list(r) for r in self.rows]

    def batch_update(self, data):
        self._check()
        for item in data:
            row, col = gspread.utils.a1_to_rowcol(item["range"])
            self.rows[row - 1][col - 1] = item["values"][0][0]


def make_task(**kw) -> Task:
    data = dict(work_type=WorkType.MOUNTING, order_no="45", due=datetime(2026, 11, 13, 18, 0),
                assignee="Пётр", chat_id=42, raw_text="Монтаж, заказ 45")
    data.update(kw)
    return Task(**data)


@pytest.fixture
def ws():
    return FakeWorksheet()


@pytest.fixture
def storage(ws):
    return GoogleSheetsStorage(lambda: ws)


def test_row_roundtrip():
    task = make_task(created_at=datetime(2026, 11, 7, 10, 0))
    assert row_to_task(task_to_row(task)) == task


def test_row_accepts_manual_edits():
    row = ["abc", "Покраска", "1", "13.11.2026 18:00", "Иван", "выполнено", "", "", "7", ""]
    task = row_to_task(row)
    assert task.due == datetime(2026, 11, 13, 18, 0)
    assert task.status is TaskStatus.DONE


def test_add_is_idempotent(storage, ws):
    task = make_task()
    asyncio.run(storage.add(task))
    asyncio.run(storage.add(task))  # повтор из очереди
    assert len(ws.rows) == 2
    assert ws.rows[1][:6] == [task.id, "Монтаж", "45", "2026-11-13 18:00", "Пётр", "Открыта"]


def test_list_open_skips_done_and_broken_rows(storage, ws):
    a = make_task(due=datetime(2026, 11, 20, 18, 0))
    b = make_task(due=datetime(2026, 11, 10, 18, 0))
    for t in (a, b):
        asyncio.run(storage.add(t))
    ws.rows.append(["bad", "Покраска", "1", "когда-нибудь", "Иван", "Открыта", "", "", "1", ""])
    ws.rows.append([""] * len(HEADER))
    asyncio.run(storage.mark_done(a.id, datetime(2026, 11, 8, 9, 0)))

    assert [t.id for t in asyncio.run(storage.list_open())] == [b.id]


def test_mark_done_updates_cells(storage, ws):
    task = make_task()
    asyncio.run(storage.add(task))
    done = asyncio.run(storage.mark_done(task.id, datetime(2026, 11, 8, 9, 30)))
    assert done.status is TaskStatus.DONE
    assert ws.rows[1][5] == "Выполнена" and ws.rows[1][7] == "2026-11-08 09:30"
    assert asyncio.run(storage.mark_done("nope", datetime.now())) is None


@pytest.mark.parametrize("error", [
    requests.ConnectionError("нет сети"),
    gspread.exceptions.GSpreadException("квота"),
    OSError("timeout"),
])
def test_errors_become_storage_error(storage, ws, error):
    ws.fail = error
    with pytest.raises(StorageError):
        asyncio.run(storage.list_open())


def test_reconnects_after_error(ws):
    opened = []

    def open_ws():
        opened.append(1)
        return ws

    storage = GoogleSheetsStorage(open_ws)
    ws.fail = requests.ConnectionError("x")
    with pytest.raises(StorageError):
        asyncio.run(storage.list_open())
    ws.fail = None
    asyncio.run(storage.list_open())
    assert len(opened) == 2


def test_spreadsheet_url_is_accepted_as_id():
    s = Settings.model_validate({
        "bot_token": "1:a", "weather_lat": 0, "weather_lon": 0,
        "storage_backend": "sheets", "google_credentials_json": "k.json",
        "spreadsheet_id": "https://docs.google.com/spreadsheets/d/1u7V-abc_DEF/edit?usp=sharing",
    })
    assert s.spreadsheet_id == "1u7V-abc_DEF"
