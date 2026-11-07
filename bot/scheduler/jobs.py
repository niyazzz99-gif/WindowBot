"""Утренняя сводка: просроченные, на сегодня, погода для монтажей на сегодня и завтра.

Каждый чат получает сводку только по своим задачам. Пустая сводка не отправляется.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

from bot.handlers.callbacks import tasks_done_keyboard
from bot.models import Task
from bot.services.formatting import WEEKDAYS_SHORT, format_task_line, format_weather
from bot.services.weather import WeatherService
from bot.storage import StorageError, TaskStorage

log = logging.getLogger(__name__)

# Ограничения Telegram: 4096 символов на сообщение, кнопок — с запасом
MAX_ITEMS_PER_SECTION = 30
# Пауза между чатами — лимит Telegram ~30 сообщений/с
SEND_PAUSE_S = 0.05


@dataclass(frozen=True)
class Digest:
    text: str
    markup: InlineKeyboardMarkup | None


def _section(title: str, tasks: list[Task], start: int) -> list[str]:
    lines = [title]
    shown = tasks[:MAX_ITEMS_PER_SECTION]
    lines += [format_task_line(n, t) for n, t in enumerate(shown, start)]
    if len(tasks) > len(shown):
        lines.append(f"… и ещё {len(tasks) - len(shown)}, полный список — /tasks")
    return lines


async def build_digest(tasks: list[Task], now: datetime, weather: WeatherService | None) -> Digest | None:
    """Сводка для одного чата. None — если сообщать нечего."""
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    overdue = sorted((t for t in tasks if t.is_overdue(now)), key=lambda t: t.due)
    due_today = sorted((t for t in tasks if t.due.date() == today and not t.is_overdue(now)), key=lambda t: t.due)

    # Погода: уличные задачи со сроком сегодня или завтра (включая просроченные сегодня)
    weather_lines: list[str] = []
    has_weather_warning = False
    if weather is not None:
        outdoor = sorted((t for t in tasks if t.is_outdoor and t.due.date() in (today, tomorrow)), key=lambda t: t.due)
        for t in outdoor:
            check = await weather.check_day(t.due.date(), today)
            if check is None:
                continue
            has_weather_warning |= check.has_warnings or bool(check.error)
            day = f"{WEEKDAYS_SHORT[check.day.weekday()]} {check.day:%d.%m}"
            weather_lines.append(
                f"• <b>{escape(t.work_type.value)}</b>, заказ {escape(t.order_no)} ({day}): "
                f"{format_weather(check, short=True)}"
            )

    if not (overdue or due_today or has_weather_warning):
        return None

    lines = [f"☀️ <b>Сводка на {WEEKDAYS_SHORT[today.weekday()]} {today:%d.%m.%Y}</b>"]
    if overdue:
        lines += ["", *_section(f"🔴 <b>Просрочено ({len(overdue)}):</b>", overdue, 1)]
    if due_today:
        start = len(overdue[:MAX_ITEMS_PER_SECTION]) + 1
        lines += ["", *_section(f"🟡 <b>На сегодня ({len(due_today)}):</b>", due_today, start)]
    if not (overdue or due_today):
        lines += ["", "Просроченных задач и задач на сегодня нет."]
    if weather_lines:
        lines += ["", "🌦 <b>Погода для монтажа/демонтажа (сегодня и завтра):</b>", *weather_lines]

    # Кнопки «Выполнено» — с теми же номерами, что в тексте
    buttons = overdue[:MAX_ITEMS_PER_SECTION] + due_today[:MAX_ITEMS_PER_SECTION]
    return Digest("\n".join(lines), tasks_done_keyboard(buttons))


async def send_digest(
    bot: Bot, task_storage: TaskStorage, weather: WeatherService | None, now: datetime | None = None
) -> int:
    """Рассылает сводку по всем чатам. Возвращает число отправленных сообщений."""
    now = now or datetime.now()
    try:
        tasks = await task_storage.list_open()
    except StorageError:
        log.exception("Сводка: не удалось получить задачи")
        return 0

    by_chat: dict[int, list[Task]] = defaultdict(list)
    for t in tasks:
        by_chat[t.chat_id].append(t)

    sent = 0
    for chat_id, chat_tasks in by_chat.items():
        digest = await build_digest(chat_tasks, now, weather)
        if digest is None:
            continue
        try:
            await bot.send_message(chat_id, digest.text, reply_markup=digest.markup)
            sent += 1
        except TelegramAPIError as exc:
            # Например, пользователь заблокировал бота — остальным всё равно отправляем
            log.warning("Сводка в чат %s не отправлена: %s", chat_id, exc)
        await asyncio.sleep(SEND_PAUSE_S)
    log.info("Сводка отправлена в %d чат(ов) из %d", sent, len(by_chat))
    return sent
