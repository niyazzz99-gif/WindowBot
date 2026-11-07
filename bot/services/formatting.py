"""Тексты сообщений (HTML). Пользовательский ввод всегда экранируется."""

from datetime import datetime
from html import escape

from bot.models import Task
from bot.services.parser import ParsedTask
from bot.services.weather import WeatherCheck, fmt_temp

WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_UNKNOWN = "❓ <i>не распознано</i>"


def format_due(dt: datetime) -> str:
    return f"{WEEKDAYS_SHORT[dt.weekday()]} {dt:%d.%m.%Y %H:%M}"


def format_task(task: Task) -> str:
    return (
        f"🛠 <b>{escape(task.work_type.value)}</b> · заказ <b>{escape(task.order_no)}</b>\n"
        f"📅 {format_due(task.due)}\n"
        f"👤 {escape(task.assignee)}"
    )


def format_task_line(n: int, task: Task) -> str:
    """Одна строка для списков и сводки."""
    return (
        f"{n}. <b>{escape(task.work_type.value)}</b>, заказ {escape(task.order_no)} — "
        f"{format_due(task.due)}, {escape(task.assignee)}"
    )


def format_weather(check: WeatherCheck, short: bool = False) -> str:
    """short=True — для строк сводки, где дата уже указана рядом."""
    day = f"{WEEKDAYS_SHORT[check.day.weekday()]} {check.day:%d.%m}"
    if check.error:
        return "🌐 прогноз недоступен" if short else f"🌐 Прогноз погоды на {day} недоступен — проверьте позже."
    if check.has_warnings:
        reasons = "; ".join(check.reasons)
        return f"⚠️ {reasons}" if short else f"⚠️ <b>Погода на {day}</b>: {reasons}"
    f = check.forecast
    parts = []
    if f.temp_min is not None:
        parts.append(f"от {fmt_temp(f.temp_min)}")
    if f.wind_max is not None:
        parts.append(f"ветер до {f.wind_max:.0f} м/с")
    parts.append("без осадков")
    details = ", ".join(parts)
    return f"🌤 без ограничений ({details})" if short else f"🌤 Погода на {day} без ограничений: {details}."


def format_draft(d: ParsedTask) -> str:
    def val(v: str | None) -> str:
        return f"<b>{escape(v)}</b>" if v else _UNKNOWN

    lines = [
        "📝 <b>Проверьте задачу</b>",
        "",
        f"Тип работ: {val(d.work_type.value if d.work_type else None)}",
        f"Заказ: {val(d.order_no)}",
        f"Срок: {val(format_due(d.due) if d.due else None)}",
        f"Ответственный: {val(d.assignee)}",
    ]
    if d.due_alternative:
        lines += ["", "⏰ Срок на сегодня, но 16:00 уже прошло. Выберите вариант:"]
    elif d.due_in_past:
        lines += ["", "⚠️ Срок уже в прошлом — задача сразу будет просроченной."]
    if d.work_type is None:
        lines += ["", "Выберите тип работ:"]
    return "\n".join(lines)
