"""Выбор хранилища по STORAGE_BACKEND. Единственное место, знающее о реализациях."""

import logging
from pathlib import Path

from bot.config import Settings
from bot.services.notifier import AdminNotifier
from bot.storage import SQLiteOutbox, SQLiteStorage, TaskStorage
from bot.storage.google_sheets import GoogleSheetsStorage
from bot.storage.synced import SyncedStorage

log = logging.getLogger(__name__)

# Квота Sheets — 60 запросов/мин на пользователя; ~1 запрос/с с запасом
SHEETS_MIN_INTERVAL_S = 1.0


class ConfigError(Exception):
    """Ошибка конфигурации, бот не может стартовать."""


def build_storage(settings: Settings, notifier: AdminNotifier) -> TaskStorage:
    local = SQLiteStorage(settings.sqlite_path)
    if settings.storage_backend == "sqlite":
        log.info("Хранилище: только SQLite (%s)", settings.sqlite_path)
        return local

    creds = settings.google_credentials_json or ""
    if not creds.lstrip().startswith("{") and not Path(creds).is_file():
        raise ConfigError(f"Не найден файл ключа Google: {creds} (GOOGLE_CREDENTIALS_JSON)")

    remote = GoogleSheetsStorage.from_settings(creds, settings.spreadsheet_id or "")
    log.info("Хранилище: Google Sheets %s + локальная копия %s", settings.spreadsheet_id, settings.sqlite_path)
    return SyncedStorage(
        local=local,
        outbox=SQLiteOutbox(settings.sqlite_path),
        remote=remote,
        notifier=notifier,
        remote_name="Google Sheets",
        min_interval_s=SHEETS_MIN_INTERVAL_S,
    )
