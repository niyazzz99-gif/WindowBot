"""Сценарии отказа удалённого хранилища: ничего не теряется, админ уведомлён."""

import asyncio
from datetime import datetime

import pytest

from bot.models import Task, TaskStatus, WorkType
from bot.services.notifier import AdminNotifier
from bot.storage import SQLiteOutbox, SQLiteStorage, StorageError, TaskStorage
from bot.storage.synced import SyncedStorage


class FlakyRemote(TaskStorage):
    """Удалённое хранилище в памяти, которое можно «уронить»."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.down = False
        self.calls: list[str] = []

    def _check(self, op: str) -> None:
        self.calls.append(op)
        if self.down:
            raise StorageError("503 Service Unavailable")

    async def add(self, task):
        self._check("add")
        self.tasks.setdefault(task.id, task)
        return task

    async def get(self, task_id):
        self._check("get")
        return self.tasks.get(task_id)

    async def list_open(self):
        self._check("list")
        return sorted((t for t in self.tasks.values() if t.status is TaskStatus.OPEN), key=lambda t: t.due)

    async def mark_done(self, task_id, done_at):
        self._check("done")
        if task_id not in self.tasks:
            return None
        t = self.tasks[task_id].model_copy(update={"status": TaskStatus.DONE, "done_at": done_at})
        self.tasks[task_id] = t
        return t


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id, text):
        self.sent.append(text)


def make_task(**kw) -> Task:
    data = dict(work_type=WorkType.PAINTING, order_no="123", due=datetime(2026, 11, 13, 18, 0),
                assignee="Иван", chat_id=42)
    data.update(kw)
    return Task(**data)


class Env:
    def __init__(self, tmp_path) -> None:
        db = tmp_path / "t.db"
        self.local = SQLiteStorage(db)
        self.outbox = SQLiteOutbox(db)
        self.remote = FlakyRemote()
        self.sender = FakeSender()
        self.storage = SyncedStorage(self.local, self.outbox, self.remote, AdminNotifier(self.sender, 1))

    def run(self, coro):
        return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_happy_path_writes_both(env):
    task = make_task()
    env.run(env.storage.add(task))
    assert task.id in env.remote.tasks
    assert env.run(env.local.get(task.id)) is not None
    assert env.run(env.outbox.count()) == 0
    assert env.sender.sent == []


def test_remote_down_task_not_lost_and_admin_notified_once(env):
    env.remote.down = True
    t1, t2 = make_task(), make_task(order_no="124")
    env.run(env.storage.add(t1))  # не бросает исключение
    env.run(env.storage.add(t2))

    assert env.run(env.local.get(t1.id)) is not None
    assert env.run(env.outbox.count()) == 2
    assert len(env.sender.sent) == 1  # без спама
    assert "Google Sheets недоступен" in env.sender.sent[0]
    assert not env.storage.remote_available


def test_recovery_flushes_queue_in_order(env):
    env.remote.down = True
    task = make_task()
    env.run(env.storage.add(task))
    env.run(env.storage.mark_done(task.id, datetime(2026, 11, 8, 9, 0)))

    env.remote.down = False
    env.remote.calls.clear()
    sent = env.run(env.storage.sync())

    assert sent == 2
    assert env.remote.calls == ["add", "done"]  # create строго раньше mark_done
    assert env.remote.tasks[task.id].status is TaskStatus.DONE
    assert env.run(env.outbox.count()) == 0
    assert "снова доступен" in env.sender.sent[-1]


def test_list_open_falls_back_to_sqlite(env):
    task = make_task()
    env.run(env.storage.add(task))
    env.remote.down = True
    assert [t.id for t in env.run(env.storage.list_open())] == [task.id]


def test_list_open_merges_pending_local_changes(env):
    synced = make_task(order_no="1")
    env.run(env.storage.add(synced))
    # Задачу добавили в таблицу вручную — бот её тоже видит
    manual = make_task(order_no="2", due=datetime(2026, 11, 9, 18, 0))
    env.remote.tasks[manual.id] = manual

    result = env.run(env.storage.list_open())
    assert [t.order_no for t in result] == ["2", "1"]


def test_unexpected_error_keeps_item_in_queue(env):
    async def boom(task):
        raise ValueError("bug")

    env.remote.add = boom
    env.run(env.storage.add(make_task()))
    [item] = env.run(env.outbox.pending())
    assert item.attempts == 1 and "bug" in item.last_error


def test_notifier_without_admin_does_not_fail():
    asyncio.run(AdminNotifier(None, None).notify("x"))
