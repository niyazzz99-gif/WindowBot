import asyncio
from datetime import datetime, time

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.config import Settings
from bot.models import Task, WorkType
from bot.scheduler.jobs import build_digest, send_digest
from bot.scheduler.setup import build_scheduler
from bot.services.weather import DailyForecast, WeatherCheck

NOW = datetime(2026, 11, 7, 9, 0)  # суббота, 9:00


def make(order, due, wt=WorkType.PAINTING, chat=1) -> Task:
    return Task(work_type=wt, order_no=order, due=due, assignee="Иван", chat_id=chat)


class FakeWeather:
    """Прогноз: 7.11 — нормально, 8.11 — снег и мороз."""

    def __init__(self, error=False) -> None:
        self.error, self.days = error, []

    async def check_day(self, day, today):
        self.days.append(day)
        if self.error:
            return WeatherCheck(day=day, error="timeout")
        if day.day == 8:
            f = DailyForecast(day, -15.0, 4.0, 3.0)
            return WeatherCheck(day=day, forecast=f, reasons=["холод до -15 °C (порог -10 °C)", "осадки 3.0 мм"])
        f = DailyForecast(day, 1.0, 3.0, 0.0)
        return WeatherCheck(day=day, forecast=f)


class FakeStorage:
    def __init__(self, tasks) -> None:
        self.tasks = tasks

    async def list_open(self):
        return self.tasks


class FakeBot:
    def __init__(self, blocked=()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.blocked = set(blocked)

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=SendMessage(chat_id=chat_id, text=text), message="blocked")
        self.sent.append((chat_id, text))


def digest(tasks, weather=None):
    return asyncio.run(build_digest(tasks, NOW, weather))


def test_overdue_and_today_separated():
    tasks = [
        make("1", datetime(2026, 11, 5, 18, 0)),  # просрочена 2 дня
        make("2", datetime(2026, 11, 7, 8, 0)),  # сегодня, но уже прошла 8:00 → просрочена
        make("3", datetime(2026, 11, 7, 18, 0)),  # сегодня
        make("4", datetime(2026, 11, 10, 18, 0)),  # позже — не в сводке
    ]
    d = digest(tasks)
    overdue_part, today_part = d.text.split("На сегодня")
    assert "Просрочено (2)" in overdue_part and "заказ 1" in overdue_part and "заказ 2" in overdue_part
    assert "заказ 3" in today_part
    assert "заказ 4" not in d.text
    # Кнопки «Выполнено» — для просроченных и сегодняшних, нумерация как в тексте
    labels = [b.text for row in d.markup.inline_keyboard for b in row]
    assert labels == ["✅ 1. заказ 1", "✅ 2. заказ 2", "✅ 3. заказ 3"]


def test_nothing_to_report_returns_none():
    assert digest([make("4", datetime(2026, 11, 10, 18, 0))]) is None
    assert digest([]) is None


def test_weather_for_mounting_today_and_tomorrow_only():
    w = FakeWeather()
    tasks = [
        make("10", datetime(2026, 11, 7, 18, 0), WorkType.MOUNTING),
        make("11", datetime(2026, 11, 8, 18, 0), WorkType.DISMANTLING),
        make("12", datetime(2026, 11, 9, 18, 0), WorkType.MOUNTING),  # послезавтра — нет
        make("13", datetime(2026, 11, 8, 18, 0), WorkType.PAINTING),  # не уличные — нет
    ]
    d = digest(tasks, w)
    assert [day.day for day in w.days] == [7, 8]
    weather_part = d.text.split("Погода для монтажа")[1]
    assert "заказ 10" in weather_part and "без ограничений" in weather_part
    assert "заказ 11" in weather_part and "холод до -15" in weather_part
    assert "заказ 12" not in weather_part and "заказ 13" not in weather_part


def test_tomorrow_bad_weather_alone_triggers_digest():
    d = digest([make("11", datetime(2026, 11, 8, 18, 0), WorkType.MOUNTING)], FakeWeather())
    assert d is not None
    assert "Просроченных задач и задач на сегодня нет" in d.text
    assert "⚠️" in d.text


def test_tomorrow_good_weather_alone_no_digest():
    class Good(FakeWeather):
        async def check_day(self, day, today):
            return WeatherCheck(day=day, forecast=DailyForecast(day, 1.0, 2.0, 0.0))

    assert digest([make("11", datetime(2026, 11, 8, 18, 0), WorkType.MOUNTING)], Good()) is None


def test_weather_unavailable_is_shown():
    d = digest([make("10", datetime(2026, 11, 7, 18, 0), WorkType.MOUNTING)], FakeWeather(error=True))
    assert "недоступен" in d.text


def test_user_text_is_escaped():
    t = Task(work_type=WorkType.PAINTING, order_no="<b>1</b>", due=datetime(2026, 11, 7, 18, 0),
             assignee="Иван & Co", chat_id=1)
    assert "&lt;b&gt;1&lt;/b&gt;" in digest([t]).text and "Иван &amp; Co" in digest([t]).text


def test_long_list_is_truncated():
    tasks = [make(str(i), datetime(2026, 11, 1, 18, 0)) for i in range(50)]
    d = digest(tasks)
    assert "и ещё 20" in d.text and len(d.text) < 4096


def test_send_digest_per_chat_and_survives_blocked_chat():
    tasks = [
        make("1", datetime(2026, 11, 7, 18, 0), chat=1),
        make("2", datetime(2026, 11, 6, 18, 0), chat=2),
        make("3", datetime(2026, 11, 7, 18, 0), chat=3),
        make("4", datetime(2026, 11, 20, 18, 0), chat=4),  # нечего сообщать
    ]
    bot = FakeBot(blocked={2})
    sent = asyncio.run(send_digest(bot, FakeStorage(tasks), None, now=NOW))
    assert sent == 2
    assert [c for c, _ in bot.sent] == [1, 3]
    chat1 = dict(bot.sent)[1]
    assert "заказ 1" in chat1 and "заказ 3" not in chat1  # чужие задачи не видны


def test_scheduler_runs_digest_at_configured_time():
    s = Settings.model_validate({"bot_token": "1:a", "weather_lat": 54.7, "weather_lon": 55.9, "digest_time": "09:00"})
    sched = build_scheduler(s, FakeStorage([]), FakeBot(), None)
    job = {j.id: j for j in sched.get_jobs()}["morning-digest"]
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("9", "0")
    assert job.trigger.timezone == sched.timezone  # часовой пояс сервера
    assert s.digest_time == time(9, 0)
