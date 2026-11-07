"""Служебные команды."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, Message


COMMANDS = [
    BotCommand(command="help", description="Формат задачи"),
    BotCommand(command="tasks", description="Открытые задачи"),
    BotCommand(command="digest", description="Сводка сейчас"),
    BotCommand(command="cancel", description="Отменить ввод"),
    BotCommand(command="myid", description="Мой chat_id"),
]

HELP = (
    "Пришлите задачу одним сообщением:\n"
    "<code>Покраска, заказ 123, срок пятница, ответственный Иван</code>\n"
    "или короче:\n"
    "<code>Покраска, заказ 321, 20.11 12:00, Алекс</code>\n\n"
    "<b>Типы работ:</b> Монтаж, Демонтаж, Покраска, Обслуживание\n"
    "<b>Срок:</b> день недели, сегодня/завтра/послезавтра, 15.11.2026, 20.11, "
    "можно со временем: <i>пятница 12:00</i>.\n"
    "Слова «срок» и «ответственный» можно не писать, если поля идут по порядку.\n"
    "Без времени срок — 18:00; если срок сегодня — 16:00.\n\n"
    "Если что-то не распознаю — покажу карточку с кнопками для правки.\n"
    "Каждое утро пришлю сводку: просроченные и задачи на сегодня."
)


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("👋 Я веду задачи по заказам.\n\n" + HELP)


async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ввод отменён.")


async def cmd_myid(message: Message) -> None:
    # Нужен для ADMIN_CHAT_ID в .env
    await message.answer(f"Ваш chat_id: <code>{message.chat.id}</code>")


def build_router() -> Router:
    r = Router(name="common")
    r.message.filter(F.chat.type == ChatType.PRIVATE)
    r.message.register(cmd_start, CommandStart())
    r.message.register(cmd_help, Command("help"))
    r.message.register(cmd_cancel, Command("cancel"))
    r.message.register(cmd_myid, Command("myid"))
    return r
