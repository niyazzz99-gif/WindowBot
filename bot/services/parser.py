"""Разбор текста задачи без LLM: строгий шаблон, затем мягкий поиск по полям.

Строгий шаблон: «<тип работ>, заказ <N>, срок <день>, ответственный <имя>».
Если шаблон не совпал или какое-то поле не распознано — exact=False,
и хендлер просит пользователя подтвердить/дополнить поля кнопками.
"""

import re
from datetime import datetime

from pydantic import BaseModel

from bot.models import Task, WorkType
from bot.services.dates import DueParse, parse_due

FIELD_LABELS: dict[str, str] = {
    "work_type": "Тип работ",
    "order_no": "Заказ",
    "due": "Срок",
    "assignee": "Ответственный",
}

_SEP = r"\s*[,;]\s*"
_STRICT_RE = re.compile(
    r"^\s*(?P<type>[^,;]+?)" + _SEP
    + r"заказ\s*(?:№|#)?\s*:?\s*(?P<order>\w[\w\-/]*)" + _SEP
    # Слово «срок» можно опустить: «Монтаж, заказ 45, 06.11.2026, ответственный Сеня»
    + r"(?:срок\s*:?\s*)?(?P<due>[^,;]+?)" + _SEP
    # Слово «ответственный» тоже можно опустить: «Покраска, заказ 321, 20.11 12:00, Алекс»
    + r"(?:(?:ответственн|исполнител)\w*\s*:?\s*)?(?P<assignee>[^,;]+?)\s*\.?\s*$",
    re.IGNORECASE,
)

# Мягкий режим: каждое поле ищется по всему тексту независимо
_STOP = r"(?=\s*(?:[,;\n]|\bзаказ|\bсрок|\bответственн|\bисполнител|$))"
_ORDER_RE = re.compile(r"(?:\bзаказ\w*\s*(?:№|#)?|№|#)\s*:?\s*(?P<v>\w[\w\-/]*)", re.I)
# «до» — только отдельным словом, чтобы не ловить «доводчик», «доставка»
_DUE_RE = re.compile(r"\b(?:срок\w*\s*:?|до\s)\s*(?P<v>.+?)" + _STOP, re.I)
_ASSIGNEE_RE = re.compile(r"\b(?:ответственн\w*|исполнител\w*)\s*:?\s*(?P<v>[^,;\n]+?)" + _STOP, re.I)

# Демонтаж проверяется отдельно от монтажа: \b не даёт «монтаж» совпасть внутри «демонтаж»
_TYPE_PATTERNS: tuple[tuple[WorkType, re.Pattern[str]], ...] = (
    (WorkType.DISMANTLING, re.compile(r"\bдемонт\w*|\bсняти\w*|\bснять\b", re.I)),
    (WorkType.MOUNTING, re.compile(r"\bмонтаж\w*|\bмонтир\w*|\bустанов(?:ка|ки|ку|ить)\b", re.I)),
    (WorkType.PAINTING, re.compile(r"\bпокрас\w*|\bокрас\w*", re.I)),
    # «ТО» — только заглавными, иначе путается с местоимением «то»
    (WorkType.MAINTENANCE, re.compile(r"\b(?i:обслужива\w*|сервис\w*)|\bТО\b")),
)


class ParsedTask(BaseModel):
    raw_text: str
    work_type: WorkType | None = None
    order_no: str | None = None
    due: datetime | None = None
    due_alternative: datetime | None = None
    assignee: str | None = None
    exact: bool = False  # совпал строгий шаблон и все поля распознаны
    due_in_past: bool = False

    def missing_fields(self) -> list[str]:
        return [name for name in FIELD_LABELS if getattr(self, name) is None]

    @property
    def needs_confirmation(self) -> bool:
        return (
            not self.exact
            or bool(self.missing_fields())
            or self.due_alternative is not None
            or self.due_in_past
        )

    def set_due(self, due: DueParse, now: datetime) -> None:
        self.due = due.due
        self.due_alternative = due.alternative
        self.due_in_past = due.alternative is None and due.due < now

    def keep_today(self, now: datetime) -> None:
        """Пользователь выбрал «сегодня, хоть и просрочено»."""
        self.due_alternative = None
        self.due_in_past = self.due is not None and self.due < now

    def take_alternative(self) -> None:
        if self.due_alternative:
            self.due, self.due_alternative, self.due_in_past = self.due_alternative, None, False

    def to_task(self, chat_id: int) -> Task:
        if missing := self.missing_fields():
            raise ValueError(f"не заполнены поля: {', '.join(missing)}")
        return Task(
            work_type=self.work_type,
            order_no=self.order_no,
            due=self.due,
            assignee=self.assignee,
            chat_id=chat_id,
            raw_text=self.raw_text,
        )


def detect_work_type(text: str) -> WorkType | None:
    for work_type, pattern in _TYPE_PATTERNS:
        if pattern.search(text):
            return work_type
    return None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or None


def _find_due_in_chunks(text: str, now: datetime) -> DueParse | None:
    """Срок без ключевого слова: первый фрагмент между запятыми, похожий на дату/день."""
    for chunk in re.split(r"[,;\n]", text):
        if due := parse_due(chunk, now):
            return due
    return None


def parse_task(text: str, now: datetime | None = None) -> ParsedTask:
    now = now or datetime.now()
    strict = _STRICT_RE.match(text)
    soft_order = (m := _ORDER_RE.search(text)) and m["v"]
    soft_due = (m := _DUE_RE.search(text)) and m["v"]
    soft_assignee = (m := _ASSIGNEE_RE.search(text)) and m["v"]

    if strict and parse_due(strict["due"], now) is None:
        # Шаблон совпал по запятым, но 3-е поле не срок — вероятно, поля переставлены
        # («…, ответственный Иван, срок пятница»). Берём мягкий поиск, шаблон — как запасной.
        type_text = strict["type"]
        order = soft_order or strict["order"]
        due_text = soft_due
        assignee = soft_assignee or (None if soft_due else strict["assignee"])
        strict = None
    elif strict:
        type_text, order, due_text, assignee = (
            strict["type"], strict["order"], strict["due"], strict["assignee"],
        )
    else:
        type_text, order, due_text, assignee = text, soft_order, soft_due, soft_assignee

    result = ParsedTask(
        raw_text=text.strip(),
        work_type=detect_work_type(type_text),
        order_no=_clean(order),
        assignee=_clean(assignee),
    )
    due = parse_due(due_text, now) if due_text else None
    if due is None and not strict:
        due = _find_due_in_chunks(text, now)
    if due is not None:
        result.set_due(due, now)

    result.exact = bool(strict) and not result.missing_fields()
    return result
