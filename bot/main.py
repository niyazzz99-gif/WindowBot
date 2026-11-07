"""Точка входа: python -m bot.main"""

import asyncio
import logging
import sys

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import ValidationError

from bot.config import Settings, load_settings
from bot.handlers import build_router
from bot.handlers.common import COMMANDS
from bot.scheduler.setup import build_scheduler
from bot.services.notifier import AdminNotifier
from bot.services.storage_factory import ConfigError, build_storage
from bot.services.weather import OpenMeteoClient, Thresholds, WeatherService
from bot.storage.synced import SyncedStorage

log = logging.getLogger("bot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # aiogram и HTTP-клиенты на DEBUG слишком многословны
    for noisy in ("aiogram.event", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, logging.getLevelName(level)))


async def run(settings: Settings) -> None:
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notifier = AdminNotifier(bot, settings.admin_chat_id)
    task_storage = build_storage(settings, notifier)
    http = httpx.AsyncClient(timeout=10)
    weather = WeatherService(
        OpenMeteoClient(settings.weather_lat, settings.weather_lon, http),
        Thresholds(temp_min=settings.temp_threshold, wind_max=settings.wind_threshold),
    )
    scheduler = build_scheduler(settings, task_storage, bot, weather)

    # Черновики задач живут в памяти: после перезапуска их нужно прислать заново
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router())
    # Доступны в хендлерах как аргументы с этими именами
    dp["task_storage"] = task_storage
    dp["settings"] = settings
    dp["notifier"] = notifier
    dp["weather"] = weather

    try:
        if isinstance(task_storage, SyncedStorage):
            # Проверка связи и досылка того, что осталось с прошлого запуска
            await task_storage.list_open()
        await bot.set_my_commands(COMMANDS)
        scheduler.start()
        log.info("Бот запущен")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await task_storage.close()
        await http.aclose()
        await bot.session.close()


def main() -> None:
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(f"Ошибка в .env:\n{exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level)
    try:
        asyncio.run(run(settings))
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(2)
    except KeyboardInterrupt:
        log.info("Остановлено")


if __name__ == "__main__":
    main()
