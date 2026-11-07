import asyncio
from datetime import datetime

import pytest

from bot.models import Task, TaskStatus, WorkType
from bot.storage import OutboxOp, SQLiteOutbox, SQLiteStorage, TaskStorage


def make_task(**kw) -> Task:
    data = dict(
        work_type=WorkType.PAINTING,
        order_no="123",
        due=datetime(2026, 11, 13, 18, 0),
        assignee="Иван",
        chat_id=42,
        raw_text="Покраска, заказ 123, срок пятница, ответственный Иван",
    )
    data.update(kw)
    return Task(**data)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sub" / "tasks.db"  # подпапка создаётся автоматически


def test_implements_interface(db_path):
    assert isinstance(SQLiteStorage(db_path), TaskStorage)


def test_add_and_get_roundtrip(db_path):
    storage = SQLiteStorage(db_path)
    task = make_task()

    asyncio.run(storage.add(task))
    loaded = asyncio.run(storage.get(task.id))

    assert loaded == task
    assert loaded.work_type is WorkType.PAINTING
    assert loaded.due == datetime(2026, 11, 13, 18, 0)


def test_get_missing_returns_none(db_path):
    assert asyncio.run(SQLiteStorage(db_path).get("nope")) is None


def test_list_open_sorted_and_excludes_done(db_path):
    storage = SQLiteStorage(db_path)
    late = make_task(due=datetime(2026, 11, 20, 18, 0))
    early = make_task(due=datetime(2026, 11, 10, 18, 0))
    done = make_task(due=datetime(2026, 11, 1, 18, 0))

    async def scenario():
        for t in (late, early, done):
            await storage.add(t)
        await storage.mark_done(done.id, datetime(2026, 11, 2, 10, 0))
        return await storage.list_open()

    assert [t.id for t in asyncio.run(scenario())] == [early.id, late.id]


def test_mark_done_keeps_task_and_is_idempotent(db_path):
    storage = SQLiteStorage(db_path)
    task = make_task()
    first = datetime(2026, 11, 8, 10, 0)

    async def scenario():
        await storage.add(task)
        await storage.mark_done(task.id, first)
        return await storage.mark_done(task.id, datetime(2026, 11, 9, 10, 0))

    result = asyncio.run(scenario())
    assert result.status is TaskStatus.DONE
    assert result.done_at == first  # повторное закрытие не меняет дату


def test_mark_done_missing_returns_none(db_path):
    assert asyncio.run(SQLiteStorage(db_path).mark_done("nope", datetime.now())) is None


def test_data_survives_reopen(db_path):
    task = make_task()
    asyncio.run(SQLiteStorage(db_path).add(task))
    assert asyncio.run(SQLiteStorage(db_path).get(task.id)) == task


def test_set_external_id(db_path):
    storage = SQLiteStorage(db_path)
    task = make_task()
    asyncio.run(storage.add(task))
    asyncio.run(storage.set_external_id(task.id, "row-7"))
    assert asyncio.run(storage.get(task.id)).external_id == "row-7"


def test_outbox_fifo_and_retry(db_path):
    outbox = SQLiteOutbox(db_path)
    task = make_task()

    async def scenario():
        await outbox.enqueue(OutboxOp.CREATE, task)
        await outbox.enqueue(OutboxOp.MARK_DONE, task)
        items = await outbox.pending()
        await outbox.mark_failed(items[0].id, "Sheets 503")
        retried = (await outbox.pending())[0]
        await outbox.mark_sent(retried.id)
        rest = await outbox.pending()
        return items, retried, rest, await outbox.count(), await outbox.pending_task_ids()

    items, retried, rest, count, ids = asyncio.run(scenario())
    assert [i.op for i in items] == [OutboxOp.CREATE, OutboxOp.MARK_DONE]
    assert items[0].task == task
    assert (retried.attempts, retried.last_error) == (1, "Sheets 503")
    assert [i.op for i in rest] == [OutboxOp.MARK_DONE]
    assert count == 1
    assert ids == {task.id}


def test_storage_and_outbox_share_file(db_path):
    storage, outbox = SQLiteStorage(db_path), SQLiteOutbox(db_path)
    task = make_task()
    asyncio.run(storage.add(task))
    asyncio.run(outbox.enqueue(OutboxOp.CREATE, task))
    assert asyncio.run(storage.get(task.id)) is not None
    assert asyncio.run(outbox.count()) == 1
