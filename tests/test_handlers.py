"""Сквозные тесты хендлеров: апдейты идут через настоящий Dispatcher,
запросы к Telegram перехватывает фейковая сессия."""

import asyncio
from datetime import datetime
from itertools import count

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.handlers import build_router
from bot.handlers.callbacks import DoneCb, DraftAction, DraftCb, TypeCb
from bot.models import TaskStatus, WorkType
from bot.storage import SQLiteStorage

CHAT_ID = 1001
USER = User(id=CHAT_ID, is_bot=False, first_name="Тест")
_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_ids), date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"), text=method.text,
            )
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self) -> None:
        pass


class Harness:
    def __init__(self, tmp_path) -> None:
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.storage = SQLiteStorage(tmp_path / "t.db")
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(build_router())
        self.dp["task_storage"] = self.storage

    def _feed(self, **kw) -> None:
        asyncio.run(self.dp.feed_update(self.bot, Update(update_id=next(_ids), **kw)))

    def text(self, text: str, chat_type: str = "private") -> None:
        self._feed(message=Message(
            message_id=next(_ids), date=datetime.now(),
            chat=Chat(id=CHAT_ID, type=chat_type), from_user=USER, text=text,
        ))

    def press(self, data: str, message_text: str = "карточка") -> None:
        msg = Message(
            message_id=next(_ids), date=datetime.now(),
            chat=Chat(id=CHAT_ID, type="private"), text=message_text,
        )
        self._feed(callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=USER, chat_instance="ci", message=msg, data=data,
        ))

    def sent_texts(self) -> list[str]:
        return [c.text for c in self.session.calls if hasattr(c, "text") and c.text]

    def open_tasks(self):
        return asyncio.run(self.storage.list_open())


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_exact_phrase_saved_immediately(h):
    h.text("Покраска, заказ 123, срок 15.11.2030, ответственный Иван")

    [task] = h.open_tasks()
    assert (task.work_type, task.order_no, task.assignee) == (WorkType.PAINTING, "123", "Иван")
    assert task.due == datetime(2030, 11, 15, 18, 0)
    assert task.chat_id == CHAT_ID
    assert "Задача сохранена" in h.sent_texts()[-1]


def test_unknown_type_chosen_by_button(h):
    h.text("Монтж, заказ 5, срок 15.11.2030, ответственный Иван")
    assert h.open_tasks() == []
    assert "Проверьте задачу" in h.sent_texts()[-1]

    h.press(TypeCb(work_type=WorkType.MOUNTING).pack())
    h.press(DraftCb(action=DraftAction.SAVE).pack())

    [task] = h.open_tasks()
    assert task.work_type is WorkType.MOUNTING and task.is_outdoor


def test_missing_order_entered_by_text(h):
    h.text("Обслуживание, срок 15.11.2030, ответственный Олег")
    h.press(DraftCb(action=DraftAction.SAVE).pack())  # ещё нельзя — нет заказа
    assert h.open_tasks() == []

    h.press(DraftCb(action=DraftAction.EDIT_ORDER).pack())
    h.text("№ 77")
    h.press(DraftCb(action=DraftAction.SAVE).pack())

    [task] = h.open_tasks()
    assert task.order_no == "77"


def test_bad_due_input_asks_again(h):
    h.text("Покраска, заказ 1, ответственный Иван")
    h.press(DraftCb(action=DraftAction.EDIT_DUE).pack())
    h.text("когда-нибудь")
    assert "Не понял срок" in h.sent_texts()[-1]
    h.text("15.11.2030")
    h.press(DraftCb(action=DraftAction.SAVE).pack())
    assert h.open_tasks()[0].due == datetime(2030, 11, 15, 18, 0)


def test_cancel_drops_draft(h):
    h.text("Покраска заказ 1")
    h.press(DraftCb(action=DraftAction.CANCEL).pack())
    h.press(DraftCb(action=DraftAction.SAVE).pack())
    assert h.open_tasks() == []


def test_done_button_closes_task(h):
    h.text("Покраска, заказ 123, срок 15.11.2030, ответственный Иван")
    [task] = h.open_tasks()

    h.press(DoneCb(task_id=task.id).pack(), message_text="✅ Задача сохранена")

    assert h.open_tasks() == []
    closed = asyncio.run(h.storage.get(task.id))
    assert closed.status is TaskStatus.DONE and closed.done_at is not None


def test_foreign_task_cannot_be_closed(h):
    from bot.models import Task

    other = Task(work_type=WorkType.PAINTING, order_no="9", due=datetime(2030, 1, 1),
                 assignee="Пётр", chat_id=999)
    asyncio.run(h.storage.add(other))
    h.press(DoneCb(task_id=other.id).pack())
    assert asyncio.run(h.storage.get(other.id)).status is TaskStatus.OPEN


def test_group_messages_ignored(h):
    h.text("Покраска, заказ 123, срок 15.11.2030, ответственный Иван", chat_type="group")
    assert h.open_tasks() == []
    assert h.session.calls == []


class FakeWeather:
    """Подменяет WeatherService: на любую уличную задачу — мороз."""

    def __init__(self) -> None:
        self.checked = []

    async def check_task(self, task, today):
        from bot.services.weather import DailyForecast, WeatherCheck

        self.checked.append(task.work_type)
        if not task.is_outdoor:
            return None
        f = DailyForecast(task.due.date(), -18.0, 4.0, 0.0)
        return WeatherCheck(day=f.day, forecast=f, reasons=["мороз до -18 °C (порог -10 °C)"])


def test_cold_mounting_sends_weather_warning(h):
    h.dp["weather"] = FakeWeather()
    h.text("Монтаж, заказ 5, срок 15.11.2030, ответственный Иван")
    texts = h.sent_texts()
    assert "Задача сохранена" in texts[-2]
    assert texts[-1].startswith("⚠️") and "мороз" in texts[-1]


def test_painting_has_no_weather_message(h):
    h.dp["weather"] = FakeWeather()
    h.text("Покраска, заказ 5, срок 15.11.2030, ответственный Иван")
    assert "Задача сохранена" in h.sent_texts()[-1]


def test_weather_after_confirm_button(h):
    h.dp["weather"] = FakeWeather()
    h.text("Монтж, заказ 5, срок 15.11.2030, ответственный Иван")
    h.press(TypeCb(work_type=WorkType.DISMANTLING).pack())
    h.press(DraftCb(action=DraftAction.SAVE).pack())
    assert "мороз" in h.sent_texts()[-1]
