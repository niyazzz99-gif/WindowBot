"""CallbackData-фабрики и инлайн-клавиатуры."""

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import Task, WorkType
from bot.services.formatting import format_due
from bot.services.parser import ParsedTask


class DraftAction(StrEnum):
    SAVE = "save"
    CANCEL = "cancel"
    EDIT_TYPE = "etype"
    EDIT_ORDER = "eorder"
    EDIT_DUE = "edue"
    EDIT_ASSIGNEE = "eassignee"
    DUE_KEEP = "dkeep"  # оставить сегодняшние 16:00, хоть и прошедшие
    DUE_ALT = "dalt"  # перенести на альтернативный срок


class DraftCb(CallbackData, prefix="draft"):
    action: DraftAction


class TypeCb(CallbackData, prefix="type"):
    work_type: WorkType


class DoneCb(CallbackData, prefix="done"):
    task_id: str


def type_buttons() -> list[list[InlineKeyboardButton]]:
    b = InlineKeyboardBuilder()
    for wt in WorkType:
        b.button(text=wt.value, callback_data=TypeCb(work_type=wt))
    b.adjust(2)
    return b.export()


def draft_keyboard(d: ParsedTask) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if d.work_type is None:
        rows += type_buttons()
    if d.due and d.due_alternative:
        rows.append([
            InlineKeyboardButton(
                text=f"Сегодня {d.due:%H:%M} (просрочено)",
                callback_data=DraftCb(action=DraftAction.DUE_KEEP).pack(),
            ),
            InlineKeyboardButton(
                text=format_due(d.due_alternative),
                callback_data=DraftCb(action=DraftAction.DUE_ALT).pack(),
            ),
        ])

    edit = InlineKeyboardBuilder()
    for action, label in (
        (DraftAction.EDIT_TYPE, "✏️ Тип"),
        (DraftAction.EDIT_ORDER, "✏️ Заказ"),
        (DraftAction.EDIT_DUE, "✏️ Срок"),
        (DraftAction.EDIT_ASSIGNEE, "✏️ Ответственный"),
    ):
        edit.button(text=label, callback_data=DraftCb(action=action))
    edit.adjust(2)
    rows += edit.export()

    last = [InlineKeyboardButton(text="✖️ Отмена", callback_data=DraftCb(action=DraftAction.CANCEL).pack())]
    if can_save(d):
        last.insert(0, InlineKeyboardButton(
            text="💾 Сохранить", callback_data=DraftCb(action=DraftAction.SAVE).pack()
        ))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def can_save(d: ParsedTask) -> bool:
    return not d.missing_fields() and d.due_alternative is None


def done_keyboard(task_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выполнено", callback_data=DoneCb(task_id=task_id))
    return b.as_markup()


def tasks_done_keyboard(tasks: list[Task], start: int = 1) -> InlineKeyboardMarkup | None:
    """Кнопка «Выполнено» на каждую задачу списка; номера совпадают с текстом."""
    if not tasks:
        return None
    b = InlineKeyboardBuilder()
    for n, t in enumerate(tasks, start):
        b.button(text=f"✅ {n}. заказ {t.order_no}", callback_data=DoneCb(task_id=t.id))
    b.adjust(2)
    return b.as_markup()


def without_button(markup: InlineKeyboardMarkup | None, callback_data: str) -> InlineKeyboardMarkup | None:
    """Та же клавиатура без нажатой кнопки (None, если кнопок не осталось)."""
    if markup is None:
        return None
    rows = [
        [btn for btn in row if btn.callback_data != callback_data]
        for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
