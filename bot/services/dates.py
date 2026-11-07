"""Разбор срока задачи: дни недели, «сегодня/завтра», ДД.ММ[.ГГГГ], необязательное время.

Правила:
- обычный срок — 18:00 указанного дня;
- если срок выпадает на сегодня — 16:00 (за 2 часа до конца рабочего дня);
- если сегодняшние 16:00 уже прошли — предлагаем альтернативу (следующий такой день 18:00);
- явно указанное время («пятница 12:00») используется как есть.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WORKDAY_END = time(18, 0)
SAME_DAY_DEADLINE = time(16, 0)

# (основа слова для любых падежей, сокращение) — индекс = weekday()
_WEEKDAYS: tuple[tuple[str, str], ...] = (
    ("понедельн", "пн"),
    ("вторн", "вт"),
    ("сред", "ср"),
    ("четверг", "чт"),
    ("пятниц", "пт"),
    ("суббот", "сб"),
    ("воскресен", "вс"),
)

_PREFIX_RE = re.compile(r"^(?:(?:срок\w*|до|к|ко|в|во|на)\s+)+")
_TIME_RE = re.compile(r"\s*(?:\bв\s+)?(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?$")
_RELATIVE = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


@dataclass(frozen=True)
class DueParse:
    due: datetime
    # Если due — уже прошедшие сегодняшние 16:00, здесь вариант на выбор пользователю
    alternative: datetime | None = None


def parse_due(text: str, now: datetime) -> DueParse | None:
    """Возвращает срок или None, если текст не распознан."""
    s = _PREFIX_RE.sub("", text.strip().lower().replace("ё", "е")).strip()

    explicit_time: time | None = None
    if m := _TIME_RE.search(s):
        hh, mm = int(m[1]), int(m[2])
        if hh > 23 or mm > 59:
            return None
        explicit_time = time(hh, mm)
        s = s[: m.start()].strip()

    found = _parse_day(s, now.date())
    if found is None:
        return None
    day, next_day = found

    if explicit_time is not None:
        return DueParse(datetime.combine(day, explicit_time))
    if day == now.date():
        due = datetime.combine(day, SAME_DAY_DEADLINE)
        alt = datetime.combine(next_day, WORKDAY_END) if due <= now else None
        return DueParse(due, alt)
    return DueParse(datetime.combine(day, WORKDAY_END))


def _parse_day(s: str, today: date) -> tuple[date, date] | None:
    """(день срока, запасной день на случай «сегодня уже поздно»)."""
    if s in _RELATIVE:
        day = today + timedelta(days=_RELATIVE[s])
        return day, day + timedelta(days=1)

    if m := _DATE_RE.match(s):
        dd, mm, yy = int(m[1]), int(m[2]), m[3]
        year = today.year if yy is None else int(yy) + (2000 if len(yy) == 2 else 0)
        try:
            day = date(year, mm, dd)
            # ДД.ММ без года — ближайшая такая дата, не в прошлом
            if yy is None and day < today:
                day = date(year + 1, mm, dd)
        except ValueError:
            return None
        return day, day + timedelta(days=1)

    if (wd := _weekday(s)) is not None:
        day = today + timedelta(days=(wd - today.weekday()) % 7)
        return day, day + timedelta(days=7)
    return None


def _weekday(s: str) -> int | None:
    for idx, (stem, abbr) in enumerate(_WEEKDAYS):
        if s == abbr or (s.startswith(stem) and " " not in s):
            return idx
    return None
