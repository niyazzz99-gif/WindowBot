"""Приём задачи, подтверждение/правка полей, закрытие задач."""

import logging
from datetime import date, datetime

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.handlers.callbacks import (
    DoneCb,
    DraftAction,
    DraftCb,
    TypeCb,
    can_save,
    done_keyboard,
    draft_keyboard,
    tasks_done_keyboard,
    type_buttons,
    without_button,
)
from bot.models import Task, TaskStatus
from bot.services.dates import parse_due
from bot.services.formatting import format_draft, format_task, format_task_line, format_weather
from bot.services.parser import ParsedTask, parse_task
from bot.services.weather import WeatherService
from bot.storage import StorageError, TaskStorage

log = logging.getLogger(__name__)


NOT_COMMAND = F.text & ~F.text.startswith("/")
DUE_HELP = "Примеры: <i>пятница</i>, <i>завтра</i>, <i>15.11.2026</i>, <i>20.11 12:00</i>"


class TaskForm(StatesGroup):
    confirm = State()
    edit_order = State()
    edit_due = State()
    edit_assignee = State()


_EDIT_PROMPTS = {
    DraftAction.EDIT_ORDER: (TaskForm.edit_order, "Введите номер заказа:"),
    DraftAction.EDIT_DUE: (TaskForm.edit_due, f"Введите срок.\n{DUE_HELP}"),
    DraftAction.EDIT_ASSIGNEE: (TaskForm.edit_assignee, "Введите ответственного:"),
}


# --- черновик в FSM ---

async def _load_draft(state: FSMContext) -> ParsedTask | None:
    raw = (await state.get_data()).get("draft")
    return ParsedTask.model_validate(raw) if raw else None


async def _store_draft(state: FSMContext, draft: ParsedTask) -> None:
    await state.set_state(TaskForm.confirm)
    await state.update_data(draft=draft.model_dump(mode="json"))


async def _safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def _save(draft: ParsedTask, chat_id: int, task_storage: TaskStorage) -> Task:
    task = await task_storage.add(draft.to_task(chat_id))
    log.info("Задача %s сохранена: %s / %s", task.id, task.work_type, task.order_no)
    return task


async def _send_weather(message: Message, task: Task, weather: WeatherService | None) -> None:
    """Для монтажа/демонтажа со сроком в ближайшие 10 дней — прогноз отдельным сообщением."""
    if weather is None:
        return
    check = await weather.check_task(task, date.today())
    if check is not None:
        await message.answer(format_weather(check))


def _saved_text(task: Task) -> str:
    return f"✅ <b>Задача сохранена</b>\n\n{format_task(task)}"


# --- новая задача ---

async def on_order_input(message: Message, state: FSMContext) -> None:
    await _apply_text(message, state, "order_no", message.text.strip().lstrip("№#").strip())


async def on_assignee_input(message: Message, state: FSMContext) -> None:
    await _apply_text(message, state, "assignee", " ".join(message.text.split()).strip("."))


async def on_due_input(message: Message, state: FSMContext) -> None:
    now = datetime.now()
    due = parse_due(message.text, now)
    if due is None:
        await message.answer(f"Не понял срок. {DUE_HELP}")
        return
    draft = await _load_draft(state)
    if draft is None:
        await _expired(message, state)
        return
    draft.set_due(due, now)
    await _store_draft(state, draft)
    await message.answer(format_draft(draft), reply_markup=draft_keyboard(draft))


async def _apply_text(message: Message, state: FSMContext, field: str, value: str) -> None:
    if not value:
        await message.answer("Пустое значение, попробуйте ещё раз.")
        return
    draft = await _load_draft(state)
    if draft is None:
        await _expired(message, state)
        return
    setattr(draft, field, value)
    await _store_draft(state, draft)
    await message.answer(format_draft(draft), reply_markup=draft_keyboard(draft))


async def on_task_text(
    message: Message, state: FSMContext, task_storage: TaskStorage, weather: WeatherService | None = None
) -> None:
    """Любой текст вне режима правки — новая задача (старый черновик заменяется)."""
    draft = parse_task(message.text)
    if not draft.needs_confirmation:
        await state.clear()
        try:
            task = await _save(draft, message.chat.id, task_storage)
        except StorageError:
            log.exception("Не удалось сохранить задачу")
            await message.answer("❌ Не удалось сохранить задачу, попробуйте позже.")
            return
        await message.answer(_saved_text(task), reply_markup=done_keyboard(task.id))
        await _send_weather(message, task, weather)
        return

    await _store_draft(state, draft)
    await message.answer(format_draft(draft), reply_markup=draft_keyboard(draft))


async def on_non_text(message: Message) -> None:
    await message.answer("Пришлите задачу текстом. /help — пример формата.")


