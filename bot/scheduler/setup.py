"""Планировщик фоновых задач. Работает по локальному времени сервера."""

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import Settings
from bot.scheduler.jobs import send_digest
from bot.services.weather import WeatherService
from bot.storage import TaskStorage
from bot.storage.synced import SyncedStorage

log = logging.getLogger(__name__)


def build_scheduler(
    settings: Settings, task_storage: TaskStorage, bot: Bot, weather: WeatherService | None
) -> AsyncIOScheduler:
    # Без явного timezone APScheduler берёт часовой пояс сервера
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        send_digest,
        CronTrigger(hour=settings.digest_time.hour, minute=settings.digest_time.minute, timezone=scheduler.timezone),
        kwargs={"bot": bot, "task_storage": task_storage, "weather": weather},
        id="morning-digest",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=15 * 60,  # если цикл был занят — всё равно отправить в пределах 15 мин
    )
    log.info("Утренняя сводка: ежедневно в %s", settings.digest_time.strftime("%H:%M"))

    if isinstance(task_storage, SyncedStorage):
        scheduler.add_job(
            task_storage.sync,
            "interval",
            minutes=settings.sync_interval_minutes,
            id="outbox-sync",
            max_instances=1,
            coalesce=True,
        )
        log.info("Синхронизация очереди: каждые %d мин", settings.sync_interval_minutes)

    log.info("Часовой пояс планировщика: %s", scheduler.timezone)
    return scheduler