async def _expired(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Черновик устарел. Пришлите задачу заново.")


# --- кнопки черновика ---

async def on_draft_action(
    cb: CallbackQuery,
    callback_data: DraftCb,
    state: FSMContext,
    task_storage: TaskStorage,
    weather: WeatherService | None = None,
) -> None:
    draft = await _load_draft(state)
    if draft is None:
        await cb.answer("Черновик устарел, пришлите задачу заново.", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    action = callback_data.action
    if action is DraftAction.CANCEL:
        await state.clear()
        await _safe_edit(cb.message, "✖️ Задача отменена.", None)
        await cb.answer()
        return

    if action is DraftAction.SAVE:
        if not can_save(draft):
            await cb.answer("Заполните все поля.", show_alert=True)
            return
        try:
            task = await _save(draft, cb.message.chat.id, task_storage)
        except StorageError:
            log.exception("Не удалось сохранить задачу")
            await cb.answer("Не удалось сохранить, попробуйте позже.", show_alert=True)
            return
        await state.clear()
        await _safe_edit(cb.message, _saved_text(task), done_keyboard(task.id))
        await cb.answer("Сохранено")
        await _send_weather(cb.message, task, weather)
        return

    if action in _EDIT_PROMPTS:
        new_state, prompt = _EDIT_PROMPTS[action]
        await state.set_state(new_state)
        await cb.message.answer(prompt)
        await cb.answer()
        return

    if action is DraftAction.EDIT_TYPE:
        await cb.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=type_buttons())
        )
        await cb.answer()
        return

    if action is DraftAction.DUE_KEEP:
        draft.keep_today(datetime.now())
    elif action is DraftAction.DUE_ALT:
        draft.take_alternative()
    await _store_draft(state, draft)
    await _safe_edit(cb.message, format_draft(draft), draft_keyboard(draft))
    await cb.answer()


async def on_type_chosen(cb: CallbackQuery, callback_data: TypeCb, state: FSMContext) -> None:
    draft = await _load_draft(state)
    if draft is None:
        await cb.answer("Черновик устарел, пришлите задачу заново.", show_alert=True)
        return
    draft.work_type = callback_data.work_type
    await _store_draft(state, draft)
    await _safe_edit(cb.message, format_draft(draft), draft_keyboard(draft))
    await cb.answer()


# --- список и закрытие задач ---

async def cmd_tasks(message: Message, task_storage: TaskStorage) -> None:
    tasks = [t for t in await task_storage.list_open() if t.chat_id == message.chat.id]
    if not tasks:
        await message.answer("Открытых задач нет 🎉")
        return
    lines = [format_task_line(n, t) for n, t in enumerate(tasks, 1)]
    await message.answer(
        "📋 <b>Открытые задачи</b>\n\n" + "\n".join(lines),
        reply_markup=tasks_done_keyboard(tasks),
    )


async def cmd_digest(message: Message, task_storage: TaskStorage, weather: WeatherService | None = None) -> None:
    """Сводка по запросу — та же, что приходит утром, только для этого чата."""
    from bot.scheduler.jobs import build_digest  # локально: избегаем цикла импортов

    tasks = [t for t in await task_storage.list_open() if t.chat_id == message.chat.id]
    digest = await build_digest(tasks, datetime.now(), weather)
    if digest is None:
        await message.answer("Просроченных задач и задач на сегодня нет 🎉")
        return
    await message.answer(digest.text, reply_markup=digest.markup)


async def on_done(cb: CallbackQuery, callback_data: DoneCb, task_storage: TaskStorage) -> None:
    task = await task_storage.get(callback_data.task_id)
    # Закрыть можно только задачу своего чата
    if task is None or task.chat_id != cb.message.chat.id:
        await cb.answer("Задача не найдена.", show_alert=True)
        return
    was_open = task.status is TaskStatus.OPEN
    await task_storage.mark_done(task.id, datetime.now())

    # Убираем нажатую кнопку; если кнопок не осталось — помечаем всё сообщение
    markup = without_button(cb.message.reply_markup, cb.data)
    if markup is None:
        await _safe_edit(cb.message, cb.message.html_text + "\n\n✅ <b>Выполнено</b>", None)
    else:
        await cb.message.edit_reply_markup(reply_markup=markup)
    await cb.answer(
        f"Заказ {task.order_no}: выполнено ✅" if was_open else "Задача уже была закрыта."
    )


def build_router() -> Router:
    r = Router(name="tasks")
    r.message.filter(F.chat.type == ChatType.PRIVATE)
    r.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    # Порядок важен: сначала ввод в режиме правки, потом «любой текст = новая задача»
    r.message.register(on_order_input, TaskForm.edit_order, NOT_COMMAND)
    r.message.register(on_assignee_input, TaskForm.edit_assignee, NOT_COMMAND)
    r.message.register(on_due_input, TaskForm.edit_due, NOT_COMMAND)
    r.message.register(cmd_tasks, Command("tasks"))
    r.message.register(cmd_digest, Command("digest"))
    r.message.register(on_task_text, NOT_COMMAND)
    r.message.register(on_non_text, ~F.text)
    r.callback_query.register(on_draft_action, DraftCb.filter())
    r.callback_query.register(on_type_chosen, TypeCb.filter())
    r.callback_query.register(on_done, DoneCb.filter())
    return r
